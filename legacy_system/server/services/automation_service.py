import hashlib
import json
import re
import secrets
from collections import Counter
from datetime import datetime, timedelta
from statistics import median

from utils.redaction import redact_secrets
from time_utils import utcnow
from sqldb_config import retry_on_sqlite_lock
from models import (
    AutomationRun,
    AutomationRunAuditCycle,
    AutomationRunAuditorJob,
    AutomationRunAuditResult,
    AutomationRunEvent,
    AutomationRunProviderCall,
    AutomationScenario,
    AutomationScorecardTemplate,
    AutomationSnapshot,
    AutomationWorker,
    AutomationWorkspaceEvent,
    Campaign,
    CampaignAuditEvent,
    CampaignClock,
    CampaignMember,
    CampaignMemoryEmbedding,
    CampaignPlanningSummary,
    CampaignSession,
    CampaignShop,
    CampaignWorld,
    Character,
    CharacterPlanningMessage,
    EncounterMap,
    EncounterMapPlacement,
    LLMPlayer,
    LootBox,
    NPCActor,
    PlanningBondProposal,
    SessionDmTurn,
    SessionMessage,
    SheetProposal,
    User,
    WorldEvent,
    db,
)
from werkzeug.security import generate_password_hash
from services.character_service import build_character_from_data, character_full_dict, update_character_relations
from services.post_turn_state import (
    FAILED_STAGE_STATUSES,
    SUCCESS_POST_TURN_STATUSES,
    derive_post_turn_state,
    state_for_turn,
)


AUTOMATION_ACTIVE_STATUSES = {'queued', 'claimed', 'running', 'stop_requested', 'awaiting_audit', 'reconciling'}
AUTOMATION_SNAPSHOT_SCHEMA_VERSION = 2
CLONE_RETRIEVAL_PREFLIGHT_VERSION = 1

# Canonical, non-overlapping model retry taxonomy. Classification is ordered:
# one persisted audit event or provider-call repair attempt contributes to one
# kind only, and ``total`` is always the sum of these keys.
MODEL_RETRY_TAXONOMY = {
    'provider_retry': 'Transient provider/transport retry recorded as model_retry.',
    'parse_repair': 'Structured-output parse repair recorded on a provider call.',
    'resolver_contract_repair': 'Resolver-packet-only contract repair that preserves an accepted visible turn.',
    'contract_guard_retry': 'Finalizer contract retry that re-invokes the model.',
    'tool_repair': 'Tool-call or tool-output repair that re-invokes the model.',
    'guard_retry': 'Non-contract response guard retry that re-invokes the model.',
    'other_model_reinvocation': 'Other explicitly retry-labelled model re-invocation.',
}


class CloneRetrievalPreflightError(ValueError):
    def __init__(self, message, report=None):
        super().__init__(message)
        self.report = report or {}


class AuditScorecardValidationError(ValueError):
    """A scorecard cannot be accepted as an audited cycle."""

    def __init__(self, message, *, code='invalid_scorecard', details=None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


class InfrastructureFailureThresholdError(ValueError):
    """A reclaim was refused because consecutive identical infrastructure
    failures already reached the configured threshold (issue #131)."""

    def __init__(self, run):
        super().__init__(
            f'Run {run.id} has reached the infrastructure-failure reclaim threshold '
            f'({run.reclaim_failure_count} consecutive '
            f'"{run.reclaim_failure_fingerprint}") and will not be reclaimed'
        )
        self.run_id = run.id
        self.fingerprint = run.reclaim_failure_fingerprint
        self.count = run.reclaim_failure_count


def _normalize_memory_anchors(value):
    anchors = value if isinstance(value, dict) else {}
    return {
        'current_goal': anchors.get('current_goal'),
        'current_scene': anchors.get('current_scene'),
        'open_clues': anchors.get('open_clues') if isinstance(anchors.get('open_clues'), list) else [],
        'unresolved_questions': (
            anchors.get('unresolved_questions')
            if isinstance(anchors.get('unresolved_questions'), list)
            else []
        ),
        'npc_observations': (
            anchors.get('npc_observations')
            if isinstance(anchors.get('npc_observations'), list)
            else []
        ),
        'recent_offers_promises': (
            anchors.get('recent_offers_promises')
            if isinstance(anchors.get('recent_offers_promises'), list)
            else []
        ),
    }


def _serialize_memory_embedding(row):
    try:
        vector = json.loads(row.embedding_json)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f'Embedding {row.item_type}:{row.item_id} contains invalid JSON'
        ) from exc

    if not isinstance(vector, list):
        raise ValueError(
            f'Embedding {row.item_type}:{row.item_id} vector must be a list'
        )

    if len(vector) != row.embedding_dimensions:
        raise ValueError(
            f'Embedding {row.item_type}:{row.item_id} has '
            f'{len(vector)} values but declares {row.embedding_dimensions}'
        )

    return {
        'item_type': row.item_type,
        'item_id': str(row.item_id),
        'visibility': row.visibility,
        'canonical_text': row.canonical_text,
        'text_hash': row.text_hash,
        'embedding_model': row.embedding_model,
        'embedding_dimensions': row.embedding_dimensions,
        'embedding': vector,
        'created_at': row.created_at.isoformat() if row.created_at else None,
        'updated_at': row.updated_at.isoformat() if row.updated_at else None,
    }


def _vector_digest(vector):
    canonical = json.dumps(
        vector,
        ensure_ascii=False,
        separators=(',', ':'),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()

DEFAULT_LEASE_SECONDS = 45
DEFAULT_PROVISIONING_LEASE_SECONDS = 300
DEFAULT_MAX_RECLAIM_FAILURES = 5
# A reclaim-failure fingerprint/count records *consecutive* identical control-plane
# failures. Appending any of these gameplay milestones proves the run recovered
# and must reset the counter so a handful of transient failures spread across an
# otherwise healthy long-running run never terminalizes it as a reclaim loop.
RECLAIM_RECOVERY_EVENT_TYPES = {'player_decision', 'turn_result', 'dm_turn_status'}
CLAIMABLE_ACTIVE_STATUSES = {'claimed', 'running', 'stop_requested', 'reconciling'}
AUDIT_READY_STATUSES = {'audited', 'skipped'}
CUSTOM_SCORECARD_STATUS_ORDER = ('pass', 'warn', 'fail', 'not_assessed', 'not_applicable')
SCORECARD_SCORING_MODEL = 'cycle_assessment_v2'
SCORECARD_STATUS_VALUES = {'pass': 1.0, 'warn': 0.5, 'fail': 0.0}
DEFAULT_RETENTION_POLICY = {
    'retention_days': 14,
    'keep_recent_runs': 5,
    'cleanup_action': 'archive',
}
DEFAULT_AUDIT_CONFIG = {
    'checks': [
        {
            'id': 'run_completion',
            'metric': 'run_status',
            'kind': 'enum',
            'pass_values': ['completed'],
            'warn_values': ['queued', 'claimed', 'running'],
            'fail_values': ['stop_requested', 'stopped', 'failed'],
            'weight': 3,
            'better_direction': 'higher',
            'summary_template': 'Run status is {value}.',
        },
        {
            'id': 'error_count',
            'metric': 'error_count',
            'kind': 'number',
            'pass_if': {'lte': 0},
            'fail_if': {'gt': 0},
            'weight': 3,
            'better_direction': 'lower',
            'summary_template': '{value} automation errors recorded.',
        },
        {
            'id': 'turn_count',
            'metric': 'turn_count',
            'kind': 'number',
            'warn_if': {'lte': 0},
            'pass_if': {'gt': 0},
            'weight': 2,
            'better_direction': 'higher',
            'summary_template': '{value} turn results recorded.',
        },
        {
            'id': 'dm_silence_count',
            'metric': 'dm_silence_count',
            'kind': 'number',
            'warn_if': {'gt': 0},
            'fail_if': {'gt': 2},
            'weight': 1,
            'better_direction': 'lower',
            'summary_template': '{value} DM silence decisions.',
        },
        {
            'id': 'dm_empty_count',
            'metric': 'dm_empty_count',
            'kind': 'number',
            'warn_if': {'gt': 0},
            'fail_if': {'gt': 2},
            'weight': 1,
            'better_direction': 'lower',
            'summary_template': '{value} empty DM outputs.',
        },
        {
            'id': 'model_retry_count',
            'metric': 'model_retry_count',
            'kind': 'number',
            'warn_if': {'gt': 0},
            'fail_if': {'gt': 4},
            'weight': 1,
            'better_direction': 'lower',
            'summary_template': '{value} model retries or repair re-invocations.',
        },
    ],
}
STATUS_RANK = {'fail': 0, 'warn': 1, 'not_assessed': 1, 'not_applicable': 1, 'pass': 2}

CANONICAL_CATEGORIES = {
    "operational/runtime reliability": "operational/runtime reliability",
    "operational_reliability": "operational/runtime reliability",
    "operational": "operational/runtime reliability",
    "narrative quality": "narrative quality",
    "narrative_quality": "narrative quality",
    "narrative": "narrative quality",
    "durable state correctness": "durable state correctness",
    "durable_state_correctness": "durable state correctness",
    "state_correctness": "durable state correctness",
    "state": "durable state correctness",
    "retrieval or memory use": "retrieval or memory use",
    "retrieval_memory_use": "retrieval or memory use",
    "retrieval": "retrieval or memory use",
    "memory": "retrieval or memory use",
    "safety/private-information handling": "safety/private-information handling",
    "safety_private_info": "safety/private-information handling",
    "safety": "safety/private-information handling",
}
CANONICAL_CATEGORY_NAMES = tuple(dict.fromkeys(CANONICAL_CATEGORIES.values()))
UNCATEGORIZED_CATEGORY = 'uncategorized'
SCORECARD_TEMPLATE_SCHEMA_VERSION = 2


def normalize_criterion_category(value):
    if value is None or not str(value).strip():
        return None
    return CANONICAL_CATEGORIES.get(str(value).strip().lower())


def get_criterion_category(crit):
    cat = crit.get('category')
    if cat:
        normalized = normalize_criterion_category(cat)
        if normalized:
            return normalized
        return UNCATEGORIZED_CATEGORY
            
    # Try by substring match on ID, metric, or label
    identifier = (str(crit.get('id') or crit.get('metric') or '') + ' ' + str(crit.get('label') or '')).lower()
    if any(k in identifier for k in ('completion', 'error', 'turn', 'retry', 'status', 'reliability', 'perf', 'run')):
        return "operational/runtime reliability"
    if any(k in identifier for k in ('state', 'durable', 'db', 'entity', 'relation', 'save', 'progress')):
        return "durable state correctness"
    if any(k in identifier for k in ('memory', 'retrieval', 'context', 'history', 'anchor', 'recall')):
        return "retrieval or memory use"
    if any(k in identifier for k in ('safety', 'private', 'secure', 'moderation', 'policy', 'leak', 'spoiler')):
        return "safety/private-information handling"
    if any(k in identifier for k in ('narrative', 'story', 'quality', 'silence', 'empty', 'dialog', 'dialogue', 'rp', 'play')):
        return "narrative quality"
        
    return UNCATEGORIZED_CATEGORY


def scorecard_configuration(template):
    invalid = []
    for criterion in _json_list(_json_object(template, {}).get('criteria'), []):
        if not isinstance(criterion, dict) or not criterion.get('id'):
            continue
        category = criterion.get('category')
        if category and normalize_criterion_category(category):
            continue
        if not category and get_criterion_category(criterion) != UNCATEGORIZED_CATEGORY:
            continue
        invalid.append({
            'criterion_id': criterion.get('id'),
            'label': criterion.get('label') or criterion.get('id'),
            'category': category,
            'reason': 'missing or invalid canonical category',
        })
    return {
        'schema_version': SCORECARD_TEMPLATE_SCHEMA_VERSION,
        'valid': not invalid,
        'invalid_criteria': invalid,
        'uncategorized_criterion_count': len(invalid),
    }


def assert_scorecard_template_activatable(template):
    snapshot = template.snapshot() if hasattr(template, 'snapshot') else _json_object(template, {})
    config = scorecard_configuration(snapshot)
    if not config['valid']:
        details = '; '.join(
            f"{entry['criterion_id']} ({entry['category'] or 'no category'})"
            for entry in config['invalid_criteria']
        )
        raise ValueError(
            f'scorecard template has invalid criteria and cannot be activated: {details}'
        )


def _normalize_custom_scorecard_status(value, default='warn'):
    status = str(value or default).strip().lower()
    return status if status in CUSTOM_SCORECARD_STATUS_ORDER else default


def _coerce_scorecard_text(value, criterion_id, index, field):
    if value is None:
        return None
    if not isinstance(value, str):
        raise AuditScorecardValidationError(
            f'scorecard.criteria[{index}].{field} must be a string.',
            code='invalid_field_type',
            details={'index': index, 'criterion_id': criterion_id, 'field': field, 'received_type': type(value).__name__},
        )
    return value.strip() or None


def _aggregate_custom_scorecard_statuses(statuses, default='warn'):
    normalized = [_normalize_custom_scorecard_status(status, default='warn') for status in statuses if status]
    if not normalized:
        return default
    
    if 'fail' in normalized:
        return 'fail'
    if 'warn' in normalized:
        return 'warn'
    if 'pass' in normalized:
        return 'pass'
    if 'not_assessed' in normalized:
        return 'not_assessed'
    if 'not_applicable' in normalized:
        return 'not_applicable'
        
    return default


def _score_ratio(numerator, denominator):
    if denominator <= 0:
        return None
    score = round(numerator / denominator, 4)
    if numerator < denominator and score >= 1.0:
        return 0.9999
    return score


def _scoring_summary(statuses, *, weight=1):
    normalized = [
        _normalize_custom_scorecard_status(status)
        for status in statuses
        if status
    ]
    weight = max(1, _safe_int(weight, 1))
    assessed = [status for status in normalized if status in SCORECARD_STATUS_VALUES]
    not_assessed_count = normalized.count('not_assessed')
    not_applicable_count = normalized.count('not_applicable')
    assessment_numerator = sum(SCORECARD_STATUS_VALUES[status] for status in assessed)
    assessment_denominator = len(assessed)
    performance_score = _score_ratio(assessment_numerator, assessment_denominator)
    numerator = assessment_numerator * weight
    denominator = assessment_denominator * weight
    applicable_assessment_count = len(assessed) + not_assessed_count
    completeness = (
        round(len(assessed) / applicable_assessment_count, 4)
        if applicable_assessment_count
        else None
    )
    return {
        'performance_score': performance_score,
        'score_numerator': round(numerator, 4),
        'score_denominator': denominator,
        'assessment_numerator': round(assessment_numerator, 4),
        'assessment_denominator': assessment_denominator,
        'assessment_count': len(assessed),
        'applicable_assessment_count': applicable_assessment_count,
        'not_assessed_count': not_assessed_count,
        'not_applicable_count': not_applicable_count,
        'completeness': completeness,
    }


def _combine_scoring_summaries(summaries):
    summaries = [summary for summary in summaries if isinstance(summary, dict)]
    numerator = sum(float(summary.get('score_numerator') or 0) for summary in summaries)
    denominator = sum(float(summary.get('score_denominator') or 0) for summary in summaries)
    assessment_numerator = sum(
        float(summary.get('assessment_numerator') or 0)
        for summary in summaries
    )
    assessment_denominator = sum(
        _safe_int(summary.get('assessment_denominator'))
        for summary in summaries
    )
    assessment_count = sum(_safe_int(summary.get('assessment_count')) for summary in summaries)
    applicable_assessment_count = sum(
        _safe_int(summary.get('applicable_assessment_count'))
        for summary in summaries
    )
    not_assessed_count = sum(_safe_int(summary.get('not_assessed_count')) for summary in summaries)
    not_applicable_count = sum(_safe_int(summary.get('not_applicable_count')) for summary in summaries)
    completeness = (
        round(assessment_count / applicable_assessment_count, 4)
        if applicable_assessment_count
        else None
    )
    return {
        'performance_score': _score_ratio(numerator, denominator),
        'score_numerator': round(numerator, 4),
        'score_denominator': round(denominator, 4),
        'assessment_numerator': round(assessment_numerator, 4),
        'assessment_denominator': assessment_denominator,
        'assessment_count': assessment_count,
        'applicable_assessment_count': applicable_assessment_count,
        'not_assessed_count': not_assessed_count,
        'not_applicable_count': not_applicable_count,
        'completeness': completeness,
    }


def _utcnow():
    return utcnow()


def _parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _loads_text(value, fallback):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _latency_bucket(latency_ms):
    if latency_ms is None:
        return None
    if latency_ms < 1000:
        return '<1s'
    if latency_ms < 5000:
        return '1s-5s'
    if latency_ms < 15000:
        return '5s-15s'
    return '15s+'


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_identifier(value):
    cleaned = ''.join(ch.lower() if ch.isalnum() else '_' for ch in str(value or '').strip())
    normalized = '_'.join(part for part in cleaned.split('_') if part)
    return normalized[:120]


def _json_object(value, fallback):
    return value if isinstance(value, dict) else fallback


def _json_list(value, fallback):
    return value if isinstance(value, list) else fallback


def _session_messages(session_id):
    return [
        message.to_dict()
        for message in SessionMessage.query.filter_by(session_id=session_id)
        .order_by(SessionMessage.created_at.asc(), SessionMessage.id.asc())
        .all()
    ]


def _session_proposals(session_id):
    return [
        proposal.to_dict()
        for proposal in SheetProposal.query.filter_by(session_id=session_id)
        .order_by(SheetProposal.created_at.asc(), SheetProposal.id.asc())
        .all()
    ]


def visible_campaigns_for_user(user):
    owned = Campaign.query.filter_by(user_id=user.id, is_automation_clone=False).all()
    member_campaign_ids = [
        member.campaign_id
        for member in CampaignMember.query.filter_by(user_id=user.id).all()
    ]
    if not member_campaign_ids:
        return owned
    member_of = Campaign.query.filter(
        Campaign.id.in_(member_campaign_ids),
        Campaign.is_automation_clone.is_(False),
    ).all()
    return list({campaign.id: campaign for campaign in [*owned, *member_of]}.values())


def validate_scorecard_template_payload(data):
    name = (data.get('name') or '').strip()
    if not name:
        raise ValueError('name is required')

    criteria = []
    seen_ids = set()
    raw_criteria = _json_list(data.get('criteria'), [])
    if not raw_criteria:
        raise ValueError('criteria must contain at least one entry')
    for index, raw in enumerate(raw_criteria, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f'criteria[{index - 1}] must be an object')
        criterion_id = _normalize_identifier(raw.get('id') or raw.get('label') or f'criterion_{index}')
        if not criterion_id:
            raise ValueError(f'criteria[{index - 1}] requires id or label')
        if criterion_id in seen_ids:
            raise ValueError(f'duplicate criterion id: {criterion_id}')
        seen_ids.add(criterion_id)
        evidence_requirements = []
        for item in _json_list(raw.get('evidence_requirements'), []):
            if not isinstance(item, dict):
                continue
            evidence_requirements.append({
                'surface': (item.get('surface') or '').strip() or None,
                'reason': (item.get('reason') or '').strip() or None,
                'priority': (item.get('priority') or '').strip().lower() or None,
                'recommended_tools': [
                    str(tool).strip()
                    for tool in _json_list(item.get('recommended_tools'), [])
                    if str(tool).strip()
                ],
            })
        raw_category = (raw.get('category') or '').strip() or None
        normalized_category = normalize_criterion_category(raw_category)
        if not raw_category:
            raise ValueError(
                f'criteria[{index - 1}] requires an explicit category; '
                f'expected one of {", ".join(CANONICAL_CATEGORY_NAMES)}'
            )
        if not normalized_category:
            raise ValueError(
                f'criteria[{index - 1}] has an invalid category: {raw_category!r}; '
                f'expected one of {", ".join(CANONICAL_CATEGORY_NAMES)} or a supported alias'
            )
        criteria.append({
            'id': criterion_id,
            'label': (raw.get('label') or criterion_id.replace('_', ' ').title()).strip(),
            'description': (raw.get('description') or '').strip() or None,
            'better_direction': (raw.get('better_direction') or 'higher').strip().lower(),
            'evidence_requirements': evidence_requirements,
            'weight': max(1, min(100, _safe_int(raw.get('weight'), 2))),
            'category': normalized_category,
        })

    defaults = _json_object(data.get('defaults'), {})
    pause_phases = [
        str(item).strip().lower()
        for item in _json_list(defaults.get('pause_phases'), [])
        if str(item).strip().lower() in {'after_player', 'after_dm'}
    ]
    return {
        'schema_version': SCORECARD_TEMPLATE_SCHEMA_VERSION,
        'name': name,
        'description': (data.get('description') or '').strip() or None,
        'instructions': (data.get('instructions') or '').strip() or None,
        'criteria': criteria,
        'defaults': {
            **defaults,
            'pause_phases': pause_phases,
        },
    }


def scorecard_template_snapshot(template):
    if template is None:
        return {}
    return template.snapshot() if hasattr(template, 'snapshot') else {}


def current_scorecard_template_for_run(run):
    snapshot = _json_object(run.scorecard_template_json, {})
    if snapshot:
        return snapshot
    scenario = run.scenario
    if scenario and scenario.scorecard_template:
        return scorecard_template_snapshot(scenario.scorecard_template)
    return {}


def configured_pause_phases(run):
    config = _json_object(run.runner_config_json, {})
    raw = config.get('audit_pause_phases')
    if raw is None:
        raw = _json_object(current_scorecard_template_for_run(run).get('defaults'), {}).get('pause_phases')
    phases = []
    for value in _json_list(raw, []):
        phase = str(value).strip().lower()
        if phase in {'after_player', 'after_dm'} and phase not in phases:
            phases.append(phase)
    return phases


def _normalized_pause_phases(value):
    phases = []
    for item in _json_list(value, []):
        phase = str(item).strip().lower()
        if phase in {'after_player', 'after_dm'} and phase not in phases:
            phases.append(phase)
    return phases


def runner_config_from_request(runner_config=None, audit_config=None, raw_data=None):
    config = dict(_json_object(runner_config, {}))
    
    if isinstance(raw_data, dict):
        ignored_keys = {'snapshot_id', 'matrix', 'runner_config', 'audit_config', 'label'}
        for k, v in raw_data.items():
            if k not in ignored_keys and v is not None:
                config[k] = v

    if config.get('audit_pause_phases') is not None:
        return config

    audit_settings = _json_object(audit_config, {})
    raw_pause_phases = audit_settings.get('audit_pause_phases')
    if raw_pause_phases is None:
        raw_pause_phases = audit_settings.get('pause_phases')
    if raw_pause_phases is None and isinstance(raw_data, dict):
        raw_pause_phases = raw_data.get('audit_pause_phases') or raw_data.get('pause_phases')
    if raw_pause_phases is not None:
        config['audit_pause_phases'] = _normalized_pause_phases(raw_pause_phases)
    return config


def scenario_roster_from_campaign(campaign):
    members = CampaignMember.query.filter_by(campaign_id=campaign.id).order_by(CampaignMember.id.asc()).all()
    character_ids = {member.selected_character_id for member in members if member.selected_character_id}
    characters = {
        character.id: character
        for character in Character.query.filter(Character.id.in_(character_ids)).all()
    } if character_ids else {}
    llm_players = {
        player.user_id: player
        for player in LLMPlayer.query.filter_by(campaign_id=campaign.id).all()
    }
    roster = []
    for member in members:
        if (member.role or 'player') == 'spectator':
            continue
        character = characters.get(member.selected_character_id)
        if not character:
            continue
        llm_player = llm_players.get(member.user_id)
        roster.append({
            'user_id': member.user_id,
            'member_role': member.role,
            'character_id': character.id,
            'character_name': character.name,
            'label': llm_player.label if llm_player else (member.user.username if member.user else f'User {member.user_id}'),
            'llm_player_id': llm_player.id if llm_player else None,
        })
    return roster


def unique_llm_username(label):
    base = ' '.join((label or 'LLM Player').split())[:60] or 'LLM Player'
    username = base
    suffix = 2
    while User.query.filter_by(username=username).first():
        username = f'{base} {suffix}'
        suffix += 1
    return username


def generate_llm_api_key():
    api_key = f'dndllm_{secrets.token_urlsafe(32)}'
    return api_key, generate_password_hash(api_key), api_key[:24]


def next_safe_llm_user_id():
    max_user_id = db.session.query(db.func.max(User.id)).scalar() or 0
    max_llm_user_id = db.session.query(db.func.max(LLMPlayer.user_id)).scalar() or 0
    return max(max_user_id, max_llm_user_id) + 1


def provision_automation_player(campaign, label):
    llm_player = LLMPlayer.query.filter_by(campaign_id=campaign.id, label=label).first()
    if llm_player:
        llm_user = db.session.get(User, llm_player.user_id)
        return llm_user, llm_player, None

    username = unique_llm_username(label)
    email = f'llm-player-{campaign.id}-{secrets.token_hex(8)}@local.llm'
    llm_user = User(id=next_safe_llm_user_id(), username=username, email=email)
    llm_user.set_password(secrets.token_urlsafe(32))
    db.session.add(llm_user)
    db.session.flush()

    api_key, api_key_hash, api_key_prefix = generate_llm_api_key()
    llm_player = LLMPlayer(
        campaign_id=campaign.id,
        user_id=llm_user.id,
        label=label,
        api_key_hash=api_key_hash,
        api_key_prefix=api_key_prefix,
    )
    db.session.add(llm_player)
    db.session.flush()

    return llm_user, llm_player, api_key


def validate_and_normalize_roster(campaign, roster_data):
    if not isinstance(roster_data, list):
        raise ValueError("roster must be a list")

    if not roster_data:
        raise ValueError("roster must not be empty")

    seen_users = set()
    seen_characters = set()
    seen_llm_players = set()
    normalized_roster = []

    for idx, entry in enumerate(roster_data):
        if not isinstance(entry, dict) or entry is None:
            raise ValueError(f"roster[{idx}] must be an object")

        user_id = entry.get('user_id')
        if not user_id:
            raise ValueError(f"roster[{idx}].user_id is required")

        member = CampaignMember.query.filter_by(campaign_id=campaign.id, user_id=user_id).first()
        if not member:
            raise ValueError(f"roster[{idx}].user_id {user_id} is not a member of campaign {campaign.id}")

        if member.role == 'spectator':
            raise ValueError(f"roster[{idx}].user_id {user_id} is a spectator")

        character_id = entry.get('character_id')
        if not character_id:
            raise ValueError(f"roster[{idx}].character_id is required")

        character = Character.query.filter_by(id=character_id, campaign_id=campaign.id).first()
        if not character:
            raise ValueError(f"roster[{idx}].character_id {character_id} does not belong to campaign {campaign.id}")

        if member.selected_character_id != character_id:
            raise ValueError(f"roster[{idx}].character_id {character_id} is not the selected character for user_id {user_id}")

        llm_player_id = entry.get('llm_player_id')
        llm_player = None
        if llm_player_id:
            llm_player = LLMPlayer.query.filter_by(id=llm_player_id, campaign_id=campaign.id, user_id=user_id).first()
            if not llm_player:
                raise ValueError(f"roster[{idx}].llm_player_id {llm_player_id} does not belong to user {user_id} and campaign {campaign.id}")

        if user_id in seen_users:
            raise ValueError(f"roster[{idx}].user_id {user_id} is duplicated")
        seen_users.add(user_id)

        if character_id in seen_characters:
            raise ValueError(f"roster[{idx}].character_id {character_id} is duplicated")
        seen_characters.add(character_id)

        if llm_player_id:
            if llm_player_id in seen_llm_players:
                raise ValueError(f"roster[{idx}].llm_player_id {llm_player_id} is duplicated")
            seen_llm_players.add(llm_player_id)

        if not llm_player:
            llm_player = LLMPlayer.query.filter_by(campaign_id=campaign.id, user_id=user_id).first()

        label = llm_player.label if llm_player else (member.user.username if member.user else f"User {user_id}")

        normalized_roster.append({
            'user_id': user_id,
            'member_role': member.role,
            'character_id': character.id,
            'character_name': character.name,
            'label': label,
            'llm_player_id': llm_player.id if llm_player else None,
        })

    settings = json.loads(campaign.settings) if campaign.settings else {}
    try:
        required_players = max(1, int(settings.get('required_players', 1)))
    except (TypeError, ValueError):
        required_players = 1

    if len(normalized_roster) < required_players:
        raise ValueError(f"Roster has {len(normalized_roster)} entries, but campaign requires {required_players} players")

    return normalized_roster


def serialize_campaign_snapshot(campaign, source_session_id=None):
    members = CampaignMember.query.filter_by(campaign_id=campaign.id).order_by(CampaignMember.id.asc()).all()
    characters = Character.query.filter_by(campaign_id=campaign.id).order_by(Character.party_order.asc(), Character.id.asc()).all()
    sessions_query = CampaignSession.query.filter_by(campaign_id=campaign.id).order_by(CampaignSession.started_at.asc(), CampaignSession.id.asc())
    if source_session_id is not None:
        sessions_query = sessions_query.filter_by(id=source_session_id)
    sessions = sessions_query.all()
    world = CampaignWorld.query.filter_by(campaign_id=campaign.id).first()
    planning_summary = CampaignPlanningSummary.query.filter_by(campaign_id=campaign.id).first()
    planning_messages = CharacterPlanningMessage.query.filter_by(campaign_id=campaign.id).order_by(CharacterPlanningMessage.created_at.asc(), CharacterPlanningMessage.id.asc()).all()
    bond_proposals = PlanningBondProposal.query.filter_by(campaign_id=campaign.id).order_by(PlanningBondProposal.id.asc()).all()
    npcs = NPCActor.query.filter_by(campaign_id=campaign.id).order_by(NPCActor.id.asc()).all()
    clocks = CampaignClock.query.filter_by(campaign_id=campaign.id).order_by(CampaignClock.id.asc()).all()
    shops = CampaignShop.query.filter_by(campaign_id=campaign.id).order_by(CampaignShop.id.asc()).all()
    loot_boxes = LootBox.query.filter_by(campaign_id=campaign.id).order_by(LootBox.id.asc()).all()
    encounter_maps = EncounterMap.query.filter_by(campaign_id=campaign.id).order_by(EncounterMap.created_at.asc(), EncounterMap.id.asc()).all()
    audit_events = CampaignAuditEvent.query.filter_by(campaign_id=campaign.id).order_by(CampaignAuditEvent.created_at.asc(), CampaignAuditEvent.id.asc()).all()

    world_events = (
        WorldEvent.query
        .filter_by(campaign_id=campaign.id)
        .order_by(WorldEvent.created_at.asc(), WorldEvent.id.asc())
        .all()
    )

    memory_embeddings = (
        CampaignMemoryEmbedding.query
        .filter_by(campaign_id=campaign.id)
        .order_by(
            CampaignMemoryEmbedding.item_type.asc(),
            CampaignMemoryEmbedding.item_id.asc(),
            CampaignMemoryEmbedding.id.asc(),
        )
        .all()
    )

    serialized_sessions = []
    for session in sessions:
        serialized_sessions.append({
            **session.to_dict(),
            'running_summary': session.running_summary,
            'messages': _session_messages(session.id),
            'sheet_proposals': _session_proposals(session.id),
        })

    serialized_maps = []
    for encounter_map in encounter_maps:
        serialized_maps.append({
            **encounter_map.to_dict(include_private=True),
            'image_filename': encounter_map.image_filename,
            'labeled_image_filename': encounter_map.labeled_image_filename,
        })

    return {
        'snapshot_schema_version': AUTOMATION_SNAPSHOT_SCHEMA_VERSION,
        'campaign': campaign.to_dict(),
        'members': [member.to_dict() for member in members],
        'characters': [character_full_dict(character) for character in characters],
        'roster': scenario_roster_from_campaign(campaign),
        'sessions': serialized_sessions,
        'world': {
            'public_intro': _loads_text(world.public_intro, {}) if world else None,
            'knowledge_graph': _loads_text(world.knowledge_graph, {}) if world else None,
            'world_state': _loads_text(world.world_state, {}) if world else None,
            'dm_private': _loads_text(world.dm_private, {}) if world else None,
            'approved_at': world.approved_at.isoformat() if world and world.approved_at else None,
        } if world else None,
        'planning_summary': planning_summary.to_dict(include_private=True) if planning_summary else None,
        'planning_messages': [message.to_dict() for message in planning_messages],
        'bond_proposals': [proposal.to_dict() for proposal in bond_proposals],
        'npcs': [npc.to_dict(include_private=True) for npc in npcs],
        'clocks': [clock.to_dict(include_private=True) for clock in clocks if clock.to_dict(include_private=True) is not None],
        'shops': [shop.to_dict() for shop in shops],
        'loot_boxes': [loot_box.to_dict(is_dm=True) for loot_box in loot_boxes],
        'encounter_maps': serialized_maps,
        'audit_events': [event.to_dict() for event in audit_events],
        'world_events': [
            event.to_dict(include_private=True)
            for event in world_events
        ],
        'memory_embeddings': [
            _serialize_memory_embedding(row)
            for row in memory_embeddings
        ],
        'captured_at': _utcnow().isoformat(),
    }


def create_snapshot_for_scenario(scenario, label=None, summary=None, source_session_id=None):
    campaign = db.session.get(Campaign, scenario.source_campaign_id)
    snapshot_campaign = campaign
    if source_session_id is not None:
        source_session = db.session.get(CampaignSession, source_session_id)
        if source_session is None:
            raise ValueError("source_session_id does not exist")
        session_campaign = db.session.get(Campaign, source_session.campaign_id)
        if session_campaign is None:
            raise ValueError("source_session campaign does not exist")
            
        is_valid = False
        if session_campaign.id == scenario.source_campaign_id:
            is_valid = True
        else:
            run_ids = [run.id for run in AutomationRun.query.filter_by(scenario_id=scenario.id).all()]
            if run_ids:
                derived_campaign_exists = AutomationRun.query.filter(
                    AutomationRun.id.in_(run_ids),
                    AutomationRun.derived_campaign_id == session_campaign.id
                ).first() is not None
                if derived_campaign_exists:
                    is_valid = True
                    
        if not is_valid:
            raise ValueError("source_session_id must belong to the scenario's source campaign or a derived campaign of one of its runs")
            
        snapshot_campaign = session_campaign
    payload = serialize_campaign_snapshot(snapshot_campaign, source_session_id=source_session_id)
    if scenario:
        payload['roster'] = scenario.roster_json
    active_session = next((session for session in payload['sessions'] if session.get('is_active')), None)
    snapshot = AutomationSnapshot(
        scenario_id=scenario.id,
        source_campaign_id=snapshot_campaign.id,
        source_session_id=active_session.get('id') if active_session else source_session_id,
        label=label or f'{snapshot_campaign.name} snapshot {_utcnow().strftime("%Y-%m-%d %H:%M:%S")}',
        summary=summary or f'Snapshot of {snapshot_campaign.name}',
        snapshot_json=payload,
        metadata_json={
            'campaign_name': snapshot_campaign.name,
            'campaign_status': snapshot_campaign.status,
            'active_session_id': active_session.get('id') if active_session else None,
            'message_count': sum(len(session.get('messages') or []) for session in payload['sessions']),
            'proposal_count': sum(len(session.get('sheet_proposals') or []) for session in payload['sessions']),
            'encounter_map_count': len(payload.get('encounter_maps') or []),
            'audit_event_count': len(payload.get('audit_events') or []),
            'world_event_count': len(payload.get('world_events') or []),
            'memory_embedding_count': len(payload.get('memory_embeddings') or []),
            'memory_anchor_session_count': sum(
                1
                for session in payload.get('sessions') or []
                if any(
                    value not in (None, '', [])
                    for value in _normalize_memory_anchors(
                        session.get('memory_anchors')
                    ).values()
                )
            ),
            'snapshot_schema_version': payload.get('snapshot_schema_version'),
        },
    )
    db.session.add(snapshot)
    db.session.commit()
    return snapshot


def _clone_character(source_data, campaign_id):
    clone = Character(campaign_id=campaign_id, user_id=source_data.get('user_id'))
    build_character_from_data(clone, source_data)
    clone.party_order = source_data.get('party_order', clone.party_order)
    clone.current_location = source_data.get('current_location')
    db.session.add(clone)
    db.session.flush()
    update_character_relations(clone, source_data)
    return clone


def retention_policy_for_scenario(scenario):
    policy = dict(DEFAULT_RETENTION_POLICY)
    policy.update(scenario.retention_policy_json or {})
    return policy


def _copy_snapshot_world_events(data, clone):
    event_map = {}

    for event_data in data.get('world_events') or []:
        source_event_id = str(event_data.get('id'))

        if not source_event_id or source_event_id == 'None':
            raise CloneRetrievalPreflightError(
                'Snapshot world event is missing its source id'
            )

        if source_event_id in event_map:
            raise CloneRetrievalPreflightError(
                f'Duplicate source event ID detected in snapshot: {source_event_id}'
            )

        cloned_event = WorldEvent(
            campaign_id=clone.id,
            event_type=event_data.get('event_type') or 'world_event',
            summary=event_data.get('summary') or '',
            payload=json.dumps(
                event_data.get('payload') or {},
                ensure_ascii=False,
            ),
            visibility=event_data.get('visibility') or 'dm_private',
            created_at=_parse_iso(event_data.get('created_at')) or _utcnow(),
        )
        db.session.add(cloned_event)
        db.session.flush()

        event_map[source_event_id] = str(cloned_event.id)

    return event_map


def _mapped_embedding_item_id(item_type, item_id, world_event_map):
    item_id = str(item_id)

    if item_type != 'world_event':
        return item_id

    mapped = world_event_map.get(item_id)
    if mapped is None:
        raise CloneRetrievalPreflightError(
            f'World-event embedding {item_id} has no cloned event mapping'
        )

    return mapped


def _copy_snapshot_embeddings(data, clone, world_event_map):
    seen_keys = set()

    for embedding_data in data.get('memory_embeddings') or []:
        item_type = str(embedding_data.get('item_type') or '')
        source_item_id = str(embedding_data.get('item_id') or '')
        clone_item_id = _mapped_embedding_item_id(
            item_type,
            source_item_id,
            world_event_map,
        )

        key = (item_type, clone_item_id)
        if key in seen_keys:
            raise CloneRetrievalPreflightError(
                f'Duplicate cloned embedding key: {item_type}:{clone_item_id}'
            )
        seen_keys.add(key)

        vector = embedding_data.get('embedding')
        dimensions = embedding_data.get('embedding_dimensions')

        if not isinstance(vector, list):
            raise CloneRetrievalPreflightError(
                f'Embedding {item_type}:{source_item_id} has no vector list'
            )

        if len(vector) != dimensions:
            raise CloneRetrievalPreflightError(
                f'Embedding {item_type}:{source_item_id} dimension mismatch'
            )

        row = CampaignMemoryEmbedding(
            campaign_id=clone.id,
            item_type=item_type,
            item_id=clone_item_id,
            visibility=embedding_data.get('visibility') or 'dm_private',
            canonical_text=embedding_data.get('canonical_text') or '',
            text_hash=embedding_data.get('text_hash') or '',
            embedding_model=embedding_data.get('embedding_model') or '',
            embedding_dimensions=dimensions,
            embedding_json=json.dumps(
                vector,
                ensure_ascii=False,
                separators=(',', ':'),
                allow_nan=False,
            ),
            created_at=_parse_iso(embedding_data.get('created_at')) or _utcnow(),
            updated_at=_parse_iso(embedding_data.get('updated_at')) or _utcnow(),
        )
        db.session.add(row)

    db.session.flush()


def _snapshot_candidate_keys(data, world_event_map):
    keys = set()

    world = data.get('world') or {}
    graph = world.get('knowledge_graph') or {}

    for item in graph.get('entities') or []:
        keys.add(('entity', str(item.get('id'))))

    for item in graph.get('relations') or []:
        keys.add(('relation', str(item.get('id'))))

    for item in graph.get('facts') or []:
        keys.add(('fact', str(item.get('id'))))

    for npc in data.get('npcs') or []:
        keys.add(('npc_actor', str(npc.get('actor_id'))))

    for clock in data.get('clocks') or []:
        keys.add(('clock', str(clock.get('clock_id'))))

    # Runtime retrieval uses only the latest 30 events.
    events = sorted(
        data.get('world_events') or [],
        key=lambda event: (
            event.get('created_at') or '',
            int(event.get('id') or 0),
        ),
        reverse=True,
    )[:30]

    for event in events:
        source_id = str(event.get('id'))
        keys.add(('world_event', world_event_map[source_id]))

    keys.add(('world_state', 'current'))
    keys.add(('dm_private', 'current'))
    keys.add(('planning_summary', 'current'))

    return keys


def _campaign_candidate_keys(campaign):
    keys = set()
    world = CampaignWorld.query.filter_by(campaign_id=campaign.id).first()
    if world:
        try:
            graph = json.loads(world.knowledge_graph) if world.knowledge_graph else {}
        except (TypeError, ValueError):
            graph = {}
        if isinstance(graph, dict):
            for item in graph.get('entities') or []:
                keys.add(('entity', str(item.get('id'))))
            for item in graph.get('relations') or []:
                keys.add(('relation', str(item.get('id'))))
            for item in graph.get('facts') or []:
                keys.add(('fact', str(item.get('id'))))

    for npc in NPCActor.query.filter_by(campaign_id=campaign.id).all():
        keys.add(('npc_actor', str(npc.actor_id)))

    for clock in CampaignClock.query.filter_by(campaign_id=campaign.id).all():
        keys.add(('clock', str(clock.clock_id)))

    # Runtime retrieval uses only the latest 30 events. Order by created_at desc
    events = (
        WorldEvent.query
        .filter_by(campaign_id=campaign.id)
        .order_by(WorldEvent.created_at.desc(), WorldEvent.id.desc())
        .limit(30)
        .all()
    )
    for event in events:
        keys.add(('world_event', str(event.id)))

    keys.add(('world_state', 'current'))
    keys.add(('dm_private', 'current'))
    keys.add(('planning_summary', 'current'))

    return keys


def validate_clone_retrieval_equivalence(
    *,
    snapshot_data,
    clone,
    session_map,
    world_event_map,
):
    mismatches = []

    # 1. Compare candidate keys
    snapshot_keys = _snapshot_candidate_keys(snapshot_data, world_event_map)
    clone_keys = _campaign_candidate_keys(clone)

    if snapshot_keys != clone_keys:
        for k in snapshot_keys - clone_keys:
            mismatches.append({'type': 'missing_candidate', 'key': list(k)})
        for k in clone_keys - snapshot_keys:
            mismatches.append({'type': 'unexpected_candidate', 'key': list(k)})

    # 2. Compare semantic coverage
    # Find expected embeddings from snapshot
    expected_embeddings = {}
    for embedding_data in snapshot_data.get('memory_embeddings') or []:
        item_type = str(embedding_data.get('item_type') or '')
        source_item_id = str(embedding_data.get('item_id') or '')
        clone_item_id = _mapped_embedding_item_id(
            item_type,
            source_item_id,
            world_event_map,
        )
        expected_embeddings[(item_type, clone_item_id)] = embedding_data

    # Query actual embeddings from clone
    actual_rows = CampaignMemoryEmbedding.query.filter_by(campaign_id=clone.id).all()
    actual_embeddings = {
        (row.item_type, str(row.item_id)): row
        for row in actual_rows
    }

    # Semantic coverage: intersection of candidates & embeddings
    snapshot_coverage = snapshot_keys & set(expected_embeddings.keys())
    clone_coverage = clone_keys & set(actual_embeddings.keys())

    if snapshot_coverage != clone_coverage:
        for k in snapshot_coverage - clone_coverage:
            mismatches.append({'type': 'missing_coverage', 'key': list(k)})
        for k in clone_coverage - snapshot_coverage:
            mismatches.append({'type': 'unexpected_coverage', 'key': list(k)})

    # 3. Compare exact embedding content
    for key, expected in expected_embeddings.items():
        actual = actual_embeddings.get(key)
        if actual is None:
            mismatches.append({'type': 'missing_embedding', 'key': list(key)})
            continue

        # Compare fields
        expected_visibility = expected.get('visibility') or 'dm_private'
        actual_visibility = actual.visibility or 'dm_private'
        if expected_visibility != actual_visibility:
            mismatches.append({
                'type': 'embedding_field_mismatch',
                'key': list(key),
                'field': 'visibility',
                'expected': expected_visibility,
                'actual': actual_visibility,
            })

        expected_text = expected.get('canonical_text') or ''
        actual_text = actual.canonical_text or ''
        if expected_text != actual_text:
            mismatches.append({
                'type': 'embedding_field_mismatch',
                'key': list(key),
                'field': 'canonical_text',
                'expected': expected_text,
                'actual': actual_text,
            })

        expected_hash = expected.get('text_hash') or ''
        actual_hash = actual.text_hash or ''
        if expected_hash != actual_hash:
            mismatches.append({
                'type': 'embedding_field_mismatch',
                'key': list(key),
                'field': 'text_hash',
                'expected': expected_hash,
                'actual': actual_hash,
            })

        expected_model = expected.get('embedding_model') or ''
        actual_model = actual.embedding_model or ''
        if expected_model != actual_model:
            mismatches.append({
                'type': 'embedding_field_mismatch',
                'key': list(key),
                'field': 'embedding_model',
                'expected': expected_model,
                'actual': actual_model,
            })

        expected_dim = expected.get('embedding_dimensions')
        actual_dim = actual.embedding_dimensions
        if expected_dim != actual_dim:
            mismatches.append({
                'type': 'embedding_field_mismatch',
                'key': list(key),
                'field': 'embedding_dimensions',
                'expected': expected_dim,
                'actual': actual_dim,
            })

        expected_vector = expected.get('embedding')
        try:
            actual_vector = json.loads(actual.embedding_json) if actual.embedding_json else []
        except (TypeError, ValueError):
            actual_vector = []

        if len(expected_vector) != len(actual_vector):
            mismatches.append({
                'type': 'vector_mismatch',
                'key': list(key),
                'reason': 'length_mismatch',
                'expected_length': len(expected_vector),
                'actual_length': len(actual_vector),
            })
        else:
            expected_digest = _vector_digest(expected_vector)
            actual_digest = _vector_digest(actual_vector)
            if expected_digest != actual_digest:
                mismatches.append({
                    'type': 'vector_mismatch',
                    'key': list(key),
                    'reason': 'fingerprint_mismatch',
                })

    for key in actual_embeddings.keys():
        if key not in expected_embeddings:
            mismatches.append({'type': 'unexpected_embedding', 'key': list(key)})

    # 4. Compare anchors exactly
    # session_map is source_session_id -> cloned_session_id
    for source_session_data in snapshot_data.get('sessions') or []:
        source_id = source_session_data.get('id')
        clone_id = session_map.get(source_id)
        if clone_id is None:
            mismatches.append({
                'type': 'missing_cloned_session',
                'source_session_id': source_id,
            })
            continue

        expected_anchors = _normalize_memory_anchors(source_session_data.get('memory_anchors'))
        
        # Query actual session to get anchors
        clone_session = db.session.get(CampaignSession, clone_id)
        actual_anchors = _normalize_memory_anchors(clone_session.memory_anchors if clone_session else None)

        if expected_anchors != actual_anchors:
            mismatches.append({
                'type': 'anchor_mismatch',
                'source_session_id': source_id,
                'clone_session_id': clone_id,
                'expected': expected_anchors,
                'actual': actual_anchors,
            })

    anchored_session_count = sum(
        1
        for session in snapshot_data.get('sessions') or []
        if any(
            value not in (None, '', [])
            for value in _normalize_memory_anchors(session.get('memory_anchors')).values()
        )
    )

    report = {
        'version': CLONE_RETRIEVAL_PREFLIGHT_VERSION,
        'status': 'fail' if mismatches else 'pass',
        'snapshot_schema_version': snapshot_data.get('snapshot_schema_version'),
        'source_candidate_count': len(snapshot_keys),
        'clone_candidate_count': len(clone_keys),
        'source_embedding_count': len(expected_embeddings),
        'clone_embedding_count': len(actual_embeddings),
        'source_semantic_coverage_count': len(snapshot_coverage),
        'clone_semantic_coverage_count': len(clone_coverage),
        'session_count': len(snapshot_data.get('sessions') or []),
        'anchored_session_count': anchored_session_count,
        'world_event_count': len(snapshot_data.get('world_events') or []),
        'world_event_id_map_count': len(world_event_map),
        'mismatches': mismatches,
    }

    if mismatches:
        raise CloneRetrievalPreflightError(
            'Automation clone retrieval preflight failed',
            report=report,
        )

    return report


def materialize_run_campaign(run):
    if run.derived_campaign_id:
        return db.session.get(Campaign, run.derived_campaign_id), {}, {
            'version': CLONE_RETRIEVAL_PREFLIGHT_VERSION,
            'status': 'not_repeated',
            'reason': 'clone_already_materialized',
        }

    snapshot = run.snapshot
    data = snapshot.snapshot_json or {}

    if data.get('snapshot_schema_version') != AUTOMATION_SNAPSHOT_SCHEMA_VERSION:
        raise CloneRetrievalPreflightError(
            'Snapshot predates the retrieval-equivalent clone contract. '
            'Create a new snapshot before queueing this run.',
            report={
                'status': 'fail',
                'reason': 'legacy_snapshot_schema',
                'expected_schema_version': AUTOMATION_SNAPSHOT_SCHEMA_VERSION,
                'actual_schema_version': data.get('snapshot_schema_version'),
            },
        )

    required_sections = {
        'sessions',
        'world_events',
        'memory_embeddings',
        'world',
        'npcs',
        'clocks',
    }
    for section in required_sections:
        if section not in data:
            raise CloneRetrievalPreflightError(
                f'Snapshot is missing required section: {section}',
                report={
                    'status': 'fail',
                    'reason': f'missing_{section}_section',
                }
            )

    source_campaign = data.get('campaign') or {}
    clone = Campaign(
        name=f'{source_campaign.get("name") or "Automation"} [Run {run.id}]',
        description=source_campaign.get('description'),
        difficulty=source_campaign.get('difficulty'),
        seed=source_campaign.get('seed'),
        user_id=run.user_id,
        status='automation_run',
        settings=json.dumps(source_campaign.get('settings') or {}),
        is_automation_clone=True,
        automation_source_campaign_id=snapshot.source_campaign_id,
        automation_source_snapshot_id=snapshot.id,
        automation_source_run_id=run.id,
        last_played_at=None,
    )
    db.session.add(clone)
    db.session.flush()

    character_map = {}
    for character_data in data.get('characters') or []:
        source_character_id = character_data.get('id')
        clone_character = _clone_character(character_data, clone.id)
        character_map[source_character_id] = clone_character.id

    for member_data in data.get('members') or []:
        db.session.add(CampaignMember(
            campaign_id=clone.id,
            user_id=member_data.get('user_id'),
            role=member_data.get('role') or 'player',
            selected_character_id=character_map.get(member_data.get('selected_character_id')),
            character_ready_at=_parse_iso(member_data.get('character_ready_at')),
        ))

    session_map = {}
    for session_data in data.get('sessions') or []:
        session = CampaignSession(
            campaign_id=clone.id,
            started_at=_parse_iso(session_data.get('started_at')) or _utcnow(),
            ended_at=_parse_iso(session_data.get('ended_at')),
            recap=session_data.get('recap'),
            running_summary=session_data.get('running_summary'),
            is_active=bool(session_data.get('is_active')),
            memory_anchors=_normalize_memory_anchors(
                session_data.get('memory_anchors')
            ),
        )
        db.session.add(session)
        db.session.flush()
        
        session_id = session_data.get('id')
        if session_id is not None:
            session_map[session_id] = session.id
            session_map[str(session_id)] = session.id

        for message_data in session_data.get('messages') or []:
            db.session.add(SessionMessage(
                session_id=session.id,
                user_id=message_data.get('user_id'),
                role=message_data.get('role') or 'player',
                content=message_data.get('content') or '',
                created_at=_parse_iso(message_data.get('created_at')) or _utcnow(),
            ))
        for proposal_data in session_data.get('sheet_proposals') or []:
            db.session.add(SheetProposal(
                session_id=session.id,
                character_id=character_map.get(proposal_data.get('character_id')),
                dm_user_id=proposal_data.get('dm_user_id'),
                message_id=None,
                reason=proposal_data.get('reason') or 'Imported proposal',
                changes=proposal_data.get('changes') or [],
                status=proposal_data.get('status') or 'pending',
                created_at=_parse_iso(proposal_data.get('created_at')) or _utcnow(),
                applied_at=_parse_iso(proposal_data.get('applied_at')),
            ))
    world_data = data.get('world')
    if world_data:
        db.session.add(CampaignWorld(
            campaign_id=clone.id,
            public_intro=json.dumps(world_data.get('public_intro') or {}),
            knowledge_graph=json.dumps(world_data.get('knowledge_graph') or {}),
            world_state=json.dumps(world_data.get('world_state') or {}),
            dm_private=json.dumps(world_data.get('dm_private') or {}),
            approved_at=_parse_iso(world_data.get('approved_at')),
        ))

    planning_summary = data.get('planning_summary')
    if planning_summary:
        db.session.add(CampaignPlanningSummary(
            campaign_id=clone.id,
            party_balance=json.dumps(planning_summary.get('party_balance') or ''),
            confirmed_public_facts=json.dumps(planning_summary.get('confirmed_public_facts') or []),
            dm_private_secrets=json.dumps(planning_summary.get('dm_private_secrets') or {}),
            explicit_player_points=json.dumps(planning_summary.get('explicit_player_points') or {}),
            unresolved_gaps=json.dumps(planning_summary.get('unresolved_gaps') or []),
            accepted_hooks=json.dumps(planning_summary.get('accepted_hooks') or []),
        ))

    for planning_message in data.get('planning_messages') or []:
        db.session.add(CharacterPlanningMessage(
            campaign_id=clone.id,
            user_id=planning_message.get('user_id'),
            role=planning_message.get('role') or 'player',
            content=planning_message.get('content') or '',
            created_at=_parse_iso(planning_message.get('created_at')) or _utcnow(),
        ))

    for proposal in data.get('bond_proposals') or []:
        db.session.add(PlanningBondProposal(
            campaign_id=clone.id,
            title=proposal.get('title') or 'Bond proposal',
            description=proposal.get('description') or '',
            involved_user_ids=json.dumps(proposal.get('involved_user_ids') or []),
            approval_states=json.dumps(proposal.get('approval_states') or {}),
            status=proposal.get('status') or 'pending',
        ))

    for npc in data.get('npcs') or []:
        db.session.add(NPCActor(
            campaign_id=clone.id,
            actor_id=npc.get('actor_id') or npc.get('name') or f'npc_{npc.get("id")}',
            name=npc.get('name') or 'NPC',
            role=npc.get('role'),
            public_summary=npc.get('public_summary'),
            dossier=json.dumps(npc.get('dossier') or {}),
        ))

    for clock in data.get('clocks') or []:
        db.session.add(CampaignClock(
            campaign_id=clone.id,
            clock_id=clock.get('clock_id') or f'clock_{clock.get("id")}',
            name=clock.get('name') or 'Clock',
            segments=clock.get('segments') or 4,
            filled=clock.get('filled') or 0,
            pressure_type=clock.get('pressure_type'),
            visibility=clock.get('visibility') or 'dm_private',
            summary=clock.get('summary'),
            trigger=clock.get('trigger'),
            on_complete=clock.get('on_complete'),
            completion_criteria=clock.get('completion_criteria') or [],
            completion_state=clock.get('completion_state') or {},
            status=clock.get('status') or 'active',
        ))

    for shop in data.get('shops') or []:
        db.session.add(CampaignShop(
            campaign_id=clone.id,
            location_id=shop.get('location_id'),
            location_name=shop.get('location_name'),
            name=shop.get('name') or 'Shop',
            description=shop.get('description'),
            items_json=json.dumps(shop.get('items') or []),
            is_open=bool(shop.get('is_open', True)),
        ))

    for loot_box in data.get('loot_boxes') or []:
        db.session.add(LootBox(
            campaign_id=clone.id,
            session_id=None,
            name=loot_box.get('name') or 'Loot Box',
            description=loot_box.get('description'),
            items_json=json.dumps(loot_box.get('pools') or {}),
            currency_json=json.dumps(loot_box.get('currency') or {}),
            draw_results_json=json.dumps(loot_box.get('draws') or {}),
            status=loot_box.get('status') or 'unopened',
        ))

    for map_data in data.get('encounter_maps') or []:
        encounter_map = EncounterMap(
            campaign_id=clone.id,
            session_id=session_map.get(map_data.get('session_id')),
            title=map_data.get('title') or 'Encounter Map',
            prompt=map_data.get('prompt') or '',
            image_filename=map_data.get('image_filename') or '',
            labeled_image_filename=map_data.get('labeled_image_filename'),
            model=map_data.get('model') or 'unknown',
            size=map_data.get('size') or '1024x1024',
            quality=map_data.get('quality') or 'standard',
            grid_json=json.dumps(map_data.get('grid') or {}),
            vtt_setup_json=json.dumps(map_data.get('vtt_setup') or {}),
            encounter_state_json=json.dumps(map_data.get('encounter_state') or {}),
            setup_status=map_data.get('setup_status') or 'pending',
            setup_error=map_data.get('setup_error'),
            created_by_tool=bool(map_data.get('created_by_tool', True)),
            is_archived=bool(map_data.get('is_archived', False)),
            created_at=_parse_iso(map_data.get('created_at')) or _utcnow(),
            updated_at=_parse_iso(map_data.get('updated_at')) or _utcnow(),
        )
        db.session.add(encounter_map)
        db.session.flush()
        for placement_data in map_data.get('placements') or []:
            db.session.add(EncounterMapPlacement(
                encounter_map_id=encounter_map.id,
                actor_type=placement_data.get('actor_type') or 'npc',
                actor_id=str(placement_data.get('actor_id') or ''),
                label=placement_data.get('label') or '',
                grid_col=_safe_int(placement_data.get('col')),
                grid_row=_safe_int(placement_data.get('row')),
                created_at=_parse_iso(placement_data.get('created_at')) or _utcnow(),
                updated_at=_parse_iso(placement_data.get('updated_at')) or _utcnow(),
            ))

    # Copy world events and build ID map
    world_event_map = _copy_snapshot_world_events(data, clone)

    # Copy embeddings
    _copy_snapshot_embeddings(data, clone, world_event_map)

    db.session.flush()

    # Preflight validator
    preflight_report = validate_clone_retrieval_equivalence(
        snapshot_data=data,
        clone=clone,
        session_map=session_map,
        world_event_map=world_event_map,
    )

    policy = retention_policy_for_scenario(run.scenario) if run.scenario else dict(DEFAULT_RETENTION_POLICY)
    retention_days = max(0, _safe_int(policy.get('retention_days'), DEFAULT_RETENTION_POLICY['retention_days']))
    run.derived_campaign_id = clone.id
    run.clone_retention_expires_at = _utcnow() + timedelta(days=retention_days) if retention_days else None
    run.updated_at = _utcnow()

    return clone, character_map, preflight_report


def append_workspace_event(user_id, event_type, payload, *, resource_type=None, resource_id=None, commit=True):
    row = AutomationWorkspaceEvent(
        user_id=user_id,
        event_type=event_type,
        resource_type=resource_type,
        resource_id=resource_id,
        payload_json=payload or {},
    )
    db.session.add(row)
    if commit:
        db.session.commit()
    return row


def next_audit_cycle_number(run):
    latest = AutomationRunAuditCycle.query.filter_by(run_id=run.id).order_by(AutomationRunAuditCycle.cycle_number.desc()).first()
    return (latest.cycle_number if latest else 0) + 1


def create_audit_cycle(run, phase, payload=None, *, summary=None, player_message_id=None, dm_message_id=None, dedupe_key=None, worker_id=None, lease_token=None):
    payload = payload or {}
    cycle = AutomationRunAuditCycle(
        run_id=run.id,
        cycle_number=next_audit_cycle_number(run),
        phase=phase,
        status='pending',
        player_message_id=player_message_id,
        dm_message_id=dm_message_id,
        summary=summary,
        payload_json=payload,
    )
    db.session.add(cycle)
    db.session.flush()
    run.status = 'awaiting_audit'
    run.awaiting_audit_cycle_id = cycle.id
    run.awaiting_audit_phase = phase
    run.updated_at = _utcnow()

    # Create the run event representing the pause event
    run_event, _ = append_run_event(
        run,
        'audit_cycle_paused',
        {'phase': phase, 'audit_cycle': cycle.to_dict()},
        dedupe_key=dedupe_key or f'audit_cycle_paused:{cycle.id}',
        worker_id=worker_id,
        lease_token=lease_token,
        commit=False,
    )
    db.session.flush()

    # Query latest IDs scoped to current run/session/campaign
    campaign = run.derived_campaign or (run.scenario.source_campaign if run.scenario else None)
    campaign_id = campaign.id if campaign else None

    latest_session = latest_session_for_run(run)
    session_id = latest_session.id if latest_session else None

    boundary_audit_event_id = None
    if campaign_id:
        boundary_audit_event_id = db.session.query(db.func.max(CampaignAuditEvent.id)).filter_by(campaign_id=campaign_id).scalar()
    if boundary_audit_event_id is None:
        boundary_audit_event_id = 0

    boundary_provider_call_id = db.session.query(db.func.max(AutomationRunProviderCall.id)).filter_by(run_id=run.id).scalar()
    if boundary_provider_call_id is None:
        boundary_provider_call_id = 0

    boundary_message_id = None
    if session_id:
        boundary_message_id = db.session.query(db.func.max(SessionMessage.id)).filter_by(session_id=session_id).scalar()
    if boundary_message_id is None:
        boundary_message_id = 0

    boundary_run_event_id = run_event.id

    # Update cycle.payload_json
    payload = dict(cycle.payload_json or {})
    payload['boundary_audit_event_id'] = boundary_audit_event_id
    payload['boundary_provider_call_id'] = boundary_provider_call_id
    payload['boundary_run_event_id'] = boundary_run_event_id
    payload['boundary_message_id'] = boundary_message_id
    cycle.payload_json = payload

    db.session.commit()

    # Append workspace event for the run event
    append_workspace_event(
        run.user_id,
        'run_updated',
        _run_workspace_payload(run, run_event),
        resource_type='run',
        resource_id=run.id,
    )

    return cycle


def _parse_strict_bool(val, default=True):
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    if s in {"false", "0", "no", "n", "off"}:
        return False
    if s in {"true", "1", "yes", "y", "on"}:
        return True
    return bool(val)


def validate_evidence_ref(ref):
    if not isinstance(ref, dict):
        return False
    if not ref:
        return False
    kind = ref.get("kind")
    if kind not in {"audit_event", "provider_call", "run_event", "session_message", "memory_result", "snapshot_path"}:
        return False
    if not any(ref.get(k) is not None for k in ("id", "item_id", "text_hash", "path")):
        return False
    vis = ref.get("visibility")
    if vis not in {"public", "dm_private"}:
        ref["visibility"] = "unknown"
    path = ref.get("path")
    if path:
        import re
        if not re.match(r'^[a-zA-Z0-9_\.\[\]]+$', str(path)):
            return False
    return True


def _validate_audit_text_field(name, value):
    if value is None:
        return value
    if not isinstance(value, str):
        raise AuditScorecardValidationError(
            f'audit {name} must be a string.',
            code=f'invalid_{name}',
            details={'received_type': type(value).__name__},
        )
    return value


def submit_audit_cycle_feedback(cycle, *, summary=None, notes=None, scorecard=None):
    # Validate the complete submission schema before any ORM mutation so
    # dictionary/list values cannot reach TEXT columns and surface as opaque
    # database binder failures. This keeps validation errors structured and
    # retryable instead of a generic 500.
    summary = _validate_audit_text_field('summary', summary)
    notes = _validate_audit_text_field('notes', notes)
    if scorecard is not None and not isinstance(scorecard, dict):
        raise AuditScorecardValidationError(
            'audit scorecard must be an object.',
            code='invalid_scorecard_shape',
            details={'received_type': type(scorecard).__name__},
        )
    scorecard_payload = _json_object(scorecard, None)
    criteria_results = []
    template = current_scorecard_template_for_run(cycle.run)
    template_criteria = {
        _normalize_identifier(item.get('id')): item
        for item in _json_list(template.get('criteria'), [])
        if isinstance(item, dict) and item.get('id')
    }
    if scorecard_payload and scorecard_payload.get('__top_level_criteria__'):
        raise AuditScorecardValidationError(
            'criteria must be nested under scorecard.criteria.',
            code='top_level_criteria',
            details={'expected_path': 'scorecard.criteria'},
        )
    if scorecard_payload is None:
        if template_criteria:
            raise AuditScorecardValidationError(
                'scorecard.criteria is required for this template-backed audit cycle.',
                code='missing_scorecard',
                details={'required_criterion_ids': sorted(template_criteria)},
            )
        scorecard_payload = {}

    raw_criteria_value = scorecard_payload.get('criteria')
    if template_criteria and 'criteria' not in scorecard_payload:
        raise AuditScorecardValidationError(
            'scorecard.criteria is required for this template-backed audit cycle.',
            code='missing_criteria',
            details={'required_criterion_ids': sorted(template_criteria)},
        )
    if template_criteria and not isinstance(raw_criteria_value, list):
        raise AuditScorecardValidationError(
            'scorecard.criteria must be a non-empty array for this template-backed audit cycle.',
            code='invalid_criteria',
            details={'received_type': type(raw_criteria_value).__name__},
        )
    if template_criteria and not raw_criteria_value:
        raise AuditScorecardValidationError(
            'scorecard.criteria cannot be empty for this template-backed audit cycle.',
            code='empty_criteria',
            details={'required_criterion_ids': sorted(template_criteria)},
        )

    seen_ids = set()
    raw_criteria = _json_list(raw_criteria_value, [])
    for index, raw in enumerate(raw_criteria):
        if not isinstance(raw, dict):
            raise AuditScorecardValidationError(
                f'scorecard.criteria[{index}] must be an object.',
                code='invalid_criterion',
                details={'index': index},
            )
        criterion_id = _normalize_identifier(raw.get('criterion_id') or raw.get('id'))
        if not criterion_id:
            raise AuditScorecardValidationError(
                f'scorecard.criteria[{index}] is missing criterion_id.',
                code='missing_criterion_id',
                details={'index': index},
            )
        if criterion_id in seen_ids:
            raise AuditScorecardValidationError(
                f'Duplicate scorecard criterion: {criterion_id}.',
                code='duplicate_criterion',
                details={'criterion_id': criterion_id},
            )
        seen_ids.add(criterion_id)
        if template_criteria and criterion_id not in template_criteria:
            raise AuditScorecardValidationError(
                f'Criterion {criterion_id} is not part of the active scorecard template.',
                code='unknown_criterion',
                details={'criterion_id': criterion_id, 'required_criterion_ids': sorted(template_criteria)},
            )
        template_row = template_criteria.get(criterion_id, {})
        raw_status_value = raw.get('status')
        if raw_status_value is None:
            raise AuditScorecardValidationError(
                f'Criterion {criterion_id} is missing status.',
                code='missing_status',
                details={'criterion_id': criterion_id, 'allowed_statuses': list(CUSTOM_SCORECARD_STATUS_ORDER)},
            )
        if not isinstance(raw_status_value, str) or not raw_status_value.strip():
            raise AuditScorecardValidationError(
                f'Criterion {criterion_id} has invalid status {raw_status_value!r}.',
                code='invalid_status',
                details={'criterion_id': criterion_id, 'received_type': type(raw_status_value).__name__, 'allowed_statuses': list(CUSTOM_SCORECARD_STATUS_ORDER)},
            )
        raw_status = raw_status_value.strip().lower()
        if raw_status not in CUSTOM_SCORECARD_STATUS_ORDER:
            raise AuditScorecardValidationError(
                f'Criterion {criterion_id} has invalid status {raw_status_value!r}.',
                code='invalid_status',
                details={'criterion_id': criterion_id, 'allowed_statuses': list(CUSTOM_SCORECARD_STATUS_ORDER)},
            )
        status = raw_status
        
        evidence_refs = []
        for ref in _json_list(raw.get('evidence_refs'), []):
            if validate_evidence_ref(ref):
                evidence_refs.append({
                    'kind': ref.get('kind'),
                    'id': ref.get('id'),
                    'item_id': ref.get('item_id'),
                    'text_hash': ref.get('text_hash'),
                    'path': ref.get('path'),
                    'summary': ref.get('summary'),
                    'visibility': ref.get('visibility'),
                })
                
        applicability = _json_object(raw.get('applicability'), {})
        applicability = {
            'applicable': _parse_strict_bool(applicability.get('applicable'), True),
            'reason': str(applicability.get('reason') or '').strip() or None,
            'phase': str(applicability.get('phase') or '').strip() or None,
        }

        # Normalize to one canonical representation
        if status == 'not_applicable' or not applicability['applicable']:
            status = 'not_applicable'
            applicability['applicable'] = False

        criteria_results.append({
            'criterion_id': criterion_id,
            'label': template_row.get('label') or raw.get('label') or criterion_id,
            'status': status,
            'summary': _coerce_scorecard_text(raw.get('summary'), criterion_id, index, 'summary'),
            'primary_evidence': _coerce_scorecard_text(raw.get('primary_evidence'), criterion_id, index, 'primary_evidence'),
            'evidence': _coerce_scorecard_text(raw.get('evidence'), criterion_id, index, 'evidence'),
            'evidence_refs': evidence_refs,
            'applicability': applicability,
        })

    missing_ids = sorted(set(template_criteria) - seen_ids)
    if missing_ids:
        raise AuditScorecardValidationError(
            'The audit scorecard is missing required template criteria.',
            code='partial_criteria',
            details={'missing_criterion_ids': missing_ids, 'received_criterion_ids': sorted(seen_ids)},
        )

    cycle.summary = summary if summary is not None else cycle.summary
    cycle.notes = notes if notes is not None else cycle.notes

    if criteria_results:
        applicable_results = [
            item for item in criteria_results
            if item.get('status') != 'not_applicable' and item.get('applicability', {}).get('applicable', True)
        ]
        overall_status = (
            _aggregate_custom_scorecard_statuses([item.get('status') for item in applicable_results], default='warn')
            if applicable_results
            else _aggregate_custom_scorecard_statuses([item.get('status') for item in criteria_results], default='warn')
        )
    else:
        overall_status = _normalize_custom_scorecard_status(scorecard_payload.get('overall_status')) or 'warn'
    criteria_assessed_count = sum(1 for item in criteria_results if item.get('status') in {'pass', 'warn', 'fail'})
    criteria_not_assessed_count = sum(1 for item in criteria_results if item.get('status') == 'not_assessed' and item.get('applicability', {}).get('applicable', True))
    criteria_not_applicable_count = sum(
        1 for item in criteria_results
        if item.get('status') == 'not_applicable' or not item.get('applicability', {}).get('applicable', True)
    )
    
    cycle.scorecard_json = {
        'template': template,
        'criteria': criteria_results,
        'source': scorecard_payload.get('source'),
        'auditor_jobs': _json_list(scorecard_payload.get('auditor_jobs'), []),
        'tool_calls_used': _json_list(scorecard_payload.get('tool_calls_used'), []),
        'unresolved_evidence_gaps': _json_list(scorecard_payload.get('unresolved_evidence_gaps'), []),
        'visible_findings': _json_list(scorecard_payload.get('visible_findings'), []),
        'hidden_state_findings': _json_list(scorecard_payload.get('hidden_state_findings'), []),
    }
    cycle.scorecard_summary_json = {
        'overall_status': overall_status,
        'overall_summary': (scorecard_payload.get('overall_summary') or '').strip() or None,
        'criteria_count': len(criteria_results),
        'criteria_assessed_count': criteria_assessed_count,
        'criteria_not_assessed_count': criteria_not_assessed_count,
        'criteria_not_applicable_count': criteria_not_applicable_count,
        'auditor_job_count': len(_json_list(scorecard_payload.get('auditor_jobs'), [])),
        'unresolved_evidence_gap_count': len(_json_list(scorecard_payload.get('unresolved_evidence_gaps'), [])),
        'fully_scored': bool(
            criteria_results
            and all(item.get('status') in {'pass', 'warn', 'fail', 'not_applicable'} for item in criteria_results)
        ) if template_criteria else True,
    }
    cycle.status = 'audited'
    cycle.audited_at = _utcnow()
    cycle.updated_at = _utcnow()
    db.session.commit()
    return cycle


def continue_audit_run(run, *, force=False):
    cycle = db.session.get(AutomationRunAuditCycle, run.awaiting_audit_cycle_id) if run.awaiting_audit_cycle_id else None
    if cycle and cycle.status not in AUDIT_READY_STATUSES:
        if not force:
            raise ValueError('Current audit cycle must be audited before continuing')
        cycle.status = 'skipped'
        cycle.audited_at = cycle.audited_at or _utcnow()
    # Audit checkpoints are durable queue boundaries. The worker that reached
    # the checkpoint releases its lease, and a free worker resumes the run.
    run.status = 'queued'
    run.worker_id = None
    run.lease_token = None
    run.heartbeat_at = None
    run.lease_expires_at = None
    run.worker_api_base = None
    run.awaiting_audit_phase = None
    run.awaiting_audit_cycle_id = None
    run.audit_resumed_at = _utcnow()
    run.updated_at = _utcnow()
    db.session.commit()
    return cycle


def reconcile_stale_awaiting_audit_runs():
    """Advance runs stuck in awaiting_audit whose audit cycle is already audited.

    Uses the normal audited-cycle continuation path so repaired runs resume
    through the standard queue rather than direct state edits.
    """
    stale_runs = AutomationRun.query.filter_by(status='awaiting_audit').all()
    reconciled = 0
    for run in stale_runs:
        cycle = db.session.get(AutomationRunAuditCycle, run.awaiting_audit_cycle_id) if run.awaiting_audit_cycle_id else None
        if cycle is None or cycle.status not in AUDIT_READY_STATUSES:
            continue
        continue_audit_run(run)
        reconciled += 1
    return reconciled


def lease_seconds_for_run(run):
    return max(15, _safe_int((run.runner_config_json or {}).get('lease_seconds'), DEFAULT_LEASE_SECONDS))


def provisioning_lease_seconds_for_run(run):
    configured = _safe_int(
        (run.runner_config_json or {}).get('provisioning_lease_seconds'),
        DEFAULT_PROVISIONING_LEASE_SECONDS,
    )
    return max(60, configured)


def max_reclaim_failures_for_run(run):
    """Consecutive identical infrastructure-failure reclaims allowed before the
    run is terminalized instead of being reclaimed again (issue #131)."""
    configured = _safe_int(
        (run.runner_config_json or {}).get('max_reclaim_failures'),
        DEFAULT_MAX_RECLAIM_FAILURES,
    )
    return max(1, configured)


from sqlalchemy import update as sa_update


def reserve_run_lease(run_id, worker_id, now, provisioning=False):
    run = db.session.get(AutomationRun, run_id)
    if run is None:
        raise ValueError('Run not found')

    old_status = run.status
    is_reclaim = old_status != 'queued' and (
        run.lease_expires_at is None or run.lease_expires_at <= now
    )

    new_lease_token = secrets.token_hex(16)
    lease_seconds = max(
        lease_seconds_for_run(run),
        provisioning_lease_seconds_for_run(run) if provisioning else 0,
    )
    lease_expires_at = now + timedelta(seconds=lease_seconds)
    common_values = dict(
        worker_id=worker_id,
        lease_token=new_lease_token,
        heartbeat_at=now,
        lease_expires_at=lease_expires_at,
        status='claimed',
        attempt_count=AutomationRun.attempt_count + 1,
        updated_at=now,
    )

    # Try fresh claim (was queued)
    fresh_stmt = (
        sa_update(AutomationRun)
        .where(
            AutomationRun.id == run_id,
            AutomationRun.status == 'queued',
        )
        .values(claimed_at=now, reclaim_count=AutomationRun.reclaim_count, **common_values)
    )
    result = db.session.execute(fresh_stmt)
    if result.rowcount == 1:
        db.session.commit()
        return run, new_lease_token, False

    # Belt-and-suspenders for issue #131: never reclaim a run whose
    # consecutive identical infrastructure failures already hit the threshold.
    # The worker's /worker-error report normally terminalizes first; this guard
    # catches the case where the run became reclaimable again (e.g. the
    # terminalizing report raced with lease expiry).
    run = db.session.get(AutomationRun, run_id)
    if (
        run
        and run.reclaim_failure_fingerprint
        and run.reclaim_failure_count >= max_reclaim_failures_for_run(run)
    ):
        raise InfrastructureFailureThresholdError(run)

    # Try reclaim (active status but expired lease)
    reclaim_stmt = (
        sa_update(AutomationRun)
        .where(
            AutomationRun.id == run_id,
            AutomationRun.status.in_(tuple(CLAIMABLE_ACTIVE_STATUSES)),
            db.or_(
                AutomationRun.lease_expires_at == None,
                AutomationRun.lease_expires_at <= now,
            ),
        )
        .values(reclaim_count=AutomationRun.reclaim_count + 1, **common_values)
    )
    result = db.session.execute(reclaim_stmt)

    if result.rowcount == 0:
        run = db.session.get(AutomationRun, run_id)
        if run and run.status not in {'queued', *CLAIMABLE_ACTIVE_STATUSES}:
            raise ValueError(f'Run is not claimable from status {run.status}')
        raise ValueError(f'Run is already claimed by another worker')

    db.session.commit()
    return run, new_lease_token, True


def release_run_lease(run_id, lease_token, failure_reason):
    now = _utcnow()
    stmt = (
        sa_update(AutomationRun)
        .where(
            AutomationRun.id == run_id,
            AutomationRun.lease_token == lease_token,
        )
        .values(
            status='queued',
            worker_id=None,
            lease_token=None,
            heartbeat_at=None,
            lease_expires_at=None,
            claim_failure_reason=failure_reason,
            updated_at=now,
        )
    )
    result = db.session.execute(stmt)
    db.session.commit()
    return result.rowcount > 0


def release_run_for_audit(run_id, lease_token):
    """Release worker capacity while preserving an awaiting-audit checkpoint."""
    now = _utcnow()
    stmt = (
        sa_update(AutomationRun)
        .where(
            AutomationRun.id == run_id,
            AutomationRun.status == 'awaiting_audit',
            AutomationRun.lease_token == lease_token,
        )
        .values(
            worker_id=None,
            lease_token=None,
            heartbeat_at=None,
            lease_expires_at=None,
            worker_api_base=None,
            updated_at=now,
        )
    )
    result = db.session.execute(stmt)
    db.session.commit()
    return result.rowcount > 0


def clear_reclaim_failure(run):
    """Reset the consecutive infrastructure-failure tracking after the run has
    demonstrably recovered (a gameplay milestone was reached), so the counter
    reflects only back-to-back failures with no intervening recovery."""
    if not run.reclaim_failure_fingerprint:
        return
    run.reclaim_failure_fingerprint = None
    run.reclaim_failure_count = 0
    run.reclaim_failure_attempt = None
    run.reclaim_failure_stage = None
    run.reclaim_failure_error = None
    run.reclaim_failure_at = None


def record_worker_infrastructure_failure(
    run_id,
    worker_id,
    lease_token,
    *,
    stage,
    fingerprint,
    error,
    attempt_number=None,
):
    """Persist a stable control-plane failure fingerprint on a run and either
    release the lease for bounded retry or terminalize the run once the
    consecutive-failure threshold is reached (issue #131).

    A worker that aborts outside gameplay reports the failure with its current
    worker_id/lease_token. The server:
      - validates the reporter still owns the run lease (or matches worker_id
        when no token is available, e.g. a claim-stage failure),
      - rolls the failure into a stable fingerprint + consecutive count,
      - records one durable event per attempt (dedupe key carries the attempt
        number so every identical failure is observable),
      - releases the lease back to queued while the count is below the
        threshold so transient failures retry with bounded backoff,
      - transitions the run to a terminal `failed` state once the threshold is
        hit, ending the reclaim crash loop with a diagnosable error.

    Returns a dict with `action` in {'released', 'terminalized', 'already_terminal'}.
    """
    run = db.session.get(AutomationRun, run_id)
    if run is None:
        raise ValueError('Run not found')

    if run.status in {'completed', 'failed', 'stopped'}:
        return {'action': 'already_terminal', 'status': run.status}

    if run.worker_id and worker_id and run.worker_id != worker_id:
        raise ValueError('Run is leased by another worker')
    if lease_token and run.lease_token and run.lease_token != lease_token:
        raise ValueError('Run lease token is no longer valid')

    now = _utcnow()
    threshold = max_reclaim_failures_for_run(run)
    if run.reclaim_failure_fingerprint == fingerprint:
        run.reclaim_failure_count = (run.reclaim_failure_count or 0) + 1
    else:
        run.reclaim_failure_fingerprint = fingerprint
        run.reclaim_failure_count = 1
    run.reclaim_failure_attempt = attempt_number if attempt_number is not None else run.attempt_count
    run.reclaim_failure_stage = stage
    run.reclaim_failure_error = error
    run.reclaim_failure_at = now

    append_run_event(
        run,
        'worker_infrastructure_failure',
        {
            'stage': stage,
            'fingerprint': fingerprint,
            'attempt_number': run.reclaim_failure_attempt,
            'count': run.reclaim_failure_count,
            'threshold': threshold,
            'error': error,
            'semantic_key': f'infra:{stage}:{fingerprint}',
            'reporter_worker_id': worker_id,
            'reported_with_lease_token': bool(lease_token),
        },
        dedupe_key=f'worker_infrastructure_failure:{run.id}:{fingerprint}:attempt:{run.reclaim_failure_attempt}',
    )

    if run.reclaim_failure_count >= threshold:
        run.status = 'failed'
        run.error_text = (
            f'infrastructure_failure_reclaim_loop:'
            f'stage:{stage}:fingerprint:{fingerprint}:'
            f'count:{run.reclaim_failure_count}:attempt:{run.reclaim_failure_attempt}'
        )
        run.finished_at = now
        run.lease_expires_at = now
        run.lease_token = None
        run.worker_id = None
        run.heartbeat_at = now
        db.session.commit()
        return {
            'action': 'terminalized',
            'status': 'failed',
            'count': run.reclaim_failure_count,
            'threshold': threshold,
        }

    run.status = 'queued'
    run.worker_id = None
    run.lease_token = None
    run.heartbeat_at = None
    run.lease_expires_at = None
    db.session.commit()
    return {
        'action': 'released',
        'status': 'queued',
        'count': run.reclaim_failure_count,
        'threshold': threshold,
    }


def lease_is_expired(run, at=None):
    if run.status == 'queued':
        return False
    if not run.lease_expires_at:
        return run.status in {'claimed', 'running', 'stop_requested', 'awaiting_audit', 'reconciling'}
    return (at or _utcnow()) >= run.lease_expires_at


def ensure_worker_lease(run, *, worker_id=None, lease_token=None):
    if run.status in {'claimed', 'running', 'stop_requested', 'awaiting_audit', 'reconciling'}:
        if not worker_id:
            raise ValueError('worker_id is required for active runs')
        if not lease_token:
            raise ValueError('lease_token is required for active runs')
    if run.worker_id and run.worker_id != worker_id:
        raise ValueError('Run is leased by another worker')
    if run.lease_token and run.lease_token != lease_token:
        raise ValueError('Run lease token is no longer valid')
    if run.status in {'claimed', 'running', 'stop_requested', 'awaiting_audit', 'reconciling'} and lease_is_expired(run):
        raise ValueError('Run lease has expired')


def _run_workspace_payload(run, event=None):
    payload = {
        'type': 'run_updated',
        'run': run.to_dict(),
        'scenario_id': run.scenario_id,
    }
    if event is not None:
        payload['event'] = event.to_dict()
    return payload


def append_run_event(run, event_type, payload=None, *, dedupe_key=None, worker_id=None, lease_token=None, commit=True, skip_workspace=False):
    if worker_id or lease_token:
        ensure_worker_lease(run, worker_id=worker_id, lease_token=lease_token)

    if dedupe_key:
        existing = AutomationRunEvent.query.filter_by(run_id=run.id, dedupe_key=dedupe_key).first()
        if existing:
            return existing, False

    row = AutomationRunEvent(
        run_id=run.id,
        event_type=event_type,
        sequence_number=(run.last_event_sequence or 0) + 1,
        attempt_number=run.attempt_count or 0,
        dedupe_key=dedupe_key,
        payload_json=payload or {},
    )
    db.session.add(row)
    db.session.flush()
    if event_type in RECLAIM_RECOVERY_EVENT_TYPES:
        clear_reclaim_failure(run)
    run.last_event_id = row.id
    run.last_event_sequence = row.sequence_number
    run.updated_at = _utcnow()
    if commit:
        db.session.commit()
        if not skip_workspace:
            append_workspace_event(
                run.user_id,
                'run_updated',
                _run_workspace_payload(run, row),
                resource_type='run',
                resource_id=run.id,
            )
    return row, True


def record_worker_activity(worker_id, api_base=None, is_heartbeat=False):
    if not worker_id:
        return

    @retry_on_sqlite_lock(
        max_attempts=3,
        backoff_ms=100,
        category='worker_activity',
        rollback_fn=db.session.rollback,
    )
    def _inner():
        now = utcnow()
        worker = AutomationWorker.query.filter_by(worker_id=worker_id).first()
        if not worker:
            worker = AutomationWorker(worker_id=worker_id)
            db.session.add(worker)
        if api_base:
            worker.api_base = api_base
        if is_heartbeat:
            worker.last_heartbeat_at = now
        else:
            worker.last_poll_at = now
        worker.updated_at = now
        db.session.commit()

    try:
        _inner()
    except Exception:
        db.session.rollback()
        import logging
        logging.getLogger(__name__).warning(
            'Unable to record worker activity for worker_id=%s; continuing.',
            worker_id, exc_info=True,
        )


def compute_gameplay_readiness(campaign):
    if not campaign:
        return {
            'world_present': False,
            'active_session_present': False,
            'opening_dm_present': False,
            'campaign_ready': False,
        }

    world_present = CampaignWorld.query.filter_by(campaign_id=campaign.id).first() is not None
    active_session = CampaignSession.query.filter_by(campaign_id=campaign.id, is_active=True).first()
    active_session_present = active_session is not None
    opening_dm_present = False
    if active_session:
        opening_dm_present = any(
            m.role == 'dm' and m.content and m.content.strip()
            for m in active_session.messages
        )

    campaign_ready = bool(world_present and active_session_present and opening_dm_present)

    return {
        'world_present': world_present,
        'active_session_present': active_session_present,
        'opening_dm_present': opening_dm_present,
        'campaign_ready': campaign_ready,
    }


def claim_run_for_worker(run, worker_id):
    first_materialization = run.derived_campaign_id is None

    # Step 1: Atomic reservation (with provisioning lease if first materialization)
    run, lease_token, reclaimed = reserve_run_lease(
        run.id, worker_id, _utcnow(), provisioning=first_materialization,
    )

    try:
        if first_materialization:
            clone_campaign, character_map, preflight_report = materialize_run_campaign(run)
        else:
            clone_campaign = db.session.get(Campaign, run.derived_campaign_id)
            character_map = {}
            preflight_report = {
                'version': CLONE_RETRIEVAL_PREFLIGHT_VERSION,
                'status': 'not_repeated',
                'reason': 'clone_already_materialized',
            }

        # Step 2: Replace provisioning lease with normal runtime lease.
        # Use a fresh timestamp so a long materialization does not shrink the window.
        now = _utcnow()
        normal_expires = now + timedelta(seconds=lease_seconds_for_run(run))
        stmt = (
            sa_update(AutomationRun)
            .where(
                AutomationRun.id == run.id,
                AutomationRun.lease_token == lease_token,
            )
            .values(
                lease_expires_at=normal_expires,
                updated_at=now,
            )
        )
        result = db.session.execute(stmt)
        if result.rowcount == 0:
            raise ValueError('Run lease was lost during materialization')
    except Exception as exc:
        db.session.rollback()
        release_run_lease(run.id, lease_token, str(exc))
        raise

    db.session.commit()
    run = db.session.get(AutomationRun, run.id)

    latest_session = CampaignSession.query.filter_by(campaign_id=clone_campaign.id, is_active=True).first()
    if latest_session is None:
        latest_session = CampaignSession.query.filter_by(campaign_id=clone_campaign.id).order_by(CampaignSession.started_at.desc(), CampaignSession.id.desc()).first()

    gameplay_readiness = compute_gameplay_readiness(clone_campaign)

    return {
        'run': run,
        'clone_campaign': clone_campaign,
        'character_map': character_map,
        'latest_session': latest_session,
        'reclaimed': reclaimed,
        'retrieval_preflight': preflight_report,
        'gameplay_readiness': gameplay_readiness,
    }


def heartbeat_run(run, *, worker_id=None, lease_token=None, lease_seconds=None):
    run_id = run.id

    @retry_on_sqlite_lock(
        max_attempts=3,
        backoff_ms=100,
        category='run_heartbeat',
        rollback_fn=db.session.rollback,
    )
    def _heartbeat_once():
        current_run = db.session.get(AutomationRun, run_id)
        if current_run is None:
            raise ValueError('Run not found')
        ensure_worker_lease(current_run, worker_id=worker_id, lease_token=lease_token)
        now = _utcnow()
        current_run.heartbeat_at = now
        duration = lease_seconds if lease_seconds is not None else lease_seconds_for_run(current_run)
        current_run.lease_expires_at = now + timedelta(seconds=duration)
        current_run.updated_at = now
        db.session.commit()
        return current_run

    return _heartbeat_once()


def persist_provider_call(run, payload):
    dedupe_key = payload.get('dedupe_key')
    if not dedupe_key:
        raise ValueError('dedupe_key is required')
    existing = AutomationRunProviderCall.query.filter_by(run_id=run.id, dedupe_key=dedupe_key).first()
    if existing:
        return existing, False

    usage = payload.get('usage') or {}
    row = AutomationRunProviderCall(
        run_id=run.id,
        dedupe_key=dedupe_key,
        phase=payload.get('phase') or 'unknown',
        prompt_version_id=payload.get('prompt_version_id'),
        provider=payload.get('provider'),
        model=payload.get('model'),
        provider_response_id=payload.get('provider_response_id'),
        usage_input_tokens=usage.get('prompt_tokens') or usage.get('input_tokens'),
        usage_output_tokens=usage.get('completion_tokens') or usage.get('output_tokens'),
        usage_total_tokens=usage.get('total_tokens'),
        latency_ms=payload.get('latency_ms'),
        latency_bucket=payload.get('latency_bucket') or _latency_bucket(payload.get('latency_ms')),
        parse_repair_attempts=_safe_int(payload.get('parse_repair_attempts')),
        failure_class=payload.get('failure_class'),
        request_json=payload.get('request') or {},
        response_json=payload.get('response') or {},
        parsed_output_json=payload.get('parsed_output') or {},
        response_text=payload.get('response_text'),
    )
    db.session.add(row)
    db.session.commit()
    return row, True


def provider_call_for_replay(run_id, dedupe_key):
    return AutomationRunProviderCall.query.filter_by(run_id=run_id, dedupe_key=dedupe_key).first()


def _run_context(run):
    event_rows = AutomationRunEvent.query.filter_by(run_id=run.id).order_by(AutomationRunEvent.sequence_number.asc(), AutomationRunEvent.id.asc()).all()
    audit_rows = CampaignAuditEvent.query.filter_by(campaign_id=run.derived_campaign_id).order_by(CampaignAuditEvent.created_at.asc(), CampaignAuditEvent.id.asc()).all() if run.derived_campaign_id else []
    provider_rows = AutomationRunProviderCall.query.filter_by(run_id=run.id).order_by(AutomationRunProviderCall.created_at.asc(), AutomationRunProviderCall.id.asc()).all()
    cycle_rows = AutomationRunAuditCycle.query.filter_by(run_id=run.id).order_by(AutomationRunAuditCycle.cycle_number.asc(), AutomationRunAuditCycle.id.asc()).all()
    return event_rows, audit_rows, provider_rows, cycle_rows


def _audit_payload(row):
    try:
        value = json.loads(row.payload) if row.payload else {}
    except (TypeError, ValueError):
        value = {}
    return value if isinstance(value, dict) else {}


def collect_model_retry_metrics(audit_rows, provider_rows, *, run_status=None):
    """Return the canonical retry total, per-kind counts, and correlations.

    Audit row ids and provider-call ids form stable source keys, preventing a
    retry from being counted twice when scorecards and exports are refreshed.
    Parse repair attempts are expanded into stable per-attempt source keys.
    """
    counts = {kind: 0 for kind in MODEL_RETRY_TAXONOMY}
    correlations = []
    blocked_traces = {
        (row.trace_id, row.event_type.removesuffix('_blocked'))
        for row in audit_rows
        if row.trace_id and row.event_type.endswith('_guard_blocked')
    }

    for row in audit_rows:
        event_type = row.event_type or ''
        payload = _audit_payload(row)
        kind = None
        if event_type == 'model_retry':
            kind = 'provider_retry'
        elif event_type == 'resolver_contract_repair_requested':
            kind = 'resolver_contract_repair'
        elif event_type == 'finalizer_contract_guard_retry':
            kind = 'contract_guard_retry'
        elif event_type.endswith('_guard_retry') and 'tool' in event_type:
            kind = 'tool_repair'
        elif event_type.endswith('_guard_retry'):
            kind = 'guard_retry'
        elif event_type in {'tool_repair', 'tool_call_repair', 'tool_output_repair'}:
            kind = 'tool_repair'
        elif event_type == 'model_request':
            operation = str(payload.get('operation') or '')
            if operation.endswith('_retry') or '_repair_retry' in operation:
                kind = 'other_model_reinvocation'
        if kind is None:
            continue

        guard_base = event_type.removesuffix('_retry')
        if event_type.endswith('_guard_retry'):
            outcome = 'exhausted' if (row.trace_id, guard_base) in blocked_traces else (
                'pending' if run_status in AUTOMATION_ACTIVE_STATUSES else 'repaired'
            )
        else:
            outcome = payload.get('outcome') or 'retried'
        counts[kind] += 1
        correlations.append({
            'source_key': f'audit_event:{row.id}',
            'kind': kind,
            'event_type': event_type,
            'turn': payload.get('turn_number') or payload.get('turn') or payload.get('player_message_id'),
            'provider_call_id': payload.get('provider_call_id'),
            'attempt': payload.get('next_attempt') or payload.get('attempt'),
            'outcome': outcome,
            'trace_id': row.trace_id,
            'parent_trace_id': row.parent_trace_id,
        })

    for row in provider_rows:
        attempts = max(0, row.parse_repair_attempts or 0)
        for attempt in range(1, attempts + 1):
            counts['parse_repair'] += 1
            correlations.append({
                'source_key': f'provider_call:{row.id}:parse_repair:{attempt}',
                'kind': 'parse_repair',
                'event_type': 'provider_parse_repair',
                'turn': (row.request_json or {}).get('turn_number'),
                'provider_call_id': row.id,
                'attempt': attempt,
                'outcome': 'exhausted' if row.failure_class else 'repaired',
                'trace_id': (row.request_json or {}).get('trace_id'),
                'parent_trace_id': (row.request_json or {}).get('parent_trace_id'),
            })

    total = sum(counts.values())
    return {
        'taxonomy_version': 1,
        'label': 'All model retries and repair re-invocations',
        'definitions': dict(MODEL_RETRY_TAXONOMY),
        'counts': counts,
        'total': total,
        'correlations': correlations,
    }


POST_TURN_AUDIT_ERROR_KINDS = {
    'memory_update_error': 'memory',
    'clock_adjudicator_error': 'clock',
    'post_turn_summary_finalize_error': 'summary',
    'summary_finalizer_error': 'finalizer',
    'summary_verifier_error': 'durable_application',
    'post_turn_consistency_incident': 'durable_application',
    'post_turn_state_invariant_incident': 'durable_application',
    'embedding_fallback': 'embedding',
}


def _post_turn_correlation(*, payload=None, trace_id=None, parent_trace_id=None, fallback=None):
    payload = payload if isinstance(payload, dict) else {}
    player_message_id = payload.get('player_message_id') or payload.get('source_player_message_id')
    if player_message_id is None:
        for value in (trace_id, parent_trace_id):
            match = re.search(r'(?:message_|message:)(\d+)', str(value or ''))
            if match:
                player_message_id = int(match.group(1))
                break
    if player_message_id is not None:
        return f'player_message:{player_message_id}', player_message_id
    trace = parent_trace_id or trace_id
    if trace:
        return f'trace:{trace}', None
    return fallback, None


def collect_automation_errors(run, event_rows=None, audit_rows=None):
    """Collect correlated failures using canonical durable post-turn state."""
    if event_rows is None or audit_rows is None:
        event_rows, audit_rows, _, _ = _run_context(run)

    turns = (
        SessionDmTurn.query.filter_by(campaign_id=run.derived_campaign_id)
        .order_by(SessionDmTurn.id.asc()).all()
        if run.derived_campaign_id else []
    )
    turn_states = {turn.player_message_id: state_for_turn(turn) for turn in turns}
    groups = {}

    def add(kind, correlation, *, player_message_id=None, summary=None, evidence=None, transient=False):
        correlation = correlation or f'uncorrelated:{kind}:{len(groups)}'
        group = groups.setdefault(correlation, {
            'player_message_id': player_message_id,
            'candidates': [],
        })
        if group.get('player_message_id') is None and player_message_id is not None:
            group['player_message_id'] = player_message_id
        group['candidates'].append({
            'kind': kind,
            'summary': summary,
            'evidence': evidence,
            'transient': transient,
        })

    for turn in turns:
        correlation = f'player_message:{turn.player_message_id}'
        evidence = {
            'kind': 'session_message',
            'id': turn.player_message_id,
            'path': 'session_dm_turn',
            'summary': f'Durable DM turn {turn.id}',
            'visibility': 'dm_private',
        }
        state = turn_states[turn.player_message_id]
        stage_kinds = [
            kind
            for kind in ('memory', 'clock')
            if state['post_turn_stages'].get(kind) in FAILED_STAGE_STATUSES
        ]
        if state['post_turn_stages'].get('finalizer') in FAILED_STAGE_STATUSES:
            stage_kinds.append('finalizer')
        if not state.get('post_turn_complete') and state.get('post_turn_resolved') and not stage_kinds:
            stage_kinds.append('post_turn')
        if turn.status == 'error' and not stage_kinds:
            stage_kinds.append('finalizer')
        for kind in stage_kinds:
            add(
                kind,
                correlation,
                player_message_id=turn.player_message_id,
                summary=turn.error_text or f'Durable DM turn reports {kind} failure.',
                evidence=evidence,
            )

    for row in audit_rows:
        kind = POST_TURN_AUDIT_ERROR_KINDS.get(row.event_type)
        if kind is None:
            continue
        payload = _audit_payload(row)
        correlation, player_message_id = _post_turn_correlation(
            payload=payload,
            trace_id=row.trace_id,
            parent_trace_id=row.parent_trace_id,
            fallback=f'audit_event:{row.id}',
        )
        add(
            kind,
            correlation,
            player_message_id=player_message_id,
            summary=row.summary,
            transient=row.event_type == 'embedding_fallback',
            evidence={
                'kind': 'audit_event',
                'id': row.id,
                'path': 'payload',
                'summary': row.summary,
                'visibility': 'dm_private',
            },
        )

    for row in event_rows:
        payload = row.payload_json or {}
        if row.event_type not in {'error', 'dm_turn_status'}:
            continue
        correlation, player_message_id = _post_turn_correlation(
            payload=payload,
            trace_id=payload.get('trace_id'),
            parent_trace_id=payload.get('parent_trace_id'),
            fallback=f'run_event:{row.id}',
        )
        state = derive_post_turn_state(
            payload.get('post_turn_status'),
            payload.get('memory_status'),
            payload.get('clock_status'),
            correlation_id=correlation,
            error_text=payload.get('post_turn_error') or payload.get('turn_error'),
        )
        failed_kinds = [
            kind
            for kind in ('memory', 'clock')
            if state.get(f'{kind}_status') in FAILED_STAGE_STATUSES
        ]
        if not state.get('post_turn_complete') and state.get('post_turn_resolved') and not failed_kinds:
            failed_kinds.append('post_turn')
        if row.event_type == 'error' and not failed_kinds:
            failed_kinds.append('automation')
        for kind in failed_kinds:
            add(
                kind,
                correlation,
                player_message_id=player_message_id,
                summary=payload.get('post_turn_error') or payload.get('turn_error') or payload.get('error') or 'Automation run error.',
                evidence={
                    'kind': 'run_event',
                    'id': row.id,
                    'path': 'payload',
                    'summary': f'{row.event_type} run event',
                    'visibility': 'dm_private',
                },
            )

    recovered_correlations = set()
    for row in audit_rows:
        if row.event_type not in {'memory_recovery_applied', 'post_turn_consistency_reconciled'}:
            continue
        correlation, _ = _post_turn_correlation(
            payload=_audit_payload(row), trace_id=row.trace_id, parent_trace_id=row.parent_trace_id,
        )
        if correlation:
            recovered_correlations.add(correlation)

    errors = []
    for correlation, group in groups.items():
        candidates = group['candidates']
        specific_kinds = {item['kind'] for item in candidates if item['kind'] not in {'automation', 'post_turn'}}
        kinds = specific_kinds or {item['kind'] for item in candidates}
        player_message_id = group.get('player_message_id')
        turn_state = turn_states.get(player_message_id)
        durable_recovered = bool(
            turn_state
            and turn_state.get('post_turn_status') in SUCCESS_POST_TURN_STATUSES
            and turn_state.get('post_turn_complete')
        )
        for kind in sorted(kinds):
            matching = [item for item in candidates if item['kind'] == kind] or candidates
            evidence_refs = []
            seen_refs = set()
            for item in matching:
                ref = item.get('evidence')
                if not ref:
                    continue
                ref_key = (ref.get('kind'), ref.get('id'), ref.get('path'))
                if ref_key not in seen_refs:
                    evidence_refs.append(ref)
                    seen_refs.add(ref_key)
            recovered = durable_recovered or correlation in recovered_correlations or all(item.get('transient') for item in matching)
            errors.append({
                'error_id': f'{correlation}:{kind}',
                'kind': kind,
                'recovery_status': 'recovered' if recovered else 'unrecovered',
                'summary': next((item.get('summary') for item in matching if item.get('summary')), f'{kind} automation failure'),
                'correlation_key': correlation,
                'player_message_id': player_message_id,
                'evidence_refs': evidence_refs,
            })

    errors.sort(key=lambda item: (item['correlation_key'], item['kind']))
    kind_counts = Counter(item['kind'] for item in errors)
    return {
        'error_count': len(errors),
        'error_counts_by_kind': dict(sorted(kind_counts.items())),
        'recovered_error_count': sum(1 for item in errors if item['recovery_status'] == 'recovered'),
        'unrecovered_error_count': sum(1 for item in errors if item['recovery_status'] == 'unrecovered'),
        'errors': errors,
    }


def calculate_run_incidents(run, event_rows=None, audit_rows=None, provider_rows=None):
    if event_rows is None or audit_rows is None or provider_rows is None:
        event_rows, audit_rows, provider_rows, _ = _run_context(run)

    audit_counts = Counter(audit.event_type for audit in audit_rows)
    incidents = []
    automation_errors = collect_automation_errors(run, event_rows, audit_rows)
    for error in automation_errors['errors']:
        recovered = error['recovery_status'] == 'recovered'
        incidents.append({
            'incident_type': f"post_turn_{error['kind']}_failure",
            'severity': 'warn' if recovered else 'fail',
            'summary': error['summary'],
            'count': 1,
            'error_kind': error['kind'],
            'recovery_status': error['recovery_status'],
            'evidence_refs': error['evidence_refs'],
            'error_id': error['error_id'],
        })
    dm_silence_count = audit_counts.get('dm_silence_chosen', 0)
    if dm_silence_count >= 2:
        incidents.append({
            'incident_type': 'dm_silence_loop',
            'severity': 'warn' if dm_silence_count < 4 else 'fail',
            'summary': f'DM chose silence {dm_silence_count} times.',
            'count': dm_silence_count,
        })

    dm_empty_count = audit_counts.get('dm_output_empty', 0)
    if dm_empty_count >= 2:
        incidents.append({
            'incident_type': 'dm_empty_loop',
            'severity': 'warn' if dm_empty_count < 4 else 'fail',
            'summary': f'DM returned empty output {dm_empty_count} times.',
            'count': dm_empty_count,
        })

    retry_metrics = collect_model_retry_metrics(audit_rows, provider_rows, run_status=run.status)
    retry_count = retry_metrics['total']
    if retry_count >= 3:
        incidents.append({
            'incident_type': 'retry_storm',
            'severity': 'warn' if retry_count < 6 else 'fail',
            'summary': f'Recorded {retry_count} model retries or repair re-invocations.',
            'count': retry_count,
            'retry_counts': retry_metrics['counts'],
        })

    failure_count = sum(1 for row in provider_rows if row.failure_class)
    if failure_count >= 2:
        incidents.append({
            'incident_type': 'provider_failure_storm',
            'severity': 'warn' if failure_count < 4 else 'fail',
            'summary': f'Provider failures occurred {failure_count} times.',
            'count': failure_count,
        })

    no_action_streak = 0
    max_no_action_streak = 0
    for row in event_rows:
        payload = row.payload_json or {}
        action = None
        if row.event_type == 'turn_result':
            action = (payload.get('action') or '').strip().lower()
        elif row.event_type == 'player_no_action':
            action = 'no_action'
        if action == 'no_action':
            no_action_streak += 1
            max_no_action_streak = max(max_no_action_streak, no_action_streak)
        elif action:
            no_action_streak = 0
    if max_no_action_streak >= 3:
        incidents.append({
            'incident_type': 'no_progress_loop',
            'severity': 'warn' if max_no_action_streak < 5 else 'fail',
            'summary': f'Observed a no-progress loop of {max_no_action_streak} consecutive no_action turns.',
            'count': max_no_action_streak,
        })

    return incidents


def _collect_run_metrics(run, event_rows=None, audit_rows=None, provider_rows=None, cycle_rows=None):
    if event_rows is None or audit_rows is None or provider_rows is None or cycle_rows is None:
        event_rows, audit_rows, provider_rows, cycle_rows = _run_context(run)

    audit_counts = Counter(audit.event_type for audit in audit_rows)
    automation_errors = collect_automation_errors(run, event_rows, audit_rows)
    turn_results = [event for event in event_rows if event.event_type == 'turn_result']
    completed_turns = [event for event in turn_results if (event.payload_json or {}).get('action') != 'no_action']
    if not completed_turns:
        player_decisions = [event for event in event_rows if event.event_type == 'player_decision']
        completed_turns = [
            event for event in player_decisions
            if (event.payload_json or {}).get('decision', {}).get('action') != 'no_action'
        ]
    latencies = [row.latency_ms for row in provider_rows if row.latency_ms is not None]
    retry_metrics = collect_model_retry_metrics(audit_rows, provider_rows, run_status=run.status)
    incidents = calculate_run_incidents(run, event_rows, audit_rows, provider_rows)
    audited_cycles = [cycle for cycle in cycle_rows if cycle.status == 'audited']
    template = current_scorecard_template_for_run(run)
    required_criterion_ids = {
        _normalize_identifier(item.get('id'))
        for item in _json_list(template.get('criteria'), [])
        if isinstance(item, dict) and item.get('id')
    }
    fully_scored_cycles = []
    for cycle in audited_cycles:
        submitted_ids = {
            _normalize_identifier(item.get('criterion_id') or item.get('id'))
            for item in _json_list((cycle.scorecard_json or {}).get('criteria'), [])
            if isinstance(item, dict)
        }
        assessments = _json_list((cycle.scorecard_json or {}).get('criteria'), [])
        has_only_assessed_results = all(
            isinstance(item, dict)
            and isinstance(item.get('status'), str)
            and item.get('status').strip().lower() in {'pass', 'warn', 'fail', 'not_applicable'}
            for item in assessments
        )
        if submitted_ids >= required_criterion_ids and has_only_assessed_results:
            fully_scored_cycles.append(cycle)

    return {
        'run_status': run.status,
        'error_count': automation_errors['error_count'],
        'error_counts_by_kind': automation_errors['error_counts_by_kind'],
        'recovered_error_count': automation_errors['recovered_error_count'],
        'unrecovered_error_count': automation_errors['unrecovered_error_count'],
        'automation_errors': automation_errors['errors'],
        'turn_count': len(completed_turns),
        'completed_turns': len(completed_turns),
        'dm_silence_count': audit_counts.get('dm_silence_chosen', 0),
        'dm_empty_count': audit_counts.get('dm_output_empty', 0),
        'model_retry_count': retry_metrics['total'],
        'model_retry_metrics': retry_metrics,
        'provider_failure_count': sum(1 for row in provider_rows if row.failure_class),
        'provider_call_count': len(provider_rows),
        'median_provider_latency_ms': median(latencies) if latencies else None,
        'incident_count': len(incidents),
        'warning_incident_count': sum(1 for incident in incidents if incident.get('severity') == 'warn'),
        'failing_incident_count': sum(1 for incident in incidents if incident.get('severity') == 'fail'),
        'audited_cycle_count': len(audited_cycles),
        'fully_scored_cycle_count': len(fully_scored_cycles),
    }


def _matches_condition(value, condition):
    if not isinstance(condition, dict):
        return False
    for operator, expected in condition.items():
        if operator == 'lt' and not (value < expected):
            return False
        if operator == 'lte' and not (value <= expected):
            return False
        if operator == 'gt' and not (value > expected):
            return False
        if operator == 'gte' and not (value >= expected):
            return False
        if operator == 'eq' and not (value == expected):
            return False
        if operator == 'neq' and not (value != expected):
            return False
        if operator == 'in' and not (value in (expected or [])):
            return False
        if operator == 'not_in' and not (value not in (expected or [])):
            return False
    return True


def _evaluate_check(metric_value, check):
    if metric_value is None:
        return 'not_assessed'
    kind = check.get('kind') or 'number'
    if kind == 'enum':
        if metric_value in (check.get('fail_values') or []):
            return 'fail'
        if metric_value in (check.get('warn_values') or []):
            return 'warn'
        if not check.get('pass_values') or metric_value in (check.get('pass_values') or []):
            return 'pass'
        return 'warn'
    if check.get('fail_if') and _matches_condition(metric_value, check.get('fail_if')):
        return 'fail'
    if check.get('warn_if') and _matches_condition(metric_value, check.get('warn_if')):
        return 'warn'
    if check.get('pass_if'):
        return 'pass' if _matches_condition(metric_value, check.get('pass_if')) else 'warn'
    return 'pass'


def baseline_run_for_scenario(scenario):
    if not scenario or not scenario.baseline_run_id:
        return None
    return db.session.get(AutomationRun, scenario.baseline_run_id)


def compare_check_values(left, right, better_direction):
    if left is None or right is None:
        return 'unchanged'
    if better_direction == 'lower':
        if right < left:
            return 'better'
        if right > left:
            return 'worse'
        return 'unchanged'
    if better_direction == 'higher':
        if right > left:
            return 'better'
        if right < left:
            return 'worse'
        return 'unchanged'
    return 'unchanged'


def custom_scorecard_results(run, cycle_rows):
    template = current_scorecard_template_for_run(run)
    criteria = [item for item in _json_list(template.get('criteria'), []) if isinstance(item, dict) and item.get('id')]
    if not criteria:
        return []

    by_criterion = {criterion['id']: [] for criterion in criteria}
    eligible_cycles = [cycle for cycle in cycle_rows if cycle.status != 'skipped']
    for cycle in eligible_cycles:
        cycle_results = {}
        for result in _json_list((cycle.scorecard_json or {}).get('criteria'), []):
            if not isinstance(result, dict):
                continue
            criterion_id = _normalize_identifier(result.get('criterion_id') or result.get('id'))
            if criterion_id in by_criterion:
                applicability = _json_object(result.get('applicability'), {})
                applicable = _parse_strict_bool(
                    applicability.get('applicable'),
                    True,
                )
                normalized_status = _normalize_custom_scorecard_status(result.get('status'))
                if normalized_status == 'not_applicable' or not applicable:
                    normalized_status = 'not_applicable'
                    applicable = False
                normalized_result = {
                    'cycle_number': cycle.cycle_number,
                    'phase': cycle.phase,
                    'cycle_status': cycle.status,
                    'status': normalized_status,
                    'summary': result.get('summary'),
                    'evidence': result.get('evidence'),
                    'evidence_refs': _json_list(result.get('evidence_refs'), []),
                    'applicability': {
                        **applicability,
                        'applicable': applicable,
                    },
                    'source': 'recorded',
                }
                existing = cycle_results.get(criterion_id)
                if existing is None:
                    cycle_results[criterion_id] = normalized_result
                else:
                    duplicate_statuses = [
                        existing.get('status'),
                        normalized_result.get('status'),
                    ]
                    applicable_statuses = [
                        status
                        for status in duplicate_statuses
                        if status != 'not_applicable'
                    ]
                    if applicable_statuses:
                        existing['status'] = _aggregate_custom_scorecard_statuses(
                            applicable_statuses,
                            default='not_assessed',
                        )
                        existing['applicability']['applicable'] = True
                    else:
                        existing['status'] = 'not_applicable'
                        existing['applicability']['applicable'] = False
                    existing['duplicate_result_count'] = (
                        _safe_int(existing.get('duplicate_result_count')) + 1
                    )
        for criterion in criteria:
            criterion_id = criterion['id']
            assessment = cycle_results.get(criterion_id)
            if assessment is None:
                assessment = {
                    'cycle_number': cycle.cycle_number,
                    'phase': cycle.phase,
                    'cycle_status': cycle.status,
                    'status': 'not_assessed',
                    'summary': 'No assessment recorded for this criterion in this cycle.',
                    'evidence': None,
                    'evidence_refs': [],
                    'applicability': {'applicable': True},
                    'source': 'missing',
                }
            by_criterion[criterion_id].append(assessment)

    rows = []
    for criterion in criteria:
        criterion_id = criterion['id']
        assessments = by_criterion.get(criterion_id) or []
        weight = max(1, _safe_int(criterion.get('weight'), 2))

        if not assessments:
            status = 'not_assessed'
            summary = 'No custom audit feedback recorded for this criterion.'
            counts = {'pass': 0, 'warn': 0, 'fail': 0, 'not_assessed': 0, 'not_applicable': 0}
            exercised_count = 0
            not_assessed_applicable_count = 0
            na_count = 0
            missing_assessment_count = 0
        else:
            applicable_assessments = [
                item for item in assessments
                if item.get('status') != 'not_applicable' and item.get('applicability', {}).get('applicable', True)
            ]

            counts = Counter(item['status'] for item in assessments if item.get('status') in CUSTOM_SCORECARD_STATUS_ORDER)
            na_count = sum(
                1 for item in assessments
                if item.get('status') == 'not_applicable' or (item.get('status') == 'not_assessed' and not item.get('applicability', {}).get('applicable', True))
            )
            not_assessed_applicable_count = sum(1 for item in assessments if item.get('status') == 'not_assessed' and item.get('applicability', {}).get('applicable', True))
            missing_assessment_count = sum(
                1 for item in assessments
                if item.get('source') == 'missing'
            )
            exercised_count = counts.get('pass', 0) + counts.get('warn', 0) + counts.get('fail', 0)

            exercised_statuses = [
                item.get('status')
                for item in applicable_assessments
                if item.get('status') in SCORECARD_STATUS_VALUES
            ]
            if exercised_statuses:
                status = _aggregate_custom_scorecard_statuses(exercised_statuses, default='warn')
            elif not_assessed_applicable_count:
                status = 'not_assessed'
            else:
                status = 'not_applicable'

            summary = (
                f'{counts.get("pass", 0)} pass, '
                f'{counts.get("warn", 0)} warn, '
                f'{counts.get("fail", 0)} fail, '
                f'{not_assessed_applicable_count} not_assessed, '
                f'{na_count} not_applicable across {len(eligible_cycles)} audit cycle(s); '
                f'exercised in {exercised_count} cycle(s).'
            )

        scoring = _scoring_summary(
            [item.get('status') for item in assessments],
            weight=weight,
        )
        rows.append({
            'check_id': f'custom:{criterion_id}',
            'status': status,
            'summary': summary,
            'details': {
                'metric': f'custom:{criterion_id}',
                'metric_value': scoring['performance_score'],
                'weight': weight,
                'thresholds': {},
                'better_direction': criterion.get('better_direction') or 'higher',
                'criterion': criterion,
                'counts': dict(counts),
                'aggregate_status': status,
                'severity': status,
                'exercised_cycle_count': exercised_count,
                'not_assessed_cycle_count': not_assessed_applicable_count,
                'not_applicable_cycle_count': na_count,
                'missing_assessment_count': missing_assessment_count,
                'eligible_cycle_count': len(eligible_cycles),
                'assessments': assessments,
                'template_name': template.get('name'),
                **scoring,
            },
        })
    return rows


def baseline_comparison_for_run(run, results):
    scenario = run.scenario
    baseline_run = baseline_run_for_scenario(scenario)
    if baseline_run is None or baseline_run.id == run.id:
        return {}

    refresh_run_scorecard(baseline_run)
    baseline_results = {
        item.check_id: item
        for item in AutomationRunAuditResult.query.filter_by(run_id=baseline_run.id).all()
    }
    comparisons = []
    better = worse = unchanged = 0
    for result in results:
        baseline_result = baseline_results.get(result.check_id)
        if baseline_result is None:
            continue
        current_value = (result.details_json or {}).get('metric_value')
        baseline_value = (baseline_result.details_json or {}).get('metric_value')
        direction = (result.details_json or {}).get('better_direction')
        baseline_performance = (baseline_result.details_json or {}).get('performance_score')
        current_performance = (result.details_json or {}).get('performance_score')
        performance_relationship = compare_check_values(
            baseline_performance,
            current_performance,
            'higher',
        )
        severity_relationship = 'unchanged'
        if STATUS_RANK.get(result.status, 0) > STATUS_RANK.get(baseline_result.status, 0):
            severity_relationship = 'better'
        elif STATUS_RANK.get(result.status, 0) < STATUS_RANK.get(baseline_result.status, 0):
            severity_relationship = 'worse'
        relationship = severity_relationship
        if relationship == 'unchanged':
            relationship = performance_relationship
        if relationship == 'unchanged':
            relationship = compare_check_values(baseline_value, current_value, direction)
        if relationship == 'better':
            better += 1
        elif relationship == 'worse':
            worse += 1
        else:
            unchanged += 1
        comparisons.append({
            'check_id': result.check_id,
            'relationship': relationship,
            'baseline_status': baseline_result.status,
            'current_status': result.status,
            'baseline_severity': baseline_result.status,
            'current_severity': result.status,
            'severity_relationship': severity_relationship,
            'baseline_performance_score': baseline_performance,
            'current_performance_score': current_performance,
            'performance_relationship': performance_relationship,
            'baseline_value': baseline_value,
            'current_value': current_value,
            **({
                'baseline_retry_metrics': (baseline_result.details_json or {}).get('retry_metrics'),
                'current_retry_metrics': (result.details_json or {}).get('retry_metrics'),
            } if result.check_id == 'model_retry_count' else {}),
        })
    return {
        'baseline_run_id': baseline_run.id,
        'better': better,
        'worse': worse,
        'unchanged': unchanged,
        'comparisons': comparisons,
    }


def refresh_run_scorecard(run, *, commit=True):
    """Recompute and persist scorecard aggregates from authoritative source rows.

    ``commit=False`` flushes without committing so the recomputation can
    participate in a caller-owned transaction (e.g. audit acceptance). Callers
    that pass ``commit=False`` own the eventual commit/rollback.
    """
    event_rows, audit_rows, provider_rows, cycle_rows = _run_context(run)
    metrics = _collect_run_metrics(run, event_rows, audit_rows, provider_rows, cycle_rows)
    config = dict(DEFAULT_AUDIT_CONFIG)
    config.update(run.scenario.audit_config_json or {} if run.scenario else {})
    checks = config.get('checks') or DEFAULT_AUDIT_CONFIG['checks']

    results_payload = []
    for check in checks:
        metric_key = check.get('metric')
        metric_value = metrics.get(metric_key)
        metric_context = {}
        if metric_key == 'error_count':
            metric_context = {
                'error_counts_by_kind': metrics.get('error_counts_by_kind', {}),
                'recovered_error_count': metrics.get('recovered_error_count', 0),
                'unrecovered_error_count': metrics.get('unrecovered_error_count', 0),
                'automation_errors': metrics.get('automation_errors', []),
            }
        status = _evaluate_check(metric_value, check)
        weight = max(1, _safe_int(check.get('weight'), 1))
        scoring = _scoring_summary([status], weight=weight)
        check_category = get_criterion_category(check)
        summary_template = check.get('summary_template') or '{id}: {value}'
        results_payload.append({
            'check_id': check.get('id') or metric_key,
            'status': status,
            'summary': summary_template.format(id=check.get('id') or metric_key, metric=metric_key, value=metric_value),
            'details': {
                'metric': metric_key,
                'metric_value': metric_value,
                'weight': weight,
                'category': check_category,
                'thresholds': {
                    'pass_if': check.get('pass_if'),
                    'warn_if': check.get('warn_if'),
                    'fail_if': check.get('fail_if'),
                    'pass_values': check.get('pass_values'),
                    'warn_values': check.get('warn_values'),
                    'fail_values': check.get('fail_values'),
                },
                'better_direction': check.get('better_direction'),
                'severity': status,
                **({'retry_metrics': metrics.get('model_retry_metrics')} if metric_key == 'model_retry_count' else {}),
                **metric_context,
                **scoring,
            },
        })

    custom_results = custom_scorecard_results(run, cycle_rows)
    for row in custom_results:
        crit = row['details']['criterion']
        check_category = get_criterion_category(crit)
        row['details']['category'] = check_category
        
    results_payload.extend(custom_results)

    fail_count = sum(1 for check in results_payload if check['status'] == 'fail')
    warn_count = sum(1 for check in results_payload if check['status'] == 'warn')
    pass_count = sum(1 for check in results_payload if check['status'] == 'pass')
    not_assessed_count = sum(1 for check in results_payload if check['status'] == 'not_assessed')

    assessment_present = bool(metrics.get('completed_turns') or metrics.get('audited_cycle_count'))
    scoring = _combine_scoring_summaries(
        [
            check.get('details') or {}
            for check in results_payload
            if assessment_present or str(check.get('check_id') or '').startswith('custom:')
        ]
    )
    if not assessment_present:
        scoring['performance_score'] = None

    if not assessment_present:
        overall_status = 'not_assessed'
    else:
        if fail_count > 0:
            overall_status = 'fail'
        elif warn_count > 0:
            overall_status = 'warn'
        elif pass_count > 0:
            overall_status = 'pass'
        elif not_assessed_count > 0:
            overall_status = 'not_assessed'
        else:
            overall_status = 'not_applicable'

    CATEGORIES_TO_BREAKDOWN = [
        *CANONICAL_CATEGORY_NAMES,
        UNCATEGORIZED_CATEGORY,
    ]
    category_breakdown = {}
    for cat in CATEGORIES_TO_BREAKDOWN:
        cat_checks = [c for c in results_payload if c['details'].get('category') == cat]
        if not cat_checks:
            category_breakdown[cat] = {
                'status': 'not_applicable',
                'severity': 'not_applicable',
                'score': None,
                **_combine_scoring_summaries([]),
            }
        else:
            category_scoring_checks = [
                c
                for c in cat_checks
                if assessment_present or str(c.get('check_id') or '').startswith('custom:')
            ]
            scored_statuses = [
                c['status']
                for c in category_scoring_checks
                if c['status'] in SCORECARD_STATUS_VALUES
            ]
            if scored_statuses:
                cat_status = _aggregate_custom_scorecard_statuses(
                    scored_statuses,
                    default='not_assessed',
                )
            elif any(c['status'] == 'not_assessed' for c in category_scoring_checks):
                cat_status = 'not_assessed'
            elif category_scoring_checks:
                cat_status = 'not_applicable'
            else:
                cat_status = 'not_assessed'
            cat_scoring = _combine_scoring_summaries(
                [c.get('details') or {} for c in category_scoring_checks]
            )
            category_breakdown[cat] = {
                'status': cat_status,
                'severity': cat_status,
                'score': cat_scoring['performance_score'],
                **cat_scoring,
            }

    AutomationRunAuditResult.query.filter_by(run_id=run.id).delete()
    created_results = []
    for check in results_payload:
        row = AutomationRunAuditResult(
            run_id=run.id,
            check_id=check['check_id'],
            status=check['status'],
            summary=check['summary'],
            details_json=check['details'],
        )
        db.session.add(row)
        created_results.append(row)

    db.session.flush()
    baseline_summary = baseline_comparison_for_run(run, created_results)
    run.baseline_comparison_json = baseline_summary
    run.scorecard_summary_json = {
        'check_count': len(results_payload),
        'failing_checks': fail_count,
        'warning_checks': warn_count,
        'completed_turns': metrics.get('completed_turns', 0),
        'error_count': metrics.get('error_count', 0),
        'error_counts_by_kind': metrics.get('error_counts_by_kind', {}),
        'recovered_error_count': metrics.get('recovered_error_count', 0),
        'unrecovered_error_count': metrics.get('unrecovered_error_count', 0),
        'automation_errors': metrics.get('automation_errors', []),
        'retry_metrics': metrics.get('model_retry_metrics'),
        'audited_cycle_count': metrics.get('audited_cycle_count', 0),
        'scoring_model': SCORECARD_SCORING_MODEL,
        'severity': overall_status,
        'fully_scored_cycle_count': metrics.get('fully_scored_cycle_count', 0),
        'overall_status': overall_status,
        'weighted_score': scoring['performance_score'],
        **scoring,
        'incidents': calculate_run_incidents(run, event_rows, audit_rows, provider_rows),
        'custom_scorecard_name': current_scorecard_template_for_run(run).get('name'),
        'scorecard_configuration': scorecard_configuration(current_scorecard_template_for_run(run)),
        'category_breakdown': category_breakdown,
    }
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return results_payload


def _record_scorecard_refresh_failure(run, exc, *, correlation_id=None):
    """Durably record a structured, diagnosable scorecard refresh failure.

    The failure is recorded on the run (``scorecard_summary_json``) so
    control-plane reads can surface structured diagnostics without re-running
    the failing recomputation. The pre-existing (possibly stale) aggregate rows
    are preserved so an active run stays observable while refresh is unhealthy.
    """
    correlation_id = correlation_id or f'scorecard_refresh_{run.id}_{utcnow().isoformat()}'
    summary = dict(run.scorecard_summary_json or {})
    summary['scorecard_refresh'] = {
        'status': 'failed',
        'error_class': type(exc).__name__,
        'message': str(exc),
        'correlation_id': correlation_id,
        'recorded_at': utcnow().isoformat(),
    }
    run.scorecard_summary_json = summary
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
    try:
        append_run_event(
            run,
            'run_scorecard_refresh_failed',
            {
                'error_class': type(exc).__name__,
                'message': str(exc),
                'correlation_id': correlation_id,
                'stale_scorecard_preserved': True,
            },
            dedupe_key=f'run_scorecard_refresh_failed:{run.id}:{correlation_id}',
            skip_workspace=True,
        )
    except Exception:
        # The run-level diagnostic above is the durable source of truth; a
        # control-plane event append must never mask the underlying failure.
        pass
    return correlation_id


def scorecard_refresh_diagnostic(run):
    """Return the stored scorecard refresh diagnostic, if any."""
    return (run.scorecard_summary_json or {}).get('scorecard_refresh')


def try_refresh_run_scorecard(run, *, commit=True):
    """Best-effort scorecard refresh for control-plane and evidence paths.

    A downstream scorecard defect must not make an active run unobservable or
    unresumable, and must not make evidence endpoints wholly unavailable. On
    failure the exception is recorded as a structured diagnostic, the stored
    (stale) aggregates are preserved, and ``(False, None, diagnostic)`` is
    returned. Returns ``(True, results_payload, None)`` on success.
    """
    try:
        results = refresh_run_scorecard(run, commit=commit)
    except Exception as exc:
        try:
            db.session.rollback()
        except Exception:
            pass
        diagnostic = {
            'status': 'failed',
            'error_class': type(exc).__name__,
            'message': str(exc),
            'correlation_id': _record_scorecard_refresh_failure(run, exc),
        }
        return False, None, diagnostic
    return True, results, None


def repair_run_scorecard(run, *, commit=True):
    """Rebuild stale aggregate scorecard state from authoritative audited cycles.

    This is the repair/retry path for runs whose aggregates drifted because a
    downstream scorecard refresh previously failed (Run 42-style). It recomputes
    from the durable source rows and returns the same contract as
    :func:`try_refresh_run_scorecard`, so a repeated failure is diagnosed
    structurally instead of surfacing as a generic 500.
    """
    return try_refresh_run_scorecard(run, commit=commit)


def latest_session_for_run(run):
    if not run.derived_campaign_id:
        return None
    latest_session = CampaignSession.query.filter_by(campaign_id=run.derived_campaign_id, is_active=True).first()
    if latest_session is None:
        latest_session = CampaignSession.query.filter_by(campaign_id=run.derived_campaign_id).order_by(CampaignSession.started_at.desc(), CampaignSession.id.desc()).first()
    return latest_session


def run_watch_payload(run, current_user=None):
    latest_session = latest_session_for_run(run)
    encounter_map = None
    messages = []
    pending_sheet_proposals = []
    if latest_session:
        messages = _session_messages(latest_session.id)
        pending_sheet_proposals = [
            proposal.to_dict()
            for proposal in SheetProposal.query.filter_by(session_id=latest_session.id, status='pending')
            .order_by(SheetProposal.created_at.asc(), SheetProposal.id.asc())
            .all()
        ]
    if run.derived_campaign_id:
        encounter_map = EncounterMap.query.filter_by(campaign_id=run.derived_campaign_id, is_archived=False).order_by(EncounterMap.created_at.desc(), EncounterMap.id.desc()).first()

    provider_calls = [
        row.to_dict()
        for row in AutomationRunProviderCall.query.filter_by(run_id=run.id)
        .order_by(AutomationRunProviderCall.created_at.asc(), AutomationRunProviderCall.id.asc())
        .all()
    ]
    audit_cycles = [
        cycle.to_dict()
        for cycle in AutomationRunAuditCycle.query.filter_by(run_id=run.id)
        .order_by(AutomationRunAuditCycle.cycle_number.asc(), AutomationRunAuditCycle.id.asc())
        .all()
    ]
    auditor_jobs = [
        job.to_dict()
        for job in AutomationRunAuditorJob.query.filter_by(run_id=run.id)
        .order_by(AutomationRunAuditorJob.cycle_id.asc(), AutomationRunAuditorJob.auditor_slot.asc(), AutomationRunAuditorJob.id.asc())
        .all()
    ]
    current_audit_cycle = next((cycle for cycle in audit_cycles if cycle['id'] == run.awaiting_audit_cycle_id), None)

    pending_memory_recovery = []
    if run.derived_campaign_id:
        from models import SessionMemoryRecoveryTask

        pending_memory_recovery = [
            task.to_dict()
            for task in SessionMemoryRecoveryTask.query
            .filter_by(campaign_id=run.derived_campaign_id, status='pending')
            .order_by(SessionMemoryRecoveryTask.id.asc())
            .all()
        ]

    return redact_secrets({
        'run': run.to_dict(),
        'pending_memory_recovery': pending_memory_recovery,
        'scenario': run.scenario.to_dict() if run.scenario else None,
        'snapshot': run.snapshot.to_dict() if run.snapshot else None,
        'viewer_permissions': {
            'manage_run': True,
            'stop_run': True,
            'submit_audit': True,
            'continue_run': True,
        },
        'scorecard_template': current_scorecard_template_for_run(run),
        'current_audit_cycle': current_audit_cycle,
        'audit_cycles': audit_cycles,
        'auditor_jobs': auditor_jobs,
        'latest_session': {
            **latest_session.to_dict(),
            'running_summary': latest_session.running_summary,
            'messages': messages,
            'pending_sheet_proposals': pending_sheet_proposals,
        } if latest_session else None,
        'encounter_map': encounter_map.to_dict(include_private=True) if encounter_map else None,
        'events': [
            event.to_dict()
            for event in AutomationRunEvent.query.filter_by(run_id=run.id)
            .order_by(AutomationRunEvent.sequence_number.asc(), AutomationRunEvent.id.asc())
            .all()
        ],
        'scorecard': [
            result.to_dict()
            for result in AutomationRunAuditResult.query.filter_by(run_id=run.id)
            .order_by(AutomationRunAuditResult.check_id.asc())
            .all()
        ],
        'incidents': run.scorecard_summary_json.get('incidents') or [],
        'retry_metrics': run.scorecard_summary_json.get('retry_metrics') or {},
        'scorecard_refresh': scorecard_refresh_diagnostic(run),
        'provider_calls': provider_calls,
        'baseline_comparison': run.baseline_comparison_json or {},
    })


def workspace_trends_for_user(user_id=None):
    query = AutomationScenario.query
    if user_id is not None:
        query = query.filter_by(user_id=user_id)
    scenarios = query.all()
    trends = []
    for scenario in scenarios:
        runs = AutomationRun.query.filter_by(scenario_id=scenario.id).order_by(AutomationRun.created_at.asc(), AutomationRun.id.asc()).all()
        completed_like = [run for run in runs if run.status in {'completed', 'failed', 'stopped'}]
        if not completed_like:
            trends.append({
                'scenario_id': scenario.id,
                'scenario_name': scenario.name,
                'run_count': len(runs),
                'failure_rate': 0.0,
                'median_turns': 0,
                'retry_count': 0,
                'silence_incidents': 0,
                'score_movement': 0,
            })
            continue
        failure_rate = round(sum(1 for run in completed_like if run.status != 'completed') / len(completed_like), 4)
        median_turns = median([_safe_int((run.scorecard_summary_json or {}).get('completed_turns')) for run in completed_like]) if completed_like else 0
        retry_count = sum(
            _safe_int((run.scorecard_summary_json or {}).get('warning_checks'))
            for run in completed_like
        )
        silence_incidents = sum(
            1
            for run in completed_like
            for incident in ((run.scorecard_summary_json or {}).get('incidents') or [])
            if incident.get('incident_type') == 'dm_silence_loop'
        )
        scores = [
            _safe_int((
                (run.scorecard_summary_json or {}).get(
                    'performance_score',
                    (run.scorecard_summary_json or {}).get('weighted_score'),
                )
                or 0
            ) * 1000)
            for run in completed_like
        ]
        trends.append({
            'scenario_id': scenario.id,
            'scenario_name': scenario.name,
            'run_count': len(runs),
            'failure_rate': failure_rate,
            'median_turns': median_turns,
            'retry_count': retry_count,
            'silence_incidents': silence_incidents,
            'score_movement': (scores[-1] - scores[0]) / 1000 if len(scores) >= 2 else 0,
        })
    return trends


def cleanup_hidden_clone_campaigns(scenario, *, action=None, older_than_days=None, keep_recent_runs=None):
    policy = retention_policy_for_scenario(scenario)
    action = (action or policy.get('cleanup_action') or 'archive').strip().lower()
    older_than_days = _safe_int(older_than_days, policy.get('retention_days'))
    keep_recent_runs = _safe_int(keep_recent_runs, policy.get('keep_recent_runs'))

    runs = AutomationRun.query.filter(
        AutomationRun.scenario_id == scenario.id,
        AutomationRun.derived_campaign_id.isnot(None),
        AutomationRun.status.in_(('completed', 'failed', 'stopped')),
    ).order_by(AutomationRun.created_at.desc(), AutomationRun.id.desc()).all()

    now = _utcnow()
    cutoff = now - timedelta(days=max(0, older_than_days))
    targets = []
    for index, run in enumerate(runs):
        if index < keep_recent_runs:
            continue
        if run.finished_at and run.finished_at > cutoff:
            continue
        targets.append(run)

    archived = []
    deleted = []
    for run in targets:
        campaign = db.session.get(Campaign, run.derived_campaign_id) if run.derived_campaign_id else None
        if action == 'delete' and campaign is not None:
            from services.campaign_cleanup import delete_campaign_graph
            campaign_id = campaign.id
            run.derived_campaign_id = None
            run.clone_retention_status = 'deleted'
            delete_campaign_graph([campaign_id], character_policy='delete')
            deleted.append(run.id)
        else:
            if campaign is not None:
                campaign.status = 'automation_archived'
            run.clone_retention_status = 'archived'
            archived.append(run.id)
        run.updated_at = now
    db.session.commit()
    return {'archived_run_ids': archived, 'deleted_run_ids': deleted}


def merged_runner_config_for_scenario(scenario, override=None):
    config = dict(scenario.runner_config_json or {})
    config.update(override or {})
    template = scenario.scorecard_template
    if template is not None:
        defaults = _json_object(template.defaults_json, {})
        if config.get('audit_pause_phases') is None and isinstance(defaults.get('pause_phases'), list):
            config['audit_pause_phases'] = defaults.get('pause_phases') or []
    return config


def create_matrix_runs(scenario, snapshot, current_user, matrix_entries):
    group_id = f'matrix-{secrets.token_hex(6)}'
    runs = []
    for index, entry in enumerate(matrix_entries):
        merged_runner_config = merged_runner_config_for_scenario(scenario, entry.get('runner_config') or {})
        run = AutomationRun(
            scenario_id=scenario.id,
            snapshot_id=snapshot.id,
            user_id=current_user.id,
            status='queued',
            matrix_group_id=group_id,
            matrix_label=entry.get('label') or f'Variant {index + 1}',
            scorecard_template_json=scorecard_template_snapshot(scenario.scorecard_template),
            runner_config_json=merged_runner_config,
        )
        db.session.add(run)
        db.session.flush()
        append_run_event(run, 'run_queued', {'status': run.status, 'matrix_label': run.matrix_label}, dedupe_key=f'run_queued:{run.id}', commit=False, skip_workspace=True)
        runs.append(run)
    db.session.commit()
    for run in runs:
        append_workspace_event(
            current_user.id,
            'run_created',
            {'type': 'run_created', 'run': run.to_dict(), 'scenario_id': scenario.id},
            resource_type='run',
            resource_id=run.id,
        )
    return group_id, runs


def _json_diff(left, right, path=''):
    diffs = []
    if type(left) != type(right):
        return [{'path': path or '$', 'left': left, 'right': right}]
    if isinstance(left, dict):
        for key in sorted(set(left) | set(right)):
            child_path = f'{path}.{key}' if path else key
            if key not in left or key not in right:
                diffs.append({'path': child_path, 'left': left.get(key), 'right': right.get(key)})
                continue
            diffs.extend(_json_diff(left.get(key), right.get(key), child_path))
        return diffs
    if isinstance(left, list):
        max_len = max(len(left), len(right))
        for index in range(max_len):
            child_path = f'{path}[{index}]' if path else f'[{index}]'
            if index >= len(left) or index >= len(right):
                diffs.append({'path': child_path, 'left': left[index] if index < len(left) else None, 'right': right[index] if index < len(right) else None})
                continue
            diffs.extend(_json_diff(left[index], right[index], child_path))
        return diffs
    if left != right:
        return [{'path': path or '$', 'left': left, 'right': right}]
    return diffs


def compare_runs_payload(left_run, right_run):
    refresh_run_scorecard(left_run)
    refresh_run_scorecard(right_run)

    left_results = {result.check_id: result for result in AutomationRunAuditResult.query.filter_by(run_id=left_run.id).all()}
    right_results = {result.check_id: result for result in AutomationRunAuditResult.query.filter_by(run_id=right_run.id).all()}
    all_check_ids = sorted(set(left_results) | set(right_results))
    scorecard_comparisons = []
    for check_id in all_check_ids:
        left = left_results.get(check_id)
        right = right_results.get(check_id)
        scorecard_comparisons.append({
            'check_id': check_id,
            'left': left.to_dict() if left else None,
            'right': right.to_dict() if right else None,
        })

    left_session = latest_session_for_run(left_run)
    right_session = latest_session_for_run(right_run)
    left_messages = _session_messages(left_session.id) if left_session else []
    right_messages = _session_messages(right_session.id) if right_session else []
    transcript_diff = _json_diff(
        [{'role': item.get('role'), 'content': item.get('content')} for item in left_messages],
        [{'role': item.get('role'), 'content': item.get('content')} for item in right_messages],
    )[:50]

    left_audit = [event.to_dict() for event in CampaignAuditEvent.query.filter_by(campaign_id=left_run.derived_campaign_id).order_by(CampaignAuditEvent.created_at.asc(), CampaignAuditEvent.id.asc()).all()] if left_run.derived_campaign_id else []
    right_audit = [event.to_dict() for event in CampaignAuditEvent.query.filter_by(campaign_id=right_run.derived_campaign_id).order_by(CampaignAuditEvent.created_at.asc(), CampaignAuditEvent.id.asc()).all()] if right_run.derived_campaign_id else []
    audit_event_counts = {
        'left': Counter(item['event_type'] for item in left_audit),
        'right': Counter(item['event_type'] for item in right_audit),
    }
    audit_event_diff = [
        {
            'event_type': event_type,
            'left_count': audit_event_counts['left'].get(event_type, 0),
            'right_count': audit_event_counts['right'].get(event_type, 0),
        }
        for event_type in sorted(set(audit_event_counts['left']) | set(audit_event_counts['right']))
    ]

    left_world = CampaignWorld.query.filter_by(campaign_id=left_run.derived_campaign_id).first() if left_run.derived_campaign_id else None
    right_world = CampaignWorld.query.filter_by(campaign_id=right_run.derived_campaign_id).first() if right_run.derived_campaign_id else None
    world_state_diff = _json_diff(
        _loads_text(left_world.world_state, {}) if left_world else {},
        _loads_text(right_world.world_state, {}) if right_world else {},
    )[:50]

    def _clock_payload(run):
        if not run.derived_campaign_id:
            return []
        return [
            clock.to_dict(include_private=True)
            for clock in CampaignClock.query.filter_by(campaign_id=run.derived_campaign_id)
            .order_by(CampaignClock.clock_id.asc(), CampaignClock.id.asc())
            .all()
            if clock.to_dict(include_private=True) is not None
        ]

    clock_diff = _json_diff(_clock_payload(left_run), _clock_payload(right_run))[:50]
    left_trace = [
        event.to_dict()
        for event in AutomationRunEvent.query.filter(
            AutomationRunEvent.run_id == left_run.id,
            AutomationRunEvent.event_type.in_(('overseer_decision', 'player_decision', 'turn_result', 'dm_turn_status', 'error')),
        ).order_by(AutomationRunEvent.sequence_number.asc(), AutomationRunEvent.id.asc()).all()
    ]
    right_trace = [
        event.to_dict()
        for event in AutomationRunEvent.query.filter(
            AutomationRunEvent.run_id == right_run.id,
            AutomationRunEvent.event_type.in_(('overseer_decision', 'player_decision', 'turn_result', 'dm_turn_status', 'error')),
        ).order_by(AutomationRunEvent.sequence_number.asc(), AutomationRunEvent.id.asc()).all()
    ]
    decision_trace_diff = _json_diff(left_trace, right_trace)[:50]

    return {
        'left_run': left_run.to_dict(),
        'right_run': right_run.to_dict(),
        'comparisons': scorecard_comparisons,
        'transcript_diff': transcript_diff,
        'audit_event_diff': audit_event_diff,
        'world_state_diff': world_state_diff,
        'clock_diff': clock_diff,
        'decision_trace_diff': decision_trace_diff,
    }


def find_provider_calls_for_turn(run_id, trace_id):
    if not trace_id:
        return []
    provider_calls = AutomationRunProviderCall.query.filter_by(run_id=run_id).all()
    matching = []
    for pc in provider_calls:
        if trace_id in str(pc.dedupe_key):
            matching.append(pc)
            continue
        req = pc.request_json or {}
        if isinstance(req, dict):
            if str(trace_id) in str(req):
                matching.append(pc)
                continue
    return matching


def run_debug_summary(run_id):
    run = db.session.get(AutomationRun, run_id)
    if not run:
        return {'error': 'Run not found'}
        
    worker = None
    if run.worker_id:
        worker = AutomationWorker.query.filter_by(worker_id=run.worker_id).first()
        
    recent_workers = AutomationWorker.query.all()
    has_recent_poll = False
    now = utcnow()
    for wk in recent_workers:
        if wk.last_poll_at and (now - wk.last_poll_at).total_seconds() < 300:
            has_recent_poll = True
            break
            
    stuck_reasons = []
    
    if run.status == 'queued' and not has_recent_poll:
        stuck_reasons.append('queued_no_recent_worker_poll')
        
    if run.claim_failure_reason:
        stuck_reasons.append('worker_claim_failure')

    reclaim_failure = None
    if run.reclaim_failure_fingerprint:
        reclaim_threshold = max_reclaim_failures_for_run(run)
        reclaim_failure = {
            'fingerprint': run.reclaim_failure_fingerprint,
            'count': run.reclaim_failure_count,
            'threshold': reclaim_threshold,
            'last_attempt': run.reclaim_failure_attempt,
            'stage': run.reclaim_failure_stage,
            'error': run.reclaim_failure_error,
            'last_at': run.reclaim_failure_at.isoformat() if run.reclaim_failure_at else None,
            'terminal': run.reclaim_failure_count >= reclaim_threshold,
        }
        if run.reclaim_failure_count >= reclaim_threshold:
            stuck_reasons.append('reclaim_failure_threshold_reached')
        else:
            stuck_reasons.append('worker_infrastructure_failure')
        
    if run.status == 'claimed' and run.lease_expires_at and now >= run.lease_expires_at:
        stuck_reasons.append('claimed_lease_expired')
        
    if run.status == 'running':
        if not run.heartbeat_at or (now - run.heartbeat_at).total_seconds() > 120:
            stuck_reasons.append('running_heartbeat_stale')
            
    if run.status == 'awaiting_audit' and run.awaiting_audit_cycle_id:
        cycle = db.session.get(AutomationRunAuditCycle, run.awaiting_audit_cycle_id)
        if cycle:
            if cycle.status == 'pending':
                stuck_reasons.append('awaiting_audit_pending_cycle')
            elif cycle.status == 'audited':
                stuck_reasons.append('awaiting_audit_audited_but_not_continued')
                
    if run.status == 'stop_requested' and run.stop_requested_at:
        if (now - run.stop_requested_at).total_seconds() > 60:
            stuck_reasons.append('stop_requested_not_acknowledged')
            
    last_turn = SessionDmTurn.query.filter_by(campaign_id=run.derived_campaign_id).order_by(SessionDmTurn.id.desc()).first() if run.derived_campaign_id else None
    if last_turn and last_turn.status != 'completed':
        pcs = []
        if last_turn.trace_id:
            pcs = find_provider_calls_for_turn(run.id, last_turn.trace_id)
        if not pcs and last_turn.status == 'pending':
            stuck_reasons.append('provider_calls_missing_for_dm_turn')
        if last_turn.memory_status == 'pending':
            stuck_reasons.append('post_turn_memory_pending')
        if last_turn.clock_status == 'pending':
            stuck_reasons.append('post_turn_clock_pending')
            
    lease_summary = {
        'has_lease_token': bool(run.lease_token),
        'worker_id': run.worker_id,
        'heartbeat_age_seconds': int((now - run.heartbeat_at).total_seconds()) if run.heartbeat_at else None,
        'lease_expires_at': run.lease_expires_at.isoformat() if run.lease_expires_at else None,
        'lease_expired': bool(run.lease_expires_at and now >= run.lease_expires_at)
    }
    
    last_event = None
    last_event_row = AutomationRunEvent.query.filter_by(run_id=run.id).order_by(AutomationRunEvent.id.desc()).first()
    if last_event_row:
        last_event = {
            'id': last_event_row.id,
            'type': last_event_row.event_type,
            'created_at': last_event_row.created_at.isoformat() if last_event_row.created_at else None
        }
        
    return {
        'run_id': run.id,
        'status': run.status,
        'last_event': last_event,
        'lease': lease_summary,
        'reclaim_failure': reclaim_failure,
        'worker': {
            'id': worker.worker_id if worker else None,
            'last_heartbeat_at': worker.last_heartbeat_at.isoformat() if worker and worker.last_heartbeat_at else None,
            'last_poll_at': worker.last_poll_at.isoformat() if worker and worker.last_poll_at else None,
        } if worker else None,
        'queue': {
            'position': AutomationRun.query.filter(AutomationRun.status == 'queued', AutomationRun.id < run.id).count() if run.status == 'queued' else None,
            'queued_run_count': AutomationRun.query.filter_by(status='queued').count()
        },
        'audit': {
            'configured_pause_phases': (run.runner_config_json or {}).get('audit_pause_phases') or (run.runner_config_json or {}).get('pause_phases') or [],
            'current_cycle_id': run.awaiting_audit_cycle_id,
            'last_audited_cycle_id': AutomationRunAuditCycle.query.filter_by(run_id=run.id, status='audited').order_by(AutomationRunAuditCycle.id.desc()).first().id if AutomationRunAuditCycle.query.filter_by(run_id=run.id, status='audited').first() else None,
            'next_expected_pause': run.awaiting_audit_phase
        },
        'stuck_reasons': stuck_reasons,
        'next_expected_action': 'worker_should_continue_run' if run.status == 'running' and not stuck_reasons else ('awaiting_manual_audit' if run.status == 'awaiting_audit' else 'queued_waiting_for_worker')
    }

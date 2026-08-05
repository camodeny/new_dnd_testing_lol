import json
from collections import Counter
from uuid import uuid4

from utils.redaction import redact_secrets, is_sensitive_key
from time_utils import utcnow
from models import (
    AutomationRun,
    AutomationRunAuditCycle,
    AutomationRunAuditorJob,
    AutomationRunEvent,
    AutomationRunProviderCall,
    AutomationSnapshot,
    CampaignAuditEvent,
    CampaignClock,
    CampaignMemoryEmbedding,
    CampaignMemoryLog,
    CampaignWorld,
    Character,
    NPCActor,
    SessionDmTurn,
    SessionMessage,
    WorldEvent,
    db,
)
from llm_providers import ProviderError, provider_registry
from openrouter import _post_chat_normalized, get_llm_model, get_llm_provider
from services.automation_service import (
    append_run_event,
    continue_audit_run,
    current_scorecard_template_for_run,
    find_provider_calls_for_turn,
    get_criterion_category,
    latest_session_for_run,
    persist_provider_call,
    refresh_run_scorecard,
    scorecard_configuration,
    submit_audit_cycle_feedback,
)
from services.character_service import character_full_dict


AUDITOR_PROMPT_VERSION = 'automation_auditor_tool_loop_v4'
AUDITOR_STATUS_RANK = {'pass': 2, 'warn': 1, 'fail': 0}
AUDITOR_VALID_STATUSES = set(AUDITOR_STATUS_RANK) | {'not_assessed', 'not_applicable'}
AUDITOR_TERMINAL_JOB_STATUSES = {'completed', 'failed', 'canceled'}
DEFAULT_AUDITOR_CONFIG = {
    'mode': 'manual',
    'model': None,
    'count': 1,
    'auto_continue': False,
    'target_cycles': None,
    'required_tools': 'runtime_truth_full',
}
SUPPORTED_AUDITOR_MODES = {'manual', 'built_in', 'external'}


AUDITOR_TOOL_DEFINITIONS = [
    {
        'type': 'function',
        'function': {
            'name': 'get_run_status',
            'description': 'Read compact automation run status, scenario metadata, current audit cycle metadata, auditor config, and compact auditor-job summaries. This is not a full trace dump.',
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_current_audit_bundle',
            'description': 'Read high-level audit table of contents/bundle for the current cycle: run info, cycle info, scorecard criteria, recommended detail paths, evidence gaps, retrieval summaries, and context exposure check.',
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_transcript',
            'description': 'Read stored session transcript messages for the derived automation campaign.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200, 'default': 80},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_cycle_evidence_packet',
            'description': 'Read a compact evidence packet for the current audit cycle: focused transcript window, scene state, clocks, compact audit traces, provider-call summaries, and run-event summaries. Prefer the smallest limits that still support the scorecard.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'transcript_limit': {'type': 'integer', 'minimum': 2, 'maximum': 40, 'default': 12},
                    'audit_event_limit': {'type': 'integer', 'minimum': 1, 'maximum': 40, 'default': 12},
                    'provider_call_limit': {'type': 'integer', 'minimum': 1, 'maximum': 20, 'default': 6},
                    'run_event_limit': {'type': 'integer', 'minimum': 1, 'maximum': 30, 'default': 8},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_audit_events',
            'description': 'Read compact campaign_audit_event summaries for the derived automation campaign. Use get_audit_event_detail for deep inspection of a specific event.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200, 'default': 40},
                    'event_type': {'type': 'string'},
                    'trace_id': {'type': 'string'},
                    'include_payload': {'type': 'boolean', 'default': False},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_world_state',
            'description': 'Read full persisted public intro, knowledge graph, world_state, and DM-private memory for the derived campaign. This is a broad raw-state tool; prefer the compact cycle evidence packet first.',
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_clocks',
            'description': 'Read all campaign clocks, including DM-private clocks and private trigger/on-complete fields. This is a broad raw-state tool; prefer the compact cycle evidence packet first.',
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_npcs',
            'description': 'Read NPC actor records, including private dossiers. This is a broad raw-state tool; prefer the compact cycle evidence packet first.',
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_characters',
            'description': 'Read character sheets for the derived automation campaign. This is a broad raw-state tool; prefer the compact cycle evidence packet first.',
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_provider_calls',
            'description': 'Read compact recorded automation provider-call summaries for this run. Use get_provider_call_detail for deep inspection of a specific provider call.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 12},
                    'include_artifacts': {'type': 'boolean', 'default': False},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_run_events',
            'description': 'Read compact structured automation run-event summaries for this run. Use get_run_event_detail for deep inspection of a specific run event.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200, 'default': 20},
                    'include_payload': {'type': 'boolean', 'default': False},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_audit_event_detail',
            'description': 'Read one audit event by id. Prefer exact payload paths for targeted fields. Full payload access requires an explicit include_full_payload escalation.',
            'parameters': {
                'type': 'object',
                'required': ['event_id'],
                'properties': {
                    'event_id': {'type': 'integer', 'minimum': 1},
                    'paths': {
                        'type': 'array',
                        'items': {'type': 'string'},
                        'description': 'Optional payload field paths such as payload.scene_patch or payload.facts[0].id.',
                    },
                    'include_full_payload': {
                        'type': 'boolean',
                        'default': False,
                        'description': 'Explicit escalation for the full audit-event payload when exact paths are not sufficient.',
                    },
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_provider_call_detail',
            'description': 'Read one provider call by id. By default this returns the full request/response artifacts; optionally request exact request, response, or parsed-output paths when you only need specific fields.',
            'parameters': {
                'type': 'object',
                'required': ['provider_call_id'],
                'properties': {
                    'provider_call_id': {'type': 'integer', 'minimum': 1},
                    'request_paths': {'type': 'array', 'items': {'type': 'string'}},
                    'response_paths': {'type': 'array', 'items': {'type': 'string'}},
                    'parsed_output_paths': {'type': 'array', 'items': {'type': 'string'}},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_run_event_detail',
            'description': 'Read one run event by id. By default this returns the full payload; optionally request exact payload paths when you only need specific fields.',
            'parameters': {
                'type': 'object',
                'required': ['event_id'],
                'properties': {
                    'event_id': {'type': 'integer', 'minimum': 1},
                    'paths': {
                        'type': 'array',
                        'items': {'type': 'string'},
                        'description': 'Optional payload field paths such as payload.decision.action.',
                    },
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'search_campaign_memory',
            'description': 'Keyword-search campaign memory across graph entities, relations, facts, NPC actors, clocks, world events, world state, and DM-private memory.',
            'parameters': {
                'type': 'object',
                'required': ['query'],
                'properties': {
                    'query': {'type': 'string'},
                    'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20, 'default': 8},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_snapshot_manifest',
            'description': 'Read the source automation snapshot metadata used to materialize this run. Prefer top-level sections or exact payload paths when auditing specific snapshot materialization questions.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'include_payload': {'type': 'boolean', 'default': False},
                    'sections': {
                        'type': 'array',
                        'items': {'type': 'string'},
                        'description': 'Optional top-level snapshot sections to return without the entire payload.',
                    },
                    'paths': {
                        'type': 'array',
                        'items': {'type': 'string'},
                        'description': 'Optional snapshot field paths such as campaign.world_state.current_scene or session.messages[0].content.',
                    },
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_scorecard_template',
            'description': 'Read the scorecard template and audit instructions for this run.',
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
]


def _utcnow():
    return utcnow()


def _json_loads(value, fallback):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _json_object(value, fallback=None):
    return value if isinstance(value, dict) else (fallback or {})


def _json_list(value, fallback=None):
    return value if isinstance(value, list) else (fallback or [])


def _safe_int(value, default=0, minimum=None, maximum=None):
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    if minimum is not None:
        number = max(minimum, number)
    if maximum is not None:
        number = min(maximum, number)
    return number


def resolve_auditor_provider_model(model_hint=None):
    raw = str(model_hint or '').strip()
    if '/' in raw:
        provider_hint, maybe_model = raw.split('/', 1)
        try:
            provider = provider_registry.get(provider_hint).name
        except RuntimeError:
            provider = None
        if provider:
            return provider, maybe_model.strip()
    return get_llm_provider(), raw or get_llm_model()


def normalize_auditor_config(raw):
    config = dict(DEFAULT_AUDITOR_CONFIG)
    incoming = raw if isinstance(raw, dict) else {}
    config.update({key: incoming.get(key) for key in DEFAULT_AUDITOR_CONFIG if key in incoming})
    mode = str(config.get('mode') or 'manual').strip().lower()
    config['mode'] = mode if mode in SUPPORTED_AUDITOR_MODES else 'manual'
    model = str(config.get('model') or '').strip()
    config['model'] = model or None
    config['count'] = _safe_int(config.get('count'), 1, minimum=1, maximum=8)
    config['auto_continue'] = bool(config.get('auto_continue'))
    target_cycles = config.get('target_cycles')
    config['target_cycles'] = _safe_int(target_cycles, 0, minimum=1, maximum=100) if target_cycles not in (None, '', 0) else None
    required_tools = str(config.get('required_tools') or 'runtime_truth_full').strip() or 'runtime_truth_full'
    config['required_tools'] = required_tools
    return config


def auditor_config_for_run(run):
    runner_config = _json_object(run.runner_config_json, {})
    return normalize_auditor_config(runner_config.get('auditor_config'))


def merge_auditor_config_into_runner_config(runner_config, auditor_config=None):
    config = dict(runner_config or {})
    if auditor_config is not None:
        config['auditor_config'] = normalize_auditor_config(auditor_config)
    elif 'auditor_config' in config:
        config['auditor_config'] = normalize_auditor_config(config.get('auditor_config'))
    return config


def list_auditor_jobs(run_id, cycle_id=None):
    query = AutomationRunAuditorJob.query.filter_by(run_id=run_id)
    if cycle_id is not None:
        query = query.filter_by(cycle_id=cycle_id)
    return query.order_by(
        AutomationRunAuditorJob.cycle_id.asc(),
        AutomationRunAuditorJob.auditor_slot.asc(),
        AutomationRunAuditorJob.id.asc(),
    ).all()


def ensure_auditor_jobs_for_cycle(run, cycle, config=None, *, rerun_failed=False):
    config = normalize_auditor_config(config or auditor_config_for_run(run))
    provider, model = resolve_auditor_provider_model(config.get('model'))
    jobs = []
    for slot in range(1, config['count'] + 1):
        existing = AutomationRunAuditorJob.query.filter_by(
            run_id=run.id,
            cycle_id=cycle.id,
            auditor_slot=slot,
        ).first()
        if existing is None:
            existing = AutomationRunAuditorJob(
                run_id=run.id,
                cycle_id=cycle.id,
                auditor_slot=slot,
                status='queued',
                provider=provider,
                model=model,
            )
            db.session.add(existing)
            db.session.flush()
        elif rerun_failed and existing.status in {'failed', 'canceled'}:
            existing.status = 'queued'
            existing.provider = provider
            existing.model = model
            existing.provider_call_id = None
            existing.tool_call_count = 0
            existing.submitted_scorecard_json = {}
            existing.tool_trace_json = []
            existing.error_text = None
            existing.started_at = None
            existing.finished_at = None
            existing.updated_at = _utcnow()
        jobs.append(existing)
    db.session.commit()
    return jobs


def _campaign_for_run(run):
    return run.derived_campaign or (run.scenario.source_campaign if run.scenario else None)


def _cycle_boundaries(cycle):
    if not cycle:
        return {
            "audit_event_id": None,
            "provider_call_id": None,
            "run_event_id": None,
            "message_id": None,
        }
    payload = cycle.payload_json or {}

    def get_boundary(key):
        if key not in payload:
            return None
        val = payload[key]
        if val is None:
            return 0
        try:
            return int(val)
        except (ValueError, TypeError):
            return 0

    return {
        "audit_event_id": get_boundary("boundary_audit_event_id"),
        "provider_call_id": get_boundary("boundary_provider_call_id"),
        "run_event_id": get_boundary("boundary_run_event_id"),
        "message_id": get_boundary("boundary_message_id"),
    }


def _latest_session_messages(run, limit, max_message_id=None):
    session = latest_session_for_run(run)
    if session is None:
        return []
    query = SessionMessage.query.filter_by(session_id=session.id)
    if max_message_id is not None:
        query = query.filter(SessionMessage.id <= max_message_id)
    rows = query.order_by(SessionMessage.id.desc()).limit(limit).all()
    return [row.to_dict() for row in reversed(rows)]


def _limit_arg(args, default, maximum):
    return _safe_int((args or {}).get('limit'), default, minimum=1, maximum=maximum)


def _world_payload(campaign):
    world = CampaignWorld.query.filter_by(campaign_id=campaign.id).first()
    if not world:
        return {'has_world': False}
    return {
        'has_world': True,
        'id': world.id,
        'campaign_id': world.campaign_id,
        'public_intro': _json_loads(world.public_intro, {}),
        'knowledge_graph': _json_loads(world.knowledge_graph, {}),
        'world_state': _json_loads(world.world_state, {}),
        'dm_private': _json_loads(world.dm_private, {}),
        'approved_at': world.approved_at.isoformat() if world.approved_at else None,
        'created_at': world.created_at.isoformat() if world.created_at else None,
        'updated_at': world.updated_at.isoformat() if world.updated_at else None,
    }


def _truncate_text(value, max_chars=320):
    text = str(value or '')
    if len(text) <= max_chars:
        return text
    return f'{text[:max_chars]}...'


def _payload_keys(value):
    if isinstance(value, dict):
        return sorted([k for k in value.keys() if not is_sensitive_key(k)])
    if isinstance(value, list):
        return [f'list[{len(value)}]']
    return []


def _redacted_key_count(value):
    if isinstance(value, dict):
        return sum(1 for k in value.keys() if is_sensitive_key(k))
    return 0



def _payload_preview(value, max_chars=320):
    try:
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        rendered = str(value)
    return _truncate_text(rendered, max_chars=max_chars)


def _path_args(args, key):
    raw = (args or {}).get(key)
    if not isinstance(raw, list):
        return []
    values = []
    for item in raw:
        clean = str(item or '').strip()
        if clean:
            values.append(clean)
    return values


def _parse_selector_path(path):
    clean = str(path or '').strip()
    if not clean:
        return []
    segments = []
    token = ''
    index = 0
    while index < len(clean):
        char = clean[index]
        if char == '.':
            if token:
                segments.append(token)
                token = ''
            index += 1
            continue
        if char == '[':
            if token:
                segments.append(token)
                token = ''
            end = clean.find(']', index)
            if end == -1:
                return []
            raw_index = clean[index + 1:end].strip()
            if not raw_index.isdigit():
                return []
            segments.append(int(raw_index))
            index = end + 1
            continue
        token += char
        index += 1
    if token:
        segments.append(token)
    return segments


def _value_at_selector_path(value, path):
    current = value
    segments = _parse_selector_path(path)
    if not segments:
        return False, None
    for segment in segments:
        if isinstance(segment, int):
            if not isinstance(current, list) or segment >= len(current):
                return False, None
            current = current[segment]
            continue
        if not isinstance(current, dict) or segment not in current:
            return False, None
        current = current.get(segment)
    return True, current


def _select_paths(value, paths):
    selected = {}
    missing = []
    for path in paths:
        found, selected_value = _value_at_selector_path(value, path)
        if found:
            selected[path] = selected_value
        else:
            missing.append(path)
    return {'selected_paths': selected, 'missing_paths': missing}


def _compact_audit_event(row, *, include_payload=False):
    data = row.to_dict()
    payload = redact_secrets(data.get('payload') or {})
    redacted_count = _redacted_key_count(payload)
    compact = {
        'id': data.get('id'),
        'event_type': data.get('event_type'),
        'source': data.get('source'),
        'actor': data.get('actor'),
        'trace_id': data.get('trace_id'),
        'parent_trace_id': data.get('parent_trace_id'),
        'trace_label': data.get('trace_label'),
        'audit_role': data.get('audit_role'),
        'summary': data.get('summary'),
        'created_at': data.get('created_at'),
        'payload_size_chars': len(json.dumps(payload, ensure_ascii=False)) if payload else 0,
        'payload_keys': _payload_keys(payload),
        'redacted_payload_key_count': redacted_count,
        'has_redacted_payload_keys': redacted_count > 0,
    }
    if include_payload:
        compact['payload'] = payload
    else:
        compact['payload_preview'] = _payload_preview(payload, max_chars=240) if payload else None
    return compact


def _compact_provider_call(row, *, include_artifacts=False):
    data = redact_secrets(row.to_dict(include_artifacts=include_artifacts))
    compact = {
        'id': data.get('id'),
        'dedupe_key': data.get('dedupe_key'),
        'phase': data.get('phase'),
        'prompt_version_id': data.get('prompt_version_id'),
        'provider': data.get('provider'),
        'model': data.get('model'),
        'provider_response_id': data.get('provider_response_id'),
        'usage_input_tokens': data.get('usage_input_tokens'),
        'usage_output_tokens': data.get('usage_output_tokens'),
        'usage_total_tokens': data.get('usage_total_tokens'),
        'latency_ms': data.get('latency_ms'),
        'latency_bucket': data.get('latency_bucket'),
        'parse_repair_attempts': data.get('parse_repair_attempts'),
        'failure_class': data.get('failure_class'),
        'created_at': data.get('created_at'),
    }
    if include_artifacts:
        compact['request'] = data.get('request') or {}
        compact['response'] = data.get('response') or {}
        compact['parsed_output'] = data.get('parsed_output') or {}
        compact['response_text'] = data.get('response_text')
    else:
        compact['artifact_sizes'] = {
            'request_chars': len(json.dumps(redact_secrets(row.request_json or {}), ensure_ascii=False)),
            'response_chars': len(json.dumps(redact_secrets(row.response_json or {}), ensure_ascii=False)),
            'parsed_output_chars': len(json.dumps(redact_secrets(row.parsed_output_json or {}), ensure_ascii=False)),
            'response_text_chars': len(row.response_text or ''),
        }
        compact['parsed_output_preview'] = _payload_preview(redact_secrets(row.parsed_output_json or {}), max_chars=220)
    return compact


def _compact_run_event(row, *, include_payload=False):
    data = row.to_dict()
    payload = redact_secrets(data.get('payload') or {})
    redacted_count = _redacted_key_count(payload)
    compact = {
        'id': data.get('id'),
        'event_type': data.get('event_type'),
        'sequence_number': data.get('sequence_number'),
        'attempt_number': data.get('attempt_number'),
        'dedupe_key': data.get('dedupe_key'),
        'created_at': data.get('created_at'),
        'payload_size_chars': len(json.dumps(payload, ensure_ascii=False)) if payload else 0,
        'payload_keys': _payload_keys(payload),
        'redacted_payload_key_count': redacted_count,
        'has_redacted_payload_keys': redacted_count > 0,
    }
    if include_payload:
        compact['payload'] = payload
    else:
        compact['payload_preview'] = _payload_preview(payload, max_chars=220) if payload else None
    return compact


def _compact_memory_log(row):
    prov = row.provenance_json if isinstance(row.provenance_json, dict) else {}
    safe_provenance = {}
    if prov:
        safe_fields = (
            'pipeline_stage', 'evidence_status', 'tool_name',
            'resolution_confidence', 'ambiguity_status',
            'source_player_message_id', 'source_dm_message_id',
            'trace_id', 'clock_trigger_rule_id', 'build_sha',
            'rejection_reason',
        )
        for key in safe_fields:
            if key in prov and prov[key] is not None:
                safe_provenance[key] = prov[key]
        if 'evidence_sources' in prov and isinstance(prov['evidence_sources'], list):
            safe_provenance['evidence_source_count'] = len(prov['evidence_sources'])
            safe_provenance['evidence_source_types'] = sorted(set(
                s.get('source_type') or s.get('source', '')
                for s in prov['evidence_sources'] if isinstance(s, dict)
            ))
        if 'evidence_basis' in prov and isinstance(prov['evidence_basis'], list):
            safe_provenance['evidence_basis_count'] = len(prov['evidence_basis'])
    return {
        'id': row.id,
        'memory_run_id': row.memory_run_id,
        'memory_id': row.memory_id,
        'operation': row.operation,
        'status': row.status,
        'memory_type': row.memory_type,
        'visibility': row.visibility,
        'certainty': row.certainty,
        'importance': row.importance,
        'evidence_status': row.evidence_status,
        'reason': _truncate_text(row.reason, 320) if row.reason else None,
        'error': _truncate_text(row.error, 320) if row.error else None,
        'created_at': row.created_at.isoformat() if row.created_at else None,
        'has_provenance': bool(prov),
        'provenance': safe_provenance if safe_provenance else None,
    }


def _selected_audit_event_detail(row, paths):
    redacted_data = redact_secrets(row.to_dict())
    return {
        'event': _compact_audit_event(row, include_payload=False),
        **_select_paths(redacted_data, paths),
    }


def _audit_event_detail_escalation(row):
    compact = _compact_audit_event(row, include_payload=False)
    suggested_paths = [f'payload.{key}' for key in compact.get('payload_keys') or []][:8]
    return {
        'event': compact,
        'selected_paths': {},
        'missing_paths': [],
        'escalation_required': (
            'Use exact payload paths for targeted fields, or set include_full_payload=true '
            'when a full audit-event payload is genuinely required.'
        ),
        'suggested_paths': suggested_paths,
    }


def _selected_provider_call_detail(row, request_paths, response_paths, parsed_output_paths):
    detail = {
        'provider_call': _compact_provider_call(row, include_artifacts=False),
        'selected_request_paths': {},
        'selected_response_paths': {},
        'selected_parsed_output_paths': {},
        'missing_paths': [],
    }
    redacted_req = redact_secrets({'request': row.request_json or {}})
    redacted_resp = redact_secrets({'response': row.response_json or {}})
    redacted_parsed = redact_secrets({'parsed_output': row.parsed_output_json or {}})
    if request_paths:
        request_selection = _select_paths(redacted_req, [f'request.{path}' for path in request_paths])
        detail['selected_request_paths'] = request_selection['selected_paths']
        detail['missing_paths'].extend(request_selection['missing_paths'])
    if response_paths:
        response_selection = _select_paths(redacted_resp, [f'response.{path}' for path in response_paths])
        detail['selected_response_paths'] = response_selection['selected_paths']
        detail['missing_paths'].extend(response_selection['missing_paths'])
    if parsed_output_paths:
        parsed_output_selection = _select_paths(redacted_parsed, [f'parsed_output.{path}' for path in parsed_output_paths])
        detail['selected_parsed_output_paths'] = parsed_output_selection['selected_paths']
        detail['missing_paths'].extend(parsed_output_selection['missing_paths'])
    return detail


def _selected_run_event_detail(row, paths):
    redacted_data = redact_secrets(row.to_dict())
    return {
        'event': _compact_run_event(row, include_payload=False),
        **_select_paths(redacted_data, paths),
    }


def _selected_snapshot_detail(snapshot, sections, paths):
    payload = snapshot.snapshot_json or {}
    selected_sections = {}
    missing_sections = []
    for section in sections:
        clean = str(section or '').strip()
        if not clean:
            continue
        if clean in payload:
            selected_sections[clean] = payload.get(clean)
        else:
            missing_sections.append(clean)
    path_selection = _select_paths(payload, paths)
    return {
        'snapshot': snapshot.to_dict(include_payload=False),
        'snapshot_payload_keys': _payload_keys(payload),
        'snapshot_payload_size_chars': len(json.dumps(payload, ensure_ascii=False)) if payload else 0,
        'selected_sections': selected_sections,
        'selected_paths': path_selection['selected_paths'],
        'missing_sections': missing_sections,
        'missing_paths': path_selection['missing_paths'],
    }


def _compact_auditor_job(job):
    return {
        'id': job.id,
        'run_id': job.run_id,
        'cycle_id': job.cycle_id,
        'auditor_slot': job.auditor_slot,
        'status': job.status,
        'provider': job.provider,
        'model': job.model,
        'provider_call_id': job.provider_call_id,
        'tool_call_count': job.tool_call_count,
        'error_text': job.error_text,
        'started_at': job.started_at.isoformat() if job.started_at else None,
        'finished_at': job.finished_at.isoformat() if job.finished_at else None,
        'created_at': job.created_at.isoformat() if job.created_at else None,
        'updated_at': job.updated_at.isoformat() if job.updated_at else None,
    }


def _compact_cycle_status(row):
    if row is None:
        return None
    return {
        'id': row.id,
        'run_id': row.run_id,
        'cycle_number': row.cycle_number,
        'phase': row.phase,
        'status': row.status,
        'summary': row.summary,
        'notes': row.notes,
        'player_message_id': row.player_message_id,
        'dm_message_id': row.dm_message_id,
        'payload_keys': sorted((row.payload_json or {}).keys()),
        'scorecard_summary': row.scorecard_summary_json or {},
    }


def _compact_run_status(run):
    run_data = run.to_dict()
    scenario = run.scenario.to_dict() if run.scenario else None
    snapshot = run.snapshot.to_dict() if run.snapshot else None
    if scenario:
        scenario = {
            'id': scenario.get('id'),
            'source_campaign_id': scenario.get('source_campaign_id'),
            'name': scenario.get('name'),
            'description': scenario.get('description'),
            'scorecard_template_id': scenario.get('scorecard_template_id'),
        }
    if snapshot:
        snapshot = {
            'id': snapshot.get('id'),
            'scenario_id': snapshot.get('scenario_id'),
            'source_campaign_id': snapshot.get('source_campaign_id'),
            'source_session_id': snapshot.get('source_session_id'),
            'label': snapshot.get('label'),
            'summary': snapshot.get('summary'),
            'metadata': snapshot.get('metadata'),
            'created_at': snapshot.get('created_at'),
        }
    cycle = db.session.get(AutomationRunAuditCycle, run.awaiting_audit_cycle_id) if run.awaiting_audit_cycle_id else None
    return {
        'run': {
            'id': run_data.get('id'),
            'status': run_data.get('status'),
            'scenario_id': run_data.get('scenario_id'),
            'snapshot_id': run_data.get('snapshot_id'),
            'derived_campaign_id': run_data.get('derived_campaign_id'),
            'awaiting_audit_cycle_id': run_data.get('awaiting_audit_cycle_id'),
            'awaiting_audit_phase': run_data.get('awaiting_audit_phase'),
            'scorecard_summary': run_data.get('scorecard_summary'),
            'created_at': run_data.get('created_at'),
            'updated_at': run_data.get('updated_at'),
        },
        'scenario': scenario,
        'snapshot': snapshot,
        'current_audit_cycle': _compact_cycle_status(cycle),
        'auditor_config': auditor_config_for_run(run),
        'auditor_jobs': [_compact_auditor_job(job) for job in list_auditor_jobs(run.id, run.awaiting_audit_cycle_id)],
    }


def hash_private_text(text):
    if not text:
        return None
    import hashlib
    return hashlib.sha256(str(text).encode('utf-8')).hexdigest()


def _compact_clock(row):
    data = row.to_dict(include_private=True) or {}
    return {
        'clock_id': data.get('clock_id'),
        'name': data.get('name'),
        'filled_segments': data.get('filled'),
        'max_segments': data.get('segments'),
        'visible_to_players': data.get('visibility') != 'dm_private',
        'status': data.get('status'),
        'summary': data.get('summary'),
        'has_private_trigger': bool(data.get('trigger')),
        'has_private_on_complete': bool(data.get('on_complete')),
        'trigger_hash': hash_private_text(data.get('trigger')),
        'on_complete_hash': hash_private_text(data.get('on_complete')),
    }


def _compact_npc(row):
    data = row.to_dict(include_private=True) or {}
    dos = data.get('dossier')
    return {
        'actor_id': data.get('actor_id'),
        'name': data.get('name'),
        'role': data.get('role'),
        'location_id': data.get('location_id'),
        'is_active': data.get('is_active'),
        'tags': data.get('tags') or [],
        'has_private_notes': bool(dos),
        'private_notes_hash': hash_private_text(json.dumps(dos, sort_keys=True)) if dos else None,
    }


def _transcript_focus_window(run, cycle, limit, max_message_id=None):
    messages = _latest_session_messages(run, limit, max_message_id=max_message_id)
    focus_ids = {cycle.player_message_id, cycle.dm_message_id}
    for item in messages:
        item['is_focus_message'] = item.get('id') in focus_ids
    return messages


def _scene_state_summary(world_payload):
    state = _json_object(world_payload.get('world_state'), {})
    current_scene = _json_object(state.get('current_scene'), {})
    return {
        'known_location_id': state.get('known_location_id'),
        'known_location_name': state.get('known_location_name'),
        'current_scene': {
            'location_id': current_scene.get('location_id'),
            'location_name': current_scene.get('location_name'),
            'active_npc_ids': _json_list(current_scene.get('active_npc_ids'), []),
            'immediate_tension': current_scene.get('immediate_tension'),
            'summary': current_scene.get('summary'),
        },
    }


def _cycle_evidence_packet(run, cycle, args):
    refresh_run_scorecard(run)
    campaign = _campaign_for_run(run)
    world_payload = _world_payload(campaign) if campaign else {'has_world': False}
    latest_session = latest_session_for_run(run)
    transcript_limit = _limit_arg({'limit': (args or {}).get('transcript_limit')}, 12, 40)
    audit_event_limit = _limit_arg({'limit': (args or {}).get('audit_event_limit')}, 12, 40)
    provider_call_limit = _limit_arg({'limit': (args or {}).get('provider_call_limit')}, 6, 20)
    run_event_limit = _limit_arg({'limit': (args or {}).get('run_event_limit')}, 8, 30)

    boundaries = _cycle_boundaries(cycle)

    audit_rows = []
    if campaign:
        audit_query = CampaignAuditEvent.query.filter_by(campaign_id=campaign.id)
        if boundaries.get("audit_event_id") is not None:
            audit_query = audit_query.filter(CampaignAuditEvent.id <= boundaries["audit_event_id"])
        audit_rows = audit_query.order_by(CampaignAuditEvent.id.desc()).limit(audit_event_limit).all()

    provider_query = AutomationRunProviderCall.query.filter_by(run_id=run.id)
    if boundaries.get("provider_call_id") is not None:
        provider_query = provider_query.filter(AutomationRunProviderCall.id <= boundaries["provider_call_id"])
    provider_rows = provider_query.order_by(AutomationRunProviderCall.id.desc()).limit(provider_call_limit).all()

    run_event_query = AutomationRunEvent.query.filter_by(run_id=run.id)
    if boundaries.get("run_event_id") is not None:
        run_event_query = run_event_query.filter(AutomationRunEvent.id <= boundaries["run_event_id"])
    run_event_rows = run_event_query.order_by(AutomationRunEvent.id.desc()).limit(run_event_limit).all()

    clock_rows = CampaignClock.query.filter_by(campaign_id=campaign.id).order_by(CampaignClock.id.asc()).all() if campaign else []
    npc_rows = NPCActor.query.filter_by(campaign_id=campaign.id).order_by(NPCActor.id.asc()).all() if campaign else []

    memory_log_limit = _limit_arg({'limit': (args or {}).get('memory_log_limit')}, 20, 60)
    memory_log_rows = []
    if campaign:
        log_query = CampaignMemoryLog.query.filter_by(campaign_id=campaign.id)
        if boundaries.get("message_id") is not None:
            log_query = log_query.filter(
                (CampaignMemoryLog.source_player_message_id <= boundaries["message_id"])
                | (CampaignMemoryLog.source_player_message_id.is_(None))
                | (CampaignMemoryLog.source_dm_message_id.is_(None))
            )
        memory_log_rows = log_query.order_by(CampaignMemoryLog.id.desc()).limit(memory_log_limit).all()

    return {
        'run': {
            'id': run.id,
            'status': run.status,
            'derived_campaign_id': run.derived_campaign_id,
            'scenario_id': run.scenario_id,
            'awaiting_audit_phase': run.awaiting_audit_phase,
            'scorecard_summary': run.scorecard_summary_json or {},
        },
        'audit_cycle': {
            'id': cycle.id,
            'cycle_number': cycle.cycle_number,
            'phase': cycle.phase,
            'status': cycle.status,
            'summary': cycle.summary,
            'player_message_id': cycle.player_message_id,
            'dm_message_id': cycle.dm_message_id,
            'payload': cycle.payload_json or {},
        },
        'transcript_window': _transcript_focus_window(run, cycle, transcript_limit, max_message_id=boundaries.get("message_id")),
        'scene_state_summary': _scene_state_summary(world_payload),
        'running_summary': latest_session.running_summary if latest_session else None,
        'clock_summaries': [_compact_clock(row) for row in clock_rows],
        'active_npc_summaries': [_compact_npc(row) for row in npc_rows],
        'recent_memory_logs': [_compact_memory_log(row) for row in reversed(memory_log_rows)],
        'memory_log_summary': {
            'total_returned': len(memory_log_rows),
            'has_provenance': sum(1 for r in memory_log_rows if r.provenance_json),
            'has_evidence_status': sum(1 for r in memory_log_rows if r.evidence_status),
            'evidence_status_counts': dict(Counter(
                m.evidence_status for m in memory_log_rows if m.evidence_status
            )),
        },
        'recent_audit_events': [_compact_audit_event(row, include_payload=False) for row in reversed(audit_rows)],
        'recent_provider_calls': [_compact_provider_call(row, include_artifacts=False) for row in reversed(provider_rows)],
        'recent_run_events': [_compact_run_event(row, include_payload=False) for row in reversed(run_event_rows)],
        'follow_up_tools': [
            'get_audit_event_detail',
            'get_provider_call_detail',
            'get_run_event_detail',
            'get_audit_events',
            'get_provider_calls',
            'get_run_events',
            'search_campaign_memory',
        ],
    }


def _match_score(query_terms, value):
    text = json.dumps(value, ensure_ascii=False).lower()
    return sum(1 for term in query_terms if term and term in text)


def _keyword_search_campaign_memory(campaign, args):
    query = str((args or {}).get('query') or '').strip().lower()
    limit = _limit_arg(args, 8, 20)
    terms = [term for term in query.replace('_', ' ').split() if len(term) > 2]
    world_payload = _world_payload(campaign)
    graph = _json_object(world_payload.get('knowledge_graph'), {})
    candidates = []
    for kind in ('entities', 'relations', 'facts'):
        for item in _json_list(graph.get(kind), []):
            if isinstance(item, dict):
                candidates.append({'kind': kind[:-1], 'item_id': item.get('id'), 'value': item})
    for npc in NPCActor.query.filter_by(campaign_id=campaign.id).all():
        candidates.append({'kind': 'npc_actor', 'item_id': npc.actor_id, 'value': npc.to_dict(include_private=True)})
    for clock in CampaignClock.query.filter_by(campaign_id=campaign.id).all():
        candidates.append({'kind': 'clock', 'item_id': clock.clock_id, 'value': clock.to_dict(include_private=True)})
    for event in WorldEvent.query.filter_by(campaign_id=campaign.id).order_by(WorldEvent.created_at.desc()).limit(50).all():
        candidates.append({'kind': 'world_event', 'item_id': str(event.id), 'value': event.to_dict(include_private=True)})
    if world_payload.get('has_world'):
        candidates.append({'kind': 'world_state', 'item_id': 'current', 'value': world_payload.get('world_state')})
        candidates.append({'kind': 'dm_private', 'item_id': 'current', 'value': world_payload.get('dm_private')})
    scored = []
    for item in candidates:
        score = _match_score(terms, item['value'])
        if score:
            scored.append({**item, 'score': score})
    scored.sort(key=lambda item: item['score'], reverse=True)
    return {'query': query, 'matches': scored[:limit]}


def _sanitize_response_for_lease_token(data):
    if isinstance(data, dict):
        new_dict = {}
        for k, v in data.items():
            new_key = str(k).replace('lease_token', 'redacted_token')
            new_dict[new_key] = _sanitize_response_for_lease_token(v)
        return new_dict
    elif isinstance(data, list):
        return [_sanitize_response_for_lease_token(item) for item in data]
    elif isinstance(data, str):
        return data.replace('lease_token', 'redacted_token')
    return data


def execute_auditor_tool(run, tool_name, args=None):
    args = args or {}
    campaign = _campaign_for_run(run)
    
    def _execute():
        if tool_name == 'get_run_status':
            return _compact_run_status(run)
        if tool_name == 'get_current_audit_bundle':
            return get_current_audit_bundle_data(run)
        if tool_name == 'get_cycle_evidence_packet':
            cycle = db.session.get(AutomationRunAuditCycle, run.awaiting_audit_cycle_id) if run.awaiting_audit_cycle_id else None
            if cycle is None:
                return {'error': 'Run is not currently paused at an audit cycle.'}
            return _cycle_evidence_packet(run, cycle, args)
        if campaign is None:
            return {'error': 'Run has no derived or source campaign available.'}
        cycle = db.session.get(AutomationRunAuditCycle, run.awaiting_audit_cycle_id) if run.awaiting_audit_cycle_id else None
        boundaries = _cycle_boundaries(cycle)

        if tool_name == 'get_transcript':
            return {'messages': _latest_session_messages(run, _limit_arg(args, 80, 200), max_message_id=boundaries.get("message_id"))}
        if tool_name == 'get_audit_events':
            limit = _limit_arg(args, 40, 200)
            include_payload = bool(args.get('include_payload'))
            if include_payload:
                return {'error': 'Bulk audit-event payload fetch is disabled for built-in auditors. Use get_audit_event_detail with exact paths.'}
            query = CampaignAuditEvent.query.filter_by(campaign_id=campaign.id)
            if boundaries.get("audit_event_id") is not None:
                query = query.filter(CampaignAuditEvent.id <= boundaries["audit_event_id"])
            if args.get('event_type'):
                query = query.filter_by(event_type=str(args.get('event_type')).strip())
            if args.get('trace_id'):
                query = query.filter_by(trace_id=str(args.get('trace_id')).strip())
            rows = query.order_by(CampaignAuditEvent.id.desc()).limit(limit).all()
            return {'campaign_id': campaign.id, 'events': [_compact_audit_event(row, include_payload=include_payload) for row in reversed(rows)]}
        if tool_name == 'get_world_state':
            return _world_payload(campaign)
        if tool_name == 'get_clocks':
            rows = CampaignClock.query.filter_by(campaign_id=campaign.id).order_by(CampaignClock.id.asc()).all()
            return {'clocks': [row.to_dict(include_private=True) for row in rows]}
        if tool_name == 'get_npcs':
            rows = NPCActor.query.filter_by(campaign_id=campaign.id).order_by(NPCActor.id.asc()).all()
            return {'npcs': [row.to_dict(include_private=True) for row in rows]}
        if tool_name == 'get_characters':
            rows = Character.query.filter_by(campaign_id=campaign.id).order_by(Character.id.asc()).all()
            return {'characters': [character_full_dict(row) for row in rows]}
        if tool_name == 'get_provider_calls':
            limit = _limit_arg(args, 12, 100)
            include_artifacts = bool(args.get('include_artifacts'))
            if include_artifacts:
                return {'error': 'Bulk provider artifacts are disabled for built-in auditors. Use get_provider_call_detail with exact paths.'}
            query = AutomationRunProviderCall.query.filter_by(run_id=run.id)
            if boundaries.get("provider_call_id") is not None:
                query = query.filter(AutomationRunProviderCall.id <= boundaries["provider_call_id"])
            rows = query.order_by(AutomationRunProviderCall.id.desc()).limit(limit).all()
            return {'provider_calls': [_compact_provider_call(row, include_artifacts=include_artifacts) for row in reversed(rows)]}
        if tool_name == 'get_run_events':
            limit = _limit_arg(args, 20, 200)
            include_payload = bool(args.get('include_payload'))
            if include_payload:
                return {'error': 'Bulk run-event payload fetch is disabled for built-in auditors. Use get_run_event_detail with exact paths.'}
            query = AutomationRunEvent.query.filter_by(run_id=run.id)
            if boundaries.get("run_event_id") is not None:
                query = query.filter(AutomationRunEvent.id <= boundaries["run_event_id"])
            rows = query.order_by(AutomationRunEvent.id.desc()).limit(limit).all()
            return {'events': [_compact_run_event(row, include_payload=include_payload) for row in reversed(rows)]}
        if tool_name == 'get_audit_event_detail':
            row = db.session.get(CampaignAuditEvent, _safe_int(args.get('event_id'), 0, minimum=0))
            if row is None or row.campaign_id != campaign.id:
                return {'error': 'Audit event not found for this campaign.'}
            if boundaries.get("audit_event_id") is not None and row.id > boundaries["audit_event_id"]:
                return {'error': 'Audit event not found for this campaign.'}
            paths = _path_args(args, 'paths')
            if paths:
                return _selected_audit_event_detail(row, paths)
            if bool(args.get('include_full_payload')):
                return {'event': _compact_audit_event(row, include_payload=True)}
            return _audit_event_detail_escalation(row)
        if tool_name == 'get_provider_call_detail':
            row = db.session.get(AutomationRunProviderCall, _safe_int(args.get('provider_call_id'), 0, minimum=0))
            if row is None or row.run_id != run.id:
                return {'error': 'Provider call not found for this run.'}
            if boundaries.get("provider_call_id") is not None and row.id > boundaries["provider_call_id"]:
                return {'error': 'Provider call not found for this run.'}
            request_paths = _path_args(args, 'request_paths')
            response_paths = _path_args(args, 'response_paths')
            parsed_output_paths = _path_args(args, 'parsed_output_paths')
            if request_paths or response_paths or parsed_output_paths:
                return _selected_provider_call_detail(row, request_paths, response_paths, parsed_output_paths)
            return {'provider_call': _compact_provider_call(row, include_artifacts=True)}
        if tool_name == 'get_run_event_detail':
            row = db.session.get(AutomationRunEvent, _safe_int(args.get('event_id'), 0, minimum=0))
            if row is None or row.run_id != run.id:
                return {'error': 'Run event not found for this run.'}
            if boundaries.get("run_event_id") is not None and row.id > boundaries["run_event_id"]:
                return {'error': 'Run event not found for this run.'}
            paths = _path_args(args, 'paths')
            if paths:
                return _selected_run_event_detail(row, paths)
            return {'event': _compact_run_event(row, include_payload=True)}
        if tool_name == 'search_campaign_memory':
            return _keyword_search_campaign_memory(campaign, args)
        if tool_name == 'get_snapshot_manifest':
            snapshot = db.session.get(AutomationSnapshot, run.snapshot_id)
            if snapshot is None:
                return {'snapshot': None}
            sections = _path_args(args, 'sections')
            paths = _path_args(args, 'paths')
            if sections or paths:
                return _selected_snapshot_detail(snapshot, sections, paths)
            if bool(args.get('include_payload')):
                return {'error': 'Full snapshot payload fetch is disabled for built-in auditors. Use snapshot sections or exact paths.'}
            return {'snapshot': snapshot.to_dict(include_payload=bool(args.get('include_payload')))}
        if tool_name == 'get_scorecard_template':
            return {'scorecard_template': current_scorecard_template_for_run(run)}
        return {'error': f'Unknown auditor tool: {tool_name}'}

    res = _execute()
    return _sanitize_response_for_lease_token(res)


def _json_object_from_text(text):
    raw = str(text or '').strip()
    if not raw:
        raise ValueError('Auditor response was empty.')
    start = raw.find('{')
    end = raw.rfind('}')
    if start == -1 or end == -1 or end < start:
        raise ValueError('Auditor response did not contain a JSON object.')
    return json.loads(raw[start:end + 1])


def _parse_tool_arguments(raw_arguments):
    if isinstance(raw_arguments, dict):
        return raw_arguments
    if not raw_arguments:
        return {}
    try:
        return json.loads(raw_arguments)
    except (TypeError, ValueError):
        return {}


def _choice_message(data):
    choices = data.get('choices') if isinstance(data, dict) else []
    if not choices or not isinstance(choices[0], dict):
        return {}, None
    message = choices[0].get('message') or {}
    return message if isinstance(message, dict) else {}, choices[0].get('finish_reason')


def _assistant_tool_message(message):
    return {
        'role': 'assistant',
        'content': message.get('content') or '',
        'tool_calls': message.get('tool_calls') or [],
    }


def _tool_result_message(tool_call, tool_name, result):
    return {
        'role': 'tool',
        'tool_call_id': tool_call.get('id'),
        'name': tool_name,
        'content': json.dumps(result, ensure_ascii=False),
    }


def _usage_total(usages):
    total = Counter()
    for usage in usages:
        if not isinstance(usage, dict):
            continue
        total['prompt_tokens'] += _safe_int(usage.get('prompt_tokens') or usage.get('input_tokens'))
        total['completion_tokens'] += _safe_int(usage.get('completion_tokens') or usage.get('output_tokens'))
        total['total_tokens'] += _safe_int(usage.get('total_tokens'))
    return dict(total)


def _normalize_status(value, default='warn'):
    status = str(value or default).strip().lower()
    return status if status in AUDITOR_VALID_STATUSES else default


def _aggregate_auditor_statuses(statuses, default='warn'):
    normalized = [_normalize_status(status, default='warn') for status in statuses if status]
    if not normalized:
        return default
    assessed = [status for status in normalized if status in AUDITOR_STATUS_RANK]
    if assessed:
        return min(assessed, key=lambda item: AUDITOR_STATUS_RANK[item])
    if any(status == 'not_assessed' for status in normalized):
        return 'not_assessed'
    if any(status == 'not_applicable' for status in normalized):
        return 'not_applicable'
    return default


def _normalize_final_scorecard(parsed, run):
    payload = _json_object(parsed.get('scorecard'), parsed) if isinstance(parsed, dict) else {}
    template = current_scorecard_template_for_run(run)
    template_criteria = [
        item for item in _json_list(template.get('criteria'), [])
        if isinstance(item, dict) and item.get('id')
    ]
    raw_criteria = _json_list(payload.get('criteria'), [])
    by_id = {}
    for raw in raw_criteria:
        if not isinstance(raw, dict):
            continue
        criterion_id = str(raw.get('criterion_id') or raw.get('id') or '').strip()
        if not criterion_id:
            continue
        by_id[criterion_id] = {
            'criterion_id': criterion_id,
            'status': _normalize_status(raw.get('status')),
            'summary': str(raw.get('summary') or '').strip() or None,
            'primary_evidence': str(raw.get('primary_evidence') or '').strip() or None,
            'evidence': str(raw.get('evidence') or '').strip() or None,
        }
    criteria = []
    if template_criteria:
        for criterion in template_criteria:
            existing = by_id.get(criterion['id'])
            criteria.append(existing or {
                'criterion_id': criterion['id'],
                'status': 'not_assessed',
                'summary': 'Auditor did not submit a finding for this criterion.',
                'primary_evidence': None,
                'evidence': 'Missing criterion in auditor final JSON.',
            })
    else:
        criteria = list(by_id.values())
    overall_status = (
        _aggregate_auditor_statuses([item.get('status') for item in criteria], default='warn')
        if criteria
        else _normalize_status(payload.get('overall_status'), 'warn')
    )
    return {
        'overall_status': overall_status,
        'overall_summary': str(payload.get('overall_summary') or payload.get('summary') or '').strip() or None,
        'notes': str(payload.get('notes') or '').strip() or None,
        'criteria': criteria,
        'tool_calls_used': _json_list(payload.get('tool_calls_used'), []),
        'unresolved_evidence_gaps': _json_list(payload.get('unresolved_evidence_gaps'), []),
        'visible_findings': _json_list(payload.get('visible_findings'), []),
        'hidden_state_findings': _json_list(payload.get('hidden_state_findings'), []),
    }


def _auditor_system_prompt():
    return (
        'You are an observe-only automation auditor for an AI Dungeon Master product. '
        'You grade the AI DM from runtime truth, not vibes. You may inspect hidden/private app state '
        'through read-only tools because memory, spoiler, clock, and hidden-state correctness are part of the audit. '
        'Do not suggest or perform mutations. Do not act as the DM. Do not repair visible output. '
        'Separate visible-output findings from hidden-state findings so private facts are never framed as player-visible truth. '
        'Use not_assessed when a criterion was not actually exercised in this cycle or when the available evidence cannot fairly grade it. '
        'Never mark a criterion pass just because no contradictory evidence was found. '
        'Start with get_cycle_evidence_packet exactly once. '
        'Treat orchestration status dumps and bulk payload lists as expensive. '
        'Do not call get_run_status unless the audit pause metadata itself appears inconsistent. '
        'Do not request include_payload or include_artifacts on list tools. '
        'When a compact item looks suspicious, prefer exact field-path selectors on detail tools before requesting any full object. '
        'For audit-event detail specifically, use exact payload paths first; full payload access requires explicit escalation and should be rare. '
        'Use any criterion-specific evidence requirements from the scorecard to choose the minimum necessary evidence surface for each criterion. '
        'For each criterion, nominate one primary evidence source before widening to other surfaces. '
        'Only gather additional evidence when it could realistically change the criterion status from pass to warn/fail, or warn to pass/fail. '
        'Avoid broad raw-state tools unless the compact packet cannot answer a scorecard criterion. '
        'Stop calling tools as soon as each scorecard criterion has concrete evidence. '
        'Call tools until you have enough evidence. When finished, reply with exactly one JSON object and no markdown.'
    )


def _auditor_user_prompt(run, cycle, slot, config):
    template = current_scorecard_template_for_run(run)
    criterion_evidence_requirements = [
        {
            'criterion_id': item.get('id'),
            'label': item.get('label'),
            'evidence_requirements': _json_list(item.get('evidence_requirements'), []),
        }
        for item in _json_list(template.get('criteria'), [])
        if isinstance(item, dict) and item.get('id')
    ]
    return json.dumps({
        'task': 'Audit the current automation pause cycle using read-only tools.',
        'run_id': run.id,
        'derived_campaign_id': run.derived_campaign_id,
        'audit_cycle': cycle.to_dict(),
        'auditor_slot': slot,
        'auditor_count': config.get('count'),
        'required_tools': config.get('required_tools'),
        'recommended_first_tool': 'get_cycle_evidence_packet',
        'recommended_sequence': [
            'get_cycle_evidence_packet',
            'nominate one primary evidence source per criterion',
            'detail-tool exact-path lookups for suspicious items',
            'broad raw-state tools only if a criterion remains unresolved',
        ],
        'criterion_workflow': [
            'After the first evidence packet, map each criterion to one primary evidence source.',
            'Only call another tool for a criterion if the current evidence leaves the status unresolved or another source could change the status.',
            'If a criterion was not exercised in this cycle, mark it not_assessed instead of pass.',
            'If no additional tool can change any criterion status, finalize immediately.',
        ],
        'forbidden_first_moves': [
            'get_run_status for general evidence gathering',
            'get_audit_events with include_payload=true',
            'get_run_events with include_payload=true',
            'get_provider_calls with include_artifacts=true',
            'get_snapshot_manifest with include_payload=true',
        ],
        'criterion_evidence_requirements': criterion_evidence_requirements,
        'scorecard_template': template,
        'final_response_contract': {
            'overall_status': 'pass|warn|fail|not_assessed|not_applicable',
            'overall_summary': 'short verdict',
            'notes': 'runtime-truth notes',
            'criteria': [
                {
                    'criterion_id': 'scorecard criterion id',
                    'status': 'pass|warn|fail|not_assessed|not_applicable',
                    'summary': 'short finding',
                    'primary_evidence': 'main evidence source or tool/result that anchored the status decision',
                    'evidence': 'specific transcript, audit-event, world-state, clock, provider-call, or run-event evidence',
                },
            ],
            'tool_calls_used': ['tool_name'],
            'visible_findings': ['finding visible in transcript/output'],
            'hidden_state_findings': ['finding from hidden/private persisted state'],
            'unresolved_evidence_gaps': ['missing evidence, or empty list'],
        },
    }, ensure_ascii=False)


def _criterion_decision_checkpoint_message():
    return {
        'role': 'user',
        'content': (
            'Decision checkpoint. Before calling more tools, map each scorecard criterion to one primary evidence source '
            'already collected. Call another tool only if the criterion remains unresolved or if the new evidence could '
            'realistically change the criterion status. If no remaining tool call can change a criterion status, finalize now.'
        ),
    }


def request_auditor_decision_with_tools(run, cycle, job, config, *, max_tool_rounds=12):
    provider, model = resolve_auditor_provider_model(config.get('model'))
    messages = [
        {'role': 'system', 'content': _auditor_system_prompt()},
        {'role': 'user', 'content': _auditor_user_prompt(run, cycle, job.auditor_slot, config)},
    ]
    tool_trace = []
    responses = []
    usages = []
    parse_repair_attempts = 0
    final_response = {}
    final_text = ''
    decision_checkpoint_sent = False
    trace_id = f'automation_auditor:run_{run.id}:cycle_{cycle.id}:slot_{job.auditor_slot}:{uuid4().hex[:8]}'
    campaign = _campaign_for_run(run)
    campaign_id = campaign.id if campaign else None
    for tool_round in range(max_tool_rounds + 3):
        try:
            normalized = _post_chat_normalized(
                messages,
                json_mode=False,
                tools=AUDITOR_TOOL_DEFINITIONS,
                tool_choice='auto',
                parallel_tool_calls=False,
                allow_thinking=True,
                timeout_seconds=180,
                max_attempts=2,
                provider=provider,
                model=model,
                audit_context={
                    'campaign_id': campaign_id,
                    'operation': 'automation_auditor_tool_loop',
                    'actor': 'automation_auditor',
                    'trace_id': trace_id,
                    'trace_label': f'automation_auditor run {run.id} cycle {cycle.id} slot {job.auditor_slot}',
                },
            )
        except ProviderError as err:
            if err.kind != 'malformed':
                raise
            normalized = None
        responses.append(normalized.raw if normalized is not None else {})
        usages.append(normalized.usage if normalized is not None else {})
        message = normalized.message_view() if normalized is not None else {}
        tool_calls = message.get('tool_calls') or []
        if tool_calls and tool_round < max_tool_rounds:
            messages.append(_assistant_tool_message(message))
            for tool_call in tool_calls:
                function = tool_call.get('function') if isinstance(tool_call, dict) else {}
                tool_name = (function or {}).get('name')
                args = _parse_tool_arguments((function or {}).get('arguments'))
                result = execute_auditor_tool(run, tool_name, args)
                tool_trace.append({
                    'tool_name': tool_name,
                    'arguments': args,
                    'result': result,
                })
                messages.append(_tool_result_message(tool_call, tool_name, result))
            if not decision_checkpoint_sent and len(tool_trace) >= 4:
                messages.append(_criterion_decision_checkpoint_message())
                decision_checkpoint_sent = True
            continue

        final_text = message.get('content') or ''
        try:
            parsed = _json_object_from_text(final_text)
            final_response = _normalize_final_scorecard(parsed, run)
            break
        except Exception as exc:
            parse_repair_attempts += 1
            if parse_repair_attempts > 2:
                raise RuntimeError(f'Auditor failed to produce valid final JSON: {exc}')
            messages.append({
                'role': 'user',
                'content': (
                    'Your previous answer was not valid final audit JSON. '
                    'Reply with exactly one JSON object matching the final_response_contract. '
                    'Do not call more tools unless absolutely necessary.'
                ),
            })
    else:
        raise RuntimeError('Auditor exceeded max tool rounds without a final scorecard.')

    provider_call, _created = persist_provider_call(run, {
        'dedupe_key': f'auditor:{cycle.id}:slot:{job.auditor_slot}',
        'phase': 'auditor_decision',
        'prompt_version_id': AUDITOR_PROMPT_VERSION,
        'provider': provider,
        'model': model,
        'provider_response_id': (responses[-1].get('id') if responses and isinstance(responses[-1], dict) else None),
        'usage': _usage_total(usages),
        'parse_repair_attempts': parse_repair_attempts,
        'request': {
            'messages': messages,
            'tools': AUDITOR_TOOL_DEFINITIONS,
            'tool_trace': tool_trace,
        },
        'response': responses[-1] if responses else {},
        'parsed_output': final_response,
        'response_text': final_text,
    })
    return {
        'provider': provider,
        'model': model,
        'provider_call': provider_call,
        'scorecard': final_response,
        'tool_trace': tool_trace,
        'tool_call_count': len(tool_trace),
    }


def aggregate_completed_auditor_jobs(run, cycle, jobs):
    completed = [job for job in jobs if job.status == 'completed']
    if not completed:
        raise ValueError('No completed auditor jobs to aggregate.')
    template = current_scorecard_template_for_run(run)
    template_criteria = [
        item for item in _json_list(template.get('criteria'), [])
        if isinstance(item, dict) and item.get('id')
    ]
    all_tool_names = []
    all_gaps = []
    auditor_summaries = []
    criteria = []
    for criterion in template_criteria:
        criterion_id = criterion['id']
        findings = []
        for job in completed:
            scorecard = job.submitted_scorecard_json or {}
            match = next(
                (item for item in _json_list(scorecard.get('criteria'), []) if item.get('criterion_id') == criterion_id),
                None,
            )
            if match:
                findings.append((job, match))
        status = _aggregate_auditor_statuses([match.get('status') for _job, match in findings], default='warn')
        if not findings:
            criteria.append({
                'criterion_id': criterion_id,
                'status': 'not_assessed',
                'summary': 'No built-in auditor submitted a finding for this criterion.',
                'evidence': 'Missing criterion across completed built-in auditor jobs.',
            })
            continue
        criteria.append({
            'criterion_id': criterion_id,
            'status': status,
            'summary': ' | '.join(
                f"Auditor {job.auditor_slot}: {match.get('summary') or match.get('status')}"
                for job, match in findings
            )[:1200],
            'evidence': ' | '.join(
                f"Auditor {job.auditor_slot}: {match.get('evidence') or 'No evidence supplied.'}"
                for job, match in findings
            )[:2400],
        })
    for job in completed:
        scorecard = job.submitted_scorecard_json or {}
        if scorecard.get('overall_summary'):
            auditor_summaries.append(f"Auditor {job.auditor_slot}: {scorecard.get('overall_summary')}")
        all_tool_names.extend(scorecard.get('tool_calls_used') or [])
        all_gaps.extend(scorecard.get('unresolved_evidence_gaps') or [])
    overall_status = _aggregate_auditor_statuses([item['status'] for item in criteria], default='warn')
    return {
        'overall_status': overall_status,
        'overall_summary': ' / '.join(auditor_summaries)[:1200] or f'{len(completed)} built-in auditor(s) completed.',
        'criteria': criteria,
        'auditor_jobs': [
            {
                'id': job.id,
                'auditor_slot': job.auditor_slot,
                'provider': job.provider,
                'model': job.model,
                'provider_call_id': job.provider_call_id,
                'tool_call_count': job.tool_call_count,
            }
            for job in completed
        ],
        'tool_calls_used': sorted({str(name) for name in all_tool_names if name}),
        'unresolved_evidence_gaps': all_gaps,
    }


def _audited_cycle_count(run):
    summary = run.scorecard_summary_json or {}
    return _safe_int(summary.get('audited_cycle_count'))


def run_builtin_auditors_for_current_cycle(run_id, *, rerun_failed=False):
    run = db.session.get(AutomationRun, run_id)
    if run is None:
        raise ValueError('Run not found.')
    if run.status != 'awaiting_audit' or not run.awaiting_audit_cycle_id:
        raise ValueError('Run is not awaiting an audit cycle.')
    cycle = db.session.get(AutomationRunAuditCycle, run.awaiting_audit_cycle_id)
    if cycle is None:
        raise ValueError('Current audit cycle was not found.')
    config = auditor_config_for_run(run)
    if config.get('mode') != 'built_in':
        raise ValueError('Run auditor_config.mode is not built_in.')
    jobs = ensure_auditor_jobs_for_cycle(run, cycle, config, rerun_failed=rerun_failed)
    append_run_event(
        run,
        'auditor_jobs_updated',
        {'auditor_jobs': [job.to_dict() for job in jobs], 'cycle_id': cycle.id},
        dedupe_key=f'auditor_jobs_started:{run.id}:{cycle.id}:{utcnow().isoformat()}',
    )
    for job in jobs:
        db.session.refresh(job)
        if job.status == 'completed':
            continue
        if job.status in {'canceled', 'cancel_requested'}:
            continue
        job.status = 'running'
        job.started_at = job.started_at or _utcnow()
        job.updated_at = _utcnow()
        db.session.commit()
        append_run_event(
            run,
            'auditor_job_started',
            {'auditor_job': job.to_dict()},
            dedupe_key=f'auditor_job_started:{job.id}:{job.started_at.isoformat()}',
        )
        try:
            result = request_auditor_decision_with_tools(run, cycle, job, config)
            db.session.refresh(job)
            if job.status in {'canceled', 'cancel_requested'}:
                job.provider = result.get('provider')
                job.model = result.get('model')
                job.provider_call_id = result['provider_call'].id
                job.tool_call_count = result.get('tool_call_count') or 0
                job.tool_trace_json = result.get('tool_trace') or []
                job.updated_at = _utcnow()
                db.session.commit()
                append_run_event(
                    run,
                    'auditor_jobs_updated',
                    {'auditor_job': job.to_dict(), 'canceled_after_provider_return': True},
                    dedupe_key=f'auditor_job_canceled_after_provider_return:{job.id}',
                )
                continue
            job.status = 'completed'
            job.provider = result.get('provider')
            job.model = result.get('model')
            job.provider_call_id = result['provider_call'].id
            job.tool_call_count = result.get('tool_call_count') or 0
            job.tool_trace_json = result.get('tool_trace') or []
            job.submitted_scorecard_json = result.get('scorecard') or {}
            job.error_text = None
            job.finished_at = _utcnow()
            job.updated_at = _utcnow()
            db.session.commit()
            append_run_event(
                run,
                'auditor_job_completed',
                {'auditor_job': job.to_dict()},
                dedupe_key=f'auditor_job_completed:{job.id}',
            )
        except Exception as exc:
            job.status = 'failed'
            job.error_text = str(exc)
            job.finished_at = _utcnow()
            job.updated_at = _utcnow()
            db.session.commit()
            append_run_event(
                run,
                'auditor_job_failed',
                {'auditor_job': job.to_dict(), 'error': str(exc)},
                dedupe_key=f'auditor_job_failed:{job.id}:{type(exc).__name__}',
            )
    jobs = list_auditor_jobs(run.id, cycle.id)
    completed_count = sum(1 for job in jobs if job.status == 'completed')
    if completed_count < config['count']:
        append_run_event(
            run,
            'auditor_jobs_updated',
            {'auditor_jobs': [job.to_dict() for job in jobs], 'cycle_id': cycle.id},
            dedupe_key=f'auditor_jobs_incomplete:{run.id}:{cycle.id}:{utcnow().isoformat()}',
        )
        return {'run': run.to_dict(), 'audit_cycle': cycle.to_dict(), 'auditor_jobs': [job.to_dict() for job in jobs], 'completed': False}

    aggregate = aggregate_completed_auditor_jobs(run, cycle, jobs)
    notes = (
        f"Built-in auditor aggregate from {completed_count} auditor job(s). "
        f"Unresolved evidence gaps: {json.dumps(aggregate.get('unresolved_evidence_gaps') or [], ensure_ascii=False)}"
    )
    cycle = submit_audit_cycle_feedback(
        cycle,
        summary=aggregate.get('overall_summary'),
        notes=notes,
        scorecard=aggregate,
    )
    append_run_event(
        run,
        'audit_cycle_audited',
        {'audit_cycle': cycle.to_dict(), 'auditor_jobs': [job.to_dict() for job in jobs]},
        dedupe_key=f'audit_cycle_audited:built_in:{cycle.id}:{completed_count}',
    )
    scorecard = refresh_run_scorecard(run)
    append_run_event(
        run,
        'run_scorecard_updated',
        {
            'scorecard': scorecard,
            'scorecard_summary': run.scorecard_summary_json or {},
            'baseline_comparison': run.baseline_comparison_json or {},
        },
        dedupe_key=f'run_scorecard_updated:{run.id}:{run.last_event_sequence or 0}:auditor:{cycle.id}',
    )
    should_continue = bool(config.get('auto_continue'))
    if should_continue and config.get('target_cycles') is not None:
        should_continue = _audited_cycle_count(run) < config.get('target_cycles')
    if should_continue:
        continued_cycle = continue_audit_run(run)
        append_run_event(
            run,
            'audit_cycle_continued',
            {'audit_cycle_id': continued_cycle.id if continued_cycle else None, 'auto_continue': True},
            dedupe_key=f'audit_cycle_continued:built_in:{run.id}:{cycle.id}:{run.audit_resumed_at.isoformat() if run.audit_resumed_at else "now"}',
        )
    elif config.get('target_cycles') is not None and _audited_cycle_count(run) >= config.get('target_cycles'):
        append_run_event(
            run,
            'auditor_target_cycles_reached',
            {'target_cycles': config.get('target_cycles'), 'audited_cycle_count': _audited_cycle_count(run)},
            dedupe_key=f'auditor_target_cycles_reached:{run.id}:{cycle.id}',
        )
    return {
        'run': run.to_dict(),
        'audit_cycle': cycle.to_dict(),
        'auditor_jobs': [job.to_dict() for job in jobs],
        'scorecard': scorecard,
        'completed': True,
    }


def cancel_auditor_jobs_for_current_cycle(run):
    cycle_id = run.awaiting_audit_cycle_id
    if not cycle_id:
        return []
    jobs = list_auditor_jobs(run.id, cycle_id)
    for job in jobs:
        if job.status in AUDITOR_TERMINAL_JOB_STATUSES:
            continue
        job.status = 'canceled'
        job.finished_at = _utcnow()
        job.updated_at = _utcnow()
    db.session.commit()
    append_run_event(
        run,
        'auditor_jobs_updated',
        {'auditor_jobs': [job.to_dict() for job in jobs], 'cycle_id': cycle_id, 'canceled': True},
        dedupe_key=f'auditor_jobs_canceled:{run.id}:{cycle_id}:{utcnow().isoformat()}',
    )
    return jobs


def get_private_candidates(campaign_id):
    candidates = []
    
    def extract_strings(val):
        res = []
        if isinstance(val, str):
            if len(val.strip()) > 5:
                res.append(val)
        elif isinstance(val, dict):
            for v in val.values():
                res.extend(extract_strings(v))
        elif isinstance(val, list):
            for v in val:
                res.extend(extract_strings(v))
        return res

    embeddings = CampaignMemoryEmbedding.query.filter_by(campaign_id=campaign_id, visibility='dm_private').all()
    for emb in embeddings:
        if emb.canonical_text:
            candidates.append({
                'source': 'memory_embedding',
                'id': str(emb.id),
                'text': emb.canonical_text
            })
            
    world = CampaignWorld.query.filter_by(campaign_id=campaign_id).first()
    if world and world.dm_private:
        try:
            import json
            dm_priv_dict = json.loads(world.dm_private)
        except Exception:
            dm_priv_dict = {}
            
        for s in extract_strings(dm_priv_dict):
            candidates.append({
                'source': 'world_dm_private',
                'id': 'world',
                'text': s
            })
            
    npcs = NPCActor.query.filter_by(campaign_id=campaign_id).all()
    for npc in npcs:
        if npc.dossier:
            try:
                import json
                dos_dict = json.loads(npc.dossier)
            except Exception:
                dos_dict = {}
            for s in extract_strings(dos_dict):
                candidates.append({
                    'source': 'npc_dossier',
                    'id': npc.actor_id,
                    'text': s
                })
                
    clocks = CampaignClock.query.filter_by(campaign_id=campaign_id).all()
    for clock in clocks:
        if clock.visibility == 'dm_private':
            if clock.trigger:
                candidates.append({
                    'source': 'clock_trigger',
                    'id': clock.clock_id,
                    'text': clock.trigger
                })
            if clock.on_complete:
                candidates.append({
                    'source': 'clock_on_complete',
                    'id': clock.clock_id,
                    'text': clock.on_complete
                })
                
    return candidates


def normalize_and_tokenize(text):
    if not text:
        return []
    import re
    text = text.lower()
    text = re.sub(r'[^a-z0-9\s]', '', text)
    tokens = text.split()
    stopwords = {"the", "and", "a", "of", "to", "in", "is", "that", "it", "he", "was", "for", "on", "are", "as", "with", "his", "they", "i", "at", "be", "this", "have", "from", "or", "one", "had", "by", "word", "but", "not", "what"}
    meaningful = [t for t in tokens if len(t) >= 3 and t not in stopwords]
    return meaningful


def get_ngrams(tokens, n):
    if len(tokens) < n:
        return set()
    return set(zip(*(tokens[i:] for i in range(n))))


def check_leak_status(private_text, visible_text):
    if not private_text or not visible_text:
        return 'none'
        
    private_meaningful = normalize_and_tokenize(private_text)
    visible_meaningful = normalize_and_tokenize(visible_text)
    
    if len(private_meaningful) >= 6:
        priv_6grams = get_ngrams(private_meaningful, 6)
        vis_6grams = get_ngrams(visible_meaningful, 6)
        if priv_6grams.intersection(vis_6grams):
            return 'confirmed_exact_leak'
                
    if len(private_meaningful) >= 12:
        priv_5grams = get_ngrams(private_meaningful, 5)
        vis_5grams = get_ngrams(visible_meaningful, 5)
        if priv_5grams and vis_5grams:
            overlap = len(priv_5grams.intersection(vis_5grams)) / len(priv_5grams)
            if overlap >= 0.35:
                return 'likely_leak'
                
    import re
    proper_nouns = set(re.findall(r'\b[A-Z][a-zA-Z0-9_]{2,}\b', private_text))
    common_proper = {"DM", "Player", "D&D", "DnD", "The", "He", "She", "It", "They", "You", "This", "That"}
    proper_nouns = proper_nouns - common_proper
    if len(proper_nouns) >= 2:
        matched_proper = [pn for pn in proper_nouns if pn in visible_text]
        if len(matched_proper) >= 2:
            return 'possible_overlap'
            
    return 'none'


def get_model_context_exposure(run, cycle):
    dm_message = db.session.get(SessionMessage, cycle.dm_message_id) if cycle.dm_message_id else None
    if not dm_message:
        return {
            'dm_message_id': cycle.dm_message_id,
            'private_memory_available_to_model': False,
            'private_memory_available_in_state': False,
            'private_memory_sent_to_model': False,
            'private_memory_revealed_visibly': False,
            'private_memory_items': [],
            'visible_leak_check': {
                'performed': False,
                'status': 'none',
                'max_leak_status': 'none',
                'severity': 'pass',
                'matched_private_items_in_visible_text': []
            }
        }
        
    turn = SessionDmTurn.query.filter_by(dm_message_id=cycle.dm_message_id).first()
    trace_id = turn.trace_id if turn else None
    
    pcs = find_provider_calls_for_turn(run.id, trace_id)
    private_candidates = get_private_candidates(run.derived_campaign_id)
    
    private_items = []
    matched_leaks = []
    
    for candidate in private_candidates:
        sent = False
        for pc in pcs:
            req_str = json.dumps(pc.request_json or {})
            if candidate['text'] in req_str:
                sent = True
                break
                
        leak_status = check_leak_status(candidate['text'], dm_message.content)
        
        item = {
            'item_id': candidate['id'],
            'kind': candidate['source'],
            'sent_to_model': sent,
            'eligible_for_visible_reveal': False
        }
        private_items.append(item)
        
        if leak_status != 'none':
            matched_leaks.append({
                'item_id': candidate['id'],
                'kind': candidate['source'],
                'leak_status': leak_status
            })
            
    max_leak_status = 'none'
    if any(l['leak_status'] == 'confirmed_exact_leak' for l in matched_leaks):
        max_leak_status = 'confirmed_exact_leak'
    elif any(l['leak_status'] == 'likely_leak' for l in matched_leaks):
        max_leak_status = 'likely_leak'
    elif any(l['leak_status'] == 'possible_overlap' for l in matched_leaks):
        max_leak_status = 'possible_overlap'

    severity = 'pass'
    if max_leak_status == 'confirmed_exact_leak':
        severity = 'fail'
    elif max_leak_status in ('likely_leak', 'possible_overlap'):
        severity = 'warn'

    private_memory_available_in_state = len(private_candidates) > 0
    private_memory_sent_to_model = any(item['sent_to_model'] for item in private_items)
    private_memory_revealed_visibly = max_leak_status != 'none'
        
    return {
        'dm_message_id': cycle.dm_message_id,
        'private_memory_available_to_model': private_memory_available_in_state,
        'private_memory_available_in_state': private_memory_available_in_state,
        'private_memory_sent_to_model': private_memory_sent_to_model,
        'private_memory_revealed_visibly': private_memory_revealed_visibly,
        'private_memory_items': private_items,
        'visible_leak_check': {
            'performed': True,
            'status': max_leak_status,
            'max_leak_status': max_leak_status,
            'severity': severity,
            'matched_private_items_in_visible_text': matched_leaks
        }
    }


def get_memory_retrieval_summary(run, cycle):
    turn = SessionDmTurn.query.filter_by(dm_message_id=cycle.dm_message_id).first()
    trace_id = turn.trace_id if turn else None
    
    events = []
    if trace_id:
        events = CampaignAuditEvent.query.filter_by(
            campaign_id=run.derived_campaign_id,
            trace_id=trace_id,
            event_type='dm_tool_execution'
        ).all()
    else:
        events = CampaignAuditEvent.query.filter_by(
            campaign_id=run.derived_campaign_id,
            event_type='dm_tool_execution'
        ).order_by(CampaignAuditEvent.id.desc()).limit(10).all()
        
    retrievals = []
    for ev in events:
        try:
            payload = json.loads(ev.payload) if isinstance(ev.payload, str) else (ev.payload or {})
        except Exception:
            payload = {}
        if payload.get('tool_name') == 'search_campaign_memory':
            args = payload.get('arguments') or {}
            query = args.get('query')
            result = payload.get('result') or {}
            matches = result.get('matches') or []
            
            top_match_ids = [m.get('item_id') or m.get('id') for m in matches[:3]]
            match_kinds = [m.get('kind') for m in matches[:3]]
            
            private_results_present = any(m.get('visibility') == 'dm_private' for m in matches)
            top_match_relevant = len(matches) > 0
            
            retrievals.append({
                'event_id': ev.id,
                'tool_name': 'search_campaign_memory',
                'query': query,
                'top_match_ids': top_match_ids,
                'match_kinds': match_kinds,
                'contains_private_results': private_results_present,
                'contains_public_results': any(m.get('visibility') != 'dm_private' for m in matches),
                'top_match_relevant': top_match_relevant,
                'recommended_detail_paths': [
                    'payload.arguments',
                    'payload.result.matches',
                    'payload.tool_name',
                    'payload.mutated'
                ]
            })
    return retrievals


def get_evidence_gaps(run, cycle):
    gaps = []
    
    latest_session = latest_session_for_run(run)
    if not latest_session or not latest_session.running_summary:
        gaps.append({
            'criterion_id': 'running_summary_quality',
            'reason': 'running_summary is null at this checkpoint',
            'severity': 'not_applicable_for_phase' if cycle.phase == 'after_player' else 'warn'
        })
        
    turn = SessionDmTurn.query.filter_by(dm_message_id=cycle.dm_message_id).first()
    trace_id = turn.trace_id if turn else None
    pcs = find_provider_calls_for_turn(run.id, trace_id)
    if not pcs:
        gaps.append({
            'criterion_id': 'audit_evidence_quality',
            'reason': 'no provider call artifacts available for this turn',
            'severity': 'warn'
        })
        
    has_memory_event = False
    if trace_id:
        has_memory_event = CampaignAuditEvent.query.filter_by(
            campaign_id=run.derived_campaign_id,
            trace_id=trace_id,
            event_type='memory_patch'
        ).first() is not None
        if not has_memory_event:
            has_memory_event = CampaignAuditEvent.query.filter(
                CampaignAuditEvent.campaign_id == run.derived_campaign_id,
                CampaignAuditEvent.trace_id == trace_id,
                CampaignAuditEvent.event_type == 'dm_tool_execution',
                CampaignAuditEvent.payload.like('%memory%')
            ).first() is not None
            
    if not has_memory_event:
        gaps.append({
            'criterion_id': 'memory_selection_quality',
            'reason': 'no durable memory write or memory patch observed yet',
            'severity': 'not_applicable_for_phase'
        })
        
    has_clock_event = False
    if trace_id:
        has_clock_event = CampaignAuditEvent.query.filter(
            CampaignAuditEvent.campaign_id == run.derived_campaign_id,
            CampaignAuditEvent.trace_id == trace_id,
            CampaignAuditEvent.payload.like('%clock%')
        ).first() is not None
    if not has_clock_event:
        gaps.append({
            'criterion_id': 'clock_memory_alignment',
            'reason': 'no clock update event observed for this turn',
            'severity': 'not_applicable_for_phase'
        })
        
    return gaps


def get_current_audit_bundle_data(run):
    cycle = db.session.get(AutomationRunAuditCycle, run.awaiting_audit_cycle_id) if run.awaiting_audit_cycle_id else None
    if cycle is None:
        return {'error': 'Run is not currently paused at an audit cycle.'}
        
    evidence_packet = _cycle_evidence_packet(run, cycle, {})
    template = current_scorecard_template_for_run(run)
    
    recommended_detail_paths = []
    for ev in evidence_packet.get('recent_audit_events') or []:
        recommended_detail_paths.append({
            'tool': 'get_audit_event_detail',
            'args': {
                'event_id': ev.get('id'),
                'paths': ['payload.arguments', 'payload.result.matches', 'payload.tool_name', 'payload.mutated']
            }
        })
        
    gaps = get_evidence_gaps(run, cycle)
    retrievals = get_memory_retrieval_summary(run, cycle)
    exposure = get_model_context_exposure(run, cycle)
    
    return redact_secrets({
        'manifest_version': 'audit_bundle_v1',
        'run': {
            'id': run.id,
            'status': run.status,
            'awaiting_audit_cycle_id': run.awaiting_audit_cycle_id,
            'awaiting_audit_phase': run.awaiting_audit_phase,
            'derived_campaign_id': run.derived_campaign_id,
            'worker_id': run.worker_id,
            'scorecard_summary': run.scorecard_summary_json or {},
        },
        'audit_cycle': {
            'id': cycle.id,
            'cycle_number': cycle.cycle_number,
            'phase': cycle.phase,
            'status': cycle.status
        },
        'scorecard_template': {
            'id': template.get('id'),
            'schema_version': template.get('schema_version'),
            'name': template.get('name'),
            'criteria': [
                {
                    'id': c.get('id'),
                    'category': get_criterion_category(c),
                    'raw_category': c.get('category'),
                }
                for c in _json_list(template.get('criteria'), []) if c.get('id')
            ],
            'configuration': scorecard_configuration(template),
        },
        'evidence_packet': evidence_packet,
        'recommended_detail_paths': recommended_detail_paths,
        'evidence_gaps': gaps,
        'memory_retrieval_summary': retrievals,
        'model_context_exposure': exposure,
        'redaction_policy': {
            'private_memory_text_redacted': True,
            'provider_request_messages_redacted': True
        }
    })

"""Post-turn durable-consistency reconciliation for session DM turns.

Authoritative ordering for a session DM turn:

    1. memory compile + apply      (graph, scene, structured state)
    2. memory commit
    3. clock adjudication + apply  (clock progress, clock descriptions)
    4. clock commit
    5. running-summary finalization against committed post-clock state
       (done by the summary finalizer, NOT by string-patching heuristics)
    6. final consistency check + terminal revision

This module owns the final consistency check. It:

* supersedes an active clock when the committed scene state places its subject
  at a different location than the clock description asserts, or when the
  subject's danger condition has affirmatively resolved according to committed
  facts;
* treats clocks that name more than one known subject as ambiguous and never
  retires them on an arbitrary first match;
* gates all evidence by visibility so private facts can never leak into
  party-facing clock/event text;
* emits one correlated terminal revision across memory, clock, summary, and
  scene-state commits.

The running summary itself is a derived narrative projection of the committed
structured state and is authored after the clock commits, so it cannot encode a
pre-adjudication value. Any contradiction that cannot be deterministically
repaired raises :class:`PostTurnConsistencyIncident`, so the turn is never
declared cleanly ``complete`` while durable surfaces disagree.
"""

import re

from models import (
    CampaignClock,
    CampaignWorld,
    NPCActor,
)
from services.audit_service import log_audit_event
from services.dm_tools import (
    _record_event,
    _world_json,
)
from services.world_service import clean_id, clean_text


_ACTIVE_CLOCK_STATUSES = frozenset({'active', 'ticking', 'pending', 'completion_pending'})

# Generic in-world location words used to detect a clock description's asserted
# location binding when that location is not a graph entity.
LOCATION_WORDS = frozenset({
    'water', 'sea', 'ocean', 'river', 'lake', 'pond', 'stream', 'creek',
    'dock', 'pier', 'wharf', 'harbor', 'harbour', 'shore', 'beach', 'bay',
    'cliff', 'chasm', 'canyon', 'gorge', 'pit', 'tower', 'bridge', 'road',
    'street', 'alley', 'square', 'market', 'hall', 'tavern', 'inn',
    'cave', 'cavern', 'tunnel', 'cell', 'dungeon', 'crypt', 'catacomb',
    'forest', 'woods', 'grove', 'swamp', 'marsh', 'field', 'hill', 'mountain',
    'desert', 'wasteland', 'plaza', 'courtyard', 'gate', 'wall',
})

# Danger-condition terms a clock description may assert about its subject.
CONDITION_WORDS = frozenset({
    'unconscious', 'dying', 'drowning', 'drowned', 'bleeding out', 'bleeding',
    'unstable', 'poisoned', 'restrained', 'pinned', 'trapped', 'imprisoned',
    'captured', 'ensnared', 'cornered', 'caged', 'chained', 'wounded',
    'knocked out', 'at 0 hp', '0 hp',
})

# Committed-state words that signal a subject's danger condition has resolved.
# Pure relocation phrases (e.g. 'on the dock', 'out of the water') are handled
# by the location-conflict path and deliberately excluded here so a fact like
# "Mira lies on the dock, not breathing" cannot count as resolution.
RESOLUTION_WORDS = frozenset({
    'stabilized', 'rescued', 'saved', 'revived', 'recovered', 'awake',
    'conscious', 'safe', 'healed', 'cured', 'freed', 'escaped', 'alive',
    'breathing', 'no longer', 'pulled to safety',
})

# Negation markers that flip a following resolution phrase (e.g. "not safe").
_NEGATION_MARKERS = frozenset({
    'not', 'no', 'never', 'cannot', "can't", "isn't", "aren't", "wasn't",
    "weren't", "don't", "doesn't", "didn't", "won't", 'without', 'nor',
})

# Persistence markers that introduce a still-active condition (e.g. "still
# drowning", "remains poisoned"), contradicting any resolution claim.
_PERSISTENCE_MARKERS = frozenset({
    'still', 'yet', 'remains', 'remaining', 'continues', 'ongoing', 'meanwhile',
})

# Past-tense / historical markers that place a condition mention in a
# historical clause (e.g. "was unconscious but is now conscious").
_HISTORICAL_MARKERS = frozenset({
    'was', 'were', 'had', 'been', 'used', 'once', 'previously', 'earlier',
    'formerly', 'before',
})

_NEGATION_WINDOW = 3
_HISTORICAL_WINDOW = 4

class PostTurnConsistencyIncident(Exception):
    """Raised when durable surfaces disagree and the contradiction cannot be
    deterministically repaired. Carries an actionable report."""

    def __init__(self, summary, report, terminal_revision=None):
        super().__init__(summary)
        self.summary = str(summary)
        self.report = report if isinstance(report, dict) else {}
        self.terminal_revision = terminal_revision

    def to_dict(self):
        return {
            'summary': self.summary,
            'terminal_revision': self.terminal_revision,
            'report': self.report,
        }


def _lower_words(text):
    if not text:
        return set()
    return set(re.findall(r"[a-z0-9']+", str(text).lower().replace('_', ' ')))


def _tokenize(text):
    """Lowercase, word-boundary token list (word chars and apostrophes)."""
    return re.findall(r"[a-z0-9']+", str(text).lower().replace('_', ' '))


def _contains_phrase(text, phrase):
    """True when ``phrase`` appears as a contiguous, whole-word sequence inside
    ``text``. Substring matches inside larger words never count (e.g. 'sea' in
    'season', 'conscious' in 'unconscious')."""
    if not phrase:
        return False
    phrase_tokens = _tokenize(phrase)
    if not phrase_tokens:
        return False
    tokens = _tokenize(text)
    if len(tokens) < len(phrase_tokens):
        return False
    for index in range(len(tokens) - len(phrase_tokens) + 1):
        if tokens[index:index + len(phrase_tokens)] == phrase_tokens:
            return True
    return False


def _visibility_allows(source_visibility, required_visibility):
    """True when an evidence source at ``source_visibility`` may drive a durable
    change surfaced at ``required_visibility``. Private evidence must never be
    copied into party-facing clock or event text."""
    source_visibility = source_visibility if source_visibility in {'public', 'party_known', 'dm_private'} else 'dm_private'
    required_visibility = required_visibility if required_visibility in {'public', 'party_known', 'dm_private'} else 'dm_private'
    if required_visibility == 'public':
        return source_visibility == 'public'
    if required_visibility == 'party_known':
        return source_visibility in {'party_known', 'public'}
    return True


def _subject_index(campaign, graph):
    """Map known entity/NPC id -> display name for subjects referenced by clocks.

    Location-type graph entities are deliberately excluded: a clock is not
    relocated when its *subject* (a creature or thing) moves; only creatures
    and things carry a location binding.
    """
    index = {}
    if isinstance(graph, dict):
        for entity in graph.get('entities', []):
            if not isinstance(entity, dict):
                continue
            if clean_id(entity.get('type'), '') == 'location':
                continue
            entity_id = clean_id(entity.get('id'), '')
            name = clean_text(entity.get('name'), 200)
            if entity_id and name:
                index[entity_id] = name
    for npc in NPCActor.query.filter_by(campaign_id=campaign.id).all():
        actor_id = clean_id(npc.actor_id, '')
        name = clean_text(npc.name, 200)
        if actor_id and name:
            index[actor_id] = name
    return index


def _location_index(graph):
    """Map location entity id -> display name."""
    index = {}
    if not isinstance(graph, dict):
        return index
    for entity in graph.get('entities', []):
        if not isinstance(entity, dict):
            continue
        if clean_id(entity.get('type'), '') != 'location':
            continue
        entity_id = clean_id(entity.get('id'), '')
        name = clean_text(entity.get('name'), 200)
        if entity_id and name:
            index[entity_id] = {'name': name}
    return index


def _clock_text(clock):
    return ' '.join(
        part for part in (clock.name, clock.summary)
        if part
    )


def _clock_subject_ids(clock, subject_index):
    text = _clock_text(clock)
    if not text:
        return []
    matched = []
    for subject_id, name in subject_index.items():
        if not name:
            continue
        if _contains_phrase(text, name):
            matched.append(subject_id)
    return matched


def _subject_committed_location(campaign, graph, world_state, subject_id, min_visibility='dm_private'):
    """Determine where committed state places a subject.

    Evidence used to derive a committed location must be at least as visible as
    ``min_visibility`` so a private fact can never be copied into a
    party-facing clock/event surface.

    Returns (status, location_ref, evidence):
        status: 'at_scene' | 'departed' | 'from_facts' | 'ambiguous' | 'unknown'
        location_ref: location id (or None)
        evidence: description for audit
    """
    current_scene = world_state.get('current_scene', {}) if isinstance(world_state, dict) else {}
    active_ids = current_scene.get('active_npc_ids', []) if isinstance(current_scene.get('active_npc_ids'), list) else []
    departed_ids = current_scene.get('departed_npc_ids', []) if isinstance(current_scene.get('departed_npc_ids'), list) else []

    if subject_id in active_ids:
        loc_id = clean_id(current_scene.get('location_id'), '')
        return 'at_scene', loc_id, 'subject is in the current scene cast'
    if subject_id in departed_ids:
        return 'departed', None, 'subject is in the current scene departed cast'

    location_index = _location_index(graph)
    committed_locations = set()
    evidence = []
    if isinstance(graph, dict):
        for fact in graph.get('facts', []):
            if not isinstance(fact, dict):
                continue
            entity_ids = fact.get('entity_ids', []) if isinstance(fact.get('entity_ids'), list) else []
            if subject_id not in entity_ids:
                continue
            fact_visibility = clean_text(fact.get('visibility'), 30) or 'dm_private'
            if not _visibility_allows(fact_visibility, min_visibility):
                continue
            text = clean_text(fact.get('text'), 700)
            if not text:
                continue
            for loc_id, loc in location_index.items():
                if not _contains_phrase(text, loc['name']):
                    continue
                committed_locations.add(loc_id)
                evidence.append({
                    'fact': text[:240],
                    'location_id': loc_id,
                    'location_name': loc['name'],
                })
    if not committed_locations:
        return 'unknown', None, []
    if len(committed_locations) > 1:
        return 'ambiguous', None, evidence
    loc_id = next(iter(committed_locations))
    return 'from_facts', loc_id, evidence


def _clock_asserted_locations(clock, location_index):
    """Return (known_location_ids, generic_location_words) asserted by a clock."""
    text = _clock_text(clock)
    known_ids = []
    for loc_id, loc in location_index.items():
        if _contains_phrase(text, loc['name']):
            known_ids.append(loc_id)
    generic_words = sorted(word for word in LOCATION_WORDS if _contains_phrase(text, word))
    return known_ids, generic_words


def _clock_location_conflict(campaign, graph, world_state, clock, subject_id):
    """Return (reason, kind) when the clock's location binding contradicts the
    committed location of its subject, else (None, None). kind is
    'conflict' or 'ambiguous'."""
    location_index = _location_index(graph)
    known_ids, generic_words = _clock_asserted_locations(clock, location_index)

    # A clock with no location claim cannot contradict a subject's location.
    # This matters for subjects that legitimately span multiple places, such
    # as an infrastructure network: facts may mention both the district it
    # serves and one of its component sites without relocating the network.
    if not known_ids and not generic_words:
        return None, None

    status, loc_ref, evidence = _subject_committed_location(
        campaign,
        graph,
        world_state,
        subject_id,
        min_visibility=clock.visibility or 'dm_private',
    )

    if status == 'departed':
        if known_ids or generic_words:
            return (
                f'Clock subject is committed as departed from the current scene but '
                f'the clock remains active at {", ".join(generic_words or known_ids) or "a location"}.',
                'conflict',
            )
        return None, None

    if status in ('at_scene', 'from_facts'):
        committed_loc_id = loc_ref
        if status == 'at_scene':
            current_scene = world_state.get('current_scene', {}) if isinstance(world_state, dict) else {}
            committed_name = clean_text(current_scene.get('location_name'), 200)
        else:
            loc = location_index.get(committed_loc_id) or {}
            committed_name = loc.get('name', '')
        committed_words = _lower_words(committed_name or committed_loc_id or '')

        # A known location entity asserted by the clock that differs from the
        # committed location is a direct contradiction.
        conflicting_known = [
            loc_id for loc_id in known_ids
            if loc_id != committed_loc_id
        ]
        if conflicting_known:
            names = ', '.join(
                (location_index.get(loc_id) or {}).get('name', loc_id)
                for loc_id in conflicting_known
            )
            return (
                f'Clock description asserts subject at {names} but committed scene '
                f'state places the subject at {committed_name or committed_loc_id}.',
                'conflict',
            )

        # Generic words such as "lake" describe a clock's pressure or an
        # infrastructure system's domain; they do not establish that the clock
        # subject itself moved. Only a mismatch against a canonical location is
        # safe enough to supersede a durable clock automatically.
        return None, None

    if status == 'ambiguous':
        return (
            'Committed state places the clock subject at multiple different '
            'locations: ' + '; '.join(item['fact'] for item in (evidence or [])[:3]),
            'ambiguous',
        )
    return None, None


def _negated_before(tokens, index):
    """True when a negation marker appears in the token window before
    ``index``, meaning the resolution phrase at ``index`` is negated
    (e.g. "Mira is not safe")."""
    for token in tokens[max(0, index - _NEGATION_WINDOW):index]:
        if token in _NEGATION_MARKERS:
            return True
    return False


def _condition_is_historical(tokens, condition_index, resolution_indices):
    """True when a condition mention is part of an explicit historical transition
    to resolution, e.g. 'was drowning but has been rescued': the condition sits
    in a past-tense clause AND an un-negated resolution phrase follows it in the
    same fact."""
    start = max(0, condition_index - _HISTORICAL_WINDOW)
    has_past_marker = any(
        token in _HISTORICAL_MARKERS
        for token in tokens[start:condition_index]
    )
    if not has_past_marker:
        return False
    return any(resolution_index > condition_index for resolution_index in resolution_indices)


def _fact_asserts_resolution(fact_text):
    """Return True only when ``fact_text`` affirmatively records that a danger
    condition resolved.

    Fail-closed: a condition word that is still currently asserted (not negated
    or ceased, and not part of an explicit historical transition to resolution)
    means the danger is ongoing and the fact is NOT resolution. Examples that
    are NOT resolution: 'alive but drowning', 'conscious but poisoned', 'not
    safe', 'alive but still drowning'. Examples that ARE resolution: 'stabilized
    and now conscious', 'was unconscious but is now conscious', 'was drowning
    but has been rescued', 'no longer drowning'."""
    if not fact_text:
        return False
    tokens = _tokenize(fact_text)
    if not tokens:
        return False

    # A persistence marker directly introducing a condition word means the
    # danger is ongoing, so the fact does not record resolution.
    for index, token in enumerate(tokens):
        if token not in _PERSISTENCE_MARKERS:
            continue
        following = ' '.join(tokens[index + 1:index + 4])
        if any(_contains_phrase(following, condition) for condition in CONDITION_WORDS):
            return False

    # Locate un-negated resolution phrase starts so historical transitions can
    # be distinguished from currently asserted conditions.
    resolution_indices = []
    for phrase in RESOLUTION_WORDS:
        phrase_tokens = _tokenize(phrase)
        if not phrase_tokens:
            continue
        for index in range(len(tokens) - len(phrase_tokens) + 1):
            if tokens[index:index + len(phrase_tokens)] != phrase_tokens:
                continue
            if _negated_before(tokens, index):
                continue
            resolution_indices.append(index)

    # Any condition word that is still affirmatively asserted (not negated, not
    # an explicit historical transition) contradicts resolution.
    for condition in CONDITION_WORDS:
        condition_tokens = _tokenize(condition)
        if not condition_tokens:
            continue
        for index in range(len(tokens) - len(condition_tokens) + 1):
            if tokens[index:index + len(condition_tokens)] != condition_tokens:
                continue
            if _negated_before(tokens, index):
                continue
            if _condition_is_historical(tokens, index, resolution_indices):
                continue
            return False

    return bool(resolution_indices)


def _clock_condition_resolved(campaign, graph, clock, subject_id):
    """Return a reason string when committed facts affirmatively show the
    subject's danger condition (asserted by the clock) has resolved, else None.
    Only visibility-safe facts that record an un-negated, uncontradicted
    resolution may retire the clock."""
    text = _clock_text(clock)
    if not any(_contains_phrase(text, word) for word in CONDITION_WORDS):
        return None
    if not isinstance(graph, dict):
        return None
    for fact in graph.get('facts', []):
        if not isinstance(fact, dict):
            continue
        entity_ids = fact.get('entity_ids', []) if isinstance(fact.get('entity_ids'), list) else []
        if subject_id not in entity_ids:
            continue
        fact_visibility = clean_text(fact.get('visibility'), 30) or 'dm_private'
        if not _visibility_allows(fact_visibility, clock.visibility or 'dm_private'):
            continue
        fact_text = clean_text(fact.get('text'), 700)
        if not fact_text:
            continue
        if _fact_asserts_resolution(fact_text):
            return (
                f'Committed facts record that the clock subject\'s condition '
                f'has resolved: "{clean_text(fact.get("text"), 240)}".'
            )
    return None


def _supersede_clock(campaign, clock, reason, kind, trace_id=None, parent_trace_id=None):
    """Deterministically retire an active clock whose description no longer
    matches committed scene state."""
    from time_utils import utcnow

    clock.status = 'superseded'
    clock.summary = clean_text(
        f'{clock.summary} [Superseded: {reason}]' if clock.summary else f'Superseded: {reason}',
        420,
    )
    clock.updated_at = utcnow()
    event = _record_event(
        campaign,
        'clock_superseded',
        f'Clock {clock.name} superseded: {reason}',
        {
            'clock_id': clock.clock_id,
            'reason': reason,
            'reconciliation_kind': kind,
        },
        visibility=clock.visibility or 'dm_private',
    )
    return {'clock_id': clock.clock_id, 'kind': kind, 'reason': reason, 'event_id': event.id}


def repair_post_turn_clocks(
    campaign,
    session,
    *,
    player_message_id=None,
    dm_message_id=None,
    trace_id=None,
    parent_trace_id=None,
    trace_label=None,
    bump_revision=True,
):
    """Mutating phase of the final consistency check: supersede active clocks
    whose committed state shows subject relocation or resolved danger, and emit
    the correlated terminal revision. Must run BEFORE the running summary is
    finalized so the summary is authored against repaired clock state.

    Returns a report dict. Raises :class:`PostTurnConsistencyIncident` when a
    contradiction cannot be deterministically repaired.
    """
    world, graph, world_state, _private = _world_json(campaign)
    if world is None:
        world = CampaignWorld.query.filter_by(campaign_id=campaign.id).first()
    if world is None:
        # No durable world package exists, so there are no committed surfaces to
        # contradict; repair is a benign no-op rather than an incident.
        return {
            'clocks_superseded': [],
            'checks': [{'id': 'no_world_package', 'status': 'consistent'}],
            'verified': True,
            'terminal_revision': None,
        }

    audit_context = {
        'trace_id': trace_id,
        'parent_trace_id': parent_trace_id,
        'trace_label': trace_label or 'post_turn_consistency',
    }

    # One correlated terminal revision for this post-turn finalization. The
    # bump happens up front so incident reports also carry the correlated value.
    # When ``bump_revision`` is False (memory failed this turn), the revision is
    # left untouched so a stored failed memory patch with the pre-failure
    # base_memory_revision remains retryable without tripping the stale check.
    from time_utils import utcnow
    if bump_revision:
        world.memory_revision = (world.memory_revision or 0) + 1
        world.updated_at = utcnow()
    terminal_revision = world.memory_revision or 0

    report = {
        'clocks_superseded': [],
        'checks': [],
        'verified': True,
        'terminal_revision': terminal_revision,
    }

    subject_index = _subject_index(campaign, graph)
    location_index = _location_index(graph)
    clocks = CampaignClock.query.filter_by(campaign_id=campaign.id).order_by(CampaignClock.id.asc()).all()

    def check(identity, status, detail=None):
        report['checks'].append({
            'id': identity,
            'status': status,
            'detail': detail,
        })
        if status == 'inconsistent':
            report['verified'] = False

    # ---- Clock subject location / condition repair ----
    for clock in clocks:
        if (clock.status or 'active') not in _ACTIVE_CLOCK_STATUSES:
            continue
        subject_ids = _clock_subject_ids(clock, subject_index)
        if not subject_ids:
            continue
        if len(subject_ids) > 1:
            # A clock that names more than one known subject has no structured
            # identity to make one binding authoritative. Supersession is a
            # durable destructive action, so never retire it based on an
            # arbitrary first match: treat it as ambiguous and leave it active.
            check('clock_subject_identity', 'ambiguous', {
                'clock_id': clock.clock_id,
                'subject_ids': subject_ids,
                'reason': 'Clock references multiple subjects; refusing to supersede on an arbitrary match.',
            })
            continue
        subject_id = subject_ids[0]
        location_reason, location_kind = _clock_location_conflict(campaign, graph, world_state, clock, subject_id)
        condition_reason = _clock_condition_resolved(campaign, graph, clock, subject_id)
        if location_kind == 'ambiguous':
            # Unresolvable ambiguity -> actionable incident.
            raise PostTurnConsistencyIncident(
                f'Clock {clock.name} subject relocation is ambiguous; refusing to reconcile.',
                dict(report, checks=report['checks'], clocks_superseded=report['clocks_superseded']),
                terminal_revision=terminal_revision,
            )
        if location_reason:
            change = _supersede_clock(
                campaign, clock, location_reason, 'subject_relocated',
                trace_id=trace_id, parent_trace_id=parent_trace_id,
            )
            report['clocks_superseded'].append(change)
            check('clock_subject_location', 'reconciled', change)
            continue
        if condition_reason:
            change = _supersede_clock(
                campaign, clock, condition_reason, 'condition_resolved',
                trace_id=trace_id, parent_trace_id=parent_trace_id,
            )
            report['clocks_superseded'].append(change)
            check('clock_condition_resolution', 'reconciled', change)

    if report['clocks_superseded']:
        log_audit_event(
            campaign.id,
            'post_turn_consistency_reconciled',
            'Post-turn reconciliation repaired durable surface drift.',
            {
                'player_message_id': player_message_id,
                'dm_message_id': dm_message_id,
                'report': report,
            },
            source='post_turn_consistency',
            actor='session_memory_writer',
            trace_id=trace_id,
            parent_trace_id=parent_trace_id,
            trace_label=audit_context['trace_label'],
            audit_role='tools',
            commit=False,
        )

    return report


def verify_post_turn_state(
    campaign,
    session,
    *,
    player_message_id=None,
    dm_message_id=None,
    trace_id=None,
    parent_trace_id=None,
    trace_label=None,
    summary_text=None,
    summary_context=None,
):
    """Read-only phase of the final consistency check, run AFTER the running
    summary has been finalized. It never mutates durable state. It semantically
    verifies the finalized summary against the authoritative committed
    clock/scene/fact state, and re-validates that no active clock's description
    contradicts committed scene state. Any remaining contradiction raises
    :class:`PostTurnConsistencyIncident` so the turn is not reported complete.

    Returns a report dict with a ``verified`` flag.
    """
    world, graph, world_state, _private = _world_json(campaign)
    if world is None:
        return {
            'checks': [{'id': 'no_world_package', 'status': 'consistent'}],
            'verified': True,
        }

    report = {
        'checks': [],
        'verified': True,
    }

    def check(identity, status, detail=None):
        report['checks'].append({
            'id': identity,
            'status': status,
            'detail': detail,
        })
        if status == 'inconsistent':
            report['verified'] = False

    # The finalized running summary must not contradict the authoritative
    # committed clock/scene/fact state. Fail closed if it cannot be verified.
    if summary_text:
        from openrouter import get_session_summary_consistency_check
        verdict = get_session_summary_consistency_check(
            summary_text,
            summary_context or {},
            audit_context={
                'campaign_id': campaign.id,
                'trace_id': trace_id,
                'parent_trace_id': parent_trace_id,
                'trace_label': trace_label or 'post_turn_consistency: summary verification',
            },
        )
        if verdict is None:
            raise PostTurnConsistencyIncident(
                'Running summary consistency verification failed; refusing to mark the turn complete.',
                dict(report, checks=report['checks']),
            )
        if not verdict.get('consistent', True):
            contradictions = verdict.get('contradictions') or ['running summary contradicts committed state']
            raise PostTurnConsistencyIncident(
                'Running summary contradicts committed state: ' + '; '.join(contradictions[:3]),
                dict(report, checks=report['checks'], contradictions=contradictions),
            )
        check('summary_consistency', 'consistent')

    subject_index = _subject_index(campaign, graph)
    location_index = _location_index(graph)
    clocks = CampaignClock.query.filter_by(campaign_id=campaign.id).order_by(CampaignClock.id.asc()).all()

    for clock in clocks:
        if (clock.status or 'active') not in _ACTIVE_CLOCK_STATUSES:
            continue
        subject_ids = _clock_subject_ids(clock, subject_index)
        if not subject_ids:
            continue
        if len(subject_ids) > 1:
            check('clock_subject_identity', 'ambiguous', {
                'clock_id': clock.clock_id,
                'subject_ids': subject_ids,
                'reason': 'Clock references multiple subjects; refusing to supersede on an arbitrary match.',
            })
            continue
        subject_id = subject_ids[0]
        location_reason, location_kind = _clock_location_conflict(campaign, graph, world_state, clock, subject_id)
        condition_reason = _clock_condition_resolved(campaign, graph, clock, subject_id)
        if location_kind == 'ambiguous':
            raise PostTurnConsistencyIncident(
                f'Clock {clock.name} subject relocation is ambiguous; refusing to verify.',
                dict(report, checks=report['checks']),
            )
        if location_reason:
            raise PostTurnConsistencyIncident(
                f'Clock {clock.name} subject location still contradicts committed scene state.',
                dict(report, checks=report['checks']),
            )
        if condition_reason:
            raise PostTurnConsistencyIncident(
                f'Clock {clock.name} danger condition is still resolved per committed facts.',
                dict(report, checks=report['checks']),
            )

    check('all_surfaces', 'consistent')
    return report


def reconcile_post_turn_state(
    campaign,
    session,
    *,
    player_message_id=None,
    dm_message_id=None,
    trace_id=None,
    parent_trace_id=None,
    trace_label=None,
    memory_result=None,
    clock_result=None,
    bump_revision=True,
):
    """Combined mutating repair + read-only verify, kept for callers that need a
    single entry point (memory-failure / summary-failure error paths and tests).
    The success path uses :func:`repair_post_turn_clocks` BEFORE summary
    finalization and :func:`verify_post_turn_state` AFTER it."""
    report = repair_post_turn_clocks(
        campaign,
        session,
        player_message_id=player_message_id,
        dm_message_id=dm_message_id,
        trace_id=trace_id,
        parent_trace_id=parent_trace_id,
        trace_label=trace_label,
        bump_revision=bump_revision,
    )
    if report.get('clocks_superseded'):
        # Repair already reconciled every detected clock; verification is a
        # no-op safety net for the combined path.
        report['verified'] = True
        return report
    verify = verify_post_turn_state(
        campaign,
        session,
        player_message_id=player_message_id,
        dm_message_id=dm_message_id,
        trace_id=trace_id,
        parent_trace_id=parent_trace_id,
        trace_label=trace_label,
    )
    report['verified'] = verify.get('verified', True)
    report['checks'].extend(verify.get('checks', []))
    return report

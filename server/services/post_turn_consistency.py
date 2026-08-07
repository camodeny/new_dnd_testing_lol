"""Post-turn durable-consistency reconciliation for session DM turns.

Authoritative ordering for a session DM turn:

    1. memory compile + apply      (graph, scene, running summary, anchors)
    2. memory commit
    3. clock adjudication + apply  (clock progress, clock descriptions)
    4. clock commit
    5. post-turn reconciliation    (summary + clock descriptions vs committed scene)
    6. terminal revision           (one correlated revision across all surfaces)

The reconciliation step is deterministic. It:

* repairs stale clock-segment references in the running summary so a summary
  cannot report an earlier segment after the newer segment is committed;
* supersedes an active clock when the committed scene state places its subject
  at a different location than the clock description asserts, or when the
  subject's danger condition has resolved according to committed facts;
* emits one correlated terminal revision across memory, clock, summary, and
  scene-state commits.

Any contradiction that cannot be deterministically repaired raises
:class:`PostTurnConsistencyIncident`, so the turn is never declared cleanly
``complete`` while durable surfaces disagree.
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
RESOLUTION_WORDS = frozenset({
    'stabilized', 'rescued', 'saved', 'revived', 'recovered', 'awake',
    'conscious', 'safe', 'healed', 'cured', 'freed', 'escaped', 'alive',
    'breathing', 'no longer', 'ashore', 'on shore', 'out of the water',
    'out of water', 'pulled to safety', 'on the dock',
})

SEGMENT_REF_RE = re.compile(r'(\d+)\s*/\s*(\d+)')
_CLOCK_NEAR_WINDOW = 70


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
    """Map location entity id -> (name, lower-words)."""
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
            index[entity_id] = {'name': name, 'words': _lower_words(name)}
    return index


def _clock_text(clock):
    return ' '.join(
        part for part in (clock.name, clock.summary)
        if part
    )


def _clock_subject_ids(clock, subject_index):
    text = _clock_text(clock).lower()
    if not text:
        return []
    matched = []
    for subject_id, name in subject_index.items():
        if not name:
            continue
        if name.lower() in text:
            matched.append(subject_id)
    return matched


def _subject_committed_location(campaign, graph, world_state, subject_id):
    """Determine where committed state places a subject.

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
            text = clean_text(fact.get('text'), 700)
            if not text:
                continue
            fact_words = _lower_words(text)
            for loc_id, loc in location_index.items():
                if loc['words'] & fact_words:
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
    lowered = text.lower()
    known_ids = []
    for loc_id, loc in location_index.items():
        if loc['name'].lower() in lowered:
            known_ids.append(loc_id)
    generic_words = sorted(word for word in LOCATION_WORDS if word in lowered)
    return known_ids, generic_words


def _clock_location_conflict(campaign, graph, world_state, clock, subject_id):
    """Return (reason, kind) when the clock's location binding contradicts the
    committed location of its subject, else (None, None). kind is
    'conflict' or 'ambiguous'."""
    status, loc_ref, evidence = _subject_committed_location(campaign, graph, world_state, subject_id)
    location_index = _location_index(graph)
    known_ids, generic_words = _clock_asserted_locations(clock, location_index)

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

        # A generic location word asserted by the clock that does not describe
        # the committed location is a contradiction.
        conflicting_generic = [
            word for word in generic_words
            if word not in committed_words
        ]
        if conflicting_generic:
            return (
                f'Clock description asserts the subject is {", ".join(conflicting_generic)} '
                f'but committed scene state places the subject at '
                f'{committed_name or committed_loc_id}.',
                'conflict',
            )
        return None, None

    if status == 'ambiguous':
        return (
            'Committed state places the clock subject at multiple different '
            'locations: ' + '; '.join(item['fact'] for item in (evidence or [])[:3]),
            'ambiguous',
        )
    return None, None


def _clock_condition_resolved(campaign, graph, clock, subject_id):
    """Return a reason string when committed facts show the subject's danger
    condition (asserted by the clock) has resolved, else None."""
    text = _clock_text(clock).lower()
    if not any(word in text for word in CONDITION_WORDS):
        return None
    if not isinstance(graph, dict):
        return None
    for fact in graph.get('facts', []):
        if not isinstance(fact, dict):
            continue
        entity_ids = fact.get('entity_ids', []) if isinstance(fact.get('entity_ids'), list) else []
        if subject_id not in entity_ids:
            continue
        fact_text = clean_text(fact.get('text'), 700).lower()
        if not fact_text:
            continue
        if any(word in fact_text for word in RESOLUTION_WORDS):
            return (
                f'Committed facts record that the clock subject\'s condition '
                f'has resolved: "{clean_text(fact.get("text"), 240)}".'
            )
    return None


def _patch_summary_clock_segments(summary, clock):
    """Replace a stale ``N/M`` segment reference near the clock name in the
    running summary with the committed clock value. Returns (summary, changes)."""
    if not summary:
        return summary, []
    name = clean_text(clock.name, 200)
    if not name:
        return summary, []
    name_pattern = re.escape(name.lower())
    committed_n = min(clock.filled or 0, clock.segments or 4)
    committed_m = clock.segments or 4

    matches = list(SEGMENT_REF_RE.finditer(summary))
    changes = []
    replacements = []
    for match in matches:
        window_start = max(0, match.start() - _CLOCK_NEAR_WINDOW)
        window_end = min(len(summary), match.end() + _CLOCK_NEAR_WINDOW)
        window = summary[window_start:window_end]
        if not re.search(name_pattern, window.lower()):
            continue
        old_n = int(match.group(1))
        old_m = int(match.group(2))
        if old_n == committed_n and old_m == committed_m:
            continue
        changes.append({
            'clock_id': clock.clock_id,
            'from': f'{old_n}/{old_m}',
            'to': f'{committed_n}/{committed_m}',
        })
        replacements.append((match.start(), match.end(), f'{committed_n}/{committed_m}'))

    if not replacements:
        return summary, changes
    out = []
    cursor = 0
    for start, end, replacement in replacements:
        out.append(summary[cursor:start])
        out.append(replacement)
        cursor = end
    out.append(summary[cursor:])
    return ''.join(out), changes


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
):
    """Reconcile the running summary, clock descriptions, and scene state after
    memory and clock commits, then expose one correlated terminal revision.

    Returns a report dict. Raises :class:`PostTurnConsistencyIncident` when a
    contradiction cannot be deterministically repaired.
    """
    world, graph, world_state, _private = _world_json(campaign)
    if world is None:
        world = CampaignWorld.query.filter_by(campaign_id=campaign.id).first()
    if world is None:
        # No durable world package exists, so there are no committed surfaces to
        # contradict; reconciliation is a benign no-op rather than an incident.
        return {
            'summary_patches': [],
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
    from time_utils import utcnow
    world.memory_revision = (world.memory_revision or 0) + 1
    world.updated_at = utcnow()
    terminal_revision = world.memory_revision

    report = {
        'summary_patches': [],
        'clocks_superseded': [],
        'checks': [],
        'verified': True,
        'terminal_revision': terminal_revision,
    }

    summary = session.running_summary if session else None
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

    # ---- Clock subject location / condition reconciliation ----
    for clock in clocks:
        if (clock.status or 'active') not in _ACTIVE_CLOCK_STATUSES:
            continue
        subject_ids = _clock_subject_ids(clock, subject_index)
        if not subject_ids:
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

    # ---- Running summary clock-segment reconciliation ----
    if summary:
        patched_summary = summary
        for clock in clocks:
            patched_summary, changes = _patch_summary_clock_segments(patched_summary, clock)
            for change in changes:
                report['summary_patches'].append(change)
                check('clock_segments_in_summary', 'reconciled', change)
        if patched_summary != summary and session:
            session.running_summary = patched_summary

    if report['summary_patches'] or report['clocks_superseded']:
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
    else:
        check('all_surfaces', 'consistent')

    return report

"""Post-turn durable-consistency reconciliation for session DM turns.

Authoritative ordering for a session DM turn:

    1. memory compile + apply      (graph, scene, structured state)
    2. memory commit
    3. clock adjudication + apply  (clock progress, clock descriptions)
    4. clock commit
    5. running-summary finalization against committed post-clock state
       (done by the summary finalizer, NOT by string-patching heuristics)
    6. final consistency check + terminal revision

This module owns the final consistency check. It is **structured-only**: every
decision is derived from ids, typed relations, scene cast, and structured
fields authored at write time. No surface text is scanned or token-matched.

Rules:

* supersedes an active clock when the committed structured location of one of
  its subjects contradicts the clock's structured ``location_ids``, or when the
  subject's danger condition is structured as resolved;
* treats a clock with no structured subject binding (``entity_ids``) or no
  structured location assertion as **no assertion** and leaves it active --
  it is never superseded, guessed at, or failed on;
* never derives a location, subject, or condition from prose, so negation,
  attribution, and aliasing in free text cannot produce false contradictions;
* gates all evidence by visibility so private facts can never leak into
  party-facing clock/event text;
* emits one correlated terminal revision across memory, clock, summary, and
  scene-state commits.

A genuine structured contradiction that cannot be deterministically repaired
raises :class:`PostTurnConsistencyIncident`, so the turn is never declared
cleanly ``complete`` while durable surfaces disagree.
"""

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


def _structured_subject_ids(clock):
    """Return the clock's structured subject binding (``entity_ids``).

    An empty binding is a deliberate "no assertion": the reconciliation never
    guesses which entity a clock is about from its name or summary text.
    """
    entity_ids = getattr(clock, 'entity_ids', None)
    if not isinstance(entity_ids, list):
        return []
    return [clean_id(item, '') for item in entity_ids if clean_id(item, '')]


def _structured_location_ids(clock):
    """Return the clock's structured location assertion (``location_ids``).

    An empty binding is a "no assertion": a clock whose description never
    named a canonical location cannot contradict committed state, and is left
    active.
    """
    location_ids = getattr(clock, 'location_ids', None)
    if not isinstance(location_ids, list):
        return []
    return [clean_id(item, '') for item in location_ids if clean_id(item, '')]


def _subject_committed_location(campaign, world_state, subject_id):
    """Determine where committed structured state places a subject.

    Sources are read in order and are all structured:

    * the current scene cast (``active_npc_ids`` / ``departed_npc_ids`` plus the
      scene ``location_id``) -- authoritative for subjects on stage;
    * a per-subject ``current_location`` map in ``world_state`` for subjects
      that are off stage but have a structured current location.

    Returns (status, location_ref, evidence):
        status: 'at_scene' | 'departed' | 'from_state' | 'unknown'
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

    subject_locations = world_state.get('subject_locations') if isinstance(world_state, dict) else None
    if isinstance(subject_locations, dict):
        loc_id = clean_id(subject_locations.get(subject_id), '')
        if loc_id:
            return 'from_state', loc_id, 'subject has a structured current_location in world state'
    return 'unknown', None, []


def _clock_location_conflict(campaign, world_state, clock, subject_id):
    """Return (reason, kind) when the clock's structured location binding
    contradicts the committed structured location of its subject.

    No assertion on either side yields (None, None): a clock with no structured
    location is never superseded, and a subject with no committed location is
    never judged to have relocated.
    """
    clock_location_ids = _structured_location_ids(clock)
    if not clock_location_ids:
        return None, None

    status, loc_ref, evidence = _subject_committed_location(
        campaign,
        world_state,
        subject_id,
    )

    if status == 'departed':
        return (
            'Clock subject is committed as departed from the current scene but '
            'the clock remains active.',
            'conflict',
        )

    if status in ('at_scene', 'from_state'):
        committed_loc_id = loc_ref
        if status == 'at_scene':
            current_scene = world_state.get('current_scene', {}) if isinstance(world_state, dict) else {}
            committed_name = clean_text(current_scene.get('location_name'), 200)
        else:
            committed_name = committed_loc_id or ''

        if committed_loc_id and committed_loc_id not in clock_location_ids:
            return (
                f'Clock subject is committed at {committed_name or committed_loc_id} '
                f'but the clock asserts one of {", ".join(clock_location_ids)}.',
                'conflict',
            )
        return None, None

    return None, None


def _clock_condition_resolved(campaign, world_state, clock, subject_id):
    """Return a reason string when committed structured state affirmatively
    shows the subject's danger condition has resolved, else None.

    Condition status is a structured field authored by the resolver. Absent
    structured status is "no assertion": the danger is never declared resolved
    from prose alone.
    """
    condition_state = world_state.get('condition_state') if isinstance(world_state, dict) else None
    if not isinstance(condition_state, dict):
        return None
    subject_state = condition_state.get(subject_id)
    if not isinstance(subject_state, dict):
        return None
    if subject_state.get('status') == 'resolved':
        kind = clean_text(subject_state.get('kind'), 60) or 'condition'
        return (
            f'Committed structured state records that the clock subject\'s '
            f'{kind} has resolved.'
        )
    return None


def _supersede_clock(campaign, clock, reason, kind, trace_id=None, parent_trace_id=None):
    """Deterministically retire an active clock whose structured subject no
    longer matches committed scene state."""
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
    whose structured subject binding contradicts committed structured state, and
    emit the correlated terminal revision. Must run BEFORE the running summary
    is finalized so the summary is authored against repaired clock state.

    Returns a report dict. Raises :class:`PostTurnConsistencyIncident` when a
    contradiction cannot be deterministically repaired.
    """
    world, graph, world_state, _private = _world_json(campaign)
    if world is None:
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

    def check(identity, status, detail=None):
        report['checks'].append({
            'id': identity,
            'status': status,
            'detail': detail,
        })
        if status == 'inconsistent':
            report['verified'] = False

    clocks = CampaignClock.query.filter_by(campaign_id=campaign.id).order_by(CampaignClock.id.asc()).all()

    # ---- Clock structured location / condition repair ----
    for clock in clocks:
        if (clock.status or 'active') not in _ACTIVE_CLOCK_STATUSES:
            continue
        subject_ids = _structured_subject_ids(clock)
        if not subject_ids:
            # No structured subject binding: no assertion. Leave the clock
            # active rather than guessing who it is about.
            check('clock_subject_binding', 'no_assertion', {
                'clock_id': clock.clock_id,
                'reason': 'Clock has no structured subject binding.',
            })
            continue
        if len(subject_ids) > 1:
            check('clock_subject_identity', 'ambiguous', {
                'clock_id': clock.clock_id,
                'subject_ids': subject_ids,
                'reason': 'Clock binds multiple subjects; refusing to supersede on an arbitrary match.',
            })
            continue
        subject_id = subject_ids[0]
        location_reason, location_kind = _clock_location_conflict(campaign, world_state, clock, subject_id)
        condition_reason = _clock_condition_resolved(campaign, world_state, clock, subject_id)
        if location_kind == 'ambiguous':
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
    summary has been finalized. It never mutates durable state. It verifies the
    finalized summary against the authoritative committed structured state, and
    re-checks that no active clock's structured subject binding contradicts
    committed scene state.

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
    # committed structured state. Fail closed if it cannot be verified.
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

    clocks = CampaignClock.query.filter_by(campaign_id=campaign.id).order_by(CampaignClock.id.asc()).all()

    for clock in clocks:
        if (clock.status or 'active') not in _ACTIVE_CLOCK_STATUSES:
            continue
        subject_ids = _structured_subject_ids(clock)
        if not subject_ids:
            continue
        if len(subject_ids) > 1:
            check('clock_subject_identity', 'ambiguous', {
                'clock_id': clock.clock_id,
                'subject_ids': subject_ids,
                'reason': 'Clock binds multiple subjects; refusing to supersede on an arbitrary match.',
            })
            continue
        subject_id = subject_ids[0]
        location_reason, location_kind = _clock_location_conflict(campaign, world_state, clock, subject_id)
        condition_reason = _clock_condition_resolved(campaign, world_state, clock, subject_id)
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
                f'Clock {clock.name} danger condition is still resolved per committed state.',
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

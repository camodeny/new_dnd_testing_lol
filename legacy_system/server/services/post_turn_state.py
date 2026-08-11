"""Canonical post-turn state reduction shared by APIs and audit evidence.

The durable ``SessionDmTurn`` row owns stage state.  Committed audit events are
evidence for writes, never an independent completion state machine.
"""

TERMINAL_POST_TURN_STATUSES = frozenset({
    'complete', 'reconciled', 'partial', 'failed', 'timed_out', 'error',
})
SUCCESS_POST_TURN_STATUSES = frozenset({'complete', 'reconciled'})
FAILED_STAGE_STATUSES = frozenset({'error', 'failed', 'timed_out'})
COMPLETE_STAGE_STATUSES = frozenset({'complete', 'skipped'})
DURABLE_MEMORY_EVENT_TYPES = frozenset({
    'memory_patch_applied',
    'memory_patch_applied_v2',
})
DURABLE_CLOCK_EVENT_TYPES = frozenset({'clock_adjudication_applied'})
DURABLE_FINALIZER_EVENT_TYPES = frozenset({'summary_finalizer_applied'})
DURABLE_EMBEDDING_EVENT_TYPES = frozenset({'embedding_write'})


def normalize_status(value, default='pending'):
    value = str(value or '').strip().lower()
    return value or default


def derive_post_turn_state(
    post_turn_status,
    memory_status,
    clock_status,
    *,
    durable_memory_write=False,
    durable_clock_write=False,
    correlation_id=None,
    error_text=None,
):
    """Return the one externally-consumed representation of a post-turn.

    Completion is deliberately derived, never copied from a separately stored
    boolean.  A terminal success additionally requires all required stages to
    be complete (or explicitly skipped).
    """
    raw_post_turn = normalize_status(post_turn_status)
    memory = normalize_status(memory_status)
    clock = normalize_status(clock_status)

    if raw_post_turn == 'error':
        canonical = 'partial' if ({memory, clock} & COMPLETE_STAGE_STATUSES) else 'failed'
    elif raw_post_turn in {'failed', 'timed_out', 'partial'}:
        canonical = raw_post_turn
    elif raw_post_turn in SUCCESS_POST_TURN_STATUSES:
        canonical = raw_post_turn
        if memory not in COMPLETE_STAGE_STATUSES or clock not in COMPLETE_STAGE_STATUSES:
            canonical = 'partial'
    elif {memory, clock} & FAILED_STAGE_STATUSES:
        canonical = 'partial' if ({memory, clock} & COMPLETE_STAGE_STATUSES) else 'failed'
    else:
        canonical = 'pending'

    complete = canonical in SUCCESS_POST_TURN_STATUSES
    resolved = canonical in TERMINAL_POST_TURN_STATUSES
    contradictions = []
    if raw_post_turn in SUCCESS_POST_TURN_STATUSES and not complete:
        contradictions.append('terminal success has an incomplete required stage')
    if durable_memory_write and memory not in COMPLETE_STAGE_STATUSES:
        contradictions.append('committed memory write disagrees with memory stage')
    if durable_clock_write and clock not in COMPLETE_STAGE_STATUSES:
        contradictions.append('committed clock write disagrees with clock stage')
    if not correlation_id:
        contradictions.append('missing post-turn correlation id')

    return {
        'post_turn_complete': complete,
        'post_turn_resolved': resolved,
        'post_turn_status': canonical,
        'memory_status': memory,
        'clock_status': clock,
        'durable_write_present': bool(durable_memory_write or durable_clock_write),
        'durable_memory_write': bool(durable_memory_write),
        'durable_clock_write': bool(durable_clock_write),
        'correlation_id': correlation_id,
        'post_turn_error': error_text,
        'post_turn_invariant_violations': contradictions,
    }


def event_matches_turn(event, *, trace_id, player_message_id, dm_message_id=None):
    """Correlate a committed event using trace lineage or message identity."""
    if trace_id and (event.trace_id == trace_id or event.parent_trace_id == trace_id):
        return True
    payload = event.payload_dict() if hasattr(event, 'payload_dict') else None
    if payload is None:
        import json
        try:
            payload = json.loads(event.payload) if event.payload else {}
        except (TypeError, ValueError):
            payload = {}
    identities = [payload]
    telemetry = payload.get('telemetry') if isinstance(payload, dict) else None
    if isinstance(telemetry, dict):
        identities.append(telemetry)
    return any(
        identity.get('player_message_id') == player_message_id
        or identity.get('source_player_message_id') == player_message_id
        or (dm_message_id is not None and (
            identity.get('dm_message_id') == dm_message_id
            or identity.get('source_dm_message_id') == dm_message_id
        ))
        for identity in identities
        if isinstance(identity, dict)
    )


def committed_write_evidence(turn):
    """Read committed application events correlated to one durable turn."""
    from models import CampaignAuditEvent

    event_types = (
        DURABLE_MEMORY_EVENT_TYPES
        | DURABLE_CLOCK_EVENT_TYPES
        | DURABLE_FINALIZER_EVENT_TYPES
        | DURABLE_EMBEDDING_EVENT_TYPES
    )
    events = []
    if turn.trace_id:
        events.extend(
            CampaignAuditEvent.query
            .filter(
                CampaignAuditEvent.campaign_id == turn.campaign_id,
                CampaignAuditEvent.event_type.in_(event_types),
                (
                    (CampaignAuditEvent.trace_id == turn.trace_id)
                    | (CampaignAuditEvent.parent_trace_id == turn.trace_id)
                ),
            )
            .all()
        )
    identity_candidates = (
        CampaignAuditEvent.query
        .filter(
            CampaignAuditEvent.campaign_id == turn.campaign_id,
            CampaignAuditEvent.event_type.in_(event_types),
        )
        .order_by(CampaignAuditEvent.id.desc())
        .limit(256)
        .all()
    )
    events.extend(identity_candidates)
    matched_by_id = {event.id: event for event in events if event_matches_turn(
        event,
        trace_id=turn.trace_id,
        player_message_id=turn.player_message_id,
        dm_message_id=turn.dm_message_id,
    )}
    matched = list(matched_by_id.values())
    durable_event_write = False
    for event in matched:
        if event.event_type not in DURABLE_MEMORY_EVENT_TYPES:
            continue
        import json
        try:
            payload = json.loads(event.payload) if event.payload else {}
        except (TypeError, ValueError):
            payload = {}
        result = payload.get('result') if isinstance(payload, dict) else None
        durable_event_write = durable_event_write or bool(
            isinstance(result, dict) and result.get('world_event_ids')
        )
    return {
        'durable_memory_write': any(event.event_type in DURABLE_MEMORY_EVENT_TYPES for event in matched),
        'durable_clock_write': any(event.event_type in DURABLE_CLOCK_EVENT_TYPES for event in matched),
        'durable_finalizer_write': any(event.event_type in DURABLE_FINALIZER_EVENT_TYPES for event in matched),
        'durable_embedding_write': any(event.event_type in DURABLE_EMBEDDING_EVENT_TYPES for event in matched),
        'durable_event_write': durable_event_write,
        'committed_event_ids': [event.id for event in matched],
    }


def state_for_turn(turn):
    evidence = committed_write_evidence(turn)
    state = derive_post_turn_state(
        turn.post_turn_status,
        turn.memory_status,
        turn.clock_status,
        durable_memory_write=evidence['durable_memory_write'],
        durable_clock_write=evidence['durable_clock_write'],
        correlation_id=turn.trace_id,
        error_text=turn.error_text,
    )
    outer_status = state['post_turn_status']
    if state['memory_status'] == 'skipped' and state['clock_status'] == 'skipped':
        finalizer_status = 'skipped'
    elif evidence['durable_finalizer_write'] or outer_status in SUCCESS_POST_TURN_STATUSES:
        finalizer_status = 'complete'
    elif state['memory_status'] == 'complete' and state['clock_status'] == 'complete':
        finalizer_status = 'failed'
    else:
        finalizer_status = 'pending'
    state.update({
        'durable_finalizer_write': evidence['durable_finalizer_write'],
        'durable_embedding_write': evidence['durable_embedding_write'],
        'durable_event_write': evidence['durable_event_write'],
        'durable_write_present': any(
            value for key, value in evidence.items() if key.startswith('durable_')
        ),
        'post_turn_stages': {
            'memory': state['memory_status'],
            'clock': state['clock_status'],
            'finalizer': finalizer_status,
            'embedding_write': (
                'complete' if evidence['durable_embedding_write']
                else (
                    'skipped'
                    if evidence['durable_memory_write'] or state['memory_status'] == 'skipped'
                    else 'pending'
                )
            ),
            'event_write': (
                'complete' if evidence['durable_event_write']
                else (
                    'skipped'
                    if evidence['durable_memory_write'] or state['memory_status'] == 'skipped'
                    else 'pending'
                )
            ),
        },
    })
    state['committed_post_turn_event_ids'] = evidence['committed_event_ids']
    return state

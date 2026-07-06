from datetime import datetime

from models import SessionDmTurn


def session_dm_trace_id(session_id, player_message_id):
    if player_message_id:
        return f'session_dm:session_{session_id}:message_{player_message_id}'
    return f'session_dm:session_{session_id}:message_async'


def _utcnow():
    return datetime.utcnow()


def _duration_ms(started_at, finished_at):
    if not started_at or not finished_at:
        return None
    return max(0, int((finished_at - started_at).total_seconds() * 1000))


def _get_turn(player_message_id):
    if not player_message_id:
        return None
    return SessionDmTurn.query.filter_by(player_message_id=player_message_id).first()


def begin_session_dm_turn(campaign_id, session_id, player_message_id, trace_id, started_at=None):
    started_at = started_at or _utcnow()
    turn = _get_turn(player_message_id)
    if turn is None:
        turn = SessionDmTurn(
            campaign_id=campaign_id,
            session_id=session_id,
            player_message_id=player_message_id,
            trace_id=trace_id,
            started_at=started_at,
            status='pending',
            post_turn_status='pending',
            memory_status='pending',
            clock_status='pending',
        )
    else:
        turn.campaign_id = turn.campaign_id or campaign_id
        turn.session_id = turn.session_id or session_id
        turn.trace_id = trace_id or turn.trace_id
        turn.started_at = turn.started_at or started_at
    return turn


def mark_session_dm_turn_visible(
    campaign_id,
    session_id,
    player_message_id,
    trace_id,
    status,
    dm_message_id=None,
    completed_at=None,
):
    completed_at = completed_at or _utcnow()
    turn = begin_session_dm_turn(campaign_id, session_id, player_message_id, trace_id)
    turn.dm_message_id = dm_message_id or turn.dm_message_id
    turn.trace_id = trace_id or turn.trace_id
    turn.status = status
    turn.visible_completed_at = completed_at
    turn.generation_duration_ms = _duration_ms(turn.started_at, completed_at)
    if status == 'speak':
        turn.post_turn_status = 'pending'
        turn.memory_status = 'pending'
        turn.clock_status = 'pending'
    else:
        turn.post_turn_status = 'complete'
        turn.memory_status = 'skipped'
        turn.clock_status = 'skipped'
        turn.finished_at = completed_at
        turn.full_duration_ms = _duration_ms(turn.started_at, completed_at)
    return turn


def mark_session_dm_turn_post_turn_complete(player_message_id, dm_message_id=None, finished_at=None):
    turn = _get_turn(player_message_id)
    if turn is None:
        return None
    finished_at = finished_at or _utcnow()
    if dm_message_id:
        turn.dm_message_id = dm_message_id
    turn.finished_at = finished_at
    turn.full_duration_ms = _duration_ms(turn.started_at, finished_at)
    turn.post_turn_status = 'complete'
    turn.memory_status = 'complete'
    turn.clock_status = 'complete'
    if turn.status == 'pending':
        turn.status = 'speak'
    return turn


def mark_session_dm_turn_error(
    campaign_id,
    session_id,
    player_message_id,
    trace_id,
    error_text,
    finished_at=None,
    dm_message_id=None,
    memory_status=None,
    clock_status=None,
):
    finished_at = finished_at or _utcnow()
    turn = begin_session_dm_turn(campaign_id, session_id, player_message_id, trace_id, started_at=finished_at)
    if dm_message_id:
        turn.dm_message_id = dm_message_id
    if turn.status not in {'speak', 'silent', 'empty'}:
        turn.status = 'error'
    turn.error_text = str(error_text or '')[:2000] or None
    turn.finished_at = finished_at
    turn.full_duration_ms = _duration_ms(turn.started_at, finished_at)
    if turn.visible_completed_at and turn.generation_duration_ms is None:
        turn.generation_duration_ms = _duration_ms(turn.started_at, turn.visible_completed_at)
    turn.post_turn_status = 'error'
    turn.memory_status = memory_status or ('complete' if turn.visible_completed_at else 'skipped')
    turn.clock_status = clock_status or 'skipped'
    return turn


def session_dm_turn_status_payload(player_message_id):
    turn = _get_turn(player_message_id)
    if turn is None:
        return {}
    return {
        'status': turn.status,
        'post_turn_status': turn.post_turn_status,
        'memory_status': turn.memory_status,
        'clock_status': turn.clock_status,
        'started_at': turn.started_at.isoformat() if turn.started_at else None,
        'visible_completed_at': turn.visible_completed_at.isoformat() if turn.visible_completed_at else None,
        'finished_at': turn.finished_at.isoformat() if turn.finished_at else None,
        'generation_duration_ms': turn.generation_duration_ms,
        'full_duration_ms': turn.full_duration_ms,
        'turn_error': turn.error_text,
    }

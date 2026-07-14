"""Shared phase-aware DM response polling state machine."""

import time


def dm_turn_has_visible_output(status_dict):
    if not isinstance(status_dict, dict):
        return False
    status = str(status_dict.get('status') or '').strip().lower()
    if status in {'speak', 'silent', 'empty', 'error'}:
        return True
    return status_dict.get('dm_message_id') is not None


def dm_turn_post_turn_resolved(status_dict):
    if not isinstance(status_dict, dict):
        return False
    status = str(status_dict.get('status') or '').strip().lower()
    if status in {'silent', 'empty', 'error'}:
        return True

    post_turn = str(status_dict.get('post_turn_status') or '').strip().lower()
    if post_turn in {'complete', 'error'}:
        return True

    if 'post_turn_complete' in status_dict:
        return bool(status_dict.get('post_turn_complete'))

    # Backward compatibility for servers that only report visible output state.
    return status == 'speak'


def dm_turn_fully_resolved(status_dict):
    if not isinstance(status_dict, dict):
        return False
    status = str(status_dict.get('status') or '').strip().lower()
    if not status or status == 'pending':
        return False
    return dm_turn_post_turn_resolved(status_dict)


def classify_timeout(status_dict, phase):
    if phase == 'visible':
        return 'dm_visible_response_timeout'
    if phase == 'post_turn':
        if str(status_dict.get('post_turn_status') or '').strip().lower() == 'error':
            return 'dm_post_turn_error'
        return 'dm_post_turn_timeout'
    return 'dm_response_timeout'


def build_timeout_evidence(status_dict, phase):
    del phase
    return {
        'player_message_id': status_dict.get('player_message_id'),
        'dm_message_id': status_dict.get('dm_message_id'),
        'status': status_dict.get('status'),
        'post_turn_status': status_dict.get('post_turn_status'),
        'memory_status': status_dict.get('memory_status'),
        'clock_status': status_dict.get('clock_status'),
        'started_at': status_dict.get('started_at'),
        'visible_completed_at': status_dict.get('visible_completed_at'),
        'finished_at': status_dict.get('finished_at'),
        'generation_duration_ms': status_dict.get('generation_duration_ms'),
        'full_duration_ms': status_dict.get('full_duration_ms'),
    }


def resolve_dm_response_timeouts(args):
    visible = getattr(args, 'dm_visible_response_timeout', None)
    post_turn = getattr(args, 'dm_post_turn_timeout', None)
    legacy = getattr(args, 'dm_response_timeout', None)

    if visible is not None and post_turn is not None:
        return float(visible), float(post_turn)
    if visible is not None:
        return float(visible), 180.0
    if post_turn is not None:
        return 300.0, float(post_turn)
    if legacy is not None:
        return float(legacy), 180.0
    return 300.0, 180.0


def _fetch_status(fetch_status_fn, last_status, phase, transient_error_types, on_poll_error_fn):
    try:
        return fetch_status_fn()
    except transient_error_types as exc:
        if on_poll_error_fn is not None:
            on_poll_error_fn(exc, phase)
        return last_status


def wait_for_dm_response(
    fetch_status_fn,
    maybe_heartbeat_fn,
    visible_timeout,
    post_turn_timeout,
    poll_interval,
    *,
    transient_error_types=(Exception,),
    on_poll_error_fn=None,
):
    """Poll through visible-output and post-turn phases with separate deadlines."""
    monotonic = time.monotonic
    sleep_interval = max(0.05, float(poll_interval))

    visible_deadline = monotonic() + max(0.0, float(visible_timeout))
    last_status = {'status': 'pending'}

    while True:
        maybe_heartbeat_fn()
        last_status = _fetch_status(
            fetch_status_fn,
            last_status,
            'visible',
            transient_error_types,
            on_poll_error_fn,
        )
        if dm_turn_has_visible_output(last_status):
            break
        if monotonic() >= visible_deadline:
            last_status = _fetch_status(
                fetch_status_fn,
                last_status,
                'visible_final',
                transient_error_types,
                on_poll_error_fn,
            )
            if dm_turn_has_visible_output(last_status):
                break
            return last_status, True, 'visible'
        remaining = visible_deadline - monotonic()
        time.sleep(min(sleep_interval, max(0.05, remaining)))

    post_turn_deadline = monotonic() + max(0.0, float(post_turn_timeout))
    while True:
        maybe_heartbeat_fn()
        last_status = _fetch_status(
            fetch_status_fn,
            last_status,
            'post_turn',
            transient_error_types,
            on_poll_error_fn,
        )
        if dm_turn_post_turn_resolved(last_status):
            return last_status, False, None
        if monotonic() >= post_turn_deadline:
            last_status = _fetch_status(
                fetch_status_fn,
                last_status,
                'post_turn_final',
                transient_error_types,
                on_poll_error_fn,
            )
            if dm_turn_post_turn_resolved(last_status):
                return last_status, False, None
            return last_status, True, 'post_turn'
        remaining = post_turn_deadline - monotonic()
        time.sleep(min(sleep_interval, max(0.05, remaining)))

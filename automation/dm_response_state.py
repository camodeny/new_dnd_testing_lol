"""
Shared DM response polling state machine used by both the automation worker
and the standalone autonomous LLM campaign runner.

Splits the original combined deadline into two phases:

  Phase 1 – visible DM output (speak / silent / empty)
  Phase 2 – post-turn memory + clock processing

Each phase has its own configurable timeout and performs a final
authoritative status read before recording a timeout to close
polling-boundary races.
"""

import time


def dm_turn_has_visible_output(status_dict):
    if not isinstance(status_dict, dict):
        return False
    status = status_dict.get('status')
    if status in {'speak', 'silent', 'empty'}:
        return True
    if status_dict.get('dm_message_id') is not None:
        return True
    return False


def dm_turn_post_turn_resolved(status_dict):
    if not isinstance(status_dict, dict):
        return False
    status = status_dict.get('status')
    if status in {'silent', 'empty'}:
        return True
    post_turn = str(status_dict.get('post_turn_status') or '').strip().lower()
    if post_turn in {'complete', 'error'}:
        return True
    if status_dict.get('post_turn_complete'):
        return True
    return False


def dm_turn_fully_resolved(status_dict):
    """Backward-compatible check that the whole turn is complete."""
    if not isinstance(status_dict, dict):
        return False
    status = (status_dict or {}).get('status')
    if status in {None, 'pending'}:
        return False
    if status in {'silent', 'empty'}:
        return True

    post_turn_status = str((status_dict or {}).get('post_turn_status') or '').strip().lower()
    if post_turn_status:
        return post_turn_status in {'complete', 'error'}

    if 'post_turn_complete' in (status_dict or {}):
        return bool((status_dict or {}).get('post_turn_complete'))

    return True


def classify_timeout(status_dict, phase):
    if phase == 'visible':
        return 'dm_visible_response_timeout'
    if phase == 'post_turn':
        post_turn_status = str(status_dict.get('post_turn_status') or '').strip().lower()
        if post_turn_status == 'error':
            return 'dm_post_turn_error'
        return 'dm_post_turn_timeout'
    return 'dm_response_timeout'


def build_timeout_evidence(status_dict, phase):
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
    """
    Return (visible_response_timeout, post_turn_timeout) from parsed args.
    Falls back to the old combined timeout when the new env vars are not set.
    """
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


def wait_for_dm_response(fetch_status_fn, maybe_heartbeat_fn, visible_timeout, post_turn_timeout, poll_interval):
    """
    Phase-aware DM response polling.

    Parameters:
        fetch_status_fn: callable() -> status dict
        maybe_heartbeat_fn: callable() for lease extension (can be no-op)
        visible_timeout: seconds to wait for visible DM output
        post_turn_timeout: seconds to wait for post-turn processing
        poll_interval: base sleep between polls

    Returns:
        (status_dict, timed_out: bool, timeout_phase: str or None)
    """
    monotonic = time.monotonic

    # --- Phase 1: wait for visible DM output ---
    phase1_deadline = monotonic() + max(0.0, visible_timeout)
    last_status = {'status': 'pending'}

    while True:
        maybe_heartbeat_fn()
        try:
            last_status = fetch_status_fn()
        except Exception:
            pass

        if dm_turn_has_visible_output(last_status):
            break

        if monotonic() >= phase1_deadline:
            try:
                final = fetch_status_fn()
                last_status = final
            except Exception:
                pass
            if dm_turn_has_visible_output(last_status):
                break
            return last_status, True, 'visible'

        remaining = phase1_deadline - monotonic()
        time.sleep(min(poll_interval, max(0.5, remaining)))

    # --- Phase 2: wait for post-turn completion ---
    phase2_deadline = monotonic() + max(0.0, post_turn_timeout)

    while True:
        maybe_heartbeat_fn()
        try:
            last_status = fetch_status_fn()
        except Exception:
            pass

        if dm_turn_post_turn_resolved(last_status):
            return last_status, False, None

        if monotonic() >= phase2_deadline:
            try:
                final = fetch_status_fn()
                last_status = final
            except Exception:
                pass
            if dm_turn_post_turn_resolved(last_status):
                return last_status, False, None
            return last_status, True, 'post_turn'

        remaining = phase2_deadline - monotonic()
        time.sleep(min(poll_interval, max(0.5, remaining)))

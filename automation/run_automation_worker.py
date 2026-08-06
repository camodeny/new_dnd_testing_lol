#!/usr/bin/env python3
import argparse
from datetime import datetime, timedelta
import os
import socket
import sys
import threading
import time
import uuid

from llm_campaign_common import (
    ApiError,
    api_get,
    api_post,
    default_api_base,
    default_session_start_timeout,
    start_session,
)
from provider_client import request_json_decision
import dm_response_state
import run_autonomous_llm_campaign as autonomous
import run_llm_campaign_orchestrator as orchestrator


def parse_args():
    parser = argparse.ArgumentParser(description='Claim and execute automation runs from the app backend.')
    parser.add_argument('--api-base', default=default_api_base(), help='Base app URL, for example http://127.0.0.1:5889')
    parser.add_argument('--owner-api-key', default=os.environ.get('DND_OWNER_API_KEY'), help='Owner automation API key')
    parser.add_argument('--run-id', type=int, help='Specific automation run id to claim and execute')
    parser.add_argument('--worker-id', default=None)
    parser.add_argument('--poll-interval', type=float, default=3.0)
    parser.add_argument('--max-turns', type=int, default=50)
    parser.add_argument('--max-minutes', type=float, default=None)
    parser.add_argument('--idle-timeout', type=float, default=180.0)
    parser.add_argument('--heartbeat-interval', type=float, default=10.0)
    parser.add_argument('--dm-response-timeout', type=float, default=float(os.environ.get('DND_DM_RESPONSE_TIMEOUT', '720')))
    _visible_env = os.environ.get('DND_DM_VISIBLE_RESPONSE_TIMEOUT')
    _post_turn_env = os.environ.get('DND_DM_POST_TURN_TIMEOUT')
    parser.add_argument('--dm-visible-response-timeout', type=float, default=float(_visible_env) if _visible_env else None)
    parser.add_argument('--dm-post-turn-timeout', type=float, default=float(_post_turn_env) if _post_turn_env else None)
    parser.add_argument(
        '--dm-late-completion-reconciliation-seconds',
        type=float,
        default=float(os.environ.get('DND_DM_LATE_COMPLETION_RECONCILIATION_SECONDS', '30')),
        help='Bounded window (seconds) to wait for a timed-out DM turn to finish before the run is finalized as failed',
    )
    parser.add_argument('--message-window', type=int, default=16)
    parser.add_argument('--model', default=os.environ.get('OPENCODE_GO_MODEL') or os.environ.get('OPENCODE_MODEL') or os.environ.get('OPENROUTER_MODEL') or '')
    parser.add_argument('--opencode-server', default=None, help='Unused compatibility flag')
    parser.add_argument('--opencode-password', default=None, help='Unused compatibility flag')
    parser.add_argument('--init-lease-seconds', type=float, default=float(os.environ.get('DND_INIT_LEASE_SECONDS', '900')), help='Lease duration (seconds) used during campaign initialization before run_started is recorded')
    parser.add_argument('--session-start-timeout', type=float, default=float(os.environ.get('DND_AUTO_SESSION_START_TIMEOUT', '900')), help='Timeout (seconds) for the session-start POST and subsequent polling during campaign initialization')
    parser.add_argument('--once', action='store_true', help='Claim and execute at most one run, then exit')
    return parser.parse_args()


def default_worker_id():
    return f'automation-{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}'


def resolve_worker_id(cli_value, env_var='DND_AUTOMATION_WORKER_ID'):
    if cli_value:
        return cli_value
    env_value = os.environ.get(env_var)
    if env_value:
        return env_value
    return default_worker_id()


def workspace(api_base, owner_api_key, worker_id=None):
    query = {}
    if worker_id:
        query['worker_id'] = worker_id
        query['api_base'] = api_base
    return api_get(api_base, '/api/automation', api_key=owner_api_key, query=query)


def list_candidate_run_ids(api_base, owner_api_key, worker_id=None):
    data = workspace(api_base, owner_api_key, worker_id=worker_id)
    active_runs = data.get('active_runs') or []
    claimable = [
        run for run in active_runs
        if run.get('claimable') and run.get('id') is not None
    ]
    queued = [run for run in claimable if run.get('status') == 'queued']
    expired = [run for run in claimable if run.get('status') != 'queued']
    queued.sort(key=lambda r: r.get('created_at') or '')
    expired.sort(key=lambda r: r.get('lease_expires_at') or '')
    return [r['id'] for r in [*queued, *expired]]


def claim_run(api_base, owner_api_key, run_id, worker_id):
    return api_post(
        api_base,
        f'/api/automation/runs/{run_id}/claim',
        {'worker_id': worker_id, 'api_base': api_base},
        api_key=owner_api_key,
    )


def heartbeat(api_base, owner_api_key, run_id, worker_id, lease_token, lease_seconds=None):
    body = {'worker_id': worker_id, 'lease_token': lease_token, 'api_base': api_base}
    if lease_seconds is not None:
        body['lease_seconds'] = lease_seconds
    return api_post(
        api_base,
        f'/api/automation/runs/{run_id}/heartbeat',
        body,
        api_key=owner_api_key,
    )


def append_event(
    api_base,
    owner_api_key,
    run_id,
    worker_id,
    lease_token,
    event_type,
    payload=None,
    status=None,
    error_text=None,
    dedupe_key=None,
    reconciliation_player_message_id=None,
    reconciliation_timeout_phase=None,
    reconciliation_timeout_error=None,
    reconciliation_started_at=None,
    reconciliation_deadline=None,
):
    body = {
        'event_type': event_type,
        'payload': payload or {},
        'worker_id': worker_id,
        'lease_token': lease_token,
        'dedupe_key': dedupe_key,
    }
    if status:
        body['status'] = status
    if error_text is not None:
        body['error_text'] = error_text
    if reconciliation_player_message_id is not None:
        body['reconciliation_player_message_id'] = reconciliation_player_message_id
    if reconciliation_timeout_phase is not None:
        body['reconciliation_timeout_phase'] = reconciliation_timeout_phase
    if reconciliation_timeout_error is not None:
        body['reconciliation_timeout_error'] = reconciliation_timeout_error
    if reconciliation_started_at is not None:
        body['reconciliation_started_at'] = reconciliation_started_at
    if reconciliation_deadline is not None:
        body['reconciliation_deadline'] = reconciliation_deadline
    return api_post(api_base, f'/api/automation/runs/{run_id}/events', body, api_key=owner_api_key)


def persist_provider_call(api_base, owner_api_key, run_id, worker_id, lease_token, payload):
    body = dict(payload)
    body['worker_id'] = worker_id
    body['lease_token'] = lease_token
    return api_post(api_base, f'/api/automation/runs/{run_id}/provider-calls', body, api_key=owner_api_key)


def replay_provider_call(api_base, owner_api_key, source_run_id, dedupe_key):
    return api_get(
        api_base,
        f'/api/automation/runs/{source_run_id}/provider-calls/replay',
        api_key=owner_api_key,
        query={'dedupe_key': dedupe_key},
    ).get('provider_call') or {}


def complete_run(api_base, owner_api_key, run_id, worker_id, lease_token, status='completed', error_text=None, dedupe_key=None):
    return api_post(
        api_base,
        f'/api/automation/runs/{run_id}/complete',
        {
            'status': status,
            'error_text': error_text,
            'worker_id': worker_id,
            'lease_token': lease_token,
            'dedupe_key': dedupe_key,
        },
        api_key=owner_api_key,
    )


def fetch_run(api_base, owner_api_key, run_id):
    return api_get(api_base, f'/api/automation/runs/{run_id}', api_key=owner_api_key)


def pause_run(api_base, owner_api_key, run_id, worker_id, lease_token, phase, payload=None, summary=None, player_message_id=None, dm_message_id=None, dedupe_key=None):
    return api_post(
        api_base,
        f'/api/automation/runs/{run_id}/pause',
        {
            'worker_id': worker_id,
            'lease_token': lease_token,
            'phase': phase,
            'payload': payload or {},
            'summary': summary,
            'player_message_id': player_message_id,
            'dm_message_id': dm_message_id,
            'dedupe_key': dedupe_key,
        },
        api_key=owner_api_key,
    )


def build_manifest_for_run(api_base, owner_api_key, claim_payload):
    roster = claim_payload.get('roster') or []
    llm_players = []
    for entry in roster:
        llm_players.append({
            'llm_player': {
                'id': entry.get('llm_player_id') or entry.get('user_id'),
                'user_id': entry.get('user_id'),
                'label': entry.get('label'),
            },
            'character': {
                'id': entry.get('derived_character_id'),
                'name': entry.get('character_name'),
            },
        })
    latest_session = claim_payload.get('latest_session') or {}
    return {
        'api_base': api_base,
        'owner': {'api_key': owner_api_key},
        'campaign': claim_payload.get('derived_campaign') or {},
        'session': {'id': latest_session.get('id')},
        'llm_players': llm_players,
    }


def fetch_campaign_characters(api_base, owner_api_key, campaign_id):
    return api_get(api_base, f'/api/campaigns/{campaign_id}/characters', api_key=owner_api_key).get('characters') or []


def select_chosen_player(roster_entry, campaign_characters):
    character = next((item for item in campaign_characters if item.get('id') == roster_entry.get('derived_character_id')), None)
    if character is None:
        character = {'id': roster_entry.get('derived_character_id'), 'name': roster_entry.get('character_name')}
    return {
        'llm_player': {
            'id': roster_entry.get('llm_player_id') or roster_entry.get('user_id'),
            'user_id': roster_entry.get('user_id'),
            'label': roster_entry.get('label'),
        },
        'character': character,
    }


def submit_decision(api_base, owner_api_key, run_id, chosen_player, decision, dedupe_key, worker_id, lease_token):
    return api_post(
        api_base,
        f'/api/automation/runs/{run_id}/decisions',
        {
            'llm_player_id': chosen_player['llm_player']['id'],
            'user_id': chosen_player['llm_player']['user_id'],
            'decision': decision,
            'dedupe_key': dedupe_key,
            'worker_id': worker_id,
            'lease_token': lease_token,
        },
        api_key=owner_api_key,
    )


def active_session_from_run_payload(run_payload):
    return (run_payload.get('latest_session') or {}) if isinstance(run_payload.get('latest_session'), dict) else {}


def ensure_campaign_initialized(args, claim_payload, session_start_timeout=None):
    campaign_id = claim_payload['derived_campaign']['id']
    session = active_session_from_run_payload(claim_payload)
    bootstrapped = False
    if not session:
        session = start_session(
            args.api_base,
            campaign_id,
            api_key=args.owner_api_key,
            timeout=session_start_timeout or default_session_start_timeout(),
        )
        claim_payload['latest_session'] = session
        bootstrapped = True

    # The claim response intentionally returns a compact session object, while
    # the worker needs its opening DM message to safely choose the first turn.
    # Hydrate the session before interpreting a missing messages list as an
    # uninitialized clone.
    if session.get('id') and 'messages' not in session:
        session_payload = api_get(
            args.api_base,
            f"/api/sessions/{session['id']}",
            api_key=args.owner_api_key,
        )
        hydrated_session = session_payload.get('session') if isinstance(session_payload, dict) else None
        if isinstance(hydrated_session, dict):
            session = hydrated_session
            claim_payload['latest_session'] = session

    messages = session.get('messages') or []
    if not session.get('id') or not session.get('is_active') or not messages:
        raise RuntimeError('campaign_not_initialized')

    gr = claim_payload.get('gameplay_readiness')
    if gr and not bootstrapped:
        if not gr.get('campaign_ready'):
            raise RuntimeError('campaign_not_initialized')
        world_payload = {'world': {'world_state': {}}}
    else:
        if not any(m.get('role') == 'dm' and m.get('content', '').strip() for m in messages):
            raise RuntimeError('campaign_not_initialized')
        world_payload = api_get(args.api_base, f'/api/campaigns/{campaign_id}/world', api_key=args.owner_api_key)
        if not world_payload or world_payload.get('world') is None:
            raise RuntimeError('campaign_not_initialized')

    return session, world_payload


def messages_fingerprint(session):
    messages = session.get('messages') or []
    last_message = messages[-1] if messages else {}
    return {
        'session_id': session.get('id'),
        'message_count': len(messages),
        'last_message_id': last_message.get('id'),
        'last_message_created_at': last_message.get('created_at'),
        'is_active': session.get('is_active', True),
    }


def stage_key(turns_completed, session):
    fingerprint = messages_fingerprint(session)
    return (
        f'turn:{turns_completed}'
        f':session:{fingerprint.get("session_id") or "none"}'
        f':count:{fingerprint.get("message_count") or 0}'
        f':last:{fingerprint.get("last_message_id") or "none"}'
    )


def runner_config(claim_payload):
    return ((claim_payload.get('run') or {}).get('runner_config') or {})


def pause_phases(claim_payload):
    return {
        str(value).strip().lower()
        for value in (runner_config(claim_payload).get('audit_pause_phases') or [])
        if str(value).strip().lower() in {'after_player', 'after_dm'}
    }


def replay_source_run_id(claim_payload):
    config = runner_config(claim_payload)
    return config.get('deterministic_replay_from_run_id') or config.get('replay_from_run_id')


def maybe_replay_provider_call(args, claim_payload, dedupe_key):
    source_run_id = replay_source_run_id(claim_payload)
    if not source_run_id:
        return None
    return replay_provider_call(args.api_base, args.owner_api_key, source_run_id, dedupe_key)


def record_provider_call(args, run_id, lease_token, payload):
    return persist_provider_call(
        args.api_base,
        args.owner_api_key,
        run_id,
        args.worker_id,
        lease_token,
        payload,
    )


def request_overseer_decision(args, manifest, claim_payload, session, turns_completed, last_dm_turn, run_id, lease_token):
    context = autonomous.build_overseer_context(manifest, session, args.message_window, last_dm_turn=last_dm_turn)
    prompt = autonomous.build_overseer_prompt(context)
    validation_retry_count = 0
    logical_key = stage_key(turns_completed, session)

    for _ in range(3):
        provider_dedupe_key = f'provider:overseer:{logical_key}:validation:{validation_retry_count}'
        replay_call = maybe_replay_provider_call(args, claim_payload, provider_dedupe_key)
        if replay_call:
            decision = replay_call.get('parsed_output') or {}
            response_text = replay_call.get('response_text') or ''
            provider_meta = {
                'provider': replay_call.get('provider') or 'replay',
                'model': replay_call.get('model'),
                'usage': {
                    'prompt_tokens': replay_call.get('usage_input_tokens'),
                    'completion_tokens': replay_call.get('usage_output_tokens'),
                    'total_tokens': replay_call.get('usage_total_tokens'),
                },
                'json_retry_count': replay_call.get('parse_repair_attempts') or 0,
            }
        else:
            started = time.monotonic()
            result = request_json_decision(
                autonomous.OVERSEER_SYSTEM_PROMPT,
                prompt,
                model=args.model,
                timeout_seconds=120,
                max_attempts=2,
            )
            latency_ms = int((time.monotonic() - started) * 1000)
            decision = result['decision']
            response_text = result['raw_response_text']
            provider_meta = {
                'provider': result.get('provider'),
                'model': result.get('model'),
                'usage': result.get('usage') or {},
                'json_retry_count': result.get('json_retry_count', 0),
                'raw_response': result.get('raw_response') or {},
                'latency_ms': latency_ms,
            }
            record_provider_call(args, run_id, lease_token, {
                'dedupe_key': provider_dedupe_key,
                'phase': 'overseer',
                'prompt_version_id': 'automation_overseer_v1',
                'provider': provider_meta.get('provider'),
                'model': provider_meta.get('model'),
                'provider_response_id': (provider_meta.get('raw_response') or {}).get('id'),
                'usage': provider_meta.get('usage') or {},
                'latency_ms': latency_ms,
                'parse_repair_attempts': provider_meta.get('json_retry_count', 0),
                'request': {
                    'messages': [
                        {'role': 'system', 'content': autonomous.OVERSEER_SYSTEM_PROMPT},
                        {'role': 'user', 'content': prompt},
                    ],
                },
                'response': provider_meta.get('raw_response') or {},
                'parsed_output': decision,
                'response_text': response_text,
            })
        try:
            normalized = autonomous.normalize_overseer_decision(manifest, decision)
            normalized['raw_response_text'] = response_text
            normalized['json_retry_count'] = provider_meta.get('json_retry_count', 0)
            normalized['validation_retry_count'] = validation_retry_count
            normalized['provider'] = provider_meta.get('provider')
            normalized['model'] = provider_meta.get('model')
            return normalized
        except RuntimeError as exc:
            validation_retry_count += 1
            prompt = autonomous.build_overseer_retry_prompt(context, manifest, exc)

    raise RuntimeError('Overseer failed validation after retries')


def request_player_decision(args, manifest, claim_payload, campaign, world_payload, session, chosen_player, pending_proposals, turns_completed, run_id, lease_token):
    prompt = orchestrator.build_prompt(
        manifest,
        campaign,
        world_payload,
        session,
        chosen_player,
        pending_proposals,
        args.message_window,
    )
    logical_key = f'{stage_key(turns_completed, session)}:player:{chosen_player["llm_player"]["id"]}'
    provider_dedupe_key = f'provider:player:{logical_key}'
    replay_call = maybe_replay_provider_call(args, claim_payload, provider_dedupe_key)
    if replay_call:
        return (
            replay_call.get('parsed_output') or {},
            replay_call.get('response_text') or '',
            replay_call.get('parse_repair_attempts') or 0,
            replay_call.get('provider') or 'replay',
            replay_call.get('model'),
        )

    started = time.monotonic()
    result = request_json_decision(
        orchestrator.SYSTEM_PROMPT,
        prompt,
        model=args.model,
        timeout_seconds=120,
        max_attempts=2,
    )
    latency_ms = int((time.monotonic() - started) * 1000)
    record_provider_call(args, run_id, lease_token, {
        'dedupe_key': provider_dedupe_key,
        'phase': 'player_decision',
        'prompt_version_id': 'automation_player_turn_v1',
        'provider': result.get('provider'),
        'model': result.get('model'),
        'provider_response_id': (result.get('raw_response') or {}).get('id'),
        'usage': result.get('usage') or {},
        'latency_ms': latency_ms,
        'parse_repair_attempts': result.get('json_retry_count', 0),
        'request': {
            'messages': [
                {'role': 'system', 'content': orchestrator.SYSTEM_PROMPT},
                {'role': 'user', 'content': prompt},
            ],
        },
        'response': result.get('raw_response') or {},
        'parsed_output': result.get('decision') or {},
        'response_text': result.get('raw_response_text'),
    })
    return (
        result.get('decision') or {},
        result.get('raw_response_text') or '',
        result.get('json_retry_count', 0),
        result.get('provider'),
        result.get('model'),
    )


def _reconcile_dm_turn_timeout(
    args,
    manifest,
    claim_payload,
    run_id,
    lease_token,
    player_message_id,
    logical_key,
    timeout_phase,
    timeout_error,
    timeout_evidence,
    maybe_heartbeat_fn,
    resumed_deadline_utc=None,
    resumed_started_at_utc=None,
):
    """Give a timed-out DM turn a bounded window to finish before failing the run.

    Returns (recovered_status, stop_action, terminal_error):
    - recovered: (status_dict, None, None) — the turn finished inside the window;
      the caller should continue the normal after-DM flow with this status.
    - external stop: (None, 'stop_requested' | 'already_terminal', None) — the run
      was stopped/terminated externally; the caller should exit cleanly.
    - exhausted: (None, None, error_text) — the window elapsed or the turn
      errored; the caller should finalize the run as failed with error_text.
    """
    visible_timeout, post_turn_timeout = dm_response_state.resolve_dm_response_timeouts(args)
    reconciliation_seconds = max(
        0.0,
        float(getattr(args, 'dm_late_completion_reconciliation_seconds', 30.0)),
    )
    claim_run_state = (claim_payload.get('run') or {}) if isinstance(claim_payload, dict) else {}
    
    now_utc = datetime.utcnow()
    if resumed_deadline_utc is not None:
        started_utc = resumed_started_at_utc or now_utc
        deadline_utc = resumed_deadline_utc
        remaining_seconds = (deadline_utc - now_utc).total_seconds()
        started_monotonic = time.monotonic() - (now_utc - started_utc).total_seconds()
        deadline = time.monotonic() + remaining_seconds
    else:
        started_utc = now_utc
        deadline_utc = started_utc + timedelta(seconds=reconciliation_seconds)
        started_monotonic = time.monotonic()
        deadline = started_monotonic + reconciliation_seconds

    append_event(
        args.api_base,
        args.owner_api_key,
        run_id,
        args.worker_id,
        lease_token,
        'dm_turn_reconciliation_started',
        {
            'automation_run_id': run_id,
            'player_message_id': player_message_id,
            'timeout_phase': timeout_phase,
            'timeout_classification': timeout_error,
            'visible_response_timeout_seconds': visible_timeout,
            'post_turn_timeout_seconds': post_turn_timeout,
            'reconciliation_window_seconds': reconciliation_seconds,
            'attempt_count': claim_run_state.get('attempt_count'),
            'timeout_evidence': timeout_evidence,
        },
        status='reconciling',
        dedupe_key=f'dm_turn_reconciliation_started:{logical_key}:{player_message_id}:{timeout_phase}',
        reconciliation_player_message_id=player_message_id,
        reconciliation_timeout_phase=timeout_phase,
        reconciliation_timeout_error=timeout_error,
        reconciliation_started_at=started_utc.isoformat(),
        reconciliation_deadline=deadline_utc.isoformat(),
    )

    def _exhausted_event(terminal_reason, last_status):
        append_event(
            args.api_base,
            args.owner_api_key,
            run_id,
            args.worker_id,
            lease_token,
            'dm_turn_reconciliation_exhausted',
            {
                'automation_run_id': run_id,
                'player_message_id': player_message_id,
                'timeout_phase': timeout_phase,
                'timeout_classification': timeout_error,
                'terminal_reason': terminal_reason,
                'reconciliation_window_seconds': reconciliation_seconds,
                'reconciliation_elapsed_seconds': round(time.monotonic() - started_monotonic, 3),
                'last_status': (last_status or {}).get('status'),
                'last_post_turn_status': (last_status or {}).get('post_turn_status'),
                'last_memory_status': (last_status or {}).get('memory_status'),
                'last_clock_status': (last_status or {}).get('clock_status'),
            },
            dedupe_key=f'dm_turn_reconciliation_exhausted:{logical_key}:{player_message_id}:{timeout_phase}',
        )

    last_status = None
    while True:
        maybe_heartbeat_fn()
        run_state = fetch_run(args.api_base, args.owner_api_key, run_id).get('run') or {}
        external_status = str(run_state.get('status') or '').strip().lower()
        if external_status in {'stop_requested', 'stopped', 'failed', 'completed'}:
            return None, run_state, None

        try:
            last_status = autonomous.fetch_dm_turn_status(manifest, player_message_id)
        except ApiError:
            pass
        else:
            if dm_response_state.dm_turn_fully_resolved(last_status):
                turn_status = str(last_status.get('status') or '').strip().lower()
                post_turn_status = str(last_status.get('post_turn_status') or '').strip().lower()
                if turn_status == 'error' or post_turn_status == 'error':
                    _exhausted_event('dm_turn_error', last_status)
                    terminal_error = (
                        last_status.get('turn_error')
                        or last_status.get('error_text')
                        or last_status.get('post_turn_error')
                        or dm_response_state.classify_timeout(last_status, 'post_turn')
                    )
                    return None, None, terminal_error
                append_event(
                    args.api_base,
                    args.owner_api_key,
                    run_id,
                    args.worker_id,
                    lease_token,
                    'dm_turn_reconciliation_recovered',
                    {
                        'automation_run_id': run_id,
                        'player_message_id': player_message_id,
                        'dm_message_id': last_status.get('dm_message_id'),
                        'timeout_phase': timeout_phase,
                        'timeout_classification': timeout_error,
                        'reconciliation_outcome': 'recovered',
                        'reconciliation_elapsed_seconds': round(time.monotonic() - started_monotonic, 3),
                        'final_status': last_status.get('status'),
                        'final_post_turn_status': post_turn_status,
                        'final_memory_status': last_status.get('memory_status'),
                        'final_clock_status': last_status.get('clock_status'),
                    },
                    status='running',
                    dedupe_key=f'dm_turn_reconciliation_recovered:{logical_key}:{player_message_id}',
                )
                return last_status, None, None

        if time.monotonic() >= deadline:
            _exhausted_event('window_exhausted', last_status)
            return None, None, timeout_error
        remaining = deadline - time.monotonic()
        time.sleep(min(args.poll_interval, max(0.05, remaining)))


def wait_for_dm_response(args, manifest, player_message_id, maybe_heartbeat_fn):
    visible_timeout, post_turn_timeout = dm_response_state.resolve_dm_response_timeouts(args)

    def fetch_status():
        return autonomous.fetch_dm_turn_status(manifest, player_message_id)

    return dm_response_state.wait_for_dm_response(
        fetch_status,
        maybe_heartbeat_fn,
        visible_timeout,
        post_turn_timeout,
        args.poll_interval,
        transient_error_types=(ApiError,),
    )


def pause_for_audit_if_needed(args, claim_payload, run_id, lease_token, phase, maybe_heartbeat_fn, **payload):
    if phase not in pause_phases(claim_payload):
        return False, lease_token
    response = pause_run(
        args.api_base,
        args.owner_api_key,
        run_id,
        args.worker_id,
        lease_token,
        phase,
        payload=payload.get('payload'),
        summary=payload.get('summary'),
        player_message_id=payload.get('player_message_id'),
        dm_message_id=payload.get('dm_message_id'),
        dedupe_key=payload.get('dedupe_key'),
    )
    lease_token = ((response.get('run') or {}).get('lease_token')) or lease_token
    paused = bool(response.get('paused', True))
    if not paused:
        return False, lease_token

    if response.get('worker_released'):
        return True, None

    cycle = response.get('audit_cycle') or {}
    cycle_id = cycle.get('id')
    append_event(
        args.api_base,
        args.owner_api_key,
        run_id,
        args.worker_id,
        lease_token,
        'worker_waiting_for_audit_resume',
        {'audit_cycle_id': cycle_id, 'phase': phase},
        dedupe_key=f'worker_waiting_for_audit_resume:{run_id}:{cycle_id or phase}',
    )
    stopped, lease_token = wait_for_audit_resume(
        args,
        run_id,
        lease_token,
        maybe_heartbeat_fn,
    )
    if stopped:
        return True, lease_token
    append_event(
        args.api_base,
        args.owner_api_key,
        run_id,
        args.worker_id,
        lease_token,
        'worker_resumed_after_audit',
        {'audit_cycle_id': cycle_id, 'phase': phase},
        dedupe_key=f'worker_resumed_after_audit:{run_id}:{cycle_id or phase}:{time.monotonic()}',
    )
    return False, lease_token


def wait_for_audit_resume(args, run_id, lease_token, maybe_heartbeat_fn):
    """Keep a claimed worker alive while an audit checkpoint is under review."""
    while True:
        run_payload = fetch_run(args.api_base, args.owner_api_key, run_id)
        run_state = run_payload.get('run') or {}
        status = str(run_state.get('status') or '').strip().lower()
        if status == 'running' and not run_state.get('awaiting_audit_cycle_id'):
            return False, lease_token
        if status in {'stop_requested', 'stopped', 'failed', 'completed'}:
            return True, lease_token
        maybe_heartbeat_fn()
        time.sleep(args.poll_interval)


def parse_utc_iso(iso_str):
    if not iso_str:
        return None
    if iso_str.endswith('Z'):
        iso_str = iso_str[:-1]
    if '+' in iso_str:
        iso_str = iso_str.split('+')[0]
    try:
        return datetime.fromisoformat(iso_str)
    except ValueError:
        return None


def execute_run(args, run_id):
    claim_payload = claim_run(args.api_base, args.owner_api_key, run_id, args.worker_id)
    run = claim_payload['run']
    lease_token = claim_payload['lease_token']

    # Extend lease to cover the entire initialization window before the first
    # heartbeat can be serviced (world generation blocks the single request
    # thread in SQLite deployments).  The background thread below then keeps
    # it extended as defense-in-depth.
    init_lease_seconds = getattr(args, 'init_lease_seconds', 900)
    resp = heartbeat(args.api_base, args.owner_api_key, run_id, args.worker_id, lease_token, lease_seconds=init_lease_seconds)
    lease_token = (resp.get('run') or {}).get('lease_token') or lease_token

    _init_lease_token = [lease_token]
    _init_hb_stop = threading.Event()

    def _init_heartbeat_loop():
        while not _init_hb_stop.is_set():
            try:
                resp = heartbeat(args.api_base, args.owner_api_key, run_id, args.worker_id, _init_lease_token[0], lease_seconds=init_lease_seconds)
                _init_lease_token[0] = (resp.get('run') or {}).get('lease_token') or _init_lease_token[0]
            except Exception:
                pass
            _init_hb_stop.wait(args.heartbeat_interval)

    hb_thread = threading.Thread(target=_init_heartbeat_loop, daemon=True)
    hb_thread.start()

    try:
        session_on_start, _ = ensure_campaign_initialized(args, claim_payload, session_start_timeout=getattr(args, 'session_start_timeout', 900))
    except Exception as exc:
        _init_hb_stop.set()
        hb_thread.join(timeout=5)
        lease_token = _init_lease_token[0]
        details = str(exc).strip()
        error_text = 'campaign_not_initialized'
        if details and details != error_text:
            error_text = f'{error_text}: {details}'
        complete_run(
            args.api_base,
            args.owner_api_key,
            run_id,
            args.worker_id,
            lease_token,
            status='failed',
            error_text=error_text,
            dedupe_key=f'run_completed:{run_id}:init-failed',
        )
        raise exc

    _init_hb_stop.set()
    hb_thread.join(timeout=5)
    lease_token = _init_lease_token[0]
    manifest = build_manifest_for_run(args.api_base, args.owner_api_key, claim_payload)

    resume_dm_wait_message_id = None
    resume_reconciliation_active = False
    reconciliation_player_message_id = run.get('reconciliation_player_message_id')
    reconciliation_deadline_str = run.get('reconciliation_deadline')
    reconciliation_started_at_str = run.get('reconciliation_started_at')
    reconciliation_timeout_phase = run.get('reconciliation_timeout_phase')
    reconciliation_timeout_error_str = run.get('reconciliation_timeout_error')
    reconciliation_deadline_utc = None
    reconciliation_started_at_utc = None

    if reconciliation_player_message_id and reconciliation_deadline_str:
        reconciliation_deadline_utc = parse_utc_iso(reconciliation_deadline_str)
        reconciliation_started_at_utc = parse_utc_iso(reconciliation_started_at_str)
        if reconciliation_deadline_utc:
            resume_reconciliation_active = True
            resume_dm_wait_message_id = reconciliation_player_message_id

    visible_timeout, post_turn_timeout = dm_response_state.resolve_dm_response_timeouts(args)
    append_event(
        args.api_base,
        args.owner_api_key,
        run_id,
        args.worker_id,
        lease_token,
        'run_started',
        {
            'worker_id': args.worker_id,
            'reclaimed': claim_payload.get('reclaimed', False),
            'dm_visible_response_timeout': visible_timeout,
            'dm_post_turn_timeout': post_turn_timeout,
        },
        status='running',
        dedupe_key=f'run_started:{run_id}:attempt:{run.get("attempt_count")}',
        reconciliation_player_message_id=reconciliation_player_message_id if resume_reconciliation_active else None,
        reconciliation_timeout_phase=reconciliation_timeout_phase if resume_reconciliation_active else None,
        reconciliation_timeout_error=reconciliation_timeout_error_str if resume_reconciliation_active else None,
        reconciliation_started_at=reconciliation_started_at_str if resume_reconciliation_active else None,
        reconciliation_deadline=reconciliation_deadline_str if resume_reconciliation_active else None,
    )

    start_time = time.monotonic()
    deadline = (
        start_time + (args.max_minutes * 60.0)
        if args.max_minutes is not None and args.max_minutes > 0
        else None
    )
    last_seen_fingerprint = None
    last_change_at = time.monotonic()
    last_heartbeat_at = 0.0
    turns_completed = (claim_payload.get('run') or {}).get('completed_turns') or 0
    same_fingerprint_no_action_retries = 0
    force_overseer_retry = False
    last_dm_turn = None

    run_config = runner_config(claim_payload)
    max_turns = run_config.get('max_turns') or run_config.get('max_cycles') or args.max_turns

    if session_on_start and not resume_reconciliation_active:
        latest_player_message_id = autonomous.find_latest_player_message_id(session_on_start.get('messages') or [])
        if latest_player_message_id is not None:
            try:
                dm_turn_status = autonomous.fetch_dm_turn_status(manifest, latest_player_message_id)
                if not autonomous.dm_turn_status_resolved(dm_turn_status):
                    resume_dm_wait_message_id = latest_player_message_id
                elif 'after_dm' in pause_phases(claim_payload):
                    initial_run_payload = fetch_run(args.api_base, args.owner_api_key, run_id)
                    audit_cycles = initial_run_payload.get('audit_cycles') or []
                    has_matching_after_dm = False
                    for cycle in audit_cycles:
                        if cycle.get('phase') == 'after_dm' and (
                            cycle.get('player_message_id') == latest_player_message_id
                            or (
                                dm_turn_status.get('dm_message_id') is not None
                                and cycle.get('dm_message_id') == dm_turn_status.get('dm_message_id')
                            )
                        ):
                            has_matching_after_dm = True
                            break
                    if not has_matching_after_dm:
                        resume_dm_wait_message_id = latest_player_message_id
                    elif str(dm_turn_status.get('status') or '').strip().lower() in {'silent', 'empty'}:
                        # A silent/empty DM turn is a completed state transition even
                        # though it creates no transcript message. Restore it across the
                        # audit lease boundary so the overseer knows the DM is finished.
                        last_seen_fingerprint = messages_fingerprint(session_on_start)
                        last_dm_turn = dm_turn_status
                        force_overseer_retry = True
                        last_change_at = time.monotonic()
            except Exception as exc:
                append_event(
                    args.api_base,
                    args.owner_api_key,
                    run_id,
                    args.worker_id,
                    lease_token,
                    "after_dm_resume_probe_error",
                    {
                        "latest_player_message_id": latest_player_message_id,
                        "error": str(exc),
                    },
                    dedupe_key=f"after_dm_resume_probe_error:{run_id}:{latest_player_message_id}",
                )
                if "after_dm" in pause_phases(claim_payload):
                    resume_dm_wait_message_id = latest_player_message_id

    def maybe_heartbeat():
        nonlocal last_heartbeat_at, lease_token
        if time.monotonic() - last_heartbeat_at < args.heartbeat_interval:
            return
        response = heartbeat(args.api_base, args.owner_api_key, run_id, args.worker_id, lease_token)
        lease_token = (response.get('run') or {}).get('lease_token') or lease_token
        last_heartbeat_at = time.monotonic()

    while True:
        maybe_heartbeat()
        if deadline is not None and time.monotonic() >= deadline:
            complete_run(
                args.api_base,
                args.owner_api_key,
                run_id,
                args.worker_id,
                lease_token,
                status='stopped',
                error_text='max_minutes',
                dedupe_key=f'run_completed:{run_id}:max_minutes',
            )
            return True

        run_payload = fetch_run(args.api_base, args.owner_api_key, run_id)
        run_state = run_payload.get('run') or {}
        if run_state.get('status') in {'stop_requested', 'stopped', 'failed'}:
            complete_run(
                args.api_base,
                args.owner_api_key,
                run_id,
                args.worker_id,
                lease_token,
                status='stopped',
                error_text=run_state.get('error_text'),
                dedupe_key=f'run_completed:{run_id}:external-stop',
            )
            return True

        session = active_session_from_run_payload(run_payload)
        if not session:
            complete_run(
                args.api_base,
                args.owner_api_key,
                run_id,
                args.worker_id,
                lease_token,
                status='failed',
                error_text='Run has no session',
                dedupe_key=f'run_completed:{run_id}:no-session',
            )
            return True

        if resume_dm_wait_message_id is not None:
            posted_message_id = resume_dm_wait_message_id
            resume_dm_wait_message_id = None
            last_seen_fingerprint = messages_fingerprint(session)
            session_for_prompt = {
                'id': session.get('id'),
                'started_at': session.get('started_at'),
                'is_active': session.get('is_active', True),
                'messages': session.get('messages') or [],
            }
            logical_key = stage_key(turns_completed, session_for_prompt)
            last_change_at = time.monotonic()
            if resume_reconciliation_active:
                resume_reconciliation_active = False
                dm_timed_out = True
                timeout_phase = run.get('reconciliation_timeout_phase') or 'post_turn'
                dm_turn = {
                    'status': 'speak',
                    'post_turn_status': 'pending',
                }
            else:
                dm_turn, dm_timed_out, timeout_phase = wait_for_dm_response(args, manifest, posted_message_id, maybe_heartbeat)
            append_event(
                args.api_base,
                args.owner_api_key,
                run_id,
                args.worker_id,
                lease_token,
                'dm_turn_status',
                dm_turn,
                dedupe_key=f'dm_turn_status:{logical_key}:{posted_message_id}',
            )
            if dm_turn.get('status') == 'error' or dm_turn.get('post_turn_status') == 'error':
                should_stop, lease_token = pause_for_audit_if_needed(
                    args,
                    claim_payload,
                    run_id,
                    lease_token,
                    'after_dm',
                    maybe_heartbeat,
                    payload={
                        'dm_turn': dm_turn,
                        'posted_message_id': posted_message_id,
                        'turns_completed': turns_completed,
                    },
                    summary='Paused after resolved DM error.',
                    player_message_id=posted_message_id,
                    dm_message_id=dm_turn.get('dm_message_id'),
                    dedupe_key=f'audit_pause:after_dm:{logical_key}:{posted_message_id}',
                )
                if should_stop:
                    if lease_token is None:
                        return False
                    complete_run(
                        args.api_base,
                        args.owner_api_key,
                        run_id,
                        args.worker_id,
                        lease_token,
                        status='stopped',
                        dedupe_key=f'run_completed:{run_id}:audit-stop',
                    )
                    return True

                failure_payload = {
                    'player_message_id': posted_message_id,
                    'dm_message_id': dm_turn.get('dm_message_id'),
                    'status': dm_turn.get('status'),
                    'post_turn_status': dm_turn.get('post_turn_status'),
                    'memory_status': dm_turn.get('memory_status', 'skipped'),
                    'clock_status': dm_turn.get('clock_status', 'skipped'),
                    'turn_error': dm_turn.get('turn_error') or dm_turn.get('error_text') or dm_turn.get('post_turn_error'),
                    'phase': 'after_dm',
                    'retry_count': 0,
                    'skipped_downstream_expectations': [
                        'memory_validation',
                        'clock_validation',
                    ],
                }
                append_event(
                    args.api_base,
                    args.owner_api_key,
                    run_id,
                    args.worker_id,
                    lease_token,
                    'dm_turn_failed',
                    failure_payload,
                    dedupe_key=f'dm_turn_failed:{logical_key}:{posted_message_id}',
                )
                error_classification = (
                    'dm_post_turn_error'
                    if str(dm_turn.get('post_turn_status') or '').strip().lower() == 'error'
                    else (dm_turn.get('turn_error') or dm_turn.get('error_text') or 'dm_turn_failed')
                )
                complete_run(
                    args.api_base,
                    args.owner_api_key,
                    run_id,
                    args.worker_id,
                    lease_token,
                    status='failed',
                    error_text=error_classification,
                    dedupe_key=f'run_completed:{run_id}:dm-failure:{posted_message_id}',
                )
                return True
            if dm_timed_out:
                timeout_error = dm_response_state.classify_timeout(dm_turn, timeout_phase)
                timeout_evidence = dm_response_state.build_timeout_evidence(dm_turn, timeout_phase)
                timeout_evidence['timeout_phase'] = timeout_phase
                visible_timeout, post_turn_timeout = dm_response_state.resolve_dm_response_timeouts(args)
                timeout_evidence['configured_timeouts'] = {
                    'visible_seconds': visible_timeout,
                    'post_turn_seconds': post_turn_timeout,
                }
                append_event(
                    args.api_base,
                    args.owner_api_key,
                    run_id,
                    args.worker_id,
                    lease_token,
                    'dm_turn_timeout',
                    timeout_evidence,
                    dedupe_key=f'dm_turn_timeout:{logical_key}:{posted_message_id}:{timeout_phase}',
                )
                is_resuming = (reconciliation_deadline_utc is not None)
                recovered_turn, stop_state, terminal_error = _reconcile_dm_turn_timeout(
                    args,
                    manifest,
                    claim_payload,
                    run_id,
                    lease_token,
                    posted_message_id,
                    logical_key,
                    timeout_phase,
                    timeout_error,
                    timeout_evidence,
                    maybe_heartbeat,
                    resumed_deadline_utc=reconciliation_deadline_utc if is_resuming else None,
                    resumed_started_at_utc=reconciliation_started_at_utc if is_resuming else None,
                )
                reconciliation_deadline_utc = None
                reconciliation_started_at_utc = None

                if stop_state is not None:
                    if stop_state.get('status') == 'stop_requested':
                        complete_run(
                            args.api_base,
                            args.owner_api_key,
                            run_id,
                            args.worker_id,
                            lease_token,
                            status='stopped',
                            error_text=stop_state.get('error_text'),
                            dedupe_key=f'run_completed:{run_id}:external-stop',
                        )
                        return True
                    else:
                        return True
                if recovered_turn is None:
                    complete_run(
                        args.api_base,
                        args.owner_api_key,
                        run_id,
                        args.worker_id,
                        lease_token,
                        status='failed',
                        error_text=terminal_error,
                        dedupe_key=f'run_completed:{run_id}:dm-timeout:{timeout_phase}',
                    )
                    return True
                dm_turn = recovered_turn
            last_dm_turn = dm_turn if dm_turn.get('status') in {'silent', 'empty'} else None
            force_overseer_retry = True
            same_fingerprint_no_action_retries = 0
            last_change_at = time.monotonic()
            should_stop, lease_token = pause_for_audit_if_needed(
                args,
                claim_payload,
                run_id,
                lease_token,
                'after_dm',
                maybe_heartbeat,
                payload={
                    'dm_turn': dm_turn,
                    'posted_message_id': posted_message_id,
                    'turns_completed': turns_completed,
                },
                summary='Paused after DM response.',
                player_message_id=posted_message_id,
                dm_message_id=dm_turn.get('dm_message_id'),
                dedupe_key=f'audit_pause:after_dm:{logical_key}:{posted_message_id}',
            )
            if should_stop:
                if lease_token is None:
                    return False
                complete_run(
                    args.api_base,
                    args.owner_api_key,
                    run_id,
                    args.worker_id,
                    lease_token,
                    status='stopped',
                    dedupe_key=f'run_completed:{run_id}:audit-stop',
                )
                return True
            last_change_at = time.monotonic()

            if turns_completed >= max_turns:
                complete_run(
                    args.api_base,
                    args.owner_api_key,
                    run_id,
                    args.worker_id,
                    lease_token,
                    status='completed',
                    dedupe_key=f'run_completed:{run_id}:max-turns',
                )
                return True
            continue

        if turns_completed >= max_turns:
            complete_run(
                args.api_base,
                args.owner_api_key,
                run_id,
                args.worker_id,
                lease_token,
                status='completed',
                dedupe_key=f'run_completed:{run_id}:max-turns',
            )
            return True

        current_fingerprint = messages_fingerprint(session)
        fingerprint_changed = current_fingerprint != last_seen_fingerprint
        if fingerprint_changed:
            last_seen_fingerprint = current_fingerprint
            last_change_at = time.monotonic()
            same_fingerprint_no_action_retries = 0
            force_overseer_retry = False
            last_dm_turn = None

        if fingerprint_changed or force_overseer_retry:
            if 'after_dm' in pause_phases(claim_payload) and resume_dm_wait_message_id is None:
                audit_cycles_raw = run_payload.get('audit_cycles') or []
                if audit_cycles_raw:
                    last_cycle = audit_cycles_raw[-1] if isinstance(audit_cycles_raw[-1], dict) else {}
                    if last_cycle.get('phase') == 'after_player':
                        last_player_msg_id = last_cycle.get('player_message_id')
                        if last_player_msg_id is not None:
                            has_after_dm = any(
                                c.get('phase') == 'after_dm'
                                and (
                                    c.get('player_message_id') == last_player_msg_id
                                    or (
                                        last_cycle.get('dm_message_id') is not None
                                        and c.get('dm_message_id') == last_cycle.get('dm_message_id')
                                    )
                                )
                                for c in audit_cycles_raw
                            )
                            if not has_after_dm:
                                resume_dm_wait_message_id = last_player_msg_id
                                continue
            try:
                session_for_prompt = {
                    'id': session.get('id'),
                    'started_at': session.get('started_at'),
                    'is_active': session.get('is_active', True),
                    'messages': session.get('messages') or [],
                }
                logical_key = stage_key(turns_completed, session_for_prompt)
                overseer = request_overseer_decision(
                    args,
                    manifest,
                    claim_payload,
                    session_for_prompt,
                    turns_completed,
                    last_dm_turn,
                    run_id,
                    lease_token,
                )
                last_dm_turn = None
                append_event(
                    args.api_base,
                    args.owner_api_key,
                    run_id,
                    args.worker_id,
                    lease_token,
                    'overseer_decision',
                    {'overseer': overseer},
                    dedupe_key=f'overseer_decision:{logical_key}',
                )
                if overseer['action'] == 'no_action':
                    force_overseer_retry = False
                    same_fingerprint_no_action_retries += 1
                    append_event(
                        args.api_base,
                        args.owner_api_key,
                        run_id,
                        args.worker_id,
                        lease_token,
                        'turn_result',
                        {
                            'action': 'no_action',
                            'overseer': overseer,
                            'turns_completed': turns_completed,
                        },
                        dedupe_key=f'turn_result:no_action:{logical_key}',
                    )
                    time.sleep(args.poll_interval)
                    continue

                roster_entry = next(
                    (entry for entry in (claim_payload.get('roster') or []) if (entry.get('llm_player_id') or entry.get('user_id')) == overseer['llm_player_id']),
                    None,
                )
                if roster_entry is None:
                    raise RuntimeError(f'No roster entry for llm_player_id {overseer["llm_player_id"]}')

                campaign_id = claim_payload['derived_campaign']['id']
                world_payload = api_get(args.api_base, f'/api/campaigns/{campaign_id}/world', api_key=args.owner_api_key)
                campaign = api_get(args.api_base, f'/api/campaigns/{campaign_id}', api_key=args.owner_api_key)['campaign']
                campaign_characters = fetch_campaign_characters(args.api_base, args.owner_api_key, campaign_id)
                chosen_player = select_chosen_player(roster_entry, campaign_characters)
                pending_proposals = [
                    proposal for proposal in (session.get('pending_sheet_proposals') or [])
                    if proposal.get('character_id') == chosen_player['character'].get('id')
                ]
                decision, response_text, json_retry_count, provider_name, provider_model = request_player_decision(
                    args,
                    manifest,
                    claim_payload,
                    campaign,
                    world_payload,
                    session_for_prompt,
                    chosen_player,
                    pending_proposals,
                    turns_completed,
                    run_id,
                    lease_token,
                )
                if (decision.get('action') or '').strip().lower() == 'roll':
                    roll_summary = orchestrator.execute_player_roll(decision.get('label'), decision.get('expression'))
                    decision['content'] = orchestrator.build_player_roll_message(decision.get('content'), roll_summary)
                result = submit_decision(
                    args.api_base,
                    args.owner_api_key,
                    run_id,
                    chosen_player,
                    decision,
                    dedupe_key=f'player_message:{logical_key}:{chosen_player["llm_player"]["id"]}',
                    worker_id=args.worker_id,
                    lease_token=lease_token,
                )
                posted_message_id = (result.get('message') or {}).get('id')
                append_event(
                    args.api_base,
                    args.owner_api_key,
                    run_id,
                    args.worker_id,
                    lease_token,
                    'player_decision',
                    {
                        'speaker': {
                            'llm_player_id': chosen_player['llm_player']['id'],
                            'label': chosen_player['llm_player']['label'],
                            'character_name': chosen_player['character'].get('name'),
                        },
                        'decision': decision,
                        'raw_response_text': response_text,
                        'json_retry_count': json_retry_count,
                        'provider': provider_name,
                        'model': provider_model,
                        'posted_message_id': posted_message_id,
                    },
                    dedupe_key=f'player_decision:{logical_key}:{chosen_player["llm_player"]["id"]}',
                )
                if (decision.get('action') or '').strip().lower() != 'no_action':
                    turns_completed += 1

                # Log turn result event here BEFORE any pause!
                append_event(
                    args.api_base,
                    args.owner_api_key,
                    run_id,
                    args.worker_id,
                    lease_token,
                    'turn_result',
                    {
                        'campaign_id': campaign_id,
                        'session_id': session_for_prompt['id'],
                        'speaker': {
                            'llm_player_id': chosen_player['llm_player']['id'],
                            'label': chosen_player['llm_player']['label'],
                            'character_name': chosen_player['character'].get('name'),
                        } if chosen_player else None,
                        'action': (decision.get('action') or '').strip().lower(),
                        'turns_completed': turns_completed,
                        'json_retry_count': json_retry_count,
                    },
                    dedupe_key=f'turn_result:{logical_key}',
                )

                if posted_message_id is not None:
                    should_stop, lease_token = pause_for_audit_if_needed(
                        args,
                        claim_payload,
                        run_id,
                        lease_token,
                        'after_player',
                        maybe_heartbeat,
                        payload={
                            'speaker': chosen_player['llm_player'],
                            'character': chosen_player['character'],
                            'decision': decision,
                            'posted_message_id': posted_message_id,
                            'turns_completed': turns_completed,
                        },
                        summary=f'Paused after player action from {chosen_player["character"].get("name") or chosen_player["llm_player"]["label"]}.',
                        player_message_id=posted_message_id,
                        dedupe_key=f'audit_pause:after_player:{logical_key}:{posted_message_id}',
                    )
                    if should_stop:
                        if lease_token is None:
                            return False
                        complete_run(
                            args.api_base,
                            args.owner_api_key,
                            run_id,
                            args.worker_id,
                            lease_token,
                            status='stopped',
                            dedupe_key=f'run_completed:{run_id}:audit-stop',
                        )
                        return True
                    last_change_at = time.monotonic()
                    dm_turn, dm_timed_out, timeout_phase = wait_for_dm_response(args, manifest, posted_message_id, maybe_heartbeat)
                    append_event(
                        args.api_base,
                        args.owner_api_key,
                        run_id,
                        args.worker_id,
                        lease_token,
                        'dm_turn_status',
                        dm_turn,
                        dedupe_key=f'dm_turn_status:{logical_key}:{posted_message_id}',
                    )
                    if dm_turn.get('status') == 'error' or dm_turn.get('post_turn_status') == 'error':
                        should_stop, lease_token = pause_for_audit_if_needed(
                            args,
                            claim_payload,
                            run_id,
                            lease_token,
                            'after_dm',
                            maybe_heartbeat,
                            payload={
                                'dm_turn': dm_turn,
                                'posted_message_id': posted_message_id,
                                'turns_completed': turns_completed,
                            },
                            summary='Paused after resolved DM error.',
                            player_message_id=posted_message_id,
                            dm_message_id=dm_turn.get('dm_message_id'),
                            dedupe_key=f'audit_pause:after_dm:{logical_key}:{posted_message_id}',
                        )
                        if should_stop:
                            if lease_token is None:
                                return False
                            complete_run(
                                args.api_base,
                                args.owner_api_key,
                                run_id,
                                args.worker_id,
                                lease_token,
                                status='stopped',
                                dedupe_key=f'run_completed:{run_id}:audit-stop',
                            )
                            return True

                        failure_payload = {
                            'player_message_id': posted_message_id,
                            'dm_message_id': dm_turn.get('dm_message_id'),
                            'status': dm_turn.get('status'),
                            'post_turn_status': dm_turn.get('post_turn_status'),
                            'memory_status': dm_turn.get('memory_status', 'skipped'),
                            'clock_status': dm_turn.get('clock_status', 'skipped'),
                            'turn_error': dm_turn.get('turn_error') or dm_turn.get('error_text') or dm_turn.get('post_turn_error'),
                            'phase': 'after_dm',
                            'retry_count': 0,
                            'skipped_downstream_expectations': [
                                'memory_validation',
                                'clock_validation',
                            ],
                        }
                        append_event(
                            args.api_base,
                            args.owner_api_key,
                            run_id,
                            args.worker_id,
                            lease_token,
                            'dm_turn_failed',
                            failure_payload,
                            dedupe_key=f'dm_turn_failed:{logical_key}:{posted_message_id}',
                        )
                        error_classification = (
                            'dm_post_turn_error'
                            if str(dm_turn.get('post_turn_status') or '').strip().lower() == 'error'
                            else (dm_turn.get('turn_error') or dm_turn.get('error_text') or 'dm_turn_failed')
                        )
                        complete_run(
                            args.api_base,
                            args.owner_api_key,
                            run_id,
                            args.worker_id,
                            lease_token,
                            status='failed',
                            error_text=error_classification,
                            dedupe_key=f'run_completed:{run_id}:dm-failure:{posted_message_id}',
                        )
                        return True
                    if dm_timed_out:
                        timeout_error = dm_response_state.classify_timeout(dm_turn, timeout_phase)
                        timeout_evidence = dm_response_state.build_timeout_evidence(dm_turn, timeout_phase)
                        timeout_evidence['timeout_phase'] = timeout_phase
                        visible_timeout, post_turn_timeout = dm_response_state.resolve_dm_response_timeouts(args)
                        timeout_evidence['configured_timeouts'] = {
                            'visible_seconds': visible_timeout,
                            'post_turn_seconds': post_turn_timeout,
                        }
                        append_event(
                            args.api_base,
                            args.owner_api_key,
                            run_id,
                            args.worker_id,
                            lease_token,
                            'dm_turn_timeout',
                            timeout_evidence,
                            dedupe_key=f'dm_turn_timeout:{logical_key}:{posted_message_id}:{timeout_phase}',
                        )
                        is_resuming = (reconciliation_deadline_utc is not None)
                        recovered_turn, stop_state, terminal_error = _reconcile_dm_turn_timeout(
                            args,
                            manifest,
                            claim_payload,
                            run_id,
                            lease_token,
                            posted_message_id,
                            logical_key,
                            timeout_phase,
                            timeout_error,
                            timeout_evidence,
                            maybe_heartbeat,
                            resumed_deadline_utc=reconciliation_deadline_utc if is_resuming else None,
                            resumed_started_at_utc=reconciliation_started_at_utc if is_resuming else None,
                        )
                        reconciliation_deadline_utc = None
                        reconciliation_started_at_utc = None

                        if stop_state is not None:
                            if stop_state.get('status') == 'stop_requested':
                                complete_run(
                                    args.api_base,
                                    args.owner_api_key,
                                    run_id,
                                    args.worker_id,
                                    lease_token,
                                    status='stopped',
                                    error_text=stop_state.get('error_text'),
                                    dedupe_key=f'run_completed:{run_id}:external-stop',
                                )
                                return True
                            else:
                                return True
                        if recovered_turn is None:
                            complete_run(
                                args.api_base,
                                args.owner_api_key,
                                run_id,
                                args.worker_id,
                                lease_token,
                                status='failed',
                                error_text=terminal_error,
                                dedupe_key=f'run_completed:{run_id}:dm-timeout:{timeout_phase}',
                            )
                            return True
                        dm_turn = recovered_turn
                    last_dm_turn = dm_turn if dm_turn.get('status') in {'silent', 'empty'} else None
                    force_overseer_retry = True
                    same_fingerprint_no_action_retries = 0
                    last_change_at = time.monotonic()
                    should_stop, lease_token = pause_for_audit_if_needed(
                        args,
                        claim_payload,
                        run_id,
                        lease_token,
                        'after_dm',
                        maybe_heartbeat,
                        payload={
                            'dm_turn': dm_turn,
                            'posted_message_id': posted_message_id,
                            'turns_completed': turns_completed,
                        },
                        summary='Paused after DM response.',
                        player_message_id=posted_message_id,
                        dm_message_id=dm_turn.get('dm_message_id'),
                        dedupe_key=f'audit_pause:after_dm:{logical_key}:{posted_message_id}',
                    )
                    if should_stop:
                        if lease_token is None:
                            return False
                        complete_run(
                            args.api_base,
                            args.owner_api_key,
                            run_id,
                            args.worker_id,
                            lease_token,
                            status='stopped',
                            dedupe_key=f'run_completed:{run_id}:audit-stop',
                        )
                        return True
                    last_change_at = time.monotonic()
                else:
                    force_overseer_retry, same_fingerprint_no_action_retries = autonomous.update_same_fingerprint_retry_state(
                        (decision.get('action') or '').strip().lower(),
                        same_fingerprint_no_action_retries,
                        len(manifest.get('llm_players') or []),
                    )



                if turns_completed >= max_turns:
                    complete_run(
                        args.api_base,
                        args.owner_api_key,
                        run_id,
                        args.worker_id,
                        lease_token,
                        status='completed',
                        dedupe_key=f'run_completed:{run_id}:max-turns',
                    )
                    return True
            except Exception as exc:
                append_event(
                    args.api_base,
                    args.owner_api_key,
                    run_id,
                    args.worker_id,
                    lease_token,
                    'error',
                    {
                        'error': str(exc),
                        'turns_completed': turns_completed,
                    },
                    status='running',
                    error_text=str(exc),
                    dedupe_key=f'error:{run_id}:{turns_completed}:{type(exc).__name__}',
                )
                complete_run(
                    args.api_base,
                    args.owner_api_key,
                    run_id,
                    args.worker_id,
                    lease_token,
                    status='failed',
                    error_text=str(exc),
                    dedupe_key=f'run_completed:{run_id}:error:{type(exc).__name__}',
                )
                return True

        if time.monotonic() - last_change_at >= args.idle_timeout:
            complete_run(
                args.api_base,
                args.owner_api_key,
                run_id,
                args.worker_id,
                lease_token,
                status='stopped',
                error_text='idle_timeout',
                dedupe_key=f'run_completed:{run_id}:idle-timeout',
            )
            return True

        time.sleep(args.poll_interval)


def main():
    args = parse_args()
    if not args.owner_api_key:
        raise SystemExit('DND_OWNER_API_KEY or --owner-api-key is required')
    args.worker_id = resolve_worker_id(args.worker_id)
    print(f'Worker ID: {args.worker_id}', file=sys.stderr)

    while True:
        candidate_ids = [args.run_id] if args.run_id else list_candidate_run_ids(args.api_base, args.owner_api_key, worker_id=args.worker_id)
        if not candidate_ids:
            if args.once:
                return
            time.sleep(args.poll_interval)
            continue

        claimed_one = False
        for run_id in candidate_ids:
            try:
                execute_run(args, run_id)
                claimed_one = True
                break
            except ApiError as exc:
                if 'HTTP 409' in str(exc) and not args.run_id:
                    continue
                raise

        if args.run_id or args.once:
            return
        if not claimed_one:
            time.sleep(args.poll_interval)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'error: {exc}', file=sys.stderr)
        sys.exit(1)

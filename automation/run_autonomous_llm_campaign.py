#!/usr/bin/env python3
import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
from datetime import datetime, timezone

from llm_campaign_common import (
    STATE_DIR,
    ApiError,
    api_get,
    default_api_base,
    default_opencode_server,
    load_manifest,
    save_manifest,
    start_session,
)
from provider_client import request_json_decision
import dm_response_state


ROOT = pathlib.Path(__file__).resolve().parents[1]

OVERSEER_SYSTEM_PROMPT = """You are an auto-player overseer for a live AI-run tabletop campaign.

Choose exactly one LLM player to act next when the scene calls for it.

Rules:
- Read the current campaign, world, roster, and recent session messages.
- Choose the player who is most directly positioned to respond to the latest live state.
- Prefer the player who was directly addressed by the DM, is already in the active sub-scene, or is best placed to answer an unresolved question or follow through on the last declared plan.
- If the latest visible DM prompt asks the group, multiple PCs, or the party as a whole for checks, scouting, reactions, or declared actions, continue choosing eligible players until the directly implicated PCs have answered. Do not stop after the first player's roll if the visible prompt still clearly applies to others.
- When the most recent visible player action is a roll, decide whether that roll fully satisfied the latest DM prompt or only satisfied one participant's share of a group prompt. Only return no_action when the visible transcript shows the DM is truly next, not merely when one player has already acted.
- Keep the game moving, but do not force a player action just to rotate speakers.
- Do not use hard-coded round-robin reasoning.
- The context may include a `last_dm_turn` object describing the DM's most recent completed decision for the latest player message. `status` will be `speak`, `silent`, or `empty`. If `silent` or `empty`, the DM intentionally chose not to add a visible message; treat the DM's turn as finished and decide whether the next player should act based on the visible transcript and last visible DM prompt.
- If no meaningful player action should happen yet, return no_action.

Return strict JSON only:
- {"action":"choose_player","llm_player_id":123,"reason":"short reason"}
- {"action":"no_action","reason":"short reason"}
Do not wrap the JSON in markdown fences or add any extra text.
"""


def parse_args():
    parser = argparse.ArgumentParser(description='Bootstrap and run a fully autonomous LLM campaign loop.')
    parser.add_argument('--manifest', help='Existing manifest path. If omitted, a fresh campaign is bootstrapped.')
    parser.add_argument('--fresh', action='store_true', help='Always bootstrap a new campaign before looping.')
    parser.add_argument('--owner-token', help='Bearer token for the campaign owner')
    parser.add_argument('--owner-api-key', default=os.environ.get('DND_OWNER_API_KEY'), help='Owner automation API key')
    parser.add_argument('--api-base', default=default_api_base(), help='Base app URL, for example http://127.0.0.1:5889')
    parser.add_argument('--campaign-name', help='Optional explicit campaign name override')
    parser.add_argument('--description', help='Optional explicit campaign description override')
    parser.add_argument('--seed', help='Optional deterministic seed for quick-create')
    parser.add_argument('--difficulty', help='Optional difficulty override')
    parser.add_argument('--loot-mode', choices=('frequent_gamble', 'rare_quality'), default='rare_quality')
    parser.add_argument('--llm-count', type=int, default=3, help='Number of LLM players to create')
    parser.add_argument('--required-players', type=int, help='Defaults to llm-count when omitted')
    parser.add_argument('--label-prefix', default='Auto Player', help='Base label for created LLM players')
    parser.add_argument(
        '--session-start-timeout',
        type=int,
        default=int(os.environ.get('DND_SESSION_START_TIMEOUT', '180')),
        help='Seconds to wait for initial session creation and opening DM scene',
    )
    parser.add_argument('--opencode-server', default=default_opencode_server())
    parser.add_argument('--opencode-password', default=os.environ.get('OPENCODE_SERVER_PASSWORD'))
    parser.add_argument('--model', default=os.environ.get('OPENCODE_MODEL', 'opencode-go/deepseek-v4-flash'))
    parser.add_argument('--message-window', type=int, default=16, help='How many recent messages to include in the player prompt')
    parser.add_argument('--poll-interval', type=float, default=5.0, help='Polling interval in seconds')
    parser.add_argument(
        '--dm-response-timeout',
        type=float,
        default=float(os.environ.get('DND_DM_RESPONSE_TIMEOUT', '300')),
        help='Stop the runner if the DM does not finish responding to the latest player message within this many seconds',
    )
    _visible_env = os.environ.get('DND_DM_VISIBLE_RESPONSE_TIMEOUT')
    _post_turn_env = os.environ.get('DND_DM_POST_TURN_TIMEOUT')
    parser.add_argument('--dm-visible-response-timeout', type=float, default=float(_visible_env) if _visible_env else None)
    parser.add_argument('--dm-post-turn-timeout', type=float, default=float(_post_turn_env) if _post_turn_env else None)
    parser.add_argument('--idle-timeout', type=float, default=180.0, help='Stop if the transcript stops changing for this many seconds')
    parser.add_argument('--max-turns', type=int, default=50, help='Maximum number of non-no_action orchestrator steps to run')
    parser.add_argument('--max-minutes', type=float, default=None, help='Maximum wall-clock runtime in minutes')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--stop-on-error', action='store_true')
    return parser.parse_args()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def build_overseer_context(manifest, session, message_window, last_dm_turn=None):
    api_base = manifest['api_base']
    owner_token = manifest['owner'].get('token')
    owner_api_key = manifest['owner'].get('api_key') or os.environ.get('DND_OWNER_API_KEY')
    campaign_id = manifest['campaign']['id']
    campaign = api_get(
        api_base,
        f'/api/campaigns/{campaign_id}',
        owner_token=owner_token,
        api_key=owner_api_key,
    )['campaign']
    world_payload = api_get(
        api_base,
        f'/api/campaigns/{campaign_id}/world',
        owner_token=owner_token,
        api_key=owner_api_key,
    )
    context = {
        'campaign': {
            'id': campaign['id'],
            'name': campaign['name'],
            'description': campaign.get('description'),
            'seed': campaign.get('seed'),
        },
        'world': world_payload.get('world'),
        'session': {
            'id': session['id'],
            'started_at': session.get('started_at'),
            'is_active': session.get('is_active'),
        },
        'roster': [
            {
                'llm_player_id': entry['llm_player']['id'],
                'label': entry['llm_player']['label'],
                'character_name': entry['character']['name'],
            }
            for entry in manifest['llm_players']
        ],
        'recent_messages': (session.get('messages') or [])[-message_window:],
        'message_window': message_window,
    }
    if last_dm_turn:
        context['last_dm_turn'] = last_dm_turn
    return context


def build_overseer_prompt(context):
    return (
        'Choose exactly one LLM player to act next, or return no_action if the DM is not waiting on a meaningful player move.\n\n'
        'Current structured state:\n'
        f'{json.dumps(context, indent=2)}'
    )


def build_overseer_retry_prompt(context, manifest, error):
    valid_ids = [entry['llm_player']['id'] for entry in manifest['llm_players']]
    return (
        build_overseer_prompt(context)
        + '\n\n'
        + 'Your previous selection was invalid for this campaign. '
        + f'Valid llm_player_id values are exactly: {valid_ids}. '
        + f'Error: {error}. '
        + 'Reply again with either {"action":"choose_player","llm_player_id":<one of those ids>,"reason":"..."} '
        + 'or {"action":"no_action","reason":"..."}.'
    )


def request_overseer_decision(server, password, model_payload, prompt):
    del server, password
    result = request_json_decision(
        OVERSEER_SYSTEM_PROMPT,
        prompt,
        model=model_payload if isinstance(model_payload, str) else None,
        timeout_seconds=120,
        max_attempts=2,
    )
    return result['decision'], result['raw_response_text'], result['json_retry_count']


def normalize_overseer_decision(manifest, decision):
    action = str((decision or {}).get('action') or '').strip().lower()
    reason = str((decision or {}).get('reason') or '').strip()
    if action == 'no_action':
        return {
            'action': 'no_action',
            'llm_player_id': None,
            'reason': reason,
        }
    if action != 'choose_player':
        raise RuntimeError(f'Unsupported overseer action: {action or "<missing>"}')
    try:
        llm_player_id = int(decision['llm_player_id'])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError('Overseer choose_player action requires an integer llm_player_id') from exc
    player_ids = {entry['llm_player']['id'] for entry in manifest['llm_players']}
    if llm_player_id not in player_ids:
        raise RuntimeError(f'Overseer chose unknown llm_player_id {llm_player_id}')
    return {
        'action': 'choose_player',
        'llm_player_id': llm_player_id,
        'reason': reason,
    }


def choose_player_with_overseer(args, manifest, session, last_dm_turn=None):
    context = build_overseer_context(manifest, session, args.message_window, last_dm_turn=last_dm_turn)
    prompt = build_overseer_prompt(context)
    model_payload = args.model
    validation_retry_count = 0
    last_error = None
    last_response_text = ''

    for _ in range(3):
        decision, response_text, json_retry_count = request_overseer_decision(
            args.opencode_server,
            args.opencode_password,
            model_payload,
            prompt,
        )
        last_response_text = response_text
        try:
            normalized = normalize_overseer_decision(manifest, decision)
            normalized['raw_response_text'] = response_text
            normalized['json_retry_count'] = json_retry_count
            normalized['validation_retry_count'] = validation_retry_count
            return normalized
        except RuntimeError as exc:
            last_error = exc
            validation_retry_count += 1
            prompt = build_overseer_retry_prompt(context, manifest, exc)

    raise RuntimeError(f'{last_error}; last response: {last_response_text}')


def session_fingerprint(session):
    messages = session.get('messages') or []
    last_id = messages[-1].get('id') if messages else None
    last_created_at = messages[-1].get('created_at') if messages else None
    return {
        'session_id': session.get('id'),
        'is_active': bool(session.get('is_active')),
        'message_count': len(messages),
        'last_message_id': last_id,
        'last_message_created_at': last_created_at,
    }


def fetch_session(manifest, message_limit):
    api_base = manifest['api_base']
    owner_token = manifest['owner'].get('token')
    owner_api_key = manifest['owner'].get('api_key') or os.environ.get('DND_OWNER_API_KEY')
    session_id = manifest['session']['id']
    return api_get(
        api_base,
        f'/api/sessions/{session_id}',
        owner_token=owner_token,
        api_key=owner_api_key,
        query={'limit': message_limit},
    )['session']


def find_latest_player_message_id(posted_messages):
    for message in reversed(posted_messages or []):
        if not isinstance(message, dict):
            continue
        if message.get('role') != 'player':
            continue
        message_id = message.get('id')
        if message_id is not None:
            return message_id
    return None


def fetch_dm_turn_status(manifest, player_message_id):
    api_base = manifest['api_base']
    owner_token = manifest['owner'].get('token')
    owner_api_key = manifest['owner'].get('api_key') or os.environ.get('DND_OWNER_API_KEY')
    session_id = manifest['session']['id']
    return api_get(
        api_base,
        f'/api/sessions/{session_id}/dm-turn-status',
        owner_token=owner_token,
        api_key=owner_api_key,
        query={'after_message_id': player_message_id},
    ) or {'status': 'pending', 'player_message_id': player_message_id}


def dm_turn_status_resolved(dm_turn_status):
    return dm_response_state.dm_turn_fully_resolved(dm_turn_status)


def wait_for_dm_response(args, manifest, player_message_id):
    """Poll the DM turn status until the visible turn and post-turn work have settled.

    Returns (dm_turn_status_dict, timed_out: bool, timeout_phase: str or None).
    """
    visible_timeout, post_turn_timeout = dm_response_state.resolve_dm_response_timeouts(args)

    def fetch_status():
        return fetch_dm_turn_status(manifest, player_message_id)

    last_status, timed_out, timeout_phase = dm_response_state.wait_for_dm_response(
        fetch_status,
        lambda: None,
        visible_timeout,
        post_turn_timeout,
        args.poll_interval,
    )
    return last_status, timed_out, timeout_phase


def run_bootstrap(args):
    manifest_path = pathlib.Path(args.manifest) if args.manifest else STATE_DIR / f'llm-campaign-{int(time.time())}.json'
    command = [
        sys.executable,
        str(ROOT / 'automation' / 'bootstrap_llm_campaign.py'),
        '--api-base',
        args.api_base,
        '--loot-mode',
        args.loot_mode,
        '--llm-count',
        str(args.llm_count),
        '--label-prefix',
        args.label_prefix,
        '--manifest',
        str(manifest_path),
        '--session-start-timeout',
        str(args.session_start_timeout),
    ]
    if args.owner_token:
        command.extend(['--owner-token', args.owner_token])
    if args.owner_api_key:
        command.extend(['--owner-api-key', args.owner_api_key])
    if args.required_players is not None:
        command.extend(['--required-players', str(args.required_players)])
    if args.campaign_name:
        command.extend(['--campaign-name', args.campaign_name])
    if args.description is not None:
        command.extend(['--description', args.description])
    if args.seed:
        command.extend(['--seed', args.seed])
    if args.difficulty:
        command.extend(['--difficulty', args.difficulty])

    subprocess.run(command, cwd=ROOT, check=True)
    return manifest_path


def lock_path_for_manifest(manifest_path):
    return pathlib.Path(f'{manifest_path}.lock')


def process_is_running(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire_manifest_lock(manifest_path):
    manifest_path = pathlib.Path(manifest_path)
    lock_path = lock_path_for_manifest(manifest_path)

    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                existing_pid = int(lock_path.read_text(encoding='utf-8').strip())
            except (FileNotFoundError, ValueError):
                existing_pid = None

            if existing_pid and process_is_running(existing_pid):
                raise RuntimeError(
                    f'Another autonomous runner is already active for {manifest_path} '
                    f'(pid {existing_pid}).'
                )

            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            continue

        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(f'{os.getpid()}\n')
        return lock_path


def release_manifest_lock(lock_path):
    try:
        pathlib.Path(lock_path).unlink()
    except FileNotFoundError:
        pass


def run_orchestrator(args, manifest_path, player_id=None):
    command = [
        sys.executable,
        str(ROOT / 'automation' / 'run_llm_campaign_orchestrator.py'),
        str(manifest_path),
        '--opencode-server',
        args.opencode_server,
        '--model',
        args.model,
        '--message-window',
        str(args.message_window),
    ]
    if player_id is not None:
        command.extend(['--player-id', str(player_id)])
    if args.opencode_password:
        command.extend(['--opencode-password', args.opencode_password])
    if args.dry_run:
        command.append('--dry-run')

    completed = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip() or 'orchestrator failed'
        raise RuntimeError(stderr)
    return json.loads(completed.stdout)


def print_event(payload):
    print(json.dumps(payload), flush=True)


def update_same_fingerprint_retry_state(action, retry_count, player_count):
    if action != 'no_action':
        return False, 0
    next_retry_count = retry_count + 1
    retry_budget = max(int(player_count or 0), 1)
    return next_retry_count < retry_budget, next_retry_count


def resolve_manifest(args):
    manifest_path = pathlib.Path(args.manifest).expanduser() if args.manifest else None
    if args.fresh or manifest_path is None:
        return run_bootstrap(args)
    if not manifest_path.exists():
        raise SystemExit(f'Manifest not found: {manifest_path}')
    return manifest_path


def ensure_manifest_session_started(args, manifest_path, manifest):
    session_id = ((manifest.get('session') or {}).get('id'))
    if session_id:
        return manifest

    owner_token = manifest.get('owner', {}).get('token')
    owner_api_key = manifest.get('owner', {}).get('api_key') or os.environ.get('DND_OWNER_API_KEY')
    session = start_session(
        manifest['api_base'],
        manifest['campaign']['id'],
        owner_token=owner_token,
        api_key=owner_api_key,
        timeout=args.session_start_timeout,
    )
    manifest['session'] = {'id': session['id']}
    manifest['bootstrap_state'] = 'started'
    save_manifest(manifest_path, manifest)
    print_event({
        'event': 'session_started',
        'timestamp': utc_now(),
        'manifest': str(manifest_path),
        'campaign_id': manifest['campaign']['id'],
        'session_id': session['id'],
    })
    return manifest


def main():
    args = parse_args()
    manifest_path = resolve_manifest(args)
    lock_path = acquire_manifest_lock(manifest_path)
    try:
        manifest = load_manifest(manifest_path)
        manifest = ensure_manifest_session_started(args, manifest_path, manifest)
        start_time = time.monotonic()
        deadline = (
            start_time + (args.max_minutes * 60.0)
            if args.max_minutes is not None and args.max_minutes > 0
            else None
        )
        last_seen_fingerprint = None
        last_change_at = time.monotonic()
        turns_completed = 0
        error_count = 0
        force_overseer_retry = False
        same_fingerprint_no_action_retries = 0
        last_dm_turn = None

        print_event({
            'event': 'campaign_ready',
            'timestamp': utc_now(),
            'manifest': str(manifest_path),
            'campaign_id': manifest['campaign']['id'],
            'campaign_name': manifest['campaign']['name'],
            'session_id': manifest['session']['id'],
            'api_base': manifest['api_base'],
        })

        while True:
            if deadline is not None and time.monotonic() >= deadline:
                print_event({
                    'event': 'stop',
                    'timestamp': utc_now(),
                    'reason': 'max_minutes',
                    'turns_completed': turns_completed,
                    'errors': error_count,
                })
                return

            manifest = load_manifest(manifest_path)
            session = fetch_session(manifest, args.message_window)
            current_fingerprint = session_fingerprint(session)

            if not current_fingerprint['is_active']:
                print_event({
                    'event': 'stop',
                    'timestamp': utc_now(),
                    'reason': 'session_inactive',
                    'turns_completed': turns_completed,
                    'errors': error_count,
                })
                return

            fingerprint_changed = current_fingerprint != last_seen_fingerprint
            if fingerprint_changed:
                last_seen_fingerprint = current_fingerprint
                last_change_at = time.monotonic()
                force_overseer_retry = False
                same_fingerprint_no_action_retries = 0
                last_dm_turn = None

            if fingerprint_changed or force_overseer_retry:

                try:
                    overseer = choose_player_with_overseer(args, manifest, session, last_dm_turn=last_dm_turn)
                    last_dm_turn = None
                    if overseer['action'] == 'no_action':
                        force_overseer_retry = False
                        print_event({
                            'event': 'turn_result',
                            'timestamp': utc_now(),
                            'campaign_id': manifest['campaign']['id'],
                            'session_id': session['id'],
                            'speaker': None,
                            'action': 'no_action',
                            'dry_run': bool(args.dry_run),
                            'turns_completed': turns_completed,
                            'json_retry_count': overseer['json_retry_count'],
                            'overseer': overseer,
                        })
                        time.sleep(args.poll_interval)
                        continue

                    result = run_orchestrator(args, manifest_path, player_id=overseer['llm_player_id'])
                    result['overseer'] = overseer
                except Exception as exc:
                    error_count += 1
                    print_event({
                        'event': 'error',
                        'timestamp': utc_now(),
                        'error': str(exc),
                        'fingerprint': current_fingerprint,
                        'errors': error_count,
                    })
                    if args.stop_on_error:
                        raise
                    time.sleep(args.poll_interval)
                    continue

                action = ((result.get('decision') or {}).get('action') or '').strip().lower()
                if action != 'no_action':
                    turns_completed += 1

                posted_player_message_id = find_latest_player_message_id(result.get('posted_messages') or [])
                if posted_player_message_id is not None:
                    dm_turn, dm_timed_out, timeout_phase = wait_for_dm_response(args, manifest, posted_player_message_id)
                    if dm_timed_out:
                        timeout_error = dm_response_state.classify_timeout(dm_turn, timeout_phase)
                        print_event({
                            'event': 'stop',
                            'timestamp': utc_now(),
                            'reason': timeout_error,
                            'player_message_id': posted_player_message_id,
                            'dm_turn': dm_turn,
                            'timeout_phase': timeout_phase,
                            'turns_completed': turns_completed,
                            'errors': error_count,
                        })
                        return

                    last_dm_turn = dm_turn if dm_turn.get('status') in ('silent', 'empty') else None
                    session = fetch_session(manifest, args.message_window)
                    last_seen_fingerprint = session_fingerprint(session)
                    last_change_at = time.monotonic()
                    force_overseer_retry = True
                    same_fingerprint_no_action_retries = 0
                else:
                    force_overseer_retry, same_fingerprint_no_action_retries = update_same_fingerprint_retry_state(
                        action,
                        same_fingerprint_no_action_retries,
                        len(manifest.get('llm_players') or []),
                    )

                print_event({
                    'event': 'turn_result',
                    'timestamp': utc_now(),
                    'campaign_id': result.get('campaign_id'),
                    'session_id': result.get('session_id'),
                    'speaker': result.get('speaker'),
                    'action': action,
                    'dry_run': bool(result.get('dry_run')),
                    'turns_completed': turns_completed,
                    'json_retry_count': result.get('json_retry_count'),
                })

                if turns_completed >= args.max_turns:
                    print_event({
                        'event': 'stop',
                        'timestamp': utc_now(),
                        'reason': 'max_turns',
                        'turns_completed': turns_completed,
                        'errors': error_count,
                    })
                    return

                time.sleep(args.poll_interval)
                continue

            if time.monotonic() - last_change_at >= args.idle_timeout:
                print_event({
                    'event': 'stop',
                    'timestamp': utc_now(),
                    'reason': 'idle_timeout',
                    'turns_completed': turns_completed,
                    'errors': error_count,
                    'fingerprint': current_fingerprint,
                })
                return

            time.sleep(args.poll_interval)
    finally:
        release_manifest_lock(lock_path)


if __name__ == '__main__':
    main()

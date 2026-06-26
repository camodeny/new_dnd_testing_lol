#!/usr/bin/env python3
import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
from datetime import datetime, timezone

from llm_campaign_common import STATE_DIR, api_get, default_api_base, default_opencode_server, load_manifest


ROOT = pathlib.Path(__file__).resolve().parents[1]


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
    parser.add_argument('--idle-timeout', type=float, default=180.0, help='Stop if the transcript stops changing for this many seconds')
    parser.add_argument('--max-turns', type=int, default=50, help='Maximum number of non-no_action orchestrator steps to run')
    parser.add_argument('--max-minutes', type=float, default=30.0, help='Maximum wall-clock runtime in minutes')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--stop-on-error', action='store_true')
    return parser.parse_args()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


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


def run_orchestrator(args, manifest_path):
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


def resolve_manifest(args):
    manifest_path = pathlib.Path(args.manifest).expanduser() if args.manifest else None
    if args.fresh or manifest_path is None:
        return run_bootstrap(args)
    if not manifest_path.exists():
        raise SystemExit(f'Manifest not found: {manifest_path}')
    return manifest_path


def main():
    args = parse_args()
    manifest_path = resolve_manifest(args)
    manifest = load_manifest(manifest_path)
    start_time = time.monotonic()
    deadline = start_time + (args.max_minutes * 60.0)
    last_seen_fingerprint = None
    last_change_at = time.monotonic()
    turns_completed = 0
    error_count = 0

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
        if time.monotonic() >= deadline:
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

        if current_fingerprint != last_seen_fingerprint:
            last_seen_fingerprint = current_fingerprint
            last_change_at = time.monotonic()

            try:
                result = run_orchestrator(args, manifest_path)
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


if __name__ == '__main__':
    main()

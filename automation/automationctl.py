#!/usr/bin/env python3
import argparse
import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from llm_campaign_common import ApiError, api_get, api_post, api_url, default_api_base
from llm_campaign_common import (
    STATE_DIR,
    api_put,
    api_put_with_key,
    default_session_start_timeout,
    save_manifest,
    start_session,
)


ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKER_ENTRYPOINT = ROOT / 'automation' / 'run_automation_worker.py'
EXIT_OK = 0
EXIT_API_ERROR = 2
EXIT_TIMEOUT = 3
EXIT_TERMINAL_BEFORE_TARGET = 4
EXIT_STREAM_ERROR = 5
EXIT_USAGE_ERROR = 6
TERMINAL_RUN_STATUSES = {'completed', 'failed', 'stopped'}
WAIT_CHOICES = {'after_dm', 'after_player', 'either', 'terminal'}


class CliUsageError(ValueError):
    pass


def load_json_file(path):
    source = sys.stdin.read() if path == '-' else pathlib.Path(path).read_text(encoding='utf-8')
    return json.loads(source)


def merge_payload(base, updates):
    payload = dict(base or {})
    for key, value in updates.items():
        if value is not None:
            payload[key] = value
    return payload


def print_payload(payload, pretty=False):
    if pretty:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print(json.dumps(payload, separators=(',', ':'), sort_keys=True))


def load_optional_object(path, *, expect_type=None):
    if not path:
        return None
    payload = load_json_file(path)
    if expect_type is not None and not isinstance(payload, expect_type):
        expected = expect_type.__name__
        raise CliUsageError(f'{path} must decode to {expected}')
    return payload


def require_owner_api_key(args):
    api_key = args.owner_api_key or os.environ.get('DND_OWNER_API_KEY')
    if not api_key:
        raise CliUsageError('owner API key is required; pass --owner-api-key or set DND_OWNER_API_KEY')
    return api_key


def build_base_payload(args):
    return load_optional_object(args.input_file, expect_type=dict) if getattr(args, 'input_file', None) else {}


def render_wait_result(wait_for, matched_condition, payload, *, result):
    run = payload.get('run') or {}
    event = payload.get('event') or {}
    event_payload = event.get('payload') if isinstance(event.get('payload'), dict) else {}
    delta = payload.get('delta') if isinstance(payload.get('delta'), dict) else {}

    current_cycle = payload.get('current_audit_cycle')
    if current_cycle is None and isinstance(delta.get('current_audit_cycle'), dict):
        current_cycle = delta.get('current_audit_cycle')
    if current_cycle is None and isinstance(event_payload.get('audit_cycle'), dict):
        current_cycle = event_payload.get('audit_cycle')

    player_message_id = (
        event_payload.get('player_message_id')
        or (event_payload.get('message') or {}).get('id')
        or (current_cycle or {}).get('player_message_id')
        or ((current_cycle or {}).get('payload') or {}).get('posted_message_id')
    )
    dm_message_id = (
        event_payload.get('dm_message_id')
        or (current_cycle or {}).get('dm_message_id')
        or (((current_cycle or {}).get('payload') or {}).get('dm_turn') or {}).get('dm_message_id')
    )

    return {
        'result': result,
        'requested_condition': wait_for,
        'matched_condition': matched_condition,
        'run_id': run.get('id') or payload.get('run_id'),
        'run_status': run.get('status'),
        'event_type': event.get('event_type') or payload.get('type') or 'current_state',
        'event_id': event.get('id'),
        'player_message_id': player_message_id,
        'dm_message_id': dm_message_id,
        'audit_cycle_id': (current_cycle or {}).get('id'),
        'audit_phase': (current_cycle or {}).get('phase'),
        'derived_campaign_id': run.get('derived_campaign_id'),
        'last_event_sequence': run.get('last_event_sequence'),
    }


def match_wait_condition(wait_for, payload):
    run = payload.get('run') or {}
    run_status = run.get('status')
    if wait_for == 'terminal':
        if run_status in TERMINAL_RUN_STATUSES:
            return 'matched', 'terminal'
        return None, None

    if run_status in TERMINAL_RUN_STATUSES:
        return 'terminal_before_target', None

    event = payload.get('event') or {}
    event_type = event.get('event_type')
    event_payload = event.get('payload') if isinstance(event.get('payload'), dict) else {}
    delta = payload.get('delta') if isinstance(payload.get('delta'), dict) else {}
    current_cycle = payload.get('current_audit_cycle') or delta.get('current_audit_cycle')
    if current_cycle is None and isinstance(event_payload.get('audit_cycle'), dict):
        current_cycle = event_payload.get('audit_cycle')
    cycle_phase = (current_cycle or {}).get('phase')

    if wait_for in {'after_player', 'either'}:
        if run_status == 'awaiting_audit' and cycle_phase == 'after_player':
            return 'matched', 'after_player'
        if event_type == 'audit_cycle_paused' and cycle_phase == 'after_player':
            return 'matched', 'after_player'
        if event_type == 'player_message_posted':
            return 'matched', 'after_player'

    if wait_for in {'after_dm', 'either'}:
        if run_status == 'awaiting_audit' and cycle_phase == 'after_dm':
            return 'matched', 'after_dm'
        if event_type == 'audit_cycle_paused' and cycle_phase == 'after_dm':
            return 'matched', 'after_dm'

    return None, None


def iter_sse_messages(api_base, owner_api_key, run_id, *, after_id=None, timeout_seconds=30):
    query = {'api_key': owner_api_key}
    if after_id:
        query['last_event_id'] = after_id
    request = urllib.request.Request(
        api_url(api_base, f'/api/automation/runs/{run_id}/stream', query=query),
        headers={'Accept': 'text/event-stream'},
    )
    socket_timeout = max(5, min(int(timeout_seconds) if timeout_seconds else 30, 30))
    try:
        with urllib.request.urlopen(request, timeout=socket_timeout) as response:
            event_id = None
            data_lines = []
            for raw_line in response:
                line = raw_line.decode('utf-8', errors='replace').rstrip('\r\n')
                if not line:
                    if data_lines:
                        payload_text = '\n'.join(data_lines)
                        try:
                            payload = json.loads(payload_text)
                        except json.JSONDecodeError as exc:
                            raise ApiError(f'Malformed SSE payload: {payload_text}') from exc
                        yield {'event_id': event_id, 'payload': payload}
                        event_id = None
                        data_lines = []
                    continue
                if line.startswith(':'):
                    yield None
                    continue
                if line.startswith('id:'):
                    raw_id = line[3:].strip()
                    try:
                        event_id = int(raw_id)
                    except ValueError:
                        event_id = None
                    continue
                if line.startswith('data:'):
                    data_lines.append(line[5:].lstrip())
            if data_lines:
                payload_text = '\n'.join(data_lines)
                try:
                    payload = json.loads(payload_text)
                except json.JSONDecodeError as exc:
                    raise ApiError(f'Malformed SSE payload: {payload_text}') from exc
                yield {'event_id': event_id, 'payload': payload}
    except urllib.error.HTTPError as exc:
        details = exc.read().decode('utf-8', errors='replace')
        raise ApiError(f'SSE stream failed with HTTP {exc.code}: {details}') from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
        raise ApiError(f'SSE stream failed: {exc}') from exc


def handle_scorecard_create(args):
    api_key = require_owner_api_key(args)
    payload = build_base_payload(args)
    payload = merge_payload(payload, {
        'name': args.name,
        'description': args.description,
        'instructions': args.instructions,
        'criteria': load_optional_object(args.criteria_file, expect_type=list),
        'defaults': load_optional_object(args.defaults_file, expect_type=dict),
    })
    return api_post(args.api_base, '/api/automation/scorecards', payload, api_key=api_key), EXIT_OK


def handle_campaign_create(args):
    if not args.owner_token and not args.owner_api_key:
        raise CliUsageError('owner credentials are required; pass --owner-token or --owner-api-key or set DND_OWNER_API_KEY')

    required_players = args.required_players or args.llm_count
    use_api_key = bool(args.owner_api_key and not args.owner_token)
    owner = api_get(args.api_base, '/api/me', owner_token=args.owner_token, api_key=args.owner_api_key)['user']
    created = api_post(
        args.api_base,
        '/api/campaigns/quick-create',
        {
            'seed': args.seed,
            'difficulty': args.difficulty,
            'required_players': required_players,
            'loot_mode': args.loot_mode,
        },
        owner_token=args.owner_token,
        api_key=args.owner_api_key,
    )
    campaign = created['campaign']
    brief = created['brief']

    if args.campaign_name or args.description:
        payload = {
            'name': args.campaign_name or campaign['name'],
            'description': args.description if args.description is not None else campaign['description'],
        }
        if use_api_key:
            updated = api_put_with_key(
                args.api_base,
                f'/api/campaigns/{campaign["id"]}',
                payload,
                api_key=args.owner_api_key,
            )
        else:
            updated = api_put(
                args.api_base,
                f'/api/campaigns/{campaign["id"]}',
                payload,
                owner_token=args.owner_token,
            )
        campaign = updated['campaign']

    if use_api_key:
        api_put_with_key(
            args.api_base,
            f'/api/campaigns/{campaign["id"]}/members/{owner["id"]}',
            {'role': 'spectator'},
            api_key=args.owner_api_key,
        )
    else:
        api_put(
            args.api_base,
            f'/api/campaigns/{campaign["id"]}/members/{owner["id"]}',
            {'role': 'spectator'},
            owner_token=args.owner_token,
        )

    llm_players = []
    for index in range(args.llm_count):
        created_player = api_post(
            args.api_base,
            f'/api/campaigns/{campaign["id"]}/llm-players',
            {'label': f'{args.label_prefix} {index + 1}'},
            owner_token=args.owner_token,
            api_key=args.owner_api_key,
        )
        llm_players.append({
            'llm_player': created_player['llm_player'],
            'character': created_player['character'],
            'api_key': created_player['api_key'],
        })

    session = None
    if not args.no_start:
        session = start_session(
            args.api_base,
            campaign['id'],
            owner_token=args.owner_token,
            api_key=args.owner_api_key,
            timeout=args.session_start_timeout,
        )

    manifest_path = pathlib.Path(args.manifest).expanduser() if args.manifest else STATE_DIR / f'llm-campaign-{campaign["id"]}.json'
    manifest_payload = {
        'api_base': args.api_base,
        'campaign': campaign,
        'brief': brief,
        'owner': {
            'id': owner['id'],
            'username': owner['username'],
            'token': args.owner_token,
            'api_key': args.owner_api_key,
        },
        'session': {
            'id': session['id'] if session else None,
        },
        'bootstrap_state': 'started' if session else 'prestart',
        'opencode': {
            'server': None,
            'model': 'opencode-go/deepseek-v4-flash',
        },
        'turn_order': [entry['llm_player']['id'] for entry in llm_players],
        'llm_players': llm_players,
    }
    save_manifest(manifest_path, manifest_payload)

    return {
        'campaign': campaign,
        'brief': brief,
        'session': session,
        'bootstrap_state': manifest_payload['bootstrap_state'],
        'manifest_path': str(manifest_path),
        'llm_players': llm_players,
        'owner': {
            'id': owner['id'],
            'username': owner['username'],
        },
    }, EXIT_OK


def handle_scenario_create(args):
    api_key = require_owner_api_key(args)
    payload = build_base_payload(args)
    payload = merge_payload(payload, {
        'source_campaign_id': args.source_campaign_id,
        'name': args.name,
        'description': args.description,
        'scorecard_template_id': args.scorecard_template_id,
        'runner_config': load_optional_object(args.runner_config_file, expect_type=dict),
        'audit_config': load_optional_object(args.audit_config_file, expect_type=dict),
        'retention_policy': load_optional_object(args.retention_policy_file, expect_type=dict),
    })
    return api_post(args.api_base, '/api/automation/scenarios', payload, api_key=api_key), EXIT_OK


def handle_snapshot_create(args):
    api_key = require_owner_api_key(args)
    payload = build_base_payload(args)
    payload = merge_payload(payload, {
        'label': args.label,
        'summary': args.summary,
        'source_session_id': args.source_session_id,
    })
    return api_post(args.api_base, f'/api/automation/scenarios/{args.scenario_id}/snapshots', payload, api_key=api_key), EXIT_OK


def handle_run_start(args):
    api_key = require_owner_api_key(args)
    payload = build_base_payload(args)
    payload = merge_payload(payload, {
        'snapshot_id': args.snapshot_id,
        'runner_config': load_optional_object(args.runner_config_file, expect_type=dict),
        'matrix': load_optional_object(args.matrix_file, expect_type=list),
    })
    return api_post(args.api_base, f'/api/automation/scenarios/{args.scenario_id}/runs', payload, api_key=api_key), EXIT_OK


def handle_run_status(args):
    api_key = require_owner_api_key(args)
    return api_get(args.api_base, f'/api/automation/runs/{args.run_id}', api_key=api_key), EXIT_OK


def handle_run_wait(args):
    api_key = require_owner_api_key(args)
    initial = api_get(args.api_base, f'/api/automation/runs/{args.run_id}', api_key=api_key)
    initial_result, matched_condition = match_wait_condition(args.wait_for, initial)
    if initial_result == 'matched':
        return render_wait_result(args.wait_for, matched_condition, initial, result='matched'), EXIT_OK
    if initial_result == 'terminal_before_target':
        return render_wait_result(args.wait_for, None, initial, result='terminal_before_target'), EXIT_TERMINAL_BEFORE_TARGET

    deadline = time.monotonic() + max(1, args.timeout_seconds)
    after_id = ((initial.get('run') or {}).get('last_event_id') or 0)

    for message in iter_sse_messages(
        args.api_base,
        api_key,
        args.run_id,
        after_id=after_id,
        timeout_seconds=args.timeout_seconds,
    ):
        if time.monotonic() >= deadline:
            timeout_payload = {
                'run': initial.get('run') or {'id': args.run_id},
                'event': {},
            }
            return render_wait_result(args.wait_for, None, timeout_payload, result='timeout'), EXIT_TIMEOUT
        if message is None:
            continue
        after_id = message.get('event_id') or after_id
        payload = message.get('payload') or {}
        result, matched_condition = match_wait_condition(args.wait_for, payload)
        if result == 'matched':
            return render_wait_result(args.wait_for, matched_condition, payload, result='matched'), EXIT_OK
        if result == 'terminal_before_target':
            return render_wait_result(args.wait_for, None, payload, result='terminal_before_target'), EXIT_TERMINAL_BEFORE_TARGET

    raise ApiError('Run stream ended before the requested condition was observed')


def handle_run_audit(args):
    api_key = require_owner_api_key(args)
    payload = build_base_payload(args)
    payload = merge_payload(payload, {
        'summary': args.summary,
        'notes': args.notes,
        'scorecard': load_optional_object(args.scorecard_file, expect_type=dict),
    })
    return api_post(
        args.api_base,
        f'/api/automation/runs/{args.run_id}/audit-cycles/{args.cycle_id}/audit',
        payload,
        api_key=api_key,
    ), EXIT_OK


def handle_run_continue(args):
    api_key = require_owner_api_key(args)
    payload = {'force': True} if args.force else {}
    return api_post(args.api_base, f'/api/automation/runs/{args.run_id}/continue', payload, api_key=api_key), EXIT_OK


def handle_run_scorecard(args):
    api_key = require_owner_api_key(args)
    return api_get(args.api_base, f'/api/automation/runs/{args.run_id}/scorecard', api_key=api_key), EXIT_OK


def handle_run_compare(args):
    api_key = require_owner_api_key(args)
    payload = {'left_run_id': args.left_run_id, 'right_run_id': args.right_run_id}
    return api_post(args.api_base, '/api/automation/compare', payload, api_key=api_key), EXIT_OK


def handle_worker_start(args):
    command = [
        sys.executable,
        str(WORKER_ENTRYPOINT),
        '--api-base',
        args.api_base,
        '--owner-api-key',
        require_owner_api_key(args),
    ]
    option_pairs = [
        ('run_id', '--run-id'),
        ('worker_id', '--worker-id'),
        ('poll_interval', '--poll-interval'),
        ('max_turns', '--max-turns'),
        ('max_minutes', '--max-minutes'),
        ('idle_timeout', '--idle-timeout'),
        ('heartbeat_interval', '--heartbeat-interval'),
        ('dm_response_timeout', '--dm-response-timeout'),
        ('dm_visible_response_timeout', '--dm-visible-response-timeout'),
        ('dm_post_turn_timeout', '--dm-post-turn-timeout'),
        ('message_window', '--message-window'),
        ('model', '--model'),
        ('opencode_server', '--opencode-server'),
        ('opencode_password', '--opencode-password'),
    ]
    for attr, flag in option_pairs:
        value = getattr(args, attr)
        if value is None or value == '':
            continue
        command.extend([flag, str(value)])
    if args.once:
        command.append('--once')
    completed = subprocess.run(command, env=os.environ.copy())
    return {
        'result': 'worker_exited',
        'command': command,
        'exit_code': completed.returncode,
    }, completed.returncode


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--api-base', default=default_api_base(), help='Base app URL, for example http://127.0.0.1:5889')
    common.add_argument('--owner-api-key', default=os.environ.get('DND_OWNER_API_KEY'), help='Owner automation API key')
    common.add_argument('--pretty', action='store_true', help='Pretty-print JSON output')

    parser = argparse.ArgumentParser(description='Agent-first CLI for the automation workspace.')
    subparsers = parser.add_subparsers(dest='resource', required=True)

    campaign_parser = subparsers.add_parser('campaign')
    campaign_subparsers = campaign_parser.add_subparsers(dest='action', required=True)
    campaign_create = campaign_subparsers.add_parser('create', parents=[common])
    campaign_create.add_argument('--owner-token', help='Bearer token for the campaign owner')
    campaign_create.add_argument('--campaign-name', help='Optional explicit campaign name override')
    campaign_create.add_argument('--description', help='Optional explicit campaign description override')
    campaign_create.add_argument('--seed', help='Optional deterministic seed for quick-create')
    campaign_create.add_argument('--difficulty', help='Optional difficulty override')
    campaign_create.add_argument('--loot-mode', choices=('frequent_gamble', 'rare_quality'), default='rare_quality')
    campaign_create.add_argument('--llm-count', type=int, default=3, help='Number of LLM players to create')
    campaign_create.add_argument('--required-players', type=int, help='Defaults to llm-count when omitted')
    campaign_create.add_argument('--label-prefix', default='Auto Player', help='Base label for created LLM players')
    campaign_create.add_argument('--manifest', help='Output manifest path')
    campaign_create.add_argument('--no-start', action='store_true', help='Create campaign and LLM players but do not start the first session yet')
    campaign_create.add_argument(
        '--session-start-timeout',
        type=int,
        default=default_session_start_timeout(),
        help='Seconds to wait for initial session creation and opening DM scene',
    )
    campaign_create.set_defaults(handler=handle_campaign_create)

    scorecard_parser = subparsers.add_parser('scorecard')
    scorecard_subparsers = scorecard_parser.add_subparsers(dest='action', required=True)
    scorecard_create = scorecard_subparsers.add_parser('create', parents=[common])
    scorecard_create.add_argument('--input-file', help='JSON file with the full scorecard payload')
    scorecard_create.add_argument('--name')
    scorecard_create.add_argument('--description')
    scorecard_create.add_argument('--instructions')
    scorecard_create.add_argument('--criteria-file', help='JSON file containing the criteria array')
    scorecard_create.add_argument('--defaults-file', help='JSON file containing defaults object')
    scorecard_create.set_defaults(handler=handle_scorecard_create)

    scenario_parser = subparsers.add_parser('scenario')
    scenario_subparsers = scenario_parser.add_subparsers(dest='action', required=True)
    scenario_create = scenario_subparsers.add_parser('create', parents=[common])
    scenario_create.add_argument('--input-file', help='JSON file with the full scenario payload')
    scenario_create.add_argument('--source-campaign-id', type=int)
    scenario_create.add_argument('--name')
    scenario_create.add_argument('--description')
    scenario_create.add_argument('--scorecard-template-id', type=int)
    scenario_create.add_argument('--runner-config-file', help='JSON file for runner_config')
    scenario_create.add_argument('--audit-config-file', help='JSON file for audit_config')
    scenario_create.add_argument('--retention-policy-file', help='JSON file for retention_policy')
    scenario_create.set_defaults(handler=handle_scenario_create)

    snapshot_parser = subparsers.add_parser('snapshot')
    snapshot_subparsers = snapshot_parser.add_subparsers(dest='action', required=True)
    snapshot_create = snapshot_subparsers.add_parser('create', parents=[common])
    snapshot_create.add_argument('--scenario-id', type=int, required=True)
    snapshot_create.add_argument('--input-file', help='JSON file with the snapshot payload')
    snapshot_create.add_argument('--label')
    snapshot_create.add_argument('--summary')
    snapshot_create.add_argument('--source-session-id', type=int)
    snapshot_create.set_defaults(handler=handle_snapshot_create)

    run_parser = subparsers.add_parser('run')
    run_subparsers = run_parser.add_subparsers(dest='action', required=True)

    run_start = run_subparsers.add_parser('start', parents=[common])
    run_start.add_argument('--scenario-id', type=int, required=True)
    run_start.add_argument('--input-file', help='JSON file with the run payload')
    run_start.add_argument('--snapshot-id', type=int)
    run_start.add_argument('--runner-config-file', help='JSON file for runner_config')
    run_start.add_argument('--matrix-file', help='JSON file for matrix run definitions')
    run_start.set_defaults(handler=handle_run_start)

    run_status = run_subparsers.add_parser('status', parents=[common])
    run_status.add_argument('--run-id', type=int, required=True)
    run_status.set_defaults(handler=handle_run_status)

    run_wait = run_subparsers.add_parser('wait', parents=[common])
    run_wait.add_argument('--run-id', type=int, required=True)
    run_wait.add_argument('--wait-for', choices=sorted(WAIT_CHOICES), default='after_dm')
    run_wait.add_argument('--timeout-seconds', type=int, default=600)
    run_wait.set_defaults(handler=handle_run_wait)

    run_audit = run_subparsers.add_parser('audit', parents=[common])
    run_audit.add_argument('--run-id', type=int, required=True)
    run_audit.add_argument('--cycle-id', type=int, required=True)
    run_audit.add_argument('--input-file', help='JSON file with the full audit payload')
    run_audit.add_argument('--summary')
    run_audit.add_argument('--notes')
    run_audit.add_argument('--scorecard-file', help='JSON file with scorecard payload')
    run_audit.set_defaults(handler=handle_run_audit)

    run_continue = run_subparsers.add_parser('continue', parents=[common])
    run_continue.add_argument('--run-id', type=int, required=True)
    run_continue.add_argument('--force', action='store_true')
    run_continue.set_defaults(handler=handle_run_continue)

    run_scorecard = run_subparsers.add_parser('scorecard', parents=[common])
    run_scorecard.add_argument('--run-id', type=int, required=True)
    run_scorecard.set_defaults(handler=handle_run_scorecard)

    run_compare = run_subparsers.add_parser('compare', parents=[common])
    run_compare.add_argument('--left-run-id', type=int, required=True)
    run_compare.add_argument('--right-run-id', type=int, required=True)
    run_compare.set_defaults(handler=handle_run_compare)

    worker_parser = subparsers.add_parser('worker')
    worker_subparsers = worker_parser.add_subparsers(dest='action', required=True)
    worker_start = worker_subparsers.add_parser('start', parents=[common])
    worker_start.add_argument('--run-id', type=int)
    worker_start.add_argument('--worker-id')
    worker_start.add_argument('--poll-interval', type=float)
    worker_start.add_argument('--max-turns', type=int)
    worker_start.add_argument('--max-minutes', type=float)
    worker_start.add_argument('--idle-timeout', type=float)
    worker_start.add_argument('--heartbeat-interval', type=float)
    worker_start.add_argument('--dm-response-timeout', type=float)
    worker_start.add_argument('--dm-visible-response-timeout', type=float)
    worker_start.add_argument('--dm-post-turn-timeout', type=float)
    worker_start.add_argument('--message-window', type=int)
    worker_start.add_argument('--model')
    worker_start.add_argument('--opencode-server')
    worker_start.add_argument('--opencode-password')
    worker_start.add_argument('--once', action='store_true')
    worker_start.set_defaults(handler=handle_worker_start)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        payload, exit_code = args.handler(args)
    except CliUsageError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE_ERROR
    except ApiError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_API_ERROR
    except (OSError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_STREAM_ERROR

    if payload is not None:
        print_payload(payload, pretty=args.pretty)
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())

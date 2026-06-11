#!/usr/bin/env python3
import argparse
import json
import os
import pathlib
import sys
from datetime import datetime, timezone

from llm_campaign_common import (
    STATE_DIR,
    api_get,
    api_post,
    default_opencode_server,
    extract_text_parts,
    load_manifest,
    opencode_request,
    save_manifest,
)


SYSTEM_PROMPT = """You are controlling exactly one player character in a live AI-run tabletop campaign.

Rules:
- Act only as the assigned player character.
- Do not narrate for the DM or for other player characters.
- Prefer one concrete in-character action or short spoken line.
- If no meaningful player action should happen now, return exactly: {"action":"no_action"}
- Otherwise return strict JSON: {"action":"speak","content":"..."}
- Do not wrap the JSON in markdown fences.
"""


def parse_args():
    parser = argparse.ArgumentParser(description='Run one orchestrated LLM player turn through OpenCode.')
    parser.add_argument('manifest', help='Path to the campaign manifest created by bootstrap_llm_campaign.py')
    parser.add_argument('--opencode-server', default=default_opencode_server())
    parser.add_argument('--opencode-password', default=os.environ.get('OPENCODE_SERVER_PASSWORD'))
    parser.add_argument('--model', default=os.environ.get('OPENCODE_MODEL', 'opencode-go/deepseek-v4-flash'))
    parser.add_argument('--player-id', type=int, help='Explicit LLM player id chosen by the overseer')
    parser.add_argument('--player-label', help='Explicit LLM player label chosen by the overseer')
    parser.add_argument('--message-window', type=int, default=16, help='How many recent messages to include in the player prompt')
    parser.add_argument('--dry-run', action='store_true')
    return parser.parse_args()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def find_active_session(api_base, owner_token, owner_api_key, campaign_id, preferred_session_id=None):
    if preferred_session_id:
        session = api_get(
            api_base,
            f'/api/sessions/{preferred_session_id}',
            owner_token=owner_token,
            api_key=owner_api_key,
        )['session']
        if session.get('is_active'):
            return session

    sessions = api_get(
        api_base,
        f'/api/campaigns/{campaign_id}/sessions',
        owner_token=owner_token,
        api_key=owner_api_key,
    )['sessions']
    for session in sessions:
        if session.get('is_active'):
            return api_get(
                api_base,
                f'/api/sessions/{session["id"]}',
                owner_token=owner_token,
                api_key=owner_api_key,
            )['session']
    return None


def llm_players_by_id(manifest):
    return {entry['llm_player']['id']: entry for entry in manifest['llm_players']}


def choose_next_player(manifest, session):
    roster = llm_players_by_id(manifest)
    order = manifest.get('turn_order') or list(roster)
    messages = session.get('messages') or []
    if not order:
        raise RuntimeError('Manifest has no LLM players configured')

    llm_message_ids = {entry['llm_player']['user_id']: entry['llm_player']['id'] for entry in manifest['llm_players']}
    latest_player_llm_id = None
    for message in reversed(messages):
        if message.get('role') != 'player':
            continue
        user_id = message.get('user_id')
        if user_id in llm_message_ids:
            latest_player_llm_id = llm_message_ids[user_id]
            break

    if latest_player_llm_id is None:
        return roster[order[0]]

    current_index = order.index(latest_player_llm_id)
    return roster[order[(current_index + 1) % len(order)]]


def find_player(manifest, player_id=None, player_label=None):
    roster = manifest['llm_players']
    if player_id is not None:
        for entry in roster:
            if entry['llm_player']['id'] == player_id:
                return entry
        raise RuntimeError(f'No LLM player found for id {player_id}')
    if player_label:
        normalized = player_label.strip().casefold()
        for entry in roster:
            if entry['llm_player']['label'].strip().casefold() == normalized:
                return entry
        raise RuntimeError(f'No LLM player found for label {player_label}')
    return None


def build_prompt(manifest, campaign, world_payload, session, chosen_player, message_window):
    recent_messages = (session.get('messages') or [])[-message_window:]
    other_players = [
        {
            'label': entry['llm_player']['label'],
            'character_name': entry['character']['name'],
        }
        for entry in manifest['llm_players']
        if entry['llm_player']['id'] != chosen_player['llm_player']['id']
    ]
    state = {
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
        },
        'recent_messages': recent_messages,
        'you': {
            'label': chosen_player['llm_player']['label'],
            'character_name': chosen_player['character']['name'],
            'character': chosen_player['character'],
        },
        'other_players': other_players,
    }
    return (
        'Decide whether your player character should act next.\n\n'
        'Current structured state:\n'
        f'{json.dumps(state, indent=2)}'
    )


def create_opencode_session(server, password):
    response = opencode_request(server, '/session', payload={'title': 'llm-campaign-turn'}, password=password, method='POST')
    return response['id']


def extract_json_object(text):
    stripped = text.strip()
    if not stripped:
        raise RuntimeError('OpenCode returned an empty response')
    start = stripped.find('{')
    end = stripped.rfind('}')
    if start == -1 or end == -1 or end < start:
        raise RuntimeError(f'OpenCode response did not contain JSON: {text}')
    return json.loads(stripped[start:end + 1])


def append_run_log(manifest_path, payload):
    log_path = pathlib.Path(manifest_path).with_suffix('.runs.jsonl')
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(payload) + '\n')


def main():
    args = parse_args()
    manifest = load_manifest(args.manifest)
    api_base = manifest['api_base']
    owner_token = manifest['owner'].get('token')
    owner_api_key = manifest['owner'].get('api_key') or os.environ.get('DND_OWNER_API_KEY')
    campaign_id = manifest['campaign']['id']

    campaign = api_get(api_base, f'/api/campaigns/{campaign_id}', owner_token=owner_token, api_key=owner_api_key)['campaign']
    world_payload = api_get(api_base, f'/api/campaigns/{campaign_id}/world', owner_token=owner_token, api_key=owner_api_key)
    session = find_active_session(
        api_base,
        owner_token,
        owner_api_key,
        campaign_id,
        preferred_session_id=manifest.get('session', {}).get('id'),
    )
    if session is None:
        raise RuntimeError('No active session found for this campaign')

    manifest['session'] = {'id': session['id']}
    save_manifest(args.manifest, manifest)

    chosen_player = find_player(manifest, player_id=args.player_id, player_label=args.player_label) or choose_next_player(manifest, session)
    prompt = build_prompt(manifest, campaign, world_payload, session, chosen_player, args.message_window)
    opencode_session_id = create_opencode_session(args.opencode_server, args.opencode_password)
    
    model_payload = args.model
    if isinstance(args.model, str) and '/' in args.model:
        provider_id, model_id = args.model.split('/', 1)
        model_payload = {
            'providerID': provider_id,
            'modelID': model_id
        }

    response = opencode_request(
        args.opencode_server,
        f'/session/{opencode_session_id}/message',
        payload={
            'agent': 'build',
            'model': model_payload,
            'system': SYSTEM_PROMPT,
            'parts': [{'type': 'text', 'text': prompt}],
        },
        password=args.opencode_password,
        method='POST',
    )
    response_text = extract_text_parts(response.get('parts'))
    decision = extract_json_object(response_text)

    run_result = {
        'timestamp': now_iso(),
        'campaign_id': campaign_id,
        'session_id': session['id'],
        'speaker': {
            'llm_player_id': chosen_player['llm_player']['id'],
            'label': chosen_player['llm_player']['label'],
            'character_name': chosen_player['character']['name'],
        },
        'message_window': args.message_window,
        'decision': decision,
        'dry_run': args.dry_run,
    }

    if decision.get('action') == 'speak':
        content = str(decision.get('content') or '').strip()
        if not content:
            raise RuntimeError('OpenCode returned action=speak without content')
        if not args.dry_run:
            posted = api_post(
                api_base,
                f'/api/sessions/{session["id"]}/messages',
                {'content': content, 'role': 'player'},
                api_key=chosen_player['api_key'],
            )
            run_result['posted_messages'] = posted.get('messages', [])
    else:
        run_result['status'] = 'no_action'

    append_run_log(args.manifest, run_result)
    print(json.dumps(run_result, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'error: {exc}', file=sys.stderr)
        sys.exit(1)

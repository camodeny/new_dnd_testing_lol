#!/usr/bin/env python3
import argparse
import os
import pathlib
import time

from llm_campaign_common import (
    ApiError,
    STATE_DIR,
    api_get,
    api_post,
    api_put,
    api_put_with_key,
    default_api_base,
    default_session_start_timeout,
    save_manifest,
)


def parse_args():
    parser = argparse.ArgumentParser(description='Create a fully LLM campaign and store a local manifest.')
    parser.add_argument('--owner-token', help='Bearer token for the campaign owner')
    parser.add_argument('--owner-api-key', default=os.environ.get('DND_OWNER_API_KEY'), help='Owner automation API key')
    parser.add_argument('--api-base', default=default_api_base(), help='Base app URL, for example http://100.99.192.92:5889')
    parser.add_argument('--campaign-name', help='Optional explicit campaign name override')
    parser.add_argument('--description', help='Optional explicit campaign description override')
    parser.add_argument('--seed', help='Optional deterministic seed for quick-create')
    parser.add_argument('--difficulty', help='Optional difficulty override')
    parser.add_argument('--loot-mode', choices=('frequent_gamble', 'rare_quality'), default='rare_quality')
    parser.add_argument('--llm-count', type=int, default=3, help='Number of LLM players to create')
    parser.add_argument('--required-players', type=int, help='Defaults to llm-count when omitted')
    parser.add_argument('--label-prefix', default='Auto Player', help='Base label for created LLM players')
    parser.add_argument('--manifest', help='Output manifest path')
    parser.add_argument(
        '--session-start-timeout',
        type=int,
        default=default_session_start_timeout(),
        help='Seconds to wait for initial session creation and opening DM scene',
    )
    return parser.parse_args()


def find_active_session(api_base, campaign_id, owner_token=None, owner_api_key=None):
    sessions = api_get(
        api_base,
        f'/api/campaigns/{campaign_id}/sessions',
        owner_token=owner_token,
        api_key=owner_api_key,
    )['sessions']
    for session in sessions:
        if session.get('is_active'):
            return session
    return None


def start_session(api_base, campaign_id, owner_token=None, owner_api_key=None, timeout=180):
    try:
        return api_post(
            api_base,
            f'/api/campaigns/{campaign_id}/sessions',
            {},
            owner_token=owner_token,
            api_key=owner_api_key,
            timeout=timeout,
        )['session']
    except ApiError as exc:
        if 'timed out' not in str(exc):
            raise

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        session = find_active_session(
            api_base,
            campaign_id,
            owner_token=owner_token,
            owner_api_key=owner_api_key,
        )
        if session:
            return session
        time.sleep(5)

    raise RuntimeError(
        f'Session start request timed out and no active session appeared within {timeout}s. '
        'The server may be overloaded or stuck in opening-scene generation.'
    )


def main():
    args = parse_args()
    if not args.owner_token and not args.owner_api_key:
        raise SystemExit('Provide --owner-token or --owner-api-key')
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

    session = start_session(
        args.api_base,
        campaign['id'],
        owner_token=args.owner_token,
        owner_api_key=args.owner_api_key,
        timeout=args.session_start_timeout,
    )

    manifest_path = pathlib.Path(args.manifest) if args.manifest else STATE_DIR / f'llm-campaign-{campaign["id"]}.json'
    payload = {
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
            'id': session['id'],
        },
        'opencode': {
            'server': None,
            'model': 'opencode-go/deepseek-v4-flash',
        },
        'turn_order': [entry['llm_player']['id'] for entry in llm_players],
        'llm_players': llm_players,
    }
    save_manifest(manifest_path, payload)

    print(f'Created campaign {campaign["id"]}: {campaign["name"]}')
    print(f'Active session: {session["id"]}')
    print(f'Manifest: {manifest_path}')
    print('LLM players:')
    for entry in llm_players:
        print(f'- {entry["llm_player"]["label"]} ({entry["character"]["name"]})')


if __name__ == '__main__':
    main()

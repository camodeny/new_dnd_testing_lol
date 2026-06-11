#!/usr/bin/env python3
import argparse
import json
import os
import sys

from llm_campaign_common import api_get, load_manifest


def parse_args():
    parser = argparse.ArgumentParser(description='Build a compact context payload for an external overseer.')
    parser.add_argument('manifest', help='Path to the campaign manifest')
    parser.add_argument('--message-window', type=int, default=16, help='How many recent session messages to include')
    return parser.parse_args()


def main():
    args = parse_args()
    manifest = load_manifest(args.manifest)
    api_base = manifest['api_base']
    owner_token = manifest['owner'].get('token')
    owner_api_key = manifest['owner'].get('api_key') or os.environ.get('DND_OWNER_API_KEY')
    campaign_id = manifest['campaign']['id']
    session_id = manifest['session']['id']

    campaign = api_get(api_base, f'/api/campaigns/{campaign_id}', owner_token=owner_token, api_key=owner_api_key)['campaign']
    world_payload = api_get(api_base, f'/api/campaigns/{campaign_id}/world', owner_token=owner_token, api_key=owner_api_key)
    session = api_get(
        api_base,
        f'/api/sessions/{session_id}',
        owner_token=owner_token,
        api_key=owner_api_key,
        query={'limit': args.message_window},
    )['session']

    payload = {
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
        'recent_messages': session.get('messages', [])[-args.message_window:],
        'message_window': args.message_window,
        'orchestrator_command_template': (
            '/Users/cpendergrass/Programming/new_dnd_testing_lol/automation/run_llm_campaign_orchestrator.sh '
            f'{args.manifest} --player-id <llm_player_id> --message-window {args.message_window}'
        ),
    }
    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write('\n')


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
import argparse
import os
import sys
import time

from llm_campaign_common import api_get, load_manifest


def parse_args():
    parser = argparse.ArgumentParser(description='Poll and print new session messages for a manifest-backed LLM campaign.')
    parser.add_argument('manifest', help='Path to the campaign manifest')
    parser.add_argument('--interval', type=float, default=2.0, help='Polling interval in seconds')
    parser.add_argument('--once', action='store_true', help='Print current transcript once and exit')
    return parser.parse_args()


def render_message(message):
    username = message.get('username') or message.get('role', 'unknown')
    content = (message.get('content') or '').strip()
    created_at = message.get('created_at') or ''
    return f'[{created_at}] {username}: {content}'


def main():
    args = parse_args()
    manifest = load_manifest(args.manifest)
    api_base = manifest['api_base']
    owner_token = manifest['owner'].get('token')
    owner_api_key = manifest['owner'].get('api_key') or os.environ.get('DND_OWNER_API_KEY')
    session_id = manifest['session']['id']

    seen = set()
    while True:
        session = api_get(
            api_base,
            f'/api/sessions/{session_id}',
            owner_token=owner_token,
            api_key=owner_api_key,
            query={'limit': 100},
        )['session']
        for message in session.get('messages', []):
            if message['id'] in seen:
                continue
            seen.add(message['id'])
            print(render_message(message))
        sys.stdout.flush()
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == '__main__':
    main()

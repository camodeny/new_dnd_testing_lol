#!/usr/bin/env python3
import argparse
import json
import os
import pathlib
import random
import re
import sys
from datetime import datetime, timezone

from llm_campaign_common import (
    ApiError,
    api_get,
    api_post,
    default_opencode_server,
    load_manifest,
    save_manifest,
)
from provider_client import request_json_decision


SYSTEM_PROMPT = """You are controlling exactly one player character in a live AI-run tabletop campaign.

Rules:
- Act only as the assigned player character.
- Do not narrate for the DM or for other player characters.
- Prefer one concrete in-character action or short spoken line.
- If pending character-sheet proposals are present for your character, you may choose to apply or dismiss one instead of posting a table message.
- If the DM explicitly asks you for a check, save, attack roll, damage roll, initiative, or another clear player-side roll, use the roll action instead of inventing a result.
- For rolls, return strict JSON like {"action":"roll","label":"Arcana check","expression":"1d20+5"}.
- You may include optional "content" in a roll action for a short setup line; the automation will append the visible roll line for you.
- Use 2d20kh1+5 for advantage and 2d20kl1+5 for disadvantage.
- Do not fabricate dice totals or write the [Roll: ...] line yourself inside content.
- To accept a pending character-sheet proposal, return strict JSON like {"action":"apply_proposal","proposal_id":123}.
- To reject a pending character-sheet proposal, return strict JSON like {"action":"dismiss_proposal","proposal_id":123}.
- If no meaningful player action should happen now, return exactly: {"action":"no_action"}
- Otherwise return strict JSON: {"action":"speak","content":"..."}
- Do not wrap the JSON in markdown fences.
"""

ROLL_REQUEST_PATTERNS = (
    re.compile(r'\bmake an?\b[\s\S]{0,120}?\b(check|saving throw|save|attack roll|damage roll|initiative)\b', re.IGNORECASE),
    re.compile(r'\broll(?: for)?\s+initiative\b', re.IGNORECASE),
    re.compile(r'\bplease roll\b', re.IGNORECASE),
    re.compile(r'\bgive me an?\b[\s\S]{0,120}?\b(check|saving throw|save|attack roll|damage roll)\b', re.IGNORECASE),
)


def parse_args():
    parser = argparse.ArgumentParser(description='Run one orchestrated LLM player turn through OpenCode.')
    parser.add_argument('manifest', help='Path to the campaign manifest created by bootstrap_llm_campaign.py')
    parser.add_argument('--opencode-server', default=default_opencode_server())
    parser.add_argument('--opencode-password', default=os.environ.get('OPENCODE_SERVER_PASSWORD'))
    parser.add_argument('--model', default=os.environ.get('OPENCODE_MODEL', 'opencode-go/deepseek-v4-flash'))
    parser.add_argument('--player-id', type=int, help='Explicit LLM player id chosen by the overseer')
    parser.add_argument('--player-label', help='Explicit LLM player label chosen by the overseer')
    parser.add_argument('--message-window', type=int, default=16, help='How many recent messages to include in the player prompt')
    parser.add_argument('--proposal-only', action='store_true', help='Resolve a pending character-sheet proposal without a visible table turn')
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


def llm_players_by_user_id(manifest):
    return {entry['llm_player']['user_id']: entry for entry in manifest['llm_players']}


def dm_requests_player_roll(content):
    text = str(content or '').strip()
    if not text:
        return False
    return any(pattern.search(text) for pattern in ROLL_REQUEST_PATTERNS)


def find_pending_roll_player(manifest, session):
    messages = session.get('messages') or []
    if not messages:
        return None

    latest = messages[-1]
    if latest.get('role') != 'dm' or not dm_requests_player_roll(latest.get('content')):
        return None

    roster_by_user_id = llm_players_by_user_id(manifest)
    for message in reversed(messages[:-1]):
        if message.get('role') != 'player':
            continue
        user_id = message.get('user_id')
        if user_id in roster_by_user_id:
            return roster_by_user_id[user_id]
    return None


def choose_next_player(manifest, session):
    pending_roll_player = find_pending_roll_player(manifest, session)
    if pending_roll_player is not None:
        return pending_roll_player

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


def get_pending_sheet_proposals(api_base, session_id, chosen_player):
    proposals = api_get(
        api_base,
        f'/api/sessions/{session_id}/proposals',
        api_key=chosen_player['api_key'],
    ).get('sheet_proposals')
    return proposals if isinstance(proposals, list) else []


def find_pending_proposal_player(manifest, api_base, session_id):
    roster = llm_players_by_id(manifest)
    order = manifest.get('turn_order') or list(roster)
    for llm_player_id in order:
        chosen_player = roster[llm_player_id]
        proposals = get_pending_sheet_proposals(api_base, session_id, chosen_player)
        if proposals:
            return chosen_player, proposals
    return None, []


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


def build_prompt(manifest, campaign, world_payload, session, chosen_player, pending_proposals, message_window):
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
        'pending_sheet_proposals': pending_proposals,
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


def build_proposal_only_prompt(manifest, campaign, world_payload, session, chosen_player, pending_proposals, message_window):
    prompt = build_prompt(manifest, campaign, world_payload, session, chosen_player, pending_proposals, message_window)
    return (
        prompt
        + '\n\n'
        + 'For this turn you MUST resolve one of the pending character-sheet proposals. '
        + 'Do not speak, roll, or narrate a table action. Reply with strict JSON exactly like '
        + '{"action":"apply_proposal","proposal_id":123,"reason":"..."} or '
        + '{"action":"dismiss_proposal","proposal_id":123,"reason":"..."}.'
    )


def build_proposal_only_retry_prompt(prompt):
    return (
        prompt
        + '\n\n'
        + 'Your previous decision did not resolve a pending character-sheet proposal. '
        + 'You MUST apply or dismiss one of the pending proposals and nothing else. '
        + 'Reply with strict JSON exactly like '
        + '{"action":"apply_proposal","proposal_id":123,"reason":"..."} or '
        + '{"action":"dismiss_proposal","proposal_id":123,"reason":"..."}.'
    )


def request_proposal_only_decision(opencode_server, opencode_password, model_payload, prompt, max_attempts=3):
    """Keep asking the model for an apply/dismiss decision until it complies.

    Returns (decision, response_text, json_retry_count, attempts_used). Raises
    RuntimeError when the model never returns a proposal action.
    """
    decision, response_text, json_retry_count = None, '', 0
    for attempt in range(max(1, max_attempts)):
        decision, response_text, json_retry_count = request_opencode_decision(
            opencode_server,
            opencode_password,
            model_payload,
            prompt,
        )
        action = str((decision or {}).get('action') or '').strip().lower()
        if action in {'apply_proposal', 'dismiss_proposal'}:
            return decision, response_text, json_retry_count, attempt + 1
        prompt = build_proposal_only_retry_prompt(prompt)
    raise RuntimeError('Proposal-only turn failed to resolve a pending proposal')


def request_opencode_decision(server, password, model_payload, prompt):
    del server, password
    result = request_json_decision(
        SYSTEM_PROMPT,
        prompt,
        model=model_payload if isinstance(model_payload, str) else None,
        timeout_seconds=120,
        max_attempts=2,
    )
    return result['decision'], result['raw_response_text'], result['json_retry_count']


def append_run_log(manifest_path, payload):
    log_path = pathlib.Path(manifest_path).with_suffix('.runs.jsonl')
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(payload) + '\n')


def _dice_term_tokenize(expression):
    text = (expression or '').strip().lower().replace(' ', '')
    if not text:
        return []
    if text[0] not in '+-':
        text = f'+{text}'
    parts = re.findall(r'[+-][^+-]+', text)
    return parts if ''.join(parts) == text else []


def roll_dice_expression(expression):
    parts = _dice_term_tokenize(expression)
    if not parts:
        raise ValueError('Unsupported dice expression.')

    total = 0
    breakdown = []

    for part in parts:
        sign = -1 if part[0] == '-' else 1
        token = part[1:]
        dice_match = re.fullmatch(r'(\d*)d(\d+)(kh|kl)?(\d+)?', token)
        if dice_match:
            count = int(dice_match.group(1) or 1)
            sides = int(dice_match.group(2))
            keep_mode = dice_match.group(3)
            keep_count = int(dice_match.group(4) or 1)
            if count < 1 or count > 100 or sides < 2 or sides > 1000:
                raise ValueError('Dice expression is out of allowed bounds.')
            rolls = [random.randint(1, sides) for _ in range(count)]
            if keep_mode:
                keep_count = max(1, min(keep_count, count))
                ordered = sorted(rolls, reverse=(keep_mode == 'kh'))
                kept = ordered[:keep_count]
            else:
                kept = list(rolls)
            subtotal = sum(kept) * sign
            total += subtotal
            breakdown.append({
                'kind': 'dice',
                'sign': sign,
                'count': count,
                'sides': sides,
                'rolls': rolls,
                'kept': kept,
                'keep': f'{keep_mode}{keep_count}' if keep_mode else None,
                'subtotal': subtotal,
            })
            continue

        if re.fullmatch(r'\d+', token):
            value = int(token) * sign
            total += value
            breakdown.append({
                'kind': 'constant',
                'sign': sign,
                'value': abs(value),
                'subtotal': value,
            })
            continue

        raise ValueError('Unsupported dice expression.')

    return {
        'expression': expression,
        'total': total,
        'terms': breakdown,
    }


def execute_player_roll(label, expression):
    clean_label = str(label or '').strip()[:80]
    if not clean_label:
        raise ValueError('Roll label is required.')

    result = roll_dice_expression(expression)
    dice_terms = [term for term in result['terms'] if term['kind'] == 'dice']
    if len(dice_terms) != 1:
        raise ValueError('Player roll tool supports exactly one dice term plus numeric modifiers.')

    dice_term = dice_terms[0]
    if dice_term['sign'] != 1:
        raise ValueError('Player roll tool requires a positive dice term.')

    modifier = sum(term['subtotal'] for term in result['terms'] if term['kind'] == 'constant')
    roll_summary = {
        'label': clean_label,
        'expression': result['expression'],
        'total': result['total'],
        'modifier': modifier,
        'sides': dice_term['sides'],
        'rolls': dice_term['rolls'],
        'kept': dice_term['kept'],
        'keep': dice_term['keep'],
    }
    roll_summary['message'] = (
        f'[Roll: {roll_summary["label"]}] total: {roll_summary["total"]} | '
        f'rolls: {", ".join(str(value) for value in roll_summary["rolls"])} | '
        f'mod: {roll_summary["modifier"]} | sides: {roll_summary["sides"]}'
    )
    return roll_summary


def build_player_roll_message(content, roll_summary):
    prefix = str(content or '').strip()
    if not prefix:
        return roll_summary['message']
    return f'{prefix}\n{roll_summary["message"]}'


def maybe_submit_initiative_roll(api_base, campaign_id, chosen_player, roll_summary):
    if 'initiative' not in roll_summary['label'].casefold():
        return None

    encounter_map = api_get(
        api_base,
        f'/api/campaigns/{campaign_id}/encounter-maps/current',
        api_key=chosen_player['api_key'],
    ).get('encounter_map')
    if not encounter_map or not encounter_map.get('id'):
        return {'status': 'skipped', 'reason': 'No current encounter map'}

    try:
        response = api_post(
            api_base,
            f'/api/encounter-maps/{encounter_map["id"]}/encounter/roll-initiative',
            {
                'actor_type': 'player',
                'actor_id': str(chosen_player['llm_player']['user_id']),
                'initiative': roll_summary['total'],
            },
            api_key=chosen_player['api_key'],
        )
    except ApiError as exc:
        return {
            'status': 'error',
            'encounter_map_id': encounter_map['id'],
            'error': str(exc),
        }

    return {
        'status': 'submitted',
        'encounter_map_id': encounter_map['id'],
        'initiative': roll_summary['total'],
        'encounter_state_active': bool((response.get('encounter_map') or {}).get('encounter_state_json')),
    }


def _normalize_proposal_id(raw_value):
    try:
        proposal_id = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError('Proposal action requires an integer proposal_id') from exc
    if proposal_id <= 0:
        raise RuntimeError('Proposal action requires a positive proposal_id')
    return proposal_id


def execute_player_decision(api_base, campaign_id, session_id, chosen_player, decision, pending_proposals, dry_run=False):
    run_result = {}
    action = str(decision.get('action') or '').strip().lower()

    if action == 'speak':
        content = str(decision.get('content') or '').strip()
        if not content:
            raise RuntimeError('OpenCode returned action=speak without content')
        if not dry_run:
            posted = api_post(
                api_base,
                f'/api/sessions/{session_id}/messages',
                {'content': content, 'role': 'player'},
                api_key=chosen_player['api_key'],
            )
            run_result['posted_messages'] = posted.get('messages', [])
        return run_result

    if action == 'roll':
        roll_summary = execute_player_roll(
            decision.get('label'),
            decision.get('expression'),
        )
        content = build_player_roll_message(decision.get('content'), roll_summary)
        run_result['roll'] = roll_summary
        decision['content'] = content
        if not dry_run:
            posted = api_post(
                api_base,
                f'/api/sessions/{session_id}/messages',
                {'content': content, 'role': 'player'},
                api_key=chosen_player['api_key'],
            )
            run_result['posted_messages'] = posted.get('messages', [])
            initiative_result = maybe_submit_initiative_roll(api_base, campaign_id, chosen_player, roll_summary)
            if initiative_result is not None:
                run_result['initiative_submission'] = initiative_result
        return run_result

    if action in {'apply_proposal', 'dismiss_proposal'}:
        proposal_id = _normalize_proposal_id(decision.get('proposal_id'))
        pending_proposal_ids = {int(proposal.get('id')) for proposal in pending_proposals or [] if proposal.get('id') is not None}
        if proposal_id not in pending_proposal_ids:
            raise RuntimeError(f'Proposal {proposal_id} is not pending for {chosen_player["character"]["name"]}')
        run_result['proposal_action'] = {
            'action': action,
            'proposal_id': proposal_id,
            'reason': str(decision.get('reason') or '').strip(),
        }
        if not dry_run:
            response = api_post(
                api_base,
                f'/api/sessions/{session_id}/proposals/{proposal_id}/{"apply" if action == "apply_proposal" else "dismiss"}',
                {},
                api_key=chosen_player['api_key'],
            )
            run_result['proposal_result'] = response
        return run_result

    run_result['status'] = 'no_action'
    return run_result


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

    chosen_player = find_player(manifest, player_id=args.player_id, player_label=args.player_label)
    pending_proposals = []
    if chosen_player is None:
        chosen_player, pending_proposals = find_pending_proposal_player(manifest, api_base, session['id'])
    if chosen_player is None:
        chosen_player = choose_next_player(manifest, session)
    if not pending_proposals:
        pending_proposals = get_pending_sheet_proposals(api_base, session['id'], chosen_player)
    if args.proposal_only:
        if not pending_proposals:
            raise RuntimeError('No pending sheet proposals to resolve for the chosen player')
        model_payload = args.model
        prompt = build_proposal_only_prompt(manifest, campaign, world_payload, session, chosen_player, pending_proposals, args.message_window)
        decision, response_text, json_retry_count, _attempts_used = request_proposal_only_decision(
            args.opencode_server,
            args.opencode_password,
            model_payload,
            prompt,
        )
    else:
        prompt = build_prompt(manifest, campaign, world_payload, session, chosen_player, pending_proposals, args.message_window)
        model_payload = args.model

        decision, response_text, json_retry_count = request_opencode_decision(
            args.opencode_server,
            args.opencode_password,
            model_payload,
            prompt,
        )

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
        'pending_sheet_proposals': pending_proposals,
        'decision': decision,
        'dry_run': args.dry_run,
        'raw_response_text': response_text,
        'json_retry_count': json_retry_count,
    }
    run_result.update(
        execute_player_decision(
            api_base,
            campaign_id,
            session['id'],
            chosen_player,
            run_result['decision'],
            pending_proposals,
            dry_run=args.dry_run,
        )
    )

    append_run_log(args.manifest, run_result)
    print(json.dumps(run_result, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'error: {exc}', file=sys.stderr)
        sys.exit(1)

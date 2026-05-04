import os
import json
import requests
from dotenv import load_dotenv

load_dotenv()

OPENROUTER_API_KEY = os.environ.get('OPENROUTER_API_KEY', '')
OPENROUTER_MODEL = os.environ.get('OPENROUTER_MODEL', '')
API_URL = 'https://openrouter.ai/api/v1/chat/completions'

SYSTEM_PROMPT = (
    "You are a Dungeon Master for a Dungeons & Dragons campaign. "
    "Respond in character as the DM, narrating the story, describing scenes, "
    "playing NPCs, and adjudicating player actions. "
    "Keep responses concise but vivid. Use dice rolls (via the player) when "
    "uncertainty arises. Assume standard 5e rules unless noted otherwise."
)

PLANNING_SYSTEM_PROMPT = (
    "You are a context-aware Dungeon Master helping a D&D party plan characters before play. "
    "You can see confirmed characters, party readiness, party planning summaries, pending bonds, "
    "explicit player points, DM-private secrets, and recent planning messages. Help the current "
    "player shape a character that fits the campaign, improves party balance, and creates story "
    "connections. Suggest and ask; do not force choices. Treat DM-private secrets as hidden from "
    "other players. Return only valid JSON with keys: message, active_page, form_patch. message is "
    "the visible DM response. active_page is one of identity, scores, combat, magic_gear, story, or "
    "null. form_patch contains only character-form fields that should be filled from the conversation; "
    "omit fields you are not confident about."
)

SUMMARY_SYSTEM_PROMPT = (
    "You update compact D&D campaign character-planning memory. Return only valid JSON. "
    "Preserve explicit player-stated points. Separate public facts from DM-private secrets. "
    "Suggest bonds only when two or more specific player user IDs are involved and both should approve. "
    "For explicit_player_points, return the complete canonical list for each user ID you include, not "
    "a delta to append. Only include durable player-authored character choices, preferences, constraints, "
    "boundaries, and accepted hooks. Exclude greetings, thanks, repeated wording, raw quoted messages, "
    "and operational requests like asking the DM to create, write, generate, fill in, or help. If a "
    "player delegates unspecified details to the DM, keep that as at most one concise durable preference "
    "only when it should affect future character creation. Rewrite duplicates into one clear sentence."
)

DRAFT_SYSTEM_PROMPT = (
    "You convert a D&D character planning conversation into a JSON character draft compatible with "
    "the app's character form. Return only valid JSON. Keep the draft playable at level 1 unless the "
    "campaign context clearly says otherwise. Include concise backstory hooks that fit the campaign."
)


def _require_openrouter_config():
    if not OPENROUTER_API_KEY:
        raise RuntimeError('OPENROUTER_API_KEY is not set')
    if not OPENROUTER_MODEL:
        raise RuntimeError('OPENROUTER_MODEL is not set')


def _post_chat(messages, json_mode=False):
    _require_openrouter_config()

    payload = {
        'model': OPENROUTER_MODEL,
        'messages': messages,
    }

    resp = requests.post(
        API_URL,
        headers={
            'Authorization': f'Bearer {OPENROUTER_API_KEY}',
            'Content-Type': 'application/json',
        },
        json=payload,
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    return data['choices'][0]['message']['content']


def _json_loads_or_empty(text):
    if not text:
        return {}
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        start = text.find('{')
        end = text.rfind('}')
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except (TypeError, ValueError):
                return {}
    return {}


def get_dm_response(session_messages):
    messages = [{'role': 'system', 'content': SYSTEM_PROMPT}]
    for msg in session_messages:
        if msg.role == 'dm':
            role = 'assistant'
        elif msg.role == 'player':
            role = 'user'
        else:
            role = msg.role
        messages.append({'role': role, 'content': msg.content})

    try:
        return _post_chat(messages)
    except Exception as e:
        print(f'[openrouter] Error: {e}')
        return None


def get_dm_response_with_context(session_messages, planning_context=None):
    messages = [{'role': 'system', 'content': SYSTEM_PROMPT}]
    if planning_context:
        messages.append({
            'role': 'system',
            'content': 'Campaign character planning context:\n' + json.dumps(planning_context, ensure_ascii=False),
        })

    for msg in session_messages:
        if msg.role == 'dm':
            role = 'assistant'
        elif msg.role == 'player':
            role = 'user'
        else:
            role = msg.role
        messages.append({'role': role, 'content': msg.content})

    try:
        return _post_chat(messages)
    except Exception as e:
        print(f'[openrouter] Error: {e}')
        return None


def get_planning_dm_response(context, current_user_messages, draft_character=None, active_page=None):
    messages = [
        {'role': 'system', 'content': PLANNING_SYSTEM_PROMPT},
        {
            'role': 'system',
            'content': 'Planning context:\n' + json.dumps({
                **context,
                'draft_character': draft_character or {},
                'active_page': active_page,
                'valid_pages': ['identity', 'scores', 'combat', 'magic_gear', 'story'],
            }, ensure_ascii=False),
        },
    ]
    for msg in current_user_messages:
        role = 'assistant' if msg.role == 'dm' else 'user'
        messages.append({'role': role, 'content': msg.content})

    try:
        text = _post_chat(messages, json_mode=True)
        data = _json_loads_or_empty(text)
        if isinstance(data, dict) and data.get('message'):
            return {
                'message': data.get('message') or '',
                'active_page': data.get('active_page'),
                'form_patch': data.get('form_patch') if isinstance(data.get('form_patch'), dict) else {},
            }
        return {'message': text, 'active_page': None, 'form_patch': {}}
    except Exception as e:
        print(f'[openrouter] Planning DM error: {e}')
        return None


def get_planning_summary_update(context, latest_player_message, latest_dm_message):
    messages = [
        {'role': 'system', 'content': SUMMARY_SYSTEM_PROMPT},
        {
            'role': 'user',
            'content': json.dumps({
                'context': context,
                'latest_player_message': latest_player_message,
                'latest_dm_message': latest_dm_message,
                'return_shape': {
                    'summary_update': {
                        'party_balance': 'short paragraph',
                        'confirmed_public_facts': ['facts visible to party'],
                        'dm_private_secrets': {'user_id': ['private facts or secrets']},
                        'explicit_player_points': {'user_id': ['complete canonical durable points for this user']},
                        'unresolved_gaps': ['party or story gaps still open'],
                        'accepted_hooks': ['confirmed hooks and approved bonds'],
                    },
                    'bond_suggestions': [
                        {
                            'title': 'short title',
                            'description': 'proposed connection requiring approval',
                            'involved_user_ids': [1, 2],
                        }
                    ],
                },
            }, ensure_ascii=False),
        },
    ]

    try:
        data = _json_loads_or_empty(_post_chat(messages, json_mode=True))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f'[openrouter] Planning summary error: {e}')
        return {}


def get_character_draft(context, current_user_messages):
    messages = [
        {'role': 'system', 'content': DRAFT_SYSTEM_PROMPT},
        {'role': 'system', 'content': 'Planning context:\n' + json.dumps(context, ensure_ascii=False)},
        {
            'role': 'user',
            'content': (
                "Create a JSON character draft with top-level fields accepted by the app: name, "
                "player_name, race, subrace, alignment, background, total_level, ability_scores, combat, "
                "general, spellcasting, currency, personality, appearance, background_details, classes, "
                "skills, saving_throws, proficiencies, features, weapons, equipment, spells, notes, "
                "resources, companions, conditions. Conversation:\n"
                + json.dumps([msg.to_dict() for msg in current_user_messages], ensure_ascii=False)
            ),
        },
    ]

    try:
        data = _json_loads_or_empty(_post_chat(messages, json_mode=True))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f'[openrouter] Character draft error: {e}')
        return {}

import os
import json
from uuid import uuid4
import requests
from dotenv import load_dotenv

from services.audit_service import log_model_error, log_model_request, log_model_response

load_dotenv()

OPENROUTER_API_KEY = os.environ.get('OPENROUTER_API_KEY', '')
OPENROUTER_MODEL = os.environ.get('OPENROUTER_MODEL', '')
API_URL = 'https://openrouter.ai/api/v1/chat/completions'

SYSTEM_PROMPT = (
    "You are a Dungeon Master for a Dungeons & Dragons campaign. "
    "For each response, determine from the current context whether the reply should be "
    "in character or out of character, and use whichever mode best serves the player. "
    "Player messages may contain <ic>...</ic> and <ooc>...</ooc> sections. Treat "
    "<ic> text as the player character's spoken words or direct in-world action, and "
    "<ooc> text as table talk, intent, questions, or instructions from the player. "
    "When in character, narrate the story, describe scenes, play NPCs, and adjudicate "
    "player actions. "
    "When campaign world memory includes NPC actor dossiers, silently coordinate the NPC's goals, "
    "secrets, recent offscreen activity, and relationship to the party before speaking for them. "
    "Never reveal DM-private memory unless it has become visible through play. "
    "Keep responses concise but vivid. Use dice rolls (via the player) when "
    "uncertainty arises. Assume standard 5e rules unless noted otherwise."
)

PLANNING_SYSTEM_PROMPT = (
    "You are a context-aware Dungeon Master helping a D&D party plan characters before play. "
    "For the visible message, determine from the current context whether the reply should be "
    "in character or out of character, and use whichever mode best serves the player. "
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

WORLD_GENESIS_SYSTEM_PROMPT = (
    "You are a D&D campaign architect creating the first persistent world package after all player "
    "characters are ready. Return only valid JSON. The public_intro must be a spoiler-free elevator "
    "pitch for players: no hidden villains, no true motives, no secret identities, no private clocks, "
    "and no DM-only explanations. Put all secrets, hidden agendas, true causes, NPC secrets, and "
    "pressure mechanics in dm_private, knowledge_graph, npc_actors, clocks, or world_state with "
    "visibility set to dm_private where appropriate. Build around the party's confirmed characters, "
    "accepted hooks, planning summary, campaign seed, description, and tone. Create a playable opening "
    "situation with active pressures, not a complete railroad."
)


def _require_openrouter_config():
    if not OPENROUTER_API_KEY:
        raise RuntimeError('OPENROUTER_API_KEY is not set')
    if not OPENROUTER_MODEL:
        raise RuntimeError('OPENROUTER_MODEL is not set')


def _post_chat(messages, json_mode=False, audit_context=None):
    audit_context = audit_context or {}
    campaign_id = audit_context.get('campaign_id')
    operation = audit_context.get('operation') or 'chat_completion'
    actor = audit_context.get('actor') or 'dm'
    trace_id = audit_context.get('trace_id') or f'{actor}:{operation}:{uuid4().hex[:10]}'
    parent_trace_id = audit_context.get('parent_trace_id')
    trace_label = audit_context.get('trace_label') or f'{actor}: {operation}'

    try:
        _require_openrouter_config()
    except Exception as err:
        if campaign_id:
            log_model_error(
                campaign_id,
                operation,
                actor,
                err,
                commit=True,
                trace_id=trace_id,
                parent_trace_id=parent_trace_id,
                trace_label=trace_label,
            )
        raise

    payload = {
        'model': OPENROUTER_MODEL,
        'messages': messages,
    }

    if campaign_id:
        log_model_request(
            campaign_id,
            operation,
            actor,
            messages,
            OPENROUTER_MODEL,
            json_mode=json_mode,
            commit=True,
            trace_id=trace_id,
            parent_trace_id=parent_trace_id,
            trace_label=trace_label,
        )

    try:
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
        if campaign_id:
            log_model_response(
                campaign_id,
                operation,
                actor,
                data,
                commit=True,
                trace_id=trace_id,
                parent_trace_id=parent_trace_id,
                trace_label=trace_label,
            )
        return data['choices'][0]['message']['content']
    except Exception as err:
        if campaign_id:
            log_model_error(
                campaign_id,
                operation,
                actor,
                err,
                commit=True,
                trace_id=trace_id,
                parent_trace_id=parent_trace_id,
                trace_label=trace_label,
            )
        raise


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
                pass
        return {}
    return {}


def build_dm_response_messages(session_messages, planning_context=None):
    messages = [{'role': 'system', 'content': SYSTEM_PROMPT}]
    if planning_context:
        messages.append({
            'role': 'system',
            'content': 'Campaign context, including private DM-only world memory when present:\n' + json.dumps(planning_context, ensure_ascii=False),
        })

    for msg in session_messages:
        if msg.role == 'dm':
            role = 'assistant'
        elif msg.role == 'player':
            role = 'user'
        else:
            role = msg.role
        messages.append({'role': role, 'content': msg.content})

    return messages


def build_opening_scene_messages(context, world_context):
    return [
        {'role': 'system', 'content': SYSTEM_PROMPT},
        {
            'role': 'system',
            'content': (
                'Use the private campaign world memory to write the first visible DM message. '
                'Do not reveal DM-private facts, hidden clocks, secret motives, true villains, or NPC secrets. '
                'Open at the public starting location with immediate sensory detail, establish the public party hook, '
                'and end with a clear prompt for player action.'
            ),
        },
        {
            'role': 'user',
            'content': json.dumps({
                'planning_context': context,
                'world_context': world_context,
            }, ensure_ascii=False),
        },
    ]


def build_planning_dm_messages(context, current_user_messages, draft_character=None, active_page=None):
    return [
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
        *[
            {
                'role': 'assistant' if msg.role == 'dm' else 'user',
                'content': msg.content,
            }
            for msg in current_user_messages
        ],
    ]


def build_planning_summary_messages(context, latest_player_message, latest_dm_message):
    return [
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


def build_character_draft_messages(context, current_user_messages):
    return [
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


def build_world_genesis_messages(context):
    return [
        {'role': 'system', 'content': WORLD_GENESIS_SYSTEM_PROMPT},
        {
            'role': 'user',
            'content': json.dumps({
                'context': context,
                'return_shape': {
                    'public_intro': {
                        'title': 'short campaign-facing title',
                        'elevator_pitch': '2-3 spoiler-free sentences for players',
                        'starting_location': 'public starting place name',
                        'campaign_tone': ['3-5 spoiler-free tone tags'],
                        'party_hook': 'why the party is together at the opening moment without revealing secrets',
                    },
                    'knowledge_graph': {
                        'entities': [
                            {
                                'id': 'stable_snake_case_id',
                                'type': 'npc | location | faction | item | event | threat | concept',
                                'name': 'display name',
                                'summary': 'durable fact summary',
                                'visibility': 'public | party_known | dm_private',
                                'tags': ['short tags'],
                            },
                        ],
                        'relations': [
                            {
                                'id': 'stable_snake_case_id',
                                'source_id': 'entity id',
                                'target_id': 'entity id',
                                'type': 'relationship type',
                                'summary': 'relationship summary',
                                'visibility': 'public | party_known | dm_private',
                            },
                        ],
                        'facts': [
                            {
                                'id': 'stable_snake_case_id',
                                'entity_ids': ['entity ids'],
                                'text': 'durable fact text',
                                'certainty': 'confirmed | suspected | false_rumor',
                                'visibility': 'public | party_known | dm_private',
                            },
                        ],
                    },
                    'world_state': {
                        'current_arc': 'opening arc name',
                        'current_scene': {
                            'location_id': 'entity id for starting location',
                            'location_name': 'display name',
                            'time_of_day': 'opening scene timing',
                            'active_npc_ids': ['npc actor ids present or relevant'],
                            'immediate_tension': 'what is visibly pressing right now',
                        },
                        'party': {
                            'known_location_id': 'entity id',
                            'public_reputation': 'how locals currently read the party',
                        },
                        'open_threads': ['non-spoiler and private thread summaries as appropriate'],
                    },
                    'dm_private': {
                        'true_inciting_incident': 'private truth behind the opening problem',
                        'villain_plan': 'private antagonist or pressure plan if applicable',
                        'hidden_factions': ['private faction notes'],
                        'npc_secrets': ['private NPC secrets'],
                        'opening_scene_private_notes': 'DM-only guidance for the first exchange',
                    },
                    'npc_actors': [
                        {
                            'id': 'npc_stable_id',
                            'name': 'NPC name',
                            'role': 'story role',
                            'public_summary': 'safe public-facing summary',
                            'voice': 'how they speak',
                            'background': 'private background',
                            'wants': ['motivations'],
                            'fears': ['fears'],
                            'secrets': ['private secrets'],
                            'relationships': {'party': 'initial relationship'},
                            'recent_offscreen_activity': ['what they did before the opening'],
                        },
                    ],
                    'clocks': [
                        {
                            'id': 'clock_stable_id',
                            'name': 'Clock name',
                            'segments': 4,
                            'filled': 0,
                            'pressure_type': 'faction | danger | mystery | environment | personal',
                            'visibility': 'public | party_known | dm_private',
                            'summary': 'what this pressure represents',
                            'trigger': 'when it advances',
                            'on_complete': 'what happens when filled',
                            'status': 'active',
                        },
                    ],
                },
            }, ensure_ascii=False),
        },
    ]


def get_dm_response(session_messages, audit_context=None):
    messages = build_dm_response_messages(session_messages)

    try:
        return _post_chat(messages, audit_context=audit_context)
    except Exception as e:
        print(f'[openrouter] Error: {e}')
        return None


def get_dm_response_with_context(session_messages, planning_context=None, audit_context=None):
    messages = build_dm_response_messages(session_messages, planning_context)

    try:
        return _post_chat(messages, audit_context=audit_context)
    except Exception as e:
        print(f'[openrouter] Error: {e}')
        return None


def get_opening_scene_response(context, world_context, audit_context=None):
    messages = build_opening_scene_messages(context, world_context)

    try:
        return _post_chat(messages, audit_context=audit_context)
    except Exception as e:
        print(f'[openrouter] Opening scene error: {e}')
        return None


def get_planning_dm_response(context, current_user_messages, draft_character=None, active_page=None, audit_context=None):
    messages = build_planning_dm_messages(context, current_user_messages, draft_character, active_page)

    try:
        text = _post_chat(messages, json_mode=True, audit_context=audit_context)
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


def get_planning_summary_update(context, latest_player_message, latest_dm_message, audit_context=None):
    messages = build_planning_summary_messages(context, latest_player_message, latest_dm_message)

    try:
        data = _json_loads_or_empty(_post_chat(messages, json_mode=True, audit_context=audit_context))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f'[openrouter] Planning summary error: {e}')
        return {}


def get_character_draft(context, current_user_messages, audit_context=None):
    messages = build_character_draft_messages(context, current_user_messages)

    try:
        data = _json_loads_or_empty(_post_chat(messages, json_mode=True, audit_context=audit_context))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f'[openrouter] Character draft error: {e}')
        return {}


def get_world_genesis_package(context, audit_context=None):
    messages = build_world_genesis_messages(context)

    try:
        data = _json_loads_or_empty(_post_chat(messages, json_mode=True, audit_context=audit_context))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f'[openrouter] World genesis error: {e}')
        return {}

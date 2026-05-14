import os
import json
from threading import Lock
from uuid import uuid4
import requests
from dotenv import load_dotenv

from services.audit_service import log_audit_event, log_model_error, log_model_request, log_model_response

load_dotenv()

OPENROUTER_API_KEY = os.environ.get('OPENROUTER_API_KEY', '')
OPENROUTER_MODEL = os.environ.get('OPENROUTER_MODEL', '')
_OPENROUTER_MODEL_LOCK = Lock()
_OPENROUTER_MODEL_OVERRIDE = None
API_URL = 'https://openrouter.ai/api/v1/chat/completions'

PC_CONTROL_POLICY = (
    "Player-character control policy: NPCs may speak and act. Player characters are protected. "
    "Do not write exact dialogue for any player character unless that player supplied the exact words. "
    "Do not narrate another player's PC actions, gestures, thoughts, emotions, decisions, or answers. "
    "For the current player's PC, resolve only the action or words they declared; prefer second person "
    "and do not add unprovided intent, emotion, or quoted speech. If one PC addresses another PC and "
    "no DM adjudication, NPC response, environmental consequence, or clarification is needed, stay silent."
)

SYSTEM_PROMPT = (
    "You are a Dungeon Master for a Dungeons & Dragons campaign. "
    "For each response, determine from the current context whether the reply should be "
    "in character or out of character, and use whichever mode best serves the player. "
    "Player messages may contain <ic>...</ic> and <ooc>...</ooc> sections. Treat "
    "<ic> text as the player character's spoken words or direct in-world action, and "
    "<ooc> text as table talk, intent, questions, or instructions from the player. "
    "When in character, narrate the story, describe scenes, play NPCs, and adjudicate "
    "player actions. When speaking as a specific NPC, wrap only that NPC's spoken line "
    "or performance in <npc target=\"NPC name\">...</npc>; leave narration outside the "
    "NPC tag. "
    + PC_CONTROL_POLICY + " "
    "When campaign world memory includes NPC actor dossiers, silently coordinate the NPC's goals, "
    "secrets, recent offscreen activity, and relationship to the party before speaking for them. "
    "Never reveal DM-private memory unless it has become visible through play. "
    "Keep responses concise but vivid. Use dice rolls (via the player) when "
    "uncertainty arises. Assume standard 5e rules unless noted otherwise."
)

SESSION_TOOL_PROMPT = (
    SYSTEM_PROMPT
    + " You are operating with compact hot context and server tools. Use read tools for exact "
    "character-sheet, world-memory, NPC, clock, or session facts instead of guessing. Use write tools "
    "only when the fiction has actually changed durable world state. Do not expose DM-private tool "
    "results in visible narration unless they became known through play. The hot context contains "
    "protected_player_characters and current_player_character; obey those boundaries exactly. "
    "For the final turn decision, return only valid JSON. Use {\"mode\":\"speak\",\"content\":\"...\"} "
    "when the DM should send a visible reply. Use {\"mode\":\"silent\",\"reason\":\"...\"} when the latest "
    "message is only PC-to-PC conversation or waiting on another PC and the DM should not send anything. "
    "Do not send handoff prompts such as 'How do you respond?' for ordinary PC-to-PC conversation."
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

SESSION_MEMORY_SYSTEM_PROMPT = (
    "You update durable D&D campaign memory after a visible DM turn. Return only valid JSON. "
    "Extract durable changes from the latest player message and visible DM reply. Create new clocks "
    "when new pressure, deadlines, mysteries, faction moves, or consequences emerge, especially if all "
    "existing active clocks are completed. Retire completed or resolved clocks instead of deleting them. "
    "Update knowledge graph entities, relations, and facts without duplicating existing ids. Preserve "
    "visibility as dm_private, party_known, or public. Do not invent large new lore unless it follows "
    "from the exchange. Only write graph facts for durable truths that should remain useful after the "
    "current scene changes. Do not write graph facts for current presence, current location, temporary "
    "awareness, momentary posture, weather, lighting, or other scene-state details. Put current "
    "location, occupants, and tension in scene_patch instead. If a character learns who was present, "
    "record the durable event or encounter, not a fact that someone knows who is currently present."
)


def get_openrouter_model():
    with _OPENROUTER_MODEL_LOCK:
        return _OPENROUTER_MODEL_OVERRIDE or OPENROUTER_MODEL


def get_openrouter_settings():
    model = get_openrouter_model()
    with _OPENROUTER_MODEL_LOCK:
        is_overridden = _OPENROUTER_MODEL_OVERRIDE is not None
    return {
        'model': model,
        'env_model': OPENROUTER_MODEL,
        'source': 'runtime' if is_overridden else 'env',
        'is_overridden': is_overridden,
        'api_key_configured': bool(OPENROUTER_API_KEY),
    }


def set_openrouter_model(model):
    global _OPENROUTER_MODEL_OVERRIDE

    next_model = (model or '').strip()
    if not next_model:
        raise ValueError('Model is required')
    if len(next_model) > 200:
        raise ValueError('Model must be 200 characters or fewer')
    with _OPENROUTER_MODEL_LOCK:
        _OPENROUTER_MODEL_OVERRIDE = next_model
    return get_openrouter_settings()


def reset_openrouter_model():
    global _OPENROUTER_MODEL_OVERRIDE

    with _OPENROUTER_MODEL_LOCK:
        _OPENROUTER_MODEL_OVERRIDE = None
    return get_openrouter_settings()


def _require_openrouter_config(model=None):
    if not OPENROUTER_API_KEY:
        raise RuntimeError('OPENROUTER_API_KEY is not set')
    if not (model if model is not None else get_openrouter_model()):
        raise RuntimeError('OPENROUTER_MODEL is not set')


def _estimate_tokens(value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return max(1, len(text) // 4) if text else 0


def _post_chat_response(
    messages,
    json_mode=False,
    audit_context=None,
    tools=None,
    tool_choice=None,
    parallel_tool_calls=None,
):
    audit_context = audit_context or {}
    campaign_id = audit_context.get('campaign_id')
    operation = audit_context.get('operation') or 'chat_completion'
    actor = audit_context.get('actor') or 'dm'
    trace_id = audit_context.get('trace_id') or f'{actor}:{operation}:{uuid4().hex[:10]}'
    parent_trace_id = audit_context.get('parent_trace_id')
    trace_label = audit_context.get('trace_label') or f'{actor}: {operation}'

    model = get_openrouter_model()
    try:
        _require_openrouter_config(model)
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
        'model': model,
        'messages': messages,
    }
    if tools:
        payload['tools'] = tools
    if tool_choice is not None:
        payload['tool_choice'] = tool_choice
    if parallel_tool_calls is not None:
        payload['parallel_tool_calls'] = parallel_tool_calls

    if campaign_id:
        log_model_request(
            campaign_id,
            operation,
            actor,
            messages,
            model,
            json_mode=json_mode,
            commit=True,
            trace_id=trace_id,
            parent_trace_id=parent_trace_id,
            trace_label=trace_label,
            tools=tools,
            tool_choice=tool_choice,
            parallel_tool_calls=parallel_tool_calls,
            context_manifest=audit_context.get('context_manifest'),
            token_estimate=audit_context.get('token_estimate') or {
                'estimated_message_tokens': _estimate_tokens(messages),
                'estimated_tool_schema_tokens': _estimate_tokens(tools or []),
                'full_world_graph_included': audit_context.get('full_world_graph_included', None),
            },
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
        return data
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


def _post_chat(messages, json_mode=False, audit_context=None):
    data = _post_chat_response(messages, json_mode=json_mode, audit_context=audit_context)
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
                pass
        return {}
    return {}


def normalize_session_dm_turn_decision(raw_decision):
    if isinstance(raw_decision, dict):
        data = raw_decision
    elif isinstance(raw_decision, str):
        data = _json_loads_or_empty(raw_decision)
        if not isinstance(data, dict) or not data:
            return {'mode': 'speak', 'content': raw_decision.strip()}
    else:
        return {'mode': 'speak', 'content': ''}

    mode = str(data.get('mode') or data.get('action') or '').strip().lower()
    if mode in {'silent', 'no_response', 'no_dm_response', 'none'}:
        return {
            'mode': 'silent',
            'content': '',
            'reason': str(data.get('reason') or 'The DM intentionally stayed silent.').strip(),
        }

    content = data.get('content')
    if content is None:
        content = data.get('message')
    if content is None:
        content = data.get('visible_message')
    return {
        'mode': 'speak',
        'content': str(content or '').strip(),
    }


def build_session_dm_tool_messages(hot_context):
    return [
        {'role': 'system', 'content': SESSION_TOOL_PROMPT},
        {
            'role': 'system',
            'content': 'Compact hot context. Full campaign memory is available through tools, not preloaded:\n'
            + json.dumps(hot_context, ensure_ascii=False),
        },
    ]


def _strip_npc_blocks(text):
    import re

    return re.sub(r'<npc\b[^>]*>.*?</npc>', '', text or '', flags=re.IGNORECASE | re.DOTALL)


def _pc_control_violation(response_text, hot_context):
    import re

    visible = _strip_npc_blocks(response_text)
    protected = hot_context.get('protected_player_characters') or []
    action_verbs = (
        'nods', 'nod', 'glances', 'glance', 'looks', 'look', 'turns', 'turn', 'steps', 'step',
        'moves', 'move', 'smiles', 'smile', 'frowns', 'frown', 'laughs', 'laugh', 'sighs',
        'sigh', 'shrugs', 'shrug', 'reaches', 'reach', 'draws', 'draw', 'takes', 'take',
        'gives', 'give', 'straightens', 'straighten', 'waits', 'wait',
    )
    speech_verbs = (
        'says', 'say', 'asks', 'ask', 'replies', 'reply', 'responds', 'respond', 'whispers',
        'whisper', 'mutters', 'mutter', 'shouts', 'shout', 'calls', 'call', 'answers', 'answer',
    )

    for character in protected:
        name = (character.get('name') or '').strip()
        if not name:
            continue
        escaped = re.escape(name)
        first = re.escape(name.split()[0])
        name_pattern = f'(?:{escaped}|{first})'
        handoff_pattern = rf'\b{name_pattern}\s*,\s+how do you respond\?'
        visible_for_character = re.sub(handoff_pattern, '', visible, flags=re.IGNORECASE)
        checks = [
            rf'\*\*{name_pattern}\b[^*]*:\*\*',
            rf'<npc\s+target=["\']{escaped}["\']',
            rf'\b{name_pattern}\b[^.!?\n]{{0,80}}\b(?:{"|".join(speech_verbs)})\b',
            rf'\b{name_pattern}(?:[’\']s)?\b[^.!?\n]{{0,80}}\b(?:{"|".join(action_verbs)})\b',
            rf'\b{name_pattern}[’\']s\s+(?:eyes|expression|face|voice|smirk|smile|shoulders|hands)\b',
        ]
        for pattern in checks:
            if re.search(pattern, visible_for_character, flags=re.IGNORECASE):
                return {
                    'character': character,
                    'pattern': pattern,
                }

    return None


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


def build_session_memory_messages(memory_context):
    return [
        {'role': 'system', 'content': SESSION_MEMORY_SYSTEM_PROMPT},
        {
            'role': 'user',
            'content': json.dumps({
                'context': memory_context,
                'return_shape': {
                    'running_summary': 'compact updated session summary, replacing prior summary',
                    'scene_patch': {
                        'location_id': 'optional current scene location id; use for temporary location state',
                        'location_name': 'optional current scene location name',
                        'time_of_day': 'optional current scene timing',
                        'active_npc_ids': ['npc ids currently present or active in the scene; use this instead of graph facts for occupants'],
                        'immediate_tension': 'optional current scene tension; use for transient pressure visible right now',
                    },
                    'scene_reason': 'why scene changed',
                    'upsert_graph_entities': [
                        {'id': 'stable_id', 'type': 'npc | location | faction | item | event | threat | concept', 'name': 'display name', 'summary': 'durable summary', 'visibility': 'public | party_known | dm_private', 'tags': []}
                    ],
                    'upsert_graph_relations': [
                        {'id': 'stable_id', 'source_id': 'entity id', 'target_id': 'entity id', 'type': 'relationship type', 'summary': 'durable summary', 'visibility': 'public | party_known | dm_private'}
                    ],
                    'upsert_graph_facts': [
                        {'id': 'stable_id', 'entity_ids': ['all directly relevant entity ids'], 'text': 'durable fact; not current scene state, temporary presence, temporary location, or momentary character awareness', 'certainty': 'confirmed | suspected | false_rumor', 'visibility': 'public | party_known | dm_private'}
                    ],
                    'create_clocks': [
                        {'id': 'stable_clock_id', 'name': 'Clock name', 'segments': 4, 'filled': 0, 'pressure_type': 'faction | danger | mystery | environment | personal | story', 'visibility': 'public | party_known | dm_private', 'summary': 'pressure summary', 'trigger': 'when it advances', 'on_complete': 'what happens', 'status': 'active', 'reason': 'why this clock now exists'}
                    ],
                    'retire_clocks': [
                        {'clock_id': 'existing_clock_id', 'status': 'completed | resolved | inactive', 'reason': 'why retired'}
                    ],
                    'update_npc_actors': [
                        {'id': 'npc_stable_id', 'name': 'NPC name', 'role': 'story role', 'public_summary': 'public summary', 'wants': [], 'fears': [], 'secrets': [], 'relationships': {}, 'recent_offscreen_activity': [], 'reason': 'why updated'}
                    ],
                    'record_events': [
                        {'event_type': 'short_type', 'summary': 'durable event summary', 'payload': {}, 'visibility': 'public | party_known | dm_private'}
                    ],
                },
            }, ensure_ascii=False),
        },
    ]


def _choice_message(data):
    choices = data.get('choices') if isinstance(data, dict) else []
    if not choices or not isinstance(choices[0], dict):
        return {}, None
    message = choices[0].get('message') or {}
    return message if isinstance(message, dict) else {}, choices[0].get('finish_reason')


def _parse_tool_arguments(raw_arguments):
    if isinstance(raw_arguments, dict):
        return raw_arguments
    if not raw_arguments:
        return {}
    try:
        return json.loads(raw_arguments)
    except (TypeError, ValueError):
        return {}


def _assistant_tool_message(message):
    return {
        'role': 'assistant',
        'content': message.get('content'),
        'tool_calls': message.get('tool_calls') or [],
    }


def get_session_dm_response_with_tools(
    hot_context,
    recent_messages,
    tools,
    execute_tool,
    audit_context=None,
    max_tool_rounds=4,
):
    base_audit = audit_context or {}
    trace_id = base_audit.get('trace_id') or f"session_dm:session_dm_response:{uuid4().hex[:10]}"
    trace_label = base_audit.get('trace_label') or 'session_dm: session_dm_response'
    messages = build_session_dm_tool_messages(hot_context)
    for msg in recent_messages:
        if msg.role == 'dm':
            role = 'assistant'
        elif msg.role == 'player':
            role = 'user'
        else:
            role = msg.role
        messages.append({'role': role, 'content': msg.content})

    tool_round = 0
    pc_control_retried = False
    while True:
        loop_audit = {
            **base_audit,
            'trace_id': trace_id,
            'trace_label': trace_label,
            'operation': base_audit.get('operation') or 'session_dm_response',
            'context_manifest': base_audit.get('context_manifest'),
            'token_estimate': base_audit.get('token_estimate'),
            'full_world_graph_included': False,
        }
        data = _post_chat_response(
            messages,
            audit_context=loop_audit,
            tools=tools,
            tool_choice='auto' if tool_round < max_tool_rounds else 'none',
            parallel_tool_calls=False,
        )
        message, finish_reason = _choice_message(data)
        tool_calls = message.get('tool_calls') or []
        if not tool_calls or tool_round >= max_tool_rounds:
            raw_content = message.get('content') or ''
            decision = normalize_session_dm_turn_decision(raw_content)
            content = decision.get('content') or ''
            violation = _pc_control_violation(content, hot_context) if decision.get('mode') == 'speak' else None
            if violation and not pc_control_retried:
                if base_audit.get('campaign_id'):
                    log_audit_event(
                        base_audit.get('campaign_id'),
                        'pc_control_guard_retry',
                        'Session DM response controlled a protected player character; requesting rewrite.',
                        {
                            'violation': violation,
                            'draft_response': raw_content,
                        },
                        source='session_dm.guard',
                        actor='server',
                        trace_id=trace_id,
                        trace_label=trace_label,
                        audit_role='guard',
                        commit=True,
                    )
                messages.append({'role': 'assistant', 'content': raw_content})
                messages.append({
                    'role': 'user',
                    'content': (
                        'Rewrite the previous response. It controlled a protected player character. '
                        'Do not write dialogue, actions, gestures, thoughts, emotions, or decisions for any PC. '
                        'If a player character addresses another player character and no DM adjudication is needed, '
                        'return {"mode":"silent","reason":"PC-to-PC exchange."}. Otherwise return the same JSON '
                        'contract with mode="speak" and safe DM-visible content.'
                    ),
                })
                pc_control_retried = True
                tool_round = max_tool_rounds
                continue
            if violation:
                if base_audit.get('campaign_id'):
                    log_audit_event(
                        base_audit.get('campaign_id'),
                        'pc_control_guard_blocked',
                        'Session DM response still controlled a protected player character after retry.',
                        {
                            'violation': violation,
                            'draft_response': raw_content,
                        },
                        source='session_dm.guard',
                        actor='server',
                        trace_id=trace_id,
                        trace_label=trace_label,
                        audit_role='guard',
                        commit=True,
                    )
                return {
                    'mode': 'silent',
                    'reason': 'The DM response would have controlled a protected player character.',
                }
            return decision

        messages.append(_assistant_tool_message(message))
        for tool_call in tool_calls:
            function = tool_call.get('function') or {}
            tool_name = function.get('name')
            args = _parse_tool_arguments(function.get('arguments'))
            result = execute_tool(
                tool_name,
                args,
                {
                    **loop_audit,
                    'parent_trace_id': trace_id,
                    'trace_id': trace_id,
                },
            )
            messages.append({
                'role': 'tool',
                'tool_call_id': tool_call.get('id'),
                'name': tool_name,
                'content': json.dumps(result, ensure_ascii=False),
            })
        tool_round += 1


def get_opening_scene_response(context, world_context, audit_context=None):
    messages = build_opening_scene_messages(context, world_context)

    try:
        return _post_chat(messages, audit_context=audit_context)
    except Exception as e:
        print(f'[openrouter] Opening scene error: {e}')
        return None


def get_session_memory_patch(memory_context, audit_context=None):
    messages = build_session_memory_messages(memory_context)
    audit_context = audit_context or {}
    campaign_id = audit_context.get('campaign_id')
    trace_id = audit_context.get('trace_id') or f"session_memory_writer:memory_update:{uuid4().hex[:10]}"
    trace_label = audit_context.get('trace_label') or 'session_memory_writer: memory_update'
    if campaign_id:
        log_audit_event(
            campaign_id,
            'memory_writer_request',
            'Requested post-turn session memory update.',
            {'context': memory_context, 'messages': messages},
            source='openrouter',
            actor='session_memory_writer',
            trace_id=trace_id,
            parent_trace_id=audit_context.get('parent_trace_id'),
            trace_label=trace_label,
            audit_role='tools',
            commit=True,
        )
    try:
        text = _post_chat(
            messages,
            json_mode=True,
            audit_context={
                **audit_context,
                'trace_id': trace_id,
                'trace_label': trace_label,
                'operation': audit_context.get('operation') or 'session_memory_update',
                'actor': 'session_memory_writer',
                'full_world_graph_included': False,
            },
        )
        data = _json_loads_or_empty(text)
        if campaign_id:
            log_audit_event(
                campaign_id,
                'memory_writer_response',
                'Received post-turn session memory patch.',
                {'patch': data},
                source='openrouter',
                actor='session_memory_writer',
                trace_id=trace_id,
                parent_trace_id=audit_context.get('parent_trace_id'),
                trace_label=trace_label,
                audit_role='agent',
                commit=True,
            )
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f'[openrouter] Session memory writer error: {e}')
        return {}


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


def get_world_genesis_package(context, audit_context=None):
    messages = build_world_genesis_messages(context)

    try:
        data = _json_loads_or_empty(_post_chat(messages, json_mode=True, audit_context=audit_context))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f'[openrouter] World genesis error: {e}')
        return {}

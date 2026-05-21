import json
from datetime import datetime

from models import (
    db,
    CampaignClock,
    CampaignMember,
    CampaignWorld,
    Character,
    NPCActor,
    SheetProposal,
    WorldEvent,
)
from services.audit_service import log_audit_event
from services.character_service import character_full_dict
from services.embedding_service import (
    find_duplicate_graph_item,
    search_memory_embeddings,
    search_weight,
    upsert_memory_embedding,
)
from services.lootbox_service import generate_loot_box as do_generate_loot_box
from services.planning_service import summary_dict_for_read
from services.world_service import clean_id, clean_text, get_campaign_world, json_dumps, json_loads
from openrouter import get_character_sheet_answer


VALID_VISIBILITIES = {'public', 'party_known', 'dm_private'}
ACTIVE_CLOCK_STATUSES = {'active', 'ticking', 'pending'}


def estimate_tokens(value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return max(1, len(text) // 4) if text else 0


def _safe_json(value, fallback):
    if isinstance(value, str):
        return json_loads(value, fallback)
    return value if isinstance(value, type(fallback)) else fallback


def _selected_member(campaign_id, user_id):
    return CampaignMember.query.filter_by(campaign_id=campaign_id, user_id=user_id).first()


def _compact_character(character):
    if not character:
        return None
    data = character.to_dict()
    return {
        'id': data.get('id'),
        'user_id': data.get('user_id'),
        'name': data.get('name'),
        'race': data.get('race'),
        'subrace': data.get('subrace'),
        'background': data.get('background'),
        'total_level': data.get('total_level'),
        'ability_scores': data.get('ability_scores'),
        'combat': data.get('combat'),
        'general': data.get('general'),
        'spellcasting': data.get('spellcasting'),
        'current_location': getattr(character, 'current_location', None),
    }


def _current_character(campaign, current_user):
    member = _selected_member(campaign.id, current_user.id)
    if member and member.selected_character_id:
        return db.session.get(Character, member.selected_character_id)
    return Character.query.filter_by(campaign_id=campaign.id, user_id=current_user.id).first()


def _protected_player_characters(members):
    protected = []
    for member in members:
        character = db.session.get(Character, member.selected_character_id) if member.selected_character_id else None
        if character:
            protected.append({
                'id': character.id,
                'name': character.name,
                'user_id': member.user_id,
                'username': member.user.username if member.user else None,
            })
    return protected


def _world_json(campaign):
    world = get_campaign_world(campaign.id)
    if not world:
        return None, {}, {}, {}
    return (
        world,
        json_loads(world.knowledge_graph, {'entities': [], 'relations': [], 'facts': []}),
        json_loads(world.world_state, {}),
        json_loads(world.dm_private, {}),
    )


def _active_clocks(campaign, limit=8):
    clocks = CampaignClock.query.filter_by(campaign_id=campaign.id).order_by(CampaignClock.id.asc()).all()
    active = [clock for clock in clocks if (clock.status or 'active') in ACTIVE_CLOCK_STATUSES]
    return (active or clocks)[:limit]


def _private_output_terms(campaign):
    _world, graph, _world_state, dm_private = _world_json(campaign)
    terms = set()

    def add(value):
        text = clean_text(value, 240)
        if len(text) >= 4:
            terms.add(text)

    for entity in graph.get('entities', []) if isinstance(graph, dict) else []:
        if entity.get('visibility') == 'dm_private':
            add(entity.get('name'))
    for relation in graph.get('relations', []) if isinstance(graph, dict) else []:
        if relation.get('visibility') == 'dm_private':
            add(relation.get('summary'))
    for fact in graph.get('facts', []) if isinstance(graph, dict) else []:
        if fact.get('visibility') == 'dm_private':
            add(fact.get('text'))

    for clock in CampaignClock.query.filter_by(campaign_id=campaign.id).all():
        if (clock.visibility or 'dm_private') == 'dm_private':
            add(clock.name)

    for npc in NPCActor.query.filter_by(campaign_id=campaign.id).all():
        dossier = json_loads(npc.dossier, {})
        for secret in dossier.get('secrets', []) if isinstance(dossier, dict) else []:
            add(secret)

    if isinstance(dm_private, dict):
        for key in ('true_inciting_incident', 'villain_plan'):
            add(dm_private.get(key))
        for value in dm_private.get('hidden_factions', []) if isinstance(dm_private.get('hidden_factions'), list) else []:
            add(value)
        for value in dm_private.get('npc_secrets', []) if isinstance(dm_private.get('npc_secrets'), list) else []:
            add(value)

    return sorted(terms, key=lambda value: value.lower())


def _private_spoiler_items(campaign):
    _world, graph, _world_state, dm_private = _world_json(campaign)
    items = []
    seen = set()

    def add(item_id, kind, text):
        clean = clean_text(text, 700)
        if not clean:
            return
        dedupe_key = clean.lower()
        if dedupe_key in seen:
            return
        seen.add(dedupe_key)
        items.append({
            'id': clean_id(item_id, f'private_item_{len(items) + 1}'),
            'kind': kind,
            'text': clean,
        })

    for entity in graph.get('entities', []) if isinstance(graph, dict) else []:
        if entity.get('visibility') == 'dm_private':
            add(entity.get('id'), 'entity', ' - '.join(
                part for part in [entity.get('name'), entity.get('summary')] if part
            ))
    for relation in graph.get('relations', []) if isinstance(graph, dict) else []:
        if relation.get('visibility') == 'dm_private':
            add(relation.get('id'), 'relation', relation.get('summary'))
    for fact in graph.get('facts', []) if isinstance(graph, dict) else []:
        if fact.get('visibility') == 'dm_private':
            add(fact.get('id'), 'fact', fact.get('text'))

    for clock in CampaignClock.query.filter_by(campaign_id=campaign.id).all():
        if (clock.visibility or 'dm_private') == 'dm_private':
            add(
                f'clock_{clock.clock_id}',
                'clock',
                ' - '.join(part for part in [clock.name, clock.summary, clock.trigger, clock.on_complete] if part),
            )

    for npc in NPCActor.query.filter_by(campaign_id=campaign.id).all():
        dossier = json_loads(npc.dossier, {})
        for index, secret in enumerate(dossier.get('secrets', []) if isinstance(dossier, dict) else []):
            add(f'npc_secret_{npc.actor_id}_{index + 1}', 'npc_secret', secret)

    if isinstance(dm_private, dict):
        for key in ('true_inciting_incident', 'villain_plan'):
            add(f'dm_private_{key}', key, dm_private.get(key))
        for index, value in enumerate(dm_private.get('hidden_factions', []) if isinstance(dm_private.get('hidden_factions'), list) else []):
            add(f'dm_private_hidden_faction_{index + 1}', 'hidden_faction', value)
        for index, value in enumerate(dm_private.get('npc_secrets', []) if isinstance(dm_private.get('npc_secrets'), list) else []):
            add(f'dm_private_npc_secret_{index + 1}', 'npc_secret', value)

    return items


def _campaign_loot_mode_policy(campaign):
    import json
    try:
        settings = json.loads(campaign.settings) if isinstance(campaign.settings, str) else (campaign.settings or {})
    except (TypeError, ValueError):
        settings = {}
    loot_mode = settings.get('loot_mode', 'frequent_gamble')

    if loot_mode == 'rare_quality':
        return (
            'Loot policy: The party is in "Rare Quality" mode. '
            'Use the generate_loot_box tool sparingly — only after major boss fights, '
            'significant story milestones, or truly exceptional achievements. '
            'When you do generate loot, the items should feel meaningful and memorable. '
            'Make each drop count.'
        )
    return (
        'Loot policy: The party is in "Frequent Gamble" mode. '
        'Use the generate_loot_box tool often — after combat encounters, exploration '
        'discoveries, social victories, or any notable achievement. '
        'The fun is in the frequency — keep the rewards coming regularly.'
    )


def _campaign_loot_mode(campaign):
    import json
    try:
        settings = json.loads(campaign.settings) if isinstance(campaign.settings, str) else (campaign.settings or {})
    except (TypeError, ValueError):
        settings = {}
    return settings.get('loot_mode', 'frequent_gamble')


def build_session_hot_context(campaign, session, current_user):
    character = _current_character(campaign, current_user)
    world, _graph, world_state, _private = _world_json(campaign)
    recent_messages = session.messages[-8:] if session and session.messages else []
    active_clocks = _active_clocks(campaign)
    current_scene = world_state.get('current_scene', {}) if isinstance(world_state, dict) else {}
    members = CampaignMember.query.filter_by(campaign_id=campaign.id).order_by(CampaignMember.id.asc()).all()
    protected_player_characters = _protected_player_characters(members)
    loot_mode = _campaign_loot_mode(campaign)

    context = {
        'strategy': 'compact_hot_context_with_dm_tools',
        'full_world_graph_included': False,
        'campaign': {
            'id': campaign.id,
            'name': campaign.name,
            'description': campaign.description,
            'difficulty': campaign.difficulty,
            'seed': campaign.seed,
            'loot_mode': loot_mode,
        },
        'session': {
            'id': session.id,
            'running_summary': session.running_summary or '',
            'message_count': len(session.messages or []),
        },
        'current_user': current_user.to_dict(),
        'current_character': _compact_character(character),
        'current_player_character': (
            {
                'id': character.id,
                'name': character.name,
                'user_id': character.user_id,
            }
            if character else None
        ),
        'protected_player_characters': protected_player_characters,
        'party': [
            {
                'user_id': member.user_id,
                'username': member.user.username if member.user else None,
                'selected_character_id': member.selected_character_id,
                'selected_character': next((
                    pc for pc in protected_player_characters
                    if pc['id'] == member.selected_character_id
                ), None),
                'ready': bool(member.character_ready_at and member.selected_character_id),
            }
            for member in members
        ],
        'current_scene': current_scene,
        'active_clocks': [clock.to_dict(include_private=True) for clock in active_clocks],
        'private_output_terms': _private_output_terms(campaign),
        'private_spoiler_items': _private_spoiler_items(campaign),
        'recent_messages': [message.to_dict() for message in recent_messages],
        'tool_policy': (
            'Use tools for character-sheet answers, campaign memory, NPC dossiers, clocks, and durable state writes. '
            'Do not claim to update world state unless a write tool succeeds. Never reveal DM-private tool results '
            'unless they have become visible through play. private_output_terms are reasoning-only strings that must '
            'not appear in visible narration unless they are first revealed through play. '
            + _campaign_loot_mode_policy(campaign)
        ),
    }
    return context


def context_manifest(context, tools):
    section_tokens = {
        key: estimate_tokens(value)
        for key, value in context.items()
        if key not in {'recent_messages'}
    }
    section_tokens['recent_messages'] = estimate_tokens(context.get('recent_messages', []))
    return {
        'strategy': context.get('strategy'),
        'full_world_graph_included': False,
        'fed_sections': list(context.keys()),
        'available_tools': [tool['function']['name'] for tool in tools],
        'estimated_tokens_by_section': section_tokens,
        'estimated_total_tokens': sum(section_tokens.values()),
    }


def build_session_memory_context(campaign, session, current_user, player_message, dm_message, hot_context):
    search_terms = ' '.join(
        text for text in [
            player_message,
            dm_message,
            json.dumps(hot_context.get('current_scene', {}), ensure_ascii=False),
        ]
        if text
    )
    return {
        'campaign_id': campaign.id,
        'session_id': session.id,
        'current_user': current_user.to_dict(),
        'prior_running_summary': session.running_summary or '',
        'latest_player_message': player_message,
        'latest_dm_message': dm_message,
        'hot_context': hot_context,
        'current_scene': _tool_get_current_scene(campaign, current_user, {'include_private': True}),
        'relevant_memory': _tool_search_campaign_memory(
            campaign,
            current_user,
            {'query': search_terms[:240], 'limit': 10},
        ),
        'active_clock_count': len(_active_clocks(campaign, limit=50)),
        'all_active_clocks_completed': all(
            (clock.status or 'active') not in ACTIVE_CLOCK_STATUSES or (clock.filled or 0) >= (clock.segments or 4)
            for clock in CampaignClock.query.filter_by(campaign_id=campaign.id).all()
        ),
    }


SHEET_SCALAR_FIELDS = {
    'max_hp': {'type': 'int', 'min': 0},
    'current_hp': {'type': 'int', 'min': 0},
    'temp_hp': {'type': 'int', 'min': 0},
    'armor_class': {'type': 'int', 'min': 0},
    'initiative_bonus': {'type': 'int'},
    'speed': {'type': 'int', 'min': 0},
    'death_save_successes': {'type': 'int', 'min': 0, 'max': 3},
    'death_save_failures': {'type': 'int', 'min': 0, 'max': 3},
    'inspiration': {'type': 'bool'},
    'proficiency_bonus': {'type': 'int', 'min': 0},
    'passive_perception': {'type': 'int', 'min': 0},
    'exhaustion_level': {'type': 'int', 'min': 0, 'max': 6},
    'experience_points': {'type': 'int', 'min': 0},
    'cp': {'type': 'int', 'min': 0},
    'sp': {'type': 'int', 'min': 0},
    'ep': {'type': 'int', 'min': 0},
    'gp': {'type': 'int', 'min': 0},
    'pp': {'type': 'int', 'min': 0},
    'spell_slots_used_1': {'type': 'int', 'min': 0},
    'spell_slots_used_2': {'type': 'int', 'min': 0},
    'spell_slots_used_3': {'type': 'int', 'min': 0},
    'spell_slots_used_4': {'type': 'int', 'min': 0},
    'spell_slots_used_5': {'type': 'int', 'min': 0},
    'spell_slots_used_6': {'type': 'int', 'min': 0},
    'spell_slots_used_7': {'type': 'int', 'min': 0},
    'spell_slots_used_8': {'type': 'int', 'min': 0},
    'spell_slots_used_9': {'type': 'int', 'min': 0},
}

SHEET_LIST_OPERATIONS = ('condition', 'equipment')

FIELD_LABELS = {
    'max_hp': 'Max HP',
    'current_hp': 'Current HP',
    'temp_hp': 'Temp HP',
    'armor_class': 'Armor Class',
    'initiative_bonus': 'Initiative Bonus',
    'speed': 'Speed',
    'death_save_successes': 'Death Save Successes',
    'death_save_failures': 'Death Save Failures',
    'inspiration': 'Inspiration',
    'proficiency_bonus': 'Proficiency Bonus',
    'passive_perception': 'Passive Perception',
    'exhaustion_level': 'Exhaustion Level',
    'experience_points': 'Experience Points',
    'cp': 'CP',
    'sp': 'SP',
    'ep': 'EP',
    'gp': 'GP',
    'pp': 'PP',
    'spell_slots_used_1': 'Level 1 Spell Slots Used',
    'spell_slots_used_2': 'Level 2 Spell Slots Used',
    'spell_slots_used_3': 'Level 3 Spell Slots Used',
    'spell_slots_used_4': 'Level 4 Spell Slots Used',
    'spell_slots_used_5': 'Level 5 Spell Slots Used',
    'spell_slots_used_6': 'Level 6 Spell Slots Used',
    'spell_slots_used_7': 'Level 7 Spell Slots Used',
    'spell_slots_used_8': 'Level 8 Spell Slots Used',
    'spell_slots_used_9': 'Level 9 Spell Slots Used',
}


def _sheet_field_label(field):
    clean = field.split(':', 1)[0]
    return FIELD_LABELS.get(clean, clean.replace('_', ' ').title())


def _current_list_item_count(character, relation_name, item_name):
    items = getattr(character, relation_name, [])
    return sum(
        1 for item in items
        if (getattr(item, 'condition_name', None) or getattr(item, 'name', None) or '').lower() == item_name.lower()
    )


def _compute_change(character, change):
    field = change.get('field', '')
    operation = change.get('operation', '')
    raw_value = change.get('value', 0)

    if ':' in field:
        prefix, item_name = field.split(':', 1)
        prefix = prefix.strip().lower()
        if prefix not in SHEET_LIST_OPERATIONS:
            return None
        item_name = item_name.strip()
        label_prefix = prefix.capitalize()
        count = _current_list_item_count(character, f'{prefix}s' if prefix != 'equipment' else prefix, item_name)
        if operation == 'add' or (operation == 'set' and raw_value):
            return {
                'field': field,
                'operation': operation,
                'value': {'name': item_name},
                'before': {'count': count},
                'after': {'count': count + 1},
                'label': f'{label_prefix}: {item_name}',
            }
        elif operation in ('subtract', 'remove') or (operation == 'set' and not raw_value):
            return {
                'field': field,
                'operation': operation,
                'value': {'name': item_name},
                'before': {'count': count},
                'after': {'count': max(0, count - 1)},
                'label': f'{label_prefix}: {item_name}',
            }
        return None

    config = SHEET_SCALAR_FIELDS.get(field)
    if not config:
        return None

    try:
        current = getattr(character, field, 0)
    except (TypeError, AttributeError):
        return None

    if config['type'] == 'bool':
        current = bool(current)
        new = bool(raw_value)
        if operation == 'set':
            new = bool(raw_value)
        elif operation == 'add':
            new = True
        elif operation == 'subtract':
            new = False
        else:
            return None
    else:
        try:
            current = int(current) if current is not None else 0
            delta = int(raw_value)
        except (TypeError, ValueError):
            return None

        if operation == 'set':
            new = delta
        elif operation == 'add':
            new = current + delta
        elif operation == 'subtract':
            new = current - delta
        else:
            return None

        min_val = config.get('min')
        max_val = config.get('max')
        if min_val is not None:
            new = max(min_val, new)
        if max_val is not None:
            new = min(max_val, new)

    return {
        'field': field,
        'operation': operation,
        'value': raw_value,
        'before': current,
        'after': new,
        'label': _sheet_field_label(field),
    }


DM_TOOL_DEFINITIONS = [
    {
        'type': 'function',
        'function': {
            'name': 'ask_character_sheet',
            'description': 'Ask a focused question about the current player, party, or a specific character sheet and receive only the concise answer needed.',
            'parameters': {
                'type': 'object',
                'required': ['question'],
                'properties': {
                    'scope': {
                        'type': 'string',
                        'enum': ['current_player', 'party', 'character_id'],
                        'default': 'current_player',
                    },
                    'character_id': {'type': 'integer'},
                    'question': {'type': 'string'},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_current_scene',
            'description': 'Fetch current scene, active clocks, active NPCs, and recent world events.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'include_private': {'type': 'boolean', 'default': True},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'search_campaign_memory',
            'description': 'Search campaign memory across graph entities, relations, facts, NPC actors, clocks, events, and planning summary.',
            'parameters': {
                'type': 'object',
                'required': ['query'],
                'properties': {
                    'query': {'type': 'string'},
                    'limit': {'type': 'integer', 'default': 8, 'minimum': 1, 'maximum': 20},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'record_world_event',
            'description': 'Record a durable world event after something meaningful happens in play.',
            'parameters': {
                'type': 'object',
                'required': ['event_type', 'summary'],
                'properties': {
                    'event_type': {'type': 'string'},
                    'summary': {'type': 'string'},
                    'payload': {'type': 'object'},
                    'visibility': {'type': 'string', 'enum': ['public', 'party_known', 'dm_private'], 'default': 'dm_private'},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'update_current_scene',
            'description': 'Update current scene fields in world state and record the change.',
            'parameters': {
                'type': 'object',
                'required': ['scene_patch'],
                'properties': {
                    'scene_patch': {'type': 'object'},
                    'reason': {'type': 'string'},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'advance_clock',
            'description': 'Advance or reduce an existing campaign clock and record the change.',
            'parameters': {
                'type': 'object',
                'required': ['clock_id', 'delta'],
                'properties': {
                    'clock_id': {'type': 'string'},
                    'delta': {'type': 'integer'},
                    'reason': {'type': 'string'},
                    'status': {'type': 'string'},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'reveal_fact',
            'description': 'Change a graph entity, relation, or fact visibility after it becomes known through play.',
            'parameters': {
                'type': 'object',
                'required': ['item_type', 'item_id', 'visibility'],
                'properties': {
                    'item_type': {'type': 'string', 'enum': ['entity', 'relation', 'fact']},
                    'item_id': {'type': 'string'},
                    'visibility': {'type': 'string', 'enum': ['public', 'party_known', 'dm_private']},
                    'reason': {'type': 'string'},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'propose_sheet_update',
            'description': 'Propose a mechanical update to a character sheet based on visible events in play. Use ask_character_sheet first to check current values. The player must approve the change unless the character is DM-controlled.',
            'parameters': {
                'type': 'object',
                'required': ['character_id', 'reason', 'changes'],
                'properties': {
                    'character_id': {'type': 'integer', 'description': 'ID of the character to update.'},
                    'reason': {'type': 'string', 'description': 'Brief player-visible explanation of why this change is happening.'},
                    'changes': {
                        'type': 'array',
                        'items': {
                            'type': 'object',
                            'required': ['field', 'operation', 'value'],
                            'properties': {
                                'field': {
                                    'type': 'string',
                                    'description': 'Scalar field (current_hp, gp, inspiration, spell_slots_used_1, exhaustion_level, etc.) or list field (condition:Poisoned, equipment:Potion of Healing).',
                                },
                                'operation': {
                                    'type': 'string',
                                    'enum': ['add', 'subtract', 'set'],
                                    'description': 'add, subtract, or set the value.',
                                },
                                'value': {
                                    'type': 'number',
                                    'description': 'Numeric value to add/subtract/set. For boolean fields use 1 (true) or 0 (false).',
                                },
                            },
                        },
                    },
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'generate_loot_box',
            'description': 'Generate a loot box for the party after a notable achievement like a combat victory, discovery, or reward scene. Call this when the party earns treasure through their actions. The generated loot box will appear in the campaign stash for players to inspect and open.',
            'parameters': {
                'type': 'object',
                'required': ['name'],
                'properties': {
                    'name': {
                        'type': 'string',
                        'description': 'Thematic name for this loot box, e.g. "Goblin Chieftain\'s Hoard" or "Crypt of the Forgotten Knight".',
                    },
                    'description': {
                        'type': 'string',
                        'description': 'Flavor text describing where this loot came from and what it looks like. Will be visible to players.',
                    },
                },
            },
        },
    },
]


def _tool_ask_character_sheet(campaign, current_user, args, audit_context=None):
    scope = args.get('scope') or 'current_player'
    question = clean_text(args.get('question'), 600)
    if not question:
        return {
            'scope': scope,
            'answer': 'A character-sheet question is required.',
            'character_ids': [],
            'missing': True,
        }

    if scope == 'party':
        members = CampaignMember.query.filter_by(campaign_id=campaign.id).order_by(CampaignMember.id.asc()).all()
        character_sheets = []
        for member in members:
            character = db.session.get(Character, member.selected_character_id) if member.selected_character_id else None
            if character:
                character_sheets.append({
                    'user_id': member.user_id,
                    'username': member.user.username if member.user else None,
                    'character': character_full_dict(character),
                })
    else:
        if scope == 'character_id':
            character = Character.query.filter_by(id=args.get('character_id'), campaign_id=campaign.id).first()
        else:
            character = _current_character(campaign, current_user)
        character_sheets = [{
            'user_id': character.user_id,
            'username': current_user.username if current_user and character.user_id == current_user.id else None,
            'character': character_full_dict(character),
        }] if character else []

    result = get_character_sheet_answer(question, scope, character_sheets, audit_context=audit_context)
    return {'scope': scope, **result}


def _tool_propose_sheet_update(campaign, current_user, args, session, audit_context=None):
    character_id = args.get('character_id')
    reason = clean_text(args.get('reason', ''), 500)
    raw_changes = args.get('changes', [])

    if not character_id or not reason or not raw_changes:
        return {'error': 'character_id, reason, and changes are required.'}

    character = Character.query.filter_by(id=character_id, campaign_id=campaign.id).first()
    if not character:
        return {'error': 'Character not found in this campaign.'}

    computed_changes = []
    for change in raw_changes:
        result = _compute_change(character, change)
        if result:
            computed_changes.append(result)

    if not computed_changes:
        return {'error': 'No valid changes could be computed from the provided data.'}

    proposal = SheetProposal(
        session_id=session.id,
        character_id=character_id,
        dm_user_id=current_user.id,
        reason=reason,
        changes=computed_changes,
        status='pending',
    )
    db.session.add(proposal)
    db.session.commit()

    return {
        'proposal_id': proposal.id,
        'character_id': character_id,
        'reason': reason,
        'changes': computed_changes,
        'status': 'pending',
    }


def _tool_get_current_scene(campaign, _current_user, args):
    include_private = args.get('include_private', True) is not False
    world, _graph, world_state, _private = _world_json(campaign)
    active_npc_ids = []
    current_scene = {}
    if isinstance(world_state, dict):
        current_scene = world_state.get('current_scene', {}) or {}
        active_npc_ids = current_scene.get('active_npc_ids', []) if isinstance(current_scene, dict) else []
    npcs = NPCActor.query.filter_by(campaign_id=campaign.id).order_by(NPCActor.id.asc()).all()
    if active_npc_ids:
        active_npcs = [
            npc for npc in npcs
            if npc.actor_id in active_npc_ids or str(npc.id) in {str(item) for item in active_npc_ids}
        ]
    else:
        active_npcs = npcs[:4]
    events = WorldEvent.query.filter_by(campaign_id=campaign.id).order_by(WorldEvent.created_at.desc()).limit(8).all()
    return {
        'has_world': bool(world),
        'current_scene': current_scene,
        'active_clocks': [clock.to_dict(include_private=include_private) for clock in _active_clocks(campaign)],
        'active_npcs': [npc.to_dict(include_private=include_private) for npc in active_npcs],
        'recent_events': [event.to_dict(include_private=include_private) for event in reversed(events)],
    }


def _match_score(query_terms, value):
    text = json.dumps(value, ensure_ascii=False).lower()
    return sum(1 for term in query_terms if term and term in text)


def _candidate_key(item):
    return (str(item.get('kind')), str(item.get('item_id')))


def _tool_search_campaign_memory(campaign, _current_user, args, audit_context=None):
    query = clean_text(args.get('query'), 240).lower()
    terms = [term for term in query.replace('_', ' ').split() if len(term) > 2]
    limit = min(max(int(args.get('limit') or 8), 1), 20)
    _world, graph, world_state, dm_private = _world_json(campaign)
    candidates = []

    for kind in ('entities', 'relations', 'facts'):
        for item in graph.get(kind, []) if isinstance(graph, dict) else []:
            candidates.append({'kind': kind[:-1], 'item_id': item.get('id'), 'value': item})
    for npc in NPCActor.query.filter_by(campaign_id=campaign.id).all():
        candidates.append({'kind': 'npc_actor', 'item_id': npc.actor_id, 'value': npc.to_dict(include_private=True)})
    for clock in CampaignClock.query.filter_by(campaign_id=campaign.id).all():
        candidates.append({'kind': 'clock', 'item_id': clock.clock_id, 'value': clock.to_dict(include_private=True)})
    for event in WorldEvent.query.filter_by(campaign_id=campaign.id).order_by(WorldEvent.created_at.desc()).limit(30).all():
        candidates.append({'kind': 'world_event', 'item_id': str(event.id), 'value': event.to_dict(include_private=True)})
    candidates.append({'kind': 'world_state', 'item_id': 'current', 'value': world_state})
    candidates.append({'kind': 'dm_private', 'item_id': 'current', 'value': dm_private})
    candidates.append({'kind': 'planning_summary', 'item_id': 'current', 'value': summary_dict_for_read(campaign.id, include_private=True)})

    semantic = search_memory_embeddings(campaign, query, candidates, limit, audit_context=audit_context)
    semantic_scores = semantic.get('scores') if semantic.get('ok') else {}
    weight = search_weight()

    scored = []
    for item in candidates:
        keyword_score = _match_score(terms, item['value'])
        semantic_score = semantic_scores.get(_candidate_key(item), 0.0)
        score = keyword_score + (semantic_score * weight)
        if score:
            scored.append({
                **item,
                'score': score,
                'keyword_score': keyword_score,
                'embedding_score': semantic_score,
            })
    scored.sort(key=lambda item: item['score'], reverse=True)
    return {'query': query, 'matches': scored[:limit]}


def _record_event(campaign, event_type, summary, payload=None, visibility='dm_private'):
    event = WorldEvent(
        campaign_id=campaign.id,
        event_type=clean_id(event_type, 'world_event')[:80],
        summary=clean_text(summary, 1200) or 'World event recorded.',
        payload=json_dumps(payload or {}),
        visibility=visibility if visibility in VALID_VISIBILITIES else 'dm_private',
    )
    db.session.add(event)
    db.session.flush()
    upsert_memory_embedding(campaign, 'world_event', str(event.id), event.to_dict(include_private=True))
    return event


def _tool_record_world_event(campaign, _current_user, args):
    event = _record_event(
        campaign,
        args.get('event_type'),
        args.get('summary'),
        payload=args.get('payload') if isinstance(args.get('payload'), dict) else {},
        visibility=args.get('visibility') or 'dm_private',
    )
    return {'event': event.to_dict(include_private=True), 'affected_ids': {'world_event_ids': [event.id]}}


def _tool_update_current_scene(campaign, _current_user, args):
    world, _graph, world_state, _private = _world_json(campaign)
    if not world:
        return {'error': 'No world package exists.'}
    scene_patch = args.get('scene_patch') if isinstance(args.get('scene_patch'), dict) else {}
    current_scene = world_state.get('current_scene', {}) if isinstance(world_state, dict) else {}
    current_scene.update(scene_patch)
    world_state['current_scene'] = current_scene
    world.world_state = json_dumps(world_state)
    world.updated_at = datetime.utcnow()
    upsert_memory_embedding(campaign, 'world_state', 'current', world_state)
    event = _record_event(
        campaign,
        'scene_updated',
        args.get('reason') or 'Current scene updated.',
        {'scene_patch': scene_patch, 'current_scene': current_scene},
    )
    return {'current_scene': current_scene, 'affected_ids': {'world_id': world.id, 'world_event_ids': [event.id]}}


def _tool_advance_clock(campaign, _current_user, args):
    clock_id = clean_id(args.get('clock_id'), '')
    clock = CampaignClock.query.filter_by(campaign_id=campaign.id, clock_id=clock_id).first()
    if not clock:
        return {'error': f'Clock not found: {clock_id}'}
    try:
        delta = int(args.get('delta') or 0)
    except (TypeError, ValueError):
        delta = 0
    old_filled = clock.filled or 0
    clock.filled = min(max(old_filled + delta, 0), clock.segments or 4)
    if args.get('status'):
        clock.status = clean_text(args.get('status'), 30)
    elif clock.filled >= (clock.segments or 4):
        clock.status = 'completed'
    clock.updated_at = datetime.utcnow()
    upsert_memory_embedding(campaign, 'clock', clock.clock_id, clock.to_dict(include_private=True))
    event = _record_event(
        campaign,
        'clock_advanced',
        args.get('reason') or f'{clock.name} changed by {delta}.',
        {'clock_id': clock.clock_id, 'from': old_filled, 'to': clock.filled, 'delta': delta, 'status': clock.status},
        visibility=clock.visibility or 'dm_private',
    )
    return {'clock': clock.to_dict(include_private=True), 'affected_ids': {'clock_ids': [clock.id], 'world_event_ids': [event.id]}}


def _tool_reveal_fact(campaign, _current_user, args):
    world, graph, _world_state, _private = _world_json(campaign)
    if not world:
        return {'error': 'No world package exists.'}
    item_type = args.get('item_type')
    plural = {'entity': 'entities', 'relation': 'relations', 'fact': 'facts'}.get(item_type)
    visibility = args.get('visibility') if args.get('visibility') in VALID_VISIBILITIES else 'party_known'
    item_id = clean_id(args.get('item_id'), '')
    if not plural:
        return {'error': 'Invalid item_type.'}
    target = None
    for item in graph.get(plural, []):
        if item.get('id') == item_id:
            target = item
            break
    if not target:
        return {'error': f'{item_type} not found: {item_id}'}
    old_visibility = target.get('visibility')
    target['visibility'] = visibility
    world.knowledge_graph = json_dumps(graph)
    world.updated_at = datetime.utcnow()
    upsert_memory_embedding(campaign, item_type, item_id, target)
    event = _record_event(
        campaign,
        'fact_revealed',
        args.get('reason') or f'{item_type} {item_id} visibility changed to {visibility}.',
        {'item_type': item_type, 'item_id': item_id, 'from': old_visibility, 'to': visibility},
        visibility=visibility,
    )
    return {'item': target, 'affected_ids': {'world_id': world.id, 'world_event_ids': [event.id]}}


def _tool_generate_loot_box(campaign, current_user, args, session=None, audit_context=None):
    from models import SessionMessage

    name = clean_text(args.get('name', ''), 200)
    description = clean_text(args.get('description', ''), 1000)
    if not name:
        return {'error': 'A loot box name is required.'}

    try:
        loot_box = do_generate_loot_box(campaign, session, current_user, name, description)
    except Exception as err:
        return {'error': f'Failed to generate loot box: {repr(err)}'}

    if session:
        announcement = (
            f'A loot box has appeared: **{loot_box.name}**. '
            f'The party can inspect and open it from the campaign stash.'
        )
        announcement_msg = SessionMessage(
            session_id=session.id,
            role='dm',
            content=announcement,
        )
        db.session.add(announcement_msg)
        db.session.flush()

    event = _record_event(
        campaign,
        'loot_box_generated',
        f'Loot box "{loot_box.name}" generated.',
        {'loot_box_id': loot_box.id, 'name': loot_box.name, 'description': loot_box.description},
        visibility='party_known',
    )

    return {
        'loot_box': {
            'id': loot_box.id,
            'name': loot_box.name,
            'description': loot_box.description,
            'status': loot_box.status,
        },
        'affected_ids': {'loot_box_ids': [loot_box.id], 'world_event_ids': [event.id]},
    }


TOOL_HANDLERS = {
    'ask_character_sheet': _tool_ask_character_sheet,
    'get_current_scene': _tool_get_current_scene,
    'search_campaign_memory': _tool_search_campaign_memory,
    'record_world_event': _tool_record_world_event,
    'update_current_scene': _tool_update_current_scene,
    'advance_clock': _tool_advance_clock,
    'reveal_fact': _tool_reveal_fact,
    'propose_sheet_update': _tool_propose_sheet_update,
    'generate_loot_box': _tool_generate_loot_box,
}


def execute_dm_tool(campaign, session, current_user, name, args, audit_context=None):
    audit_context = audit_context or {}
    args = args if isinstance(args, dict) else {}
    handler = TOOL_HANDLERS.get(name)
    if not handler:
        result = {'error': f'Unknown DM tool: {name}'}
        mutated = False
    else:
        before_new = len(db.session.new)
        result = (
            handler(campaign, current_user, args, session, audit_context)
            if name in {'propose_sheet_update', 'generate_loot_box'}
            else handler(campaign, current_user, args, audit_context)
            if name in {'ask_character_sheet', 'search_campaign_memory'}
            else handler(campaign, current_user, args)
        )
        mutated = name in {'record_world_event', 'update_current_scene', 'advance_clock', 'reveal_fact', 'generate_loot_box'}
        mutated = mutated or len(db.session.new) > before_new
    log_audit_event(
        campaign.id,
        'dm_tool_execution',
        f'DM tool executed: {name}',
        {
            'session_id': session.id if session else None,
            'tool_name': name,
            'arguments': args,
            'result': result,
            'mutated': mutated,
            'affected_ids': result.get('affected_ids') if isinstance(result, dict) else {},
        },
        source='dm_tools',
        actor='session_dm',
        trace_id=audit_context.get('trace_id'),
        parent_trace_id=audit_context.get('parent_trace_id'),
        trace_label=audit_context.get('trace_label'),
        audit_role='tools',
        commit=False,
    )
    return result


def _upsert_by_id(items, item, fallback_id):
    item = item if isinstance(item, dict) else {}
    item_id = clean_id(item.get('id'), fallback_id)
    clean_item = dict(item)
    clean_item['id'] = item_id
    for index, existing in enumerate(items):
        if existing.get('id') == item_id:
            merged = dict(existing)
            merged.update({key: value for key, value in clean_item.items() if value not in (None, '', [])})
            items[index] = merged
            return merged, 'updated'
    items.append(clean_item)
    return clean_item, 'created'


def _upsert_graph_item(campaign, item_type, items, item, fallback_id, audit_context=None):
    item = item if isinstance(item, dict) else {}
    requested_id = clean_id(item.get('id'), fallback_id)
    exact_exists = any(existing.get('id') == requested_id for existing in items)
    dedupe = {'duplicate_id': None}
    if not exact_exists:
        dedupe = find_duplicate_graph_item(campaign, item_type, item, audit_context=audit_context)
        duplicate_id = dedupe.get('duplicate_id')
        if duplicate_id and any(existing.get('id') == duplicate_id for existing in items):
            item = dict(item)
            item['id'] = duplicate_id

    merged, action = _upsert_by_id(items, item, fallback_id)
    upsert_memory_embedding(
        campaign,
        item_type,
        merged.get('id'),
        merged,
        audit_context=audit_context,
        embedding_result=dedupe.get('embedding'),
    )
    return merged, action, {
        'requested_id': requested_id,
        'dedupe_match_id': dedupe.get('duplicate_id'),
        'dedupe_similarity': (
            round(dedupe.get('best', {}).get('similarity'), 4)
            if isinstance(dedupe.get('best'), dict) and dedupe.get('best', {}).get('similarity') is not None
            else None
        ),
    }


def _apply_entity_id_remaps(kind, item, id_remaps):
    if not id_remaps or not isinstance(item, dict):
        return item
    item = dict(item)
    if kind == 'relation':
        for field in ('source_id', 'target_id'):
            if item.get(field) in id_remaps:
                item[field] = id_remaps[item[field]]
    elif kind == 'fact':
        entity_ids = item.get('entity_ids', [])
        if isinstance(entity_ids, list):
            item['entity_ids'] = [id_remaps.get(entity_id, entity_id) for entity_id in entity_ids]
    return item


def _create_clock_from_patch(campaign, patch):
    clock_id = clean_id(patch.get('id') or patch.get('clock_id'), f'clock_{datetime.utcnow().strftime("%Y%m%d%H%M%S")}')
    existing = CampaignClock.query.filter_by(campaign_id=campaign.id, clock_id=clock_id).first()
    try:
        segments = min(max(int(patch.get('segments') or 4), 2), 12)
    except (TypeError, ValueError):
        segments = 4
    try:
        filled = min(max(int(patch.get('filled') or 0), 0), segments)
    except (TypeError, ValueError):
        filled = 0
    clock = existing or CampaignClock(campaign_id=campaign.id, clock_id=clock_id)
    clock.name = clean_text(patch.get('name'), 200) or clock_id.replace('_', ' ').title()
    clock.segments = segments
    clock.filled = filled
    clock.pressure_type = clean_text(patch.get('pressure_type'), 80) or 'story'
    clock.visibility = patch.get('visibility') if patch.get('visibility') in VALID_VISIBILITIES else 'dm_private'
    clock.summary = clean_text(patch.get('summary'), 420)
    clock.trigger = clean_text(patch.get('trigger'), 420)
    clock.on_complete = clean_text(patch.get('on_complete'), 520)
    clock.status = clean_text(patch.get('status'), 30) or 'active'
    clock.updated_at = datetime.utcnow()
    if not existing:
        db.session.add(clock)
    db.session.flush()
    upsert_memory_embedding(campaign, 'clock', clock.clock_id, clock.to_dict(include_private=True))
    event = _record_event(
        campaign,
        'clock_created' if not existing else 'clock_updated',
        patch.get('reason') or f'Clock {clock.name} {"created" if not existing else "updated"}.',
        {'clock': clock.to_dict(include_private=True)},
        visibility=clock.visibility,
    )
    return {'clock': clock.to_dict(include_private=True), 'event_id': event.id, 'action': 'created' if not existing else 'updated'}


def _retire_clock_from_patch(campaign, patch):
    clock_id = clean_id(patch.get('clock_id') or patch.get('id'), '')
    clock = CampaignClock.query.filter_by(campaign_id=campaign.id, clock_id=clock_id).first()
    if not clock:
        return {'error': f'Clock not found: {clock_id}'}
    clock.status = clean_text(patch.get('status'), 30) or 'resolved'
    clock.updated_at = datetime.utcnow()
    upsert_memory_embedding(campaign, 'clock', clock.clock_id, clock.to_dict(include_private=True))
    event = _record_event(
        campaign,
        'clock_retired',
        patch.get('reason') or f'Clock {clock.name} retired as {clock.status}.',
        {'clock_id': clock.clock_id, 'status': clock.status},
        visibility=clock.visibility or 'dm_private',
    )
    return {'clock': clock.to_dict(include_private=True), 'event_id': event.id, 'action': 'retired'}


def _update_npc_actor(campaign, patch):
    actor_id = clean_id(patch.get('id') or patch.get('actor_id'), '')
    if not actor_id:
        return {'error': 'NPC actor id is required.'}
    actor = NPCActor.query.filter_by(campaign_id=campaign.id, actor_id=actor_id).first()
    created = actor is None
    if not actor:
        actor = NPCActor(campaign_id=campaign.id, actor_id=actor_id, name=actor_id.replace('_', ' ').title(), dossier='{}')
        db.session.add(actor)
    dossier = json_loads(actor.dossier, {})
    dossier.update({key: value for key, value in patch.items() if value not in (None, '', [])})
    dossier['id'] = actor_id
    actor.name = clean_text(patch.get('name'), 200) or actor.name
    actor.role = clean_text(patch.get('role'), 200) or actor.role
    actor.public_summary = clean_text(patch.get('public_summary'), 420) or actor.public_summary
    actor.dossier = json_dumps(dossier)
    actor.updated_at = datetime.utcnow()
    db.session.flush()
    upsert_memory_embedding(campaign, 'npc_actor', actor.actor_id, actor.to_dict(include_private=True))
    event = _record_event(
        campaign,
        'npc_actor_upserted',
        patch.get('reason') or f'NPC actor {actor.name} {"created" if created else "updated"}.',
        {'npc_actor': actor.to_dict(include_private=True)},
    )
    return {'npc_actor': actor.to_dict(include_private=True), 'event_id': event.id, 'action': 'created' if created else 'updated'}


def apply_memory_patch(campaign, session, patch, audit_context=None):
    audit_context = audit_context or {}
    patch = patch if isinstance(patch, dict) else {}
    world, graph, world_state, _private = _world_json(campaign)
    result = {
        'graph_changes': [],
        'clock_changes': [],
        'npc_changes': [],
        'world_event_ids': [],
        'running_summary_updated': False,
    }

    if world:
        entity_id_remaps = {}
        for kind, key, fallback in (
            ('entity', 'upsert_graph_entities', 'entity'),
            ('relation', 'upsert_graph_relations', 'relation'),
            ('fact', 'upsert_graph_facts', 'fact'),
        ):
            plural = {'entity': 'entities', 'relation': 'relations', 'fact': 'facts'}[kind]
            graph.setdefault(plural, [])
            items = patch.get(key) if isinstance(patch.get(key), list) else []
            for index, item in enumerate(items):
                item = _apply_entity_id_remaps(kind, item, entity_id_remaps)
                item, action, embedding_dedupe = _upsert_graph_item(
                    campaign,
                    kind,
                    graph[plural],
                    item,
                    f'{fallback}_{index + 1}',
                    audit_context=audit_context,
                )
                result['graph_changes'].append({
                    'kind': kind,
                    'action': action,
                    'id': item.get('id'),
                    'embedding_dedupe': embedding_dedupe,
                })
                if (
                    kind == 'entity'
                    and embedding_dedupe.get('dedupe_match_id')
                    and embedding_dedupe.get('requested_id') != item.get('id')
                ):
                    entity_id_remaps[embedding_dedupe['requested_id']] = item.get('id')

        scene_patch = patch.get('scene_patch') if isinstance(patch.get('scene_patch'), dict) else {}
        if scene_patch:
            current_scene = world_state.get('current_scene', {}) if isinstance(world_state, dict) else {}
            current_scene.update(scene_patch)
            world_state['current_scene'] = current_scene
            event = _record_event(campaign, 'scene_updated', patch.get('scene_reason') or 'Scene updated by memory writer.', {'scene_patch': scene_patch})
            result['world_event_ids'].append(event.id)

        world.knowledge_graph = json_dumps(graph)
        world.world_state = json_dumps(world_state)
        world.updated_at = datetime.utcnow()
        if scene_patch:
            upsert_memory_embedding(campaign, 'world_state', 'current', world_state, audit_context=audit_context)

    for item in patch.get('create_clocks', []) if isinstance(patch.get('create_clocks'), list) else []:
        change = _create_clock_from_patch(campaign, item)
        result['clock_changes'].append(change)
        if change.get('event_id'):
            result['world_event_ids'].append(change['event_id'])
    for item in patch.get('retire_clocks', []) if isinstance(patch.get('retire_clocks'), list) else []:
        change = _retire_clock_from_patch(campaign, item)
        result['clock_changes'].append(change)
        if change.get('event_id'):
            result['world_event_ids'].append(change['event_id'])
    for item in patch.get('update_npc_actors', []) if isinstance(patch.get('update_npc_actors'), list) else []:
        change = _update_npc_actor(campaign, item)
        result['npc_changes'].append(change)
        if change.get('event_id'):
            result['world_event_ids'].append(change['event_id'])
    for event_patch in patch.get('record_events', []) if isinstance(patch.get('record_events'), list) else []:
        event = _record_event(
            campaign,
            event_patch.get('event_type') or 'session_memory',
            event_patch.get('summary') or 'Session memory updated.',
            event_patch.get('payload') if isinstance(event_patch.get('payload'), dict) else {},
            event_patch.get('visibility') or 'dm_private',
        )
        result['world_event_ids'].append(event.id)

    summary = clean_text(patch.get('running_summary'), 4000)
    if summary:
        session.running_summary = summary
        result['running_summary_updated'] = True

    log_audit_event(
        campaign.id,
        'memory_patch_applied',
        'Applied post-turn session memory patch.',
        {'session_id': session.id, 'patch': patch, 'result': result},
        source='dm_tools.memory',
        actor='session_memory_writer',
        trace_id=audit_context.get('trace_id'),
        parent_trace_id=audit_context.get('parent_trace_id'),
        trace_label=audit_context.get('trace_label'),
        audit_role='tools',
        commit=False,
    )
    return result

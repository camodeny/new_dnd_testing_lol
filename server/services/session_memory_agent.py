import json
import re

from models import Campaign, CampaignClock, CampaignSession, Character, NPCActor, SessionMessage, User, WorldEvent, db
from services.dm_tools import _tool_search_campaign_memory
from services.world_service import clean_id, clean_text, get_campaign_world, json_loads


SESSION_MEMORY_TOOL_DEFINITIONS = [
    {
        'type': 'function',
        'function': {
            'name': 'get_running_summary',
            'description': 'Read the current persisted running summary for this session.',
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_transcript_window',
            'description': 'Read a compact recent transcript window for this session.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'limit': {'type': 'integer', 'minimum': 1, 'maximum': 30, 'default': 10},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'search_campaign_memory',
            'description': 'Search campaign memory across graph items, NPCs, clocks, events, and planning summary.',
            'parameters': {
                'type': 'object',
                'required': ['query'],
                'properties': {
                    'query': {'type': 'string'},
                    'limit': {'type': 'integer', 'minimum': 1, 'maximum': 12, 'default': 6},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_world_state',
            'description': 'Read persisted world state, knowledge graph, and DM-private memory for this campaign.',
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_clocks',
            'description': 'Read all campaign clocks, including hidden fields.',
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_npcs',
            'description': 'Read NPC actor records, including private dossier data.',
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_recent_world_events',
            'description': 'Read compact recent world events for this campaign.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20, 'default': 8},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_scene_candidates',
            'description': 'Resolve current or mentioned scene/location references to canonical location ids.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'query': {'type': 'string'},
                    'limit': {'type': 'integer', 'minimum': 1, 'maximum': 10, 'default': 5},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_entity_candidates',
            'description': 'Resolve named entities to canonical ids before writing facts.',
            'parameters': {
                'type': 'object',
                'required': ['query'],
                'properties': {
                    'query': {'type': 'string'},
                    'entity_type': {'type': 'string'},
                    'limit': {'type': 'integer', 'minimum': 1, 'maximum': 12, 'default': 6},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_fact_candidates',
            'description': 'Find existing fact candidates so the memory writer can reuse canonical fact ids instead of inventing siblings.',
            'parameters': {
                'type': 'object',
                'required': ['query'],
                'properties': {
                    'query': {'type': 'string'},
                    'limit': {'type': 'integer', 'minimum': 1, 'maximum': 12, 'default': 6},
                },
            },
        },
    },
]


VALID_MEMORY_VISIBILITIES = {'public', 'party_known', 'dm_private'}
VALID_MEMORY_CERTAINTIES = {'confirmed', 'suspected', 'inferred', 'false', 'retconned'}


def _safe_int(value, default, minimum=0, maximum=None):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed < minimum:
        return minimum
    if maximum is not None and parsed > maximum:
        return maximum
    return parsed


def _limit_arg(args, default, maximum):
    args = args if isinstance(args, dict) else {}
    return _safe_int(args.get('limit'), default, minimum=1, maximum=maximum)


def _memory_context_campaign(memory_context):
    campaign_id = _safe_int((memory_context or {}).get('campaign_id'), 0, minimum=0)
    return db.session.get(Campaign, campaign_id) if campaign_id else None


def _memory_context_session(memory_context):
    session_id = _safe_int((memory_context or {}).get('session_id'), 0, minimum=0)
    return db.session.get(CampaignSession, session_id) if session_id else None


def _memory_context_user(memory_context):
    current_user = (memory_context or {}).get('current_user') if isinstance((memory_context or {}).get('current_user'), dict) else {}
    user_id = _safe_int(current_user.get('id'), 0, minimum=0)
    return db.session.get(User, user_id) if user_id else None


def _world_payload(campaign):
    world = get_campaign_world(campaign.id) if campaign else None
    if not world:
        return {
            'has_world': False,
            'public_intro': {},
            'knowledge_graph': {'entities': [], 'relations': [], 'facts': []},
            'world_state': {},
            'dm_private': {},
        }
    return {
        'has_world': True,
        'public_intro': json_loads(world.public_intro, {}),
        'knowledge_graph': json_loads(world.knowledge_graph, {'entities': [], 'relations': [], 'facts': []}),
        'world_state': json_loads(world.world_state, {}),
        'dm_private': json_loads(world.dm_private, {}),
    }


def _match_score(query_terms, value):
    text = json.dumps(value, ensure_ascii=False).lower()
    return sum(1 for term in query_terms if term and term in text)


def _query_terms(query):
    text = clean_text(query, 240).lower()
    if not text:
        return []
    terms = []
    for term in re.findall(r"[a-z0-9']+", text.replace('_', ' ')):
        if len(term) < 3:
            continue
        terms.append(term)
        if len(term) > 4 and term.endswith('s'):
            terms.append(term[:-1])
    return sorted(set(terms))


def _location_candidates(campaign, query_terms, limit):
    world_payload = _world_payload(campaign)
    graph = world_payload.get('knowledge_graph') if isinstance(world_payload.get('knowledge_graph'), dict) else {}
    current_scene = world_payload.get('world_state', {}).get('current_scene', {}) if isinstance(world_payload.get('world_state'), dict) else {}
    candidates = []
    if isinstance(current_scene, dict):
        candidates.append({
            'source': 'current_scene',
            'location_id': clean_id(current_scene.get('location_id'), ''),
            'location_name': clean_text(current_scene.get('location_name'), 160) or None,
            'score': _match_score(query_terms, current_scene) if query_terms else 1,
        })
    for entity in graph.get('entities', []) if isinstance(graph, dict) else []:
        if not isinstance(entity, dict):
            continue
        if clean_text(entity.get('type'), 40).lower() != 'location':
            continue
        candidate = {
            'source': 'graph_entity',
            'location_id': clean_id(entity.get('id'), ''),
            'location_name': clean_text(entity.get('name'), 160) or None,
            'summary': clean_text(entity.get('summary'), 220) or None,
            'visibility': clean_text(entity.get('visibility'), 40) or None,
        }
        score = _match_score(query_terms, candidate) if query_terms else 1
        if score:
            candidate['score'] = score
            candidates.append(candidate)
    deduped = []
    seen = set()
    for item in sorted(candidates, key=lambda candidate: (candidate.get('score', 0), candidate.get('source') == 'current_scene'), reverse=True):
        key = item.get('location_id') or item.get('location_name')
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(item)
        if len(deduped) >= limit:
            break
    return {
        'current_scene': current_scene if isinstance(current_scene, dict) else {},
        'candidates': deduped,
    }


def _entity_candidates(campaign, query_terms, entity_type, limit):
    world_payload = _world_payload(campaign)
    graph = world_payload.get('knowledge_graph') if isinstance(world_payload.get('knowledge_graph'), dict) else {}
    candidates = []
    normalized_type = clean_text(entity_type, 40).lower() if entity_type else ''
    for entity in graph.get('entities', []) if isinstance(graph, dict) else []:
        if not isinstance(entity, dict):
            continue
        item_type = clean_text(entity.get('type'), 40).lower()
        if normalized_type and item_type != normalized_type:
            continue
        value = {
            'entity_id': clean_id(entity.get('id'), ''),
            'entity_type': item_type or None,
            'name': clean_text(entity.get('name'), 180) or None,
            'summary': clean_text(entity.get('summary'), 220) or None,
            'visibility': clean_text(entity.get('visibility'), 40) or None,
        }
        score = _match_score(query_terms, value)
        if score:
            value['score'] = score
            candidates.append(value)
    if not normalized_type or normalized_type in {'npc', 'person'}:
        for npc in NPCActor.query.filter_by(campaign_id=campaign.id).all():
            value = {
                'entity_id': clean_id(npc.actor_id, ''),
                'entity_type': 'npc',
                'name': clean_text(npc.name, 180) or None,
                'summary': clean_text(npc.public_summary, 220) or None,
                'role': clean_text(npc.role, 120) or None,
            }
            score = _match_score(query_terms, value)
            if score:
                value['score'] = score
                candidates.append(value)
    if not normalized_type or normalized_type in {'character', 'pc', 'person'}:
        for character in Character.query.filter_by(campaign_id=campaign.id).all():
            value = {
                'entity_id': clean_id(character.name, ''),
                'entity_type': 'character',
                'name': clean_text(character.name, 180) or None,
                'summary': clean_text(character.background, 220) or None,
            }
            score = _match_score(query_terms, value)
            if score:
                value['score'] = score
                candidates.append(value)
    deduped = []
    seen = set()
    for item in sorted(candidates, key=lambda candidate: candidate.get('score', 0), reverse=True):
        key = item.get('entity_id')
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(item)
        if len(deduped) >= limit:
            break
    return {
        'candidates': deduped,
    }


def _fact_candidates(campaign, query_terms, limit):
    world_payload = _world_payload(campaign)
    graph = world_payload.get('knowledge_graph') if isinstance(world_payload.get('knowledge_graph'), dict) else {}
    candidates = []
    for fact in graph.get('facts', []) if isinstance(graph, dict) else []:
        if not isinstance(fact, dict):
            continue
        value = {
            'fact_id': clean_id(fact.get('id'), ''),
            'text': clean_text(fact.get('text'), 320) or None,
            'entity_ids': fact.get('entity_ids') if isinstance(fact.get('entity_ids'), list) else [],
            'visibility': clean_text(fact.get('visibility'), 40) or None,
        }
        score = _match_score(query_terms, value)
        if score:
            value['score'] = score
            candidates.append(value)
    candidates.sort(key=lambda item: item.get('score', 0), reverse=True)
    return {'candidates': candidates[:limit]}


def execute_memory_tool(memory_context, tool_name, args=None):
    args = args if isinstance(args, dict) else {}
    campaign = _memory_context_campaign(memory_context)
    session = _memory_context_session(memory_context)
    current_user = _memory_context_user(memory_context)
    if tool_name == 'get_running_summary':
        return {'running_summary': getattr(session, 'running_summary', None)}
    if tool_name == 'get_transcript_window':
        if session is None:
            return {'messages': []}
        rows = (
            SessionMessage.query
            .filter_by(session_id=session.id)
            .order_by(SessionMessage.id.desc())
            .limit(_limit_arg(args, 10, 30))
            .all()
        )
        return {'messages': [row.to_dict() for row in reversed(rows)]}
    if campaign is None:
        return {'error': 'Memory context is missing a campaign.'}
    if tool_name == 'search_campaign_memory':
        if current_user is None:
            return {'error': 'Memory context is missing a user.'}
        return _tool_search_campaign_memory(
            campaign,
            current_user,
            {
                'query': str(args.get('query') or '').strip(),
                'limit': _limit_arg(args, 6, 12),
            },
        )
    if tool_name == 'get_world_state':
        return _world_payload(campaign)
    if tool_name == 'get_clocks':
        rows = CampaignClock.query.filter_by(campaign_id=campaign.id).order_by(CampaignClock.id.asc()).all()
        return {'clocks': [row.to_dict(include_private=True) for row in rows]}
    if tool_name == 'get_npcs':
        rows = NPCActor.query.filter_by(campaign_id=campaign.id).order_by(NPCActor.id.asc()).all()
        return {'npcs': [row.to_dict(include_private=True) for row in rows]}
    if tool_name == 'get_recent_world_events':
        rows = (
            WorldEvent.query
            .filter_by(campaign_id=campaign.id)
            .order_by(WorldEvent.id.desc())
            .limit(_limit_arg(args, 8, 20))
            .all()
        )
        return {
            'events': [
                {
                    'id': row.id,
                    'event_type': row.event_type,
                    'summary': row.summary,
                    'visibility': row.visibility,
                    'created_at': row.created_at.isoformat() if row.created_at else None,
                }
                for row in reversed(rows)
            ]
        }
    if tool_name == 'get_scene_candidates':
        return _location_candidates(campaign, _query_terms(args.get('query') or ''), _limit_arg(args, 5, 10))
    if tool_name == 'get_entity_candidates':
        return _entity_candidates(
            campaign,
            _query_terms(args.get('query') or ''),
            args.get('entity_type'),
            _limit_arg(args, 6, 12),
        )
    if tool_name == 'get_fact_candidates':
        return _fact_candidates(campaign, _query_terms(args.get('query') or ''), _limit_arg(args, 6, 12))
    return {'error': f'Unknown memory tool: {tool_name}'}


def _known_ids(campaign):
    world_payload = _world_payload(campaign)
    graph = world_payload.get('knowledge_graph') if isinstance(world_payload.get('knowledge_graph'), dict) else {}
    entity_ids = {
        clean_id(entity.get('id'), '')
        for entity in graph.get('entities', [])
        if isinstance(entity, dict) and clean_id(entity.get('id'), '')
    }
    location_ids = {
        clean_id(entity.get('id'), '')
        for entity in graph.get('entities', [])
        if isinstance(entity, dict)
        and clean_id(entity.get('id'), '')
        and clean_text(entity.get('type'), 40).lower() == 'location'
    }
    fact_ids = {
        clean_id(fact.get('id'), '')
        for fact in graph.get('facts', [])
        if isinstance(fact, dict) and clean_id(fact.get('id'), '')
    }
    npc_ids = {
        clean_id(npc.actor_id, '')
        for npc in NPCActor.query.filter_by(campaign_id=campaign.id).all()
        if clean_id(npc.actor_id, '')
    }
    character_ids = {
        clean_id(character.name, '')
        for character in Character.query.filter_by(campaign_id=campaign.id).all()
        if clean_id(character.name, '')
    }
    return {
        'entity_ids': entity_ids | npc_ids | character_ids,
        'location_ids': location_ids,
        'npc_ids': npc_ids,
        'fact_ids': fact_ids,
        'location_names': {
            clean_text(entity.get('name'), 160).lower(): clean_id(entity.get('id'), '')
            for entity in graph.get('entities', [])
            if isinstance(entity, dict)
            and clean_text(entity.get('type'), 40).lower() == 'location'
            and clean_text(entity.get('name'), 160)
            and clean_id(entity.get('id'), '')
        },
    }


def _normalize_visibility(source_surface, intended_visibility):
    source_surface = clean_text(source_surface, 40).lower()
    intended = clean_text(intended_visibility, 40).lower()
    if source_surface == 'visible_transcript':
        return 'public' if intended == 'public' else 'party_known'
    if intended == 'public':
        return 'public'
    return 'dm_private'


def _normalize_certainty(value):
    certainty = clean_text(value, 40).lower()
    return certainty if certainty in VALID_MEMORY_CERTAINTIES else 'confirmed'


def _normalize_importance(value):
    return _safe_int(value, 3, minimum=1, maximum=5)


def compile_staged_memory_patch(memory_context, extracted, resolved):
    campaign = _memory_context_campaign(memory_context)
    if campaign is None:
        prior_anchors = memory_context.get('prior_memory_anchors') if isinstance(memory_context.get('prior_memory_anchors'), dict) else {}
        extracted_anchors = (extracted or {}).get('memory_anchors') if isinstance(extracted or {}, dict) else None
        anchors = extracted_anchors or prior_anchors
        compiled_anchors = {
            "current_goal": anchors.get("current_goal"),
            "current_scene": anchors.get("current_scene"),
            "open_clues": anchors.get("open_clues") if isinstance(anchors.get("open_clues"), list) else [],
            "unresolved_questions": anchors.get("unresolved_questions") if isinstance(anchors.get("unresolved_questions"), list) else [],
            "npc_observations": anchors.get("npc_observations") if isinstance(anchors.get("npc_observations"), list) else [],
            "recent_offers_promises": anchors.get("recent_offers_promises") if isinstance(anchors.get("recent_offers_promises"), list) else []
        }
        return {
            'running_summary': clean_text((extracted or {}).get('running_summary'), 4000),
            'memory_anchors': compiled_anchors,
            'scene_patch': {},
            'scene_reason': None,
            'upsert_graph_facts': [],
            'create_clocks': [],
            'retire_clocks': [],
            'update_npc_actors': [],
            'upsert_graph_entities': [],
            'upsert_graph_relations': [],
            'record_events': [],
            'unresolved_items': [{'kind': 'context', 'reason': 'missing_campaign'}],
            'compile_summary': {'accepted_fact_count': 0, 'skipped_fact_count': 0},
        }

    extracted = extracted if isinstance(extracted, dict) else {}
    resolved = resolved if isinstance(resolved, dict) else {}
    known = _known_ids(campaign)
    unresolved = list(resolved.get('unresolved_items') if isinstance(resolved.get('unresolved_items'), list) else [])

    running_summary = clean_text(
        resolved.get('running_summary') or extracted.get('running_summary'),
        4000,
    )

    prior_anchors = memory_context.get('prior_memory_anchors') if isinstance(memory_context.get('prior_memory_anchors'), dict) else {}
    resolved_anchors = resolved.get('memory_anchors') if isinstance(resolved.get('memory_anchors'), dict) else None
    extracted_anchors = extracted.get('memory_anchors') if isinstance(extracted.get('memory_anchors'), dict) else None
    anchors = resolved_anchors or extracted_anchors or prior_anchors
    compiled_anchors = {
        "current_goal": anchors.get("current_goal"),
        "current_scene": anchors.get("current_scene"),
        "open_clues": anchors.get("open_clues") if isinstance(anchors.get("open_clues"), list) else [],
        "unresolved_questions": anchors.get("unresolved_questions") if isinstance(anchors.get("unresolved_questions"), list) else [],
        "npc_observations": anchors.get("npc_observations") if isinstance(anchors.get("npc_observations"), list) else [],
        "recent_offers_promises": anchors.get("recent_offers_promises") if isinstance(anchors.get("recent_offers_promises"), list) else []
    }

    hot_context = memory_context.get('hot_context') if isinstance(memory_context.get('hot_context'), dict) else {}
    source_player_message_id = memory_context.get('latest_player_message_id') or memory_context.get('source_player_message_id') or hot_context.get('player_message_id') or hot_context.get('source_player_message_id')
    source_dm_message_id = memory_context.get('latest_dm_message_id') or memory_context.get('source_dm_message_id') or hot_context.get('dm_message_id') or hot_context.get('source_dm_message_id')

    def make_provenance(item_raw, default_tool=None):
        raw_prov = item_raw.get('provenance') if isinstance(item_raw.get('provenance'), dict) else {}
        evidence = item_raw.get('evidence')
        if not isinstance(evidence, list):
            evidence = [evidence] if evidence else []
        basis = raw_prov.get('evidence_basis') or item_raw.get('evidence_basis') or evidence
        if not isinstance(basis, list):
            basis = [basis] if basis else []
        basis = [str(b).strip() for b in basis if str(b).strip()]
        return {
            'source_player_message_id': raw_prov.get('source_player_message_id') or source_player_message_id,
            'source_dm_message_id': raw_prov.get('source_dm_message_id') or source_dm_message_id,
            'tool_name': raw_prov.get('tool_name') or default_tool,
            'tool_result_id': raw_prov.get('tool_result_id'),
            'evidence_basis': basis
        }

    scene_patch = resolved.get('scene_patch') if isinstance(resolved.get('scene_patch'), dict) else {}
    if not scene_patch:
        scene_patch = extracted.get('scene_patch') if isinstance(extracted.get('scene_patch'), dict) else {}
    compiled_scene = {}
    from services.scene_location_resolver import resolve_scene_location_patch
    current_scene = hot_context.get('current_scene') if isinstance(hot_context.get('current_scene'), dict) else {}

    scene_resolution_mode = 'inferred'
    resolved_loc = resolve_scene_location_patch(scene_patch, campaign, current_scene)
    if resolved_loc is None:
        proposed_id = clean_id(scene_patch.get('location_id'), '')
        proposed_name = clean_text(scene_patch.get('location_name'), 160)
        unresolved.append({
            'kind': 'scene_location',
            'location_id': proposed_id,
            'location_name': proposed_name,
            'reason': 'unresolved_scene_location',
            'provenance': make_provenance(scene_patch, default_tool='resolve_scene_location_patch'),
            'resolution_mode': 'unresolved'
        })
    elif resolved_loc:
        if resolved_loc.get('location_id'):
            compiled_scene['location_id'] = resolved_loc['location_id']
            compiled_scene['location_name'] = resolved_loc['location_name']
            scene_resolution_mode = 'canonical'
        else:
            # Resolved to current (direct)
            compiled_scene['location_id'] = current_scene.get('location_id')
            compiled_scene['location_name'] = current_scene.get('location_name')
            scene_resolution_mode = 'direct'

    compiled_scene['provenance'] = make_provenance(scene_patch, default_tool='resolve_scene_location_patch')
    compiled_scene['resolution_mode'] = scene_resolution_mode

    for field, limit in (('time_of_day', 80), ('immediate_tension', 420)):
        value = clean_text(scene_patch.get(field), limit)
        if value:
            compiled_scene[field] = value

    active_npc_ids = []
    for raw_id in scene_patch.get('active_npc_ids') if isinstance(scene_patch.get('active_npc_ids'), list) else []:
        actor_id = clean_id(raw_id, '')
        if actor_id and actor_id in known['npc_ids'] and actor_id not in active_npc_ids:
            active_npc_ids.append(actor_id)
        elif actor_id:
            unresolved.append({
                'kind': 'active_npc',
                'actor_id': actor_id,
                'reason': 'unknown_npc_id',
                'provenance': make_provenance(scene_patch),
                'resolution_mode': 'unresolved'
            })
    if active_npc_ids:
        compiled_scene['active_npc_ids'] = active_npc_ids
    departed_npc_ids = []
    for raw_id in scene_patch.get('departed_npc_ids') if isinstance(scene_patch.get('departed_npc_ids'), list) else []:
        actor_id = clean_id(raw_id, '')
        if actor_id and actor_id in known['npc_ids'] and actor_id not in departed_npc_ids:
            departed_npc_ids.append(actor_id)
        elif actor_id:
            unresolved.append({
                'kind': 'departed_npc',
                'actor_id': actor_id,
                'reason': 'unknown_npc_id',
                'provenance': make_provenance(scene_patch),
                'resolution_mode': 'unresolved'
            })
    if departed_npc_ids:
        compiled_scene['departed_npc_ids'] = departed_npc_ids

    accepted_facts = []
    skipped_facts = []
    raw_facts = resolved.get('upsert_graph_facts') if isinstance(resolved.get('upsert_graph_facts'), list) else []
    for index, raw_fact in enumerate(raw_facts):
        if not isinstance(raw_fact, dict):
            continue
        text = clean_text(raw_fact.get('text'), 700)
        if not text:
            continue
        raw_entity_ids = raw_fact.get('entity_ids') if isinstance(raw_fact.get('entity_ids'), list) else []
        entity_ids = []
        unknown_entity_ids = []
        for raw_entity_id in raw_entity_ids:
            entity_id = clean_id(raw_entity_id, '')
            if not entity_id:
                continue
            if entity_id in known['entity_ids'] and entity_id not in entity_ids:
                entity_ids.append(entity_id)
            else:
                unknown_entity_ids.append(entity_id)
        if raw_entity_ids and not entity_ids:
            skipped_facts.append({
                'index': index,
                'text': text,
                'reason': 'all_entity_ids_unresolved',
                'requested_entity_ids': [clean_id(item, '') for item in raw_entity_ids if clean_id(item, '')],
            })
            unresolved.append({
                'kind': 'fact',
                'text': text,
                'reason': 'all_entity_ids_unresolved',
                'requested_entity_ids': [clean_id(item, '') for item in raw_entity_ids if clean_id(item, '')],
                'provenance': make_provenance(raw_fact),
                'resolution_mode': 'unresolved'
            })
            continue
        fact_id = clean_id(raw_fact.get('id'), '')
        if fact_id and fact_id not in known['fact_ids']:
            fact_id = ''
        accepted_facts.append({
            'id': fact_id or f'fact_{index + 1}',
            'entity_ids': entity_ids,
            'text': text,
            'visibility': _normalize_visibility(raw_fact.get('source_surface'), raw_fact.get('intended_visibility')),
            'certainty': _normalize_certainty(raw_fact.get('certainty')),
            'importance': _normalize_importance(raw_fact.get('importance')),
            'expires_or_retire_condition': clean_text(raw_fact.get('expires_or_retire_condition'), 520) or None,
            'reason': clean_text(raw_fact.get('reason'), 420) or 'Resolved staged memory fact.',
            'memory_type': clean_text(raw_fact.get('memory_type'), 40).lower() or 'fact',
            'provenance': make_provenance(raw_fact, default_tool='get_entity_candidates'),
            'resolution_mode': raw_fact.get('resolution_mode') or ('canonical' if entity_ids else 'direct')
        })
        if unknown_entity_ids:
            unresolved.append({
                'kind': 'fact_entity_refs',
                'text': text,
                'reason': 'partial_entity_resolution',
                'unknown_entity_ids': unknown_entity_ids,
                'provenance': make_provenance(raw_fact),
                'resolution_mode': 'unresolved'
            })

    # Preserve any incoming create_clocks or retire_clocks if present, populating their metadata
    create_clocks = []
    for raw_clock in resolved.get('create_clocks') or []:
        if isinstance(raw_clock, dict):
            create_clocks.append({
                **raw_clock,
                'provenance': make_provenance(raw_clock, default_tool='session_memory_update_clocks'),
                'resolution_mode': raw_clock.get('resolution_mode') or 'inferred'
            })

    retire_clocks = []
    for raw_clock in resolved.get('retire_clocks') or []:
        if isinstance(raw_clock, dict):
            retire_clocks.append({
                **raw_clock,
                'provenance': make_provenance(raw_clock, default_tool='session_memory_update_clocks'),
                'resolution_mode': raw_clock.get('resolution_mode') or 'inferred'
            })

    return {
        'running_summary': running_summary,
        'memory_anchors': compiled_anchors,
        'scene_patch': compiled_scene,
        'scene_reason': clean_text(
            resolved.get('scene_reason') or extracted.get('scene_reason'),
            420,
        ) or None,
        'upsert_graph_entities': [],
        'upsert_graph_relations': [],
        'upsert_graph_facts': accepted_facts,
        'create_clocks': create_clocks,
        'retire_clocks': retire_clocks,
        'update_npc_actors': [],
        'record_events': [],
        'unresolved_items': unresolved,
        'compile_summary': {
            'accepted_fact_count': len(accepted_facts),
            'skipped_fact_count': len(skipped_facts),
            'resolved_entity_ref_count': len(resolved.get('resolved_entity_refs') if isinstance(resolved.get('resolved_entity_refs'), list) else []),
            'resolved_location_ref_count': len(resolved.get('resolved_location_refs') if isinstance(resolved.get('resolved_location_refs'), list) else []),
        },
        'evidence_basis': resolved.get('evidence_basis') if isinstance(resolved.get('evidence_basis'), list) else [],
        'resolved_entity_refs': resolved.get('resolved_entity_refs') if isinstance(resolved.get('resolved_entity_refs'), list) else [],
        'resolved_location_refs': resolved.get('resolved_location_refs') if isinstance(resolved.get('resolved_location_refs'), list) else [],
    }

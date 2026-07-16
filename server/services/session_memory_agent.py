import json
import re
import hashlib

from models import Campaign, CampaignClock, CampaignSession, CampaignWorld, Character, NPCActor, SessionMessage, User, WorldEvent, db
from services.dm_tools import _tool_search_campaign_memory
from services.memory_resolver_schemas import (
    AUTHORITY_PRECEDENCE,
    DIAGNOSTICS_TEMPLATE,
    FINAL_STATE_INVARIANTS,
    MEMORY_RUN_STATUSES,
    SOURCE_CONTRACT_COMPILED_V2,
    is_identity_worthy,
    validate_diagnostics,
)
from services.resolution_registry import (
    allocate_durable_id,
    build_canonical_resolution_registry,
    fetch_prior_resolutions,
    fetch_pending_clarifications,
    resolve_ref,
)
from services.world_service import clean_id, clean_text, get_campaign_world, json_loads


class MemoryPipelineError(Exception):
    def __init__(self, stage, code, message, cause=None, telemetry=None):
        super().__init__(message)
        self.stage = stage
        self.code = code
        self.cause = cause
        self.telemetry = telemetry or {}


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
        'npc_names': {
            clean_id(npc.actor_id, ''): npc.name or ''
            for npc in NPCActor.query.filter_by(campaign_id=campaign.id).all()
            if clean_id(npc.actor_id, '') and npc.name
        },
        'entity_names': {
            clean_id(entity.get('id'), ''): clean_text(entity.get('name'), 160) or ''
            for entity in graph.get('entities', [])
            if isinstance(entity, dict) and clean_id(entity.get('id'), '') and clean_text(entity.get('name'), 160)
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


def _validate_final_memory_state(compiled_patch, registry_map, known, campaign):
    errors = []
    all_entity_ids = set(known.get("entity_ids", set()))
    patch_entity_ids = set()
    for entity in compiled_patch.get("upsert_graph_entities", []):
        if isinstance(entity, dict) and entity.get("id"):
            patch_entity_ids.add(entity["id"])
    all_entity_ids |= patch_entity_ids

    active_cast = compiled_patch.get("scene_patch", {}).get("active_npc_ids", [])
    if isinstance(active_cast, list):
        for actor_id in active_cast:
            resolved = resolve_ref(actor_id, registry_map, known)
            in_patch = actor_id in patch_entity_ids
            in_known = actor_id in known.get("npc_ids", set()) or actor_id in known.get("entity_ids", set())
            if (not resolved or resolved not in all_entity_ids) and not in_patch and not in_known:
                errors.append(f"active_cast_id_not_found: {actor_id}")

    departed_cast = compiled_patch.get("scene_patch", {}).get("departed_npc_ids", [])
    if isinstance(departed_cast, list):
        for actor_id in departed_cast:
            resolved = resolve_ref(actor_id, registry_map, known)
            in_patch = actor_id in patch_entity_ids
            in_known = actor_id in known.get("npc_ids", set()) or actor_id in known.get("entity_ids", set())
            if (not resolved or resolved not in all_entity_ids) and not in_patch and not in_known:
                errors.append(f"departed_cast_id_not_found: {actor_id}")

    if isinstance(active_cast, list) and isinstance(departed_cast, list):
        active_set = set(active_cast)
        departed_set = set(departed_cast)
        overlap = active_set & departed_set
        if overlap:
            errors.append(f"cast_sets_overlap: {overlap}")

    for rel in compiled_patch.get("upsert_graph_relations", []):
        if not isinstance(rel, dict):
            continue
        source_id = rel.get("source_id", "")
        if source_id and source_id not in all_entity_ids:
            errors.append(f"relation_source_not_found: {source_id}")
        target_id = rel.get("target_id", "")
        if target_id and target_id not in all_entity_ids:
            errors.append(f"relation_target_not_found: {target_id}")

    for fact in compiled_patch.get("upsert_graph_facts", []):
        if not isinstance(fact, dict):
            continue
        for eid in fact.get("entity_ids", []) if isinstance(fact.get("entity_ids"), list) else []:
            if eid and eid not in all_entity_ids:
                errors.append(f"fact_entity_ref_not_found: {eid}")

    scene_location_id = compiled_patch.get("scene_patch", {}).get("location_id", "")
    if scene_location_id and scene_location_id not in all_entity_ids:
        location_exists = any(
            isinstance(e, dict) and e.get("id") == scene_location_id
            for e in compiled_patch.get("upsert_graph_entities", [])
        )
        if not location_exists:
            errors.append(f"scene_location_not_found: {scene_location_id}")

    for npc_update in compiled_patch.get("update_npc_actors", []):
        if not isinstance(npc_update, dict):
            continue
        actor_id = npc_update.get("id") or npc_update.get("actor_id", "")
        if actor_id and actor_id not in all_entity_ids and actor_id not in known.get("npc_ids", set()):
            errors.append(f"npc_update_target_not_found: {actor_id}")

    diagnostics = compiled_patch.get("resolution_diagnostics", {})
    if isinstance(diagnostics, dict):
        substitutions = diagnostics.get("substitutions", [])
        if isinstance(substitutions, list) and len(substitutions) > 0:
            errors.append("substitutions_not_empty")

    return errors


def _get_memory_revision(campaign):
    if not campaign:
        return 0
    world = CampaignWorld.query.filter_by(campaign_id=campaign.id).first()
    if not world:
        return 0
    return world.memory_revision or 0


def _build_resolution_records(registry, compiled_patch, memory_context):
    records = []
    for entry in registry:
        if not isinstance(entry, dict):
            continue
        # Only create resolution records when a provisional entity is explicitly resolved
        # to a different canonical identity — not at provisional creation time.
        if entry.get("decision") not in ("reuse_existing", "add_alias"):
            continue
        canonical_id = entry.get("canonical_id", "")
        if not canonical_id:
            continue
        mention_entity_id = entry.get("mention_ref", "")
        if not mention_entity_id or mention_entity_id.lower() == canonical_id.lower():
            continue
        campaign_id = _safe_int((memory_context or {}).get("campaign_id"), 0, minimum=0) if isinstance(memory_context, dict) else 0
        key_raw = f"{campaign_id}:{mention_entity_id}:{canonical_id}:{entry.get('decision')}"
        resolution_id = f"ires_{hashlib.md5(key_raw.encode('utf-8')).hexdigest()[:16]}"
        records.append({
            "resolution_id": resolution_id,
            "mention_entity_id": mention_entity_id,
            "mention_name": entry.get("surface_form", ""),
            "resolution_action": "same_identity",
            "canonical_id": canonical_id,
            "canonical_name": entry.get("canonical_name", entry.get("surface_form", "")),
            "visibility": entry.get("visibility", "party_known"),
            "resolved_by": "session_memory_writer",
            "evidence": entry.get("evidence"),
        })
    return records


def _augment_registry_from_resolved(registry, resolved_entities, resolved_npcs, known, allocated_ids=None):
    if allocated_ids is None:
        allocated_ids = set()
    existing_forms = {entry.get("surface_form", "").strip().lower() for entry in registry}
    index = len(registry)

    for item in resolved_entities:
        if not isinstance(item, dict):
            continue
        name = clean_text(item.get("name"), 200)
        if not name:
            continue
        if name.lower() in existing_forms:
            continue
        existing_forms.add(name.lower())

        entity_type = clean_text(item.get("type"), 40).lower() or "other"
        proposed_id = clean_id(item.get("id") or item.get("entity_id"), "")
        mention_ref = f"resolved_entity_{index}"

        # Check if proposed ID is a known entity, but verify name consistency
        if proposed_id and proposed_id in known.get("entity_ids", set()):
            existing_name = known.get("entity_names", {}).get(proposed_id, "")
            if existing_name and name.lower() != existing_name.lower():
                # Name differs from canonical — check for conflict
                for other_id, other_name in known.get("entity_names", {}).items():
                    if other_name.lower() == name.lower() and other_id != proposed_id:
                        # This name belongs to a different entity — skip (will be unresolved)
                        index += 1
                        continue
                # Name differs but not conflicting — treat as alias
                registry.append({
                    "mention_ref": mention_ref,
                    "surface_form": name,
                    "identity_status": "known_public",
                    "visibility": "party_known",
                    "evidence": [{"source": "resolver_output", "field": "upsert_graph_entities"}],
                    "canonical_id": proposed_id,
                    "canonical_name": name,
                    "decision": "reuse_existing",
                    "blocked_operations": [],
                    "resolution_state": "resolved",
                    "entity_type": entity_type,
                })
            else:
                registry.append({
                    "mention_ref": mention_ref,
                    "surface_form": name,
                    "identity_status": "known_public",
                    "visibility": "party_known",
                    "evidence": [{"source": "resolver_output", "field": "upsert_graph_entities"}],
                    "canonical_id": proposed_id,
                    "canonical_name": name,
                    "decision": "reuse_existing",
                    "blocked_operations": [],
                    "resolution_state": "resolved",
                    "entity_type": entity_type,
                })
        else:
            new_id = allocate_durable_id(name, allocated_ids)
            allocated_ids.add(new_id)
            registry.append({
                "mention_ref": mention_ref,
                "surface_form": name,
                "identity_status": "provisional_new_entity",
                "visibility": "party_known",
                "evidence": [{"source": "resolver_output", "field": "upsert_graph_entities"}],
                "canonical_id": new_id,
                "canonical_name": name,
                "decision": "create_new",
                "blocked_operations": [],
                "resolution_state": "resolved",
                "entity_type": entity_type,
            })
        index += 1

    for item in resolved_npcs:
        if not isinstance(item, dict):
            continue
        name = clean_text(item.get("name"), 200)
        proposed_id = clean_id(item.get("id") or item.get("actor_id") or item.get("actor_ref"), "")
        if name and name.lower() not in existing_forms:
            existing_forms.add(name.lower())
            mention_ref = f"resolved_npc_{index}"
            entity_type = "npc"
            if proposed_id and proposed_id in known.get("npc_ids", set()):
                existing_name = known.get("npc_names", {}).get(proposed_id, "")
                if existing_name and name.lower() != existing_name.lower():
                    for other_id, other_name in known.get("npc_names", {}).items():
                        if other_name.lower() == name.lower() and other_id != proposed_id:
                            index += 1
                            continue
                registry.append({
                    "mention_ref": mention_ref,
                    "surface_form": name,
                    "identity_status": "known_public",
                    "visibility": "party_known",
                    "evidence": [{"source": "resolver_output", "field": "update_npc_actors"}],
                    "canonical_id": proposed_id,
                    "canonical_name": name,
                    "decision": "reuse_existing",
                    "blocked_operations": [],
                    "resolution_state": "resolved",
                    "entity_type": entity_type,
                })
            else:
                new_id = proposed_id or allocate_durable_id(name, allocated_ids)
                allocated_ids.add(new_id)
                registry.append({
                    "mention_ref": mention_ref,
                    "surface_form": name,
                    "identity_status": "provisional_new_entity",
                    "visibility": "party_known",
                    "evidence": [{"source": "resolver_output", "field": "update_npc_actors"}],
                    "canonical_id": new_id,
                    "canonical_name": name,
                    "decision": "create_new",
                    "blocked_operations": [],
                    "resolution_state": "resolved",
                    "entity_type": entity_type,
                })
            index += 1
        elif not name and proposed_id:
            # Name missing from NPC update but ID is provided — index by mention_ref for resolution
            pass
    records = []
    for entry in registry:
        if not isinstance(entry, dict):
            continue
        if entry.get("decision") != "create_provisional":
            continue
        canonical_id = entry.get("canonical_id", "")
        if not canonical_id:
            continue
        resolution_id = f"ires_{hashlib.md5(canonical_id.encode('utf-8')).hexdigest()[:12]}"
        records.append({
            "resolution_id": resolution_id,
            "mention_entity_id": canonical_id,
            "mention_name": entry.get("surface_form", ""),
            "resolution_action": "same_identity",
            "canonical_id": canonical_id,
            "canonical_name": entry.get("surface_form", ""),
            "visibility": entry.get("visibility", "party_known"),
            "resolved_by": "session_memory_writer",
            "evidence": entry.get("evidence"),
        })
    return records


def compile_staged_memory_patch(memory_context, extracted, resolved):
    campaign = _memory_context_campaign(memory_context)
    if campaign is None:
        raise MemoryPipelineError(
            stage="compilation",
            code="missing_campaign",
            message="Cannot compile staged memory patch: campaign is missing from memory context.",
            telemetry={"campaign_id": _safe_int((memory_context or {}).get("campaign_id"), 0, minimum=0)},
        )

    extracted = extracted if isinstance(extracted, dict) else {}
    resolved = resolved if isinstance(resolved, dict) else {}
    known = _known_ids(campaign)
    unresolved = list(resolved.get("unresolved_items") if isinstance(resolved.get("unresolved_items"), list) else [])

    resolver_packet = memory_context.get("resolver_packet") if isinstance(memory_context, dict) else None
    if isinstance(resolver_packet, dict) and not isinstance(resolver_packet.get("entity_mentions"), list):
        resolver_packet = None

    prior_resolutions = fetch_prior_resolutions(campaign)
    pending_clarifications = fetch_pending_clarifications(campaign)
    memory_context_with_clarifications = dict(memory_context if isinstance(memory_context, dict) else {})
    memory_context_with_clarifications["pending_clarifications"] = pending_clarifications

    registry, registry_map, clarification_requests, diagnostics = build_canonical_resolution_registry(
        campaign,
        memory_context_with_clarifications,
        extracted,
        resolver_packet,
        prior_resolutions,
        known,
    )

    # Also process resolved claims through the registry (entities and NPCs from resolver output)
    resolved_entities = resolved.get("upsert_graph_entities") if isinstance(resolved.get("upsert_graph_entities"), list) else []
    resolved_npcs = resolved.get("update_npc_actors") if isinstance(resolved.get("update_npc_actors"), list) else []
    _augment_registry_from_resolved(
        registry, resolved_entities, resolved_npcs, known, allocated_ids={e.get("canonical_id") for e in registry if e.get("canonical_id")},
    )
    registry_map = {entry["mention_ref"]: entry for entry in registry}

    resolution_records = _build_resolution_records(registry, {}, memory_context)

    running_summary = clean_text(
        resolved.get("running_summary") or extracted.get("running_summary"),
        4000,
    )

    prior_anchors = memory_context.get("prior_memory_anchors") if isinstance(memory_context.get("prior_memory_anchors"), dict) else {}
    resolved_anchors = resolved.get("memory_anchors") if isinstance(resolved.get("memory_anchors"), dict) else None
    extracted_anchors = extracted.get("memory_anchors") if isinstance(extracted.get("memory_anchors"), dict) else None
    anchors = resolved_anchors or extracted_anchors or prior_anchors
    compiled_anchors = {
        "current_goal": anchors.get("current_goal"),
        "current_scene": anchors.get("current_scene"),
        "open_clues": anchors.get("open_clues") if isinstance(anchors.get("open_clues"), list) else [],
        "unresolved_questions": anchors.get("unresolved_questions") if isinstance(anchors.get("unresolved_questions"), list) else [],
        "npc_observations": anchors.get("npc_observations") if isinstance(anchors.get("npc_observations"), list) else [],
        "recent_offers_promises": anchors.get("recent_offers_promises") if isinstance(anchors.get("recent_offers_promises"), list) else [],
    }

    hot_context = memory_context.get("hot_context") if isinstance(memory_context.get("hot_context"), dict) else {}
    source_player_message_id = memory_context.get("latest_player_message_id") or memory_context.get("source_player_message_id") or hot_context.get("player_message_id") or hot_context.get("source_player_message_id")
    source_dm_message_id = memory_context.get("latest_dm_message_id") or memory_context.get("source_dm_message_id") or hot_context.get("dm_message_id") or hot_context.get("source_dm_message_id")

    def make_provenance(item_raw, default_tool=None):
        raw_prov = item_raw.get("provenance") if isinstance(item_raw.get("provenance"), dict) else {}
        evidence = item_raw.get("evidence")
        if not isinstance(evidence, list):
            evidence = [evidence] if evidence else []
        basis = raw_prov.get("evidence_basis") or item_raw.get("evidence_basis") or evidence
        if not isinstance(basis, list):
            basis = [basis] if basis else []
        basis = [str(b).strip() for b in basis if str(b).strip()]
        return {
            "source_player_message_id": raw_prov.get("source_player_message_id") or source_player_message_id,
            "source_dm_message_id": raw_prov.get("source_dm_message_id") or source_dm_message_id,
            "tool_name": raw_prov.get("tool_name") or default_tool,
            "tool_result_id": raw_prov.get("tool_result_id"),
            "evidence_basis": basis,
        }

    allocated_entity_ids = set(known["entity_ids"])
    for entry in registry:
        if entry.get("canonical_id"):
            allocated_entity_ids.add(entry["canonical_id"])

    # ── Compile Entities from Registry ────────────────────────────────
    accepted_entities = []
    patch_created_entity_ids = set()
    patch_created_npc_ids = set()
    seen_entity_ids = set()

    for entry in registry:
        if entry.get("decision") in ("reject", "request_clarification"):
            continue
        if entry.get("decision") == "create_provisional" and not entry.get("canonical_id"):
            continue

        entity_id = entry.get("canonical_id")
        if not entity_id:
            continue
        if entity_id in seen_entity_ids:
            continue

        entity_type = entry.get("entity_type", "other")
        if entity_type in ("npc", "person"):
            patch_created_npc_ids.add(entity_id)
        patch_created_entity_ids.add(entity_id)
        seen_entity_ids.add(entity_id)

        is_new = entry.get("decision") in ("create_new", "create_provisional")
        if is_new or entity_id not in known["entity_ids"]:
            # Merge resolved entity fields if available
            resolved_item = None
            for ri in resolved_entities:
                if isinstance(ri, dict) and clean_id(ri.get("id") or ri.get("entity_id"), "") == entity_id:
                    resolved_item = ri
                    break
                if isinstance(ri, dict) and clean_text(ri.get("name"), 200).lower() == entry.get("surface_form", "").lower():
                    resolved_item = ri
                    break

            accepted_entities.append({
                "id": entity_id,
                "name": entry.get("canonical_name") or entry.get("surface_form", ""),
                "type": entity_type,
                "summary": clean_text(
                    (resolved_item or {}).get("summary") or entry.get("surface_form"),
                    500,
                ) or None,
                "tags": [
                    clean_text(t, 40) for t in (resolved_item or {}).get("tags", [])
                    if clean_text(t, 40)
                ] if isinstance((resolved_item or {}).get("tags"), list) else [],
                "visibility": (resolved_item or {}).get("intended_visibility") if (resolved_item or {}).get("intended_visibility") in VALID_MEMORY_VISIBILITIES else _normalize_visibility(
                    (resolved_item or {}).get("source_surface"), (resolved_item or {}).get("intended_visibility"),
                ) or entry.get("visibility", "party_known"),
                "certainty": _normalize_certainty((resolved_item or {}).get("certainty")) if resolved_item else "confirmed",
                "importance": _normalize_importance((resolved_item or {}).get("importance")) if resolved_item else 3,
                "expires_or_retire_condition": clean_text((resolved_item or {}).get("expires_or_retire_condition"), 520) or None,
                "reason": clean_text((resolved_item or {}).get("reason"), 420) or f"Resolved by registry: {entry.get('decision')}.",
                "memory_type": "entity",
                "provenance": make_provenance(resolved_item or entry, default_tool="resolution_registry"),
                "resolution_mode": entry.get("decision"),
            })

    # Build name-based remap for entity name → canonical ID to support old-style reference resolution
    entity_name_to_id = {}
    for entity in accepted_entities:
        name = clean_text(entity.get("name"), 200)
        if name:
            entity_name_to_id[name.lower()] = entity["id"]
            name_slug = clean_id(name.lower(), "")
            if name_slug:
                entity_name_to_id[name_slug] = entity["id"]

    # Also map proposed IDs from resolved entities to their canonical IDs
    for item in resolved_entities:
        if not isinstance(item, dict):
            continue
        name = clean_text(item.get("name"), 200)
        proposed_id = clean_id(item.get("id") or item.get("entity_id"), "")
        if name and proposed_id:
            canonical_id = entity_name_to_id.get(name.lower())
            if canonical_id:
                entity_name_to_id[proposed_id] = canonical_id
                entity_name_to_id[proposed_id.lower()] = canonical_id
    for item in resolved_npcs:
        if not isinstance(item, dict):
            continue
        name = clean_text(item.get("name"), 200)
        proposed_id = clean_id(item.get("id") or item.get("actor_id") or item.get("actor_ref"), "")
        if name and proposed_id:
            canonical_id = entity_name_to_id.get(name.lower())
            if canonical_id:
                entity_name_to_id[proposed_id] = canonical_id
                entity_name_to_id[proposed_id.lower()] = canonical_id

    def resolve_ref_with_names(ref):
        resolved = resolve_ref(ref, registry_map, known)
        if resolved:
            return resolved
        ref_lower = str(ref).strip().lower()
        if ref_lower in entity_name_to_id:
            return entity_name_to_id[ref_lower]
        for name, cid in entity_name_to_id.items():
            if clean_id(name, "") == clean_id(ref_lower, ""):
                return cid
        return ""

    # ── Compile Scene Location via Registry ────────────────────────────
    scene_patch = resolved.get("scene_patch") if isinstance(resolved.get("scene_patch"), dict) else {}
    if not scene_patch:
        scene_patch = extracted.get("scene_patch") if isinstance(extracted.get("scene_patch"), dict) else {}
    compiled_scene = {}
    from services.scene_location_resolver import resolve_scene_location_patch
    current_scene = hot_context.get("current_scene") if isinstance(hot_context.get("current_scene"), dict) else {}
    scene_resolution_mode = "inferred"

    resolved_loc = resolve_scene_location_patch(scene_patch, campaign, current_scene)
    loc_status = resolved_loc.get("status", "unresolved") if isinstance(resolved_loc, dict) else "unresolved"

    if loc_status == "canonical" or loc_status == "direct":
        compiled_scene["location_id"] = resolved_loc["location_id"]
        compiled_scene["location_name"] = resolved_loc["location_name"]
        scene_resolution_mode = "canonical" if loc_status == "canonical" else "direct"

    elif loc_status == "new":
        new_loc_id = resolved_loc.get("location_id") or allocate_durable_id(
            resolved_loc.get("location_name", "unknown_location"),
            allocated_entity_ids,
            prefix="location",
        )
        new_loc_name = resolved_loc.get("location_name", "Unknown Location")
        allocated_entity_ids.add(new_loc_id)
        patch_created_entity_ids.add(new_loc_id)

        compiled_scene["location_id"] = new_loc_id
        compiled_scene["location_name"] = new_loc_name
        scene_resolution_mode = "new"

        accepted_entities.append({
            "id": new_loc_id,
            "name": new_loc_name,
            "type": "location",
            "summary": clean_text(scene_patch.get("summary"), 500) or new_loc_name,
            "tags": [],
            "visibility": "party_known",
            "certainty": "confirmed",
            "importance": 3,
            "expires_or_retire_condition": None,
            "reason": "New location created from scene patch.",
            "memory_type": "location",
            "provenance": make_provenance(scene_patch, default_tool="resolve_scene_location_patch"),
            "resolution_mode": "create_new",
        })

    elif loc_status == "unresolved":
        proposed_id = clean_id(scene_patch.get("location_id"), "")
        proposed_name = clean_text(scene_patch.get("location_name"), 160)
        unresolved.append({
            "kind": "scene_location",
            "location_id": proposed_id,
            "location_name": proposed_name,
            "reason": "unresolved_scene_location",
            "provenance": make_provenance(scene_patch, default_tool="resolve_scene_location_patch"),
            "resolution_mode": "unresolved",
        })

    compiled_scene["provenance"] = make_provenance(scene_patch, default_tool="resolve_scene_location_patch")
    compiled_scene["resolution_mode"] = scene_resolution_mode

    for field, limit in (("time_of_day", 80), ("immediate_tension", 420)):
        value = clean_text(scene_patch.get(field), limit)
        if value:
            compiled_scene[field] = value

    # ── Compile Active/Departed Cast via Registry ──────────────────────
    active_npc_ids = []
    for raw_id in scene_patch.get("active_npc_ids") if isinstance(scene_patch.get("active_npc_ids"), list) else []:
        actor_id = clean_id(raw_id, "")
        resolved_id = resolve_ref_with_names(actor_id)
        if resolved_id:
            if resolved_id in known["npc_ids"] or resolved_id in patch_created_npc_ids or resolved_id in patch_created_entity_ids:
                if resolved_id not in active_npc_ids:
                    active_npc_ids.append(resolved_id)
            else:
                pass
        elif actor_id:
            if actor_id in patch_created_npc_ids or actor_id in patch_created_entity_ids:
                if actor_id not in active_npc_ids:
                    active_npc_ids.append(actor_id)
            else:
                unresolved.append({
                    "kind": "active_npc",
                    "actor_id": actor_id,
                    "reason": "unknown_npc_id",
                    "provenance": make_provenance(scene_patch),
                    "resolution_mode": "unresolved",
                })

    if active_npc_ids:
        compiled_scene["active_npc_ids"] = active_npc_ids

    departed_npc_ids = []
    for raw_id in scene_patch.get("departed_npc_ids") if isinstance(scene_patch.get("departed_npc_ids"), list) else []:
        actor_id = clean_id(raw_id, "")
        resolved_id = resolve_ref_with_names(actor_id)
        if resolved_id:
            if resolved_id in known["npc_ids"] or resolved_id in patch_created_npc_ids or resolved_id in patch_created_entity_ids:
                if resolved_id not in departed_npc_ids:
                    departed_npc_ids.append(resolved_id)
            else:
                pass
        elif actor_id:
            if actor_id in patch_created_npc_ids or actor_id in patch_created_entity_ids:
                if actor_id not in departed_npc_ids:
                    departed_npc_ids.append(actor_id)
            else:
                unresolved.append({
                    "kind": "departed_npc",
                    "actor_id": actor_id,
                    "reason": "unknown_npc_id",
                    "provenance": make_provenance(scene_patch),
                    "resolution_mode": "unresolved",
                })

    if departed_npc_ids:
        compiled_scene["departed_npc_ids"] = departed_npc_ids

    # ── Compile Relations via Registry ─────────────────────────────────
    accepted_relations = []
    raw_relations = resolved.get("upsert_graph_relations") if isinstance(resolved.get("upsert_graph_relations"), list) else []
    for index, raw_rel in enumerate(raw_relations):
        if not isinstance(raw_rel, dict):
            continue
        rel_type = clean_id(raw_rel.get("type"), "")
        raw_source = raw_rel.get("source_id") or raw_rel.get("source_ref") or ""
        raw_target = raw_rel.get("target_id") or raw_rel.get("target_ref") or ""
        source_id = resolve_ref_with_names(raw_source)
        target_id = resolve_ref_with_names(raw_target)

        if not source_id or not target_id or not rel_type:
            unresolved_endpoints = []
            if not source_id:
                unresolved_endpoints.append(raw_source or "missing_source")
            if not target_id:
                unresolved_endpoints.append(raw_target or "missing_target")
            unresolved.append({
                "kind": "relation",
                "type": rel_type,
                "source_id": source_id or raw_source,
                "target_id": target_id or raw_target,
                "reason": "unresolved_relation_endpoints",
                "unresolved_endpoints": unresolved_endpoints,
                "provenance": make_provenance(raw_rel),
                "resolution_mode": "unresolved",
            })
            continue

        all_valid = known["entity_ids"] | patch_created_entity_ids
        if source_id not in all_valid or target_id not in all_valid:
            unresolved_endpoints = []
            if source_id not in all_valid:
                unresolved_endpoints.append(raw_source or source_id)
            if target_id not in all_valid:
                unresolved_endpoints.append(raw_target or target_id)
            unresolved.append({
                "kind": "relation",
                "type": rel_type,
                "source_id": source_id,
                "target_id": target_id,
                "reason": "unresolved_relation_endpoints",
                "unresolved_endpoints": unresolved_endpoints,
                "provenance": make_provenance(raw_rel),
                "resolution_mode": "unresolved",
            })
            continue

        rel_key = f"{source_id}:{rel_type}:{target_id}".lower()
        stable_rel_id = f"rel_{hashlib.md5(rel_key.encode('utf-8')).hexdigest()[:12]}"

        accepted_relations.append({
            "id": stable_rel_id,
            "type": rel_type,
            "source_id": source_id,
            "target_id": target_id,
            "summary": clean_text(raw_rel.get("summary"), 500) or None,
            "visibility": _normalize_visibility(raw_rel.get("source_surface"), raw_rel.get("intended_visibility")),
            "certainty": _normalize_certainty(raw_rel.get("certainty")),
            "importance": _normalize_importance(raw_rel.get("importance")),
            "expires_or_retire_condition": clean_text(raw_rel.get("expires_or_retire_condition"), 520) or None,
            "reason": clean_text(raw_rel.get("reason"), 420) or "Resolved staged memory relation.",
            "memory_type": "relation",
            "provenance": make_provenance(raw_rel, default_tool="resolution_registry"),
            "resolution_mode": raw_rel.get("resolution_mode") or "canonical",
        })

    # ── Compile NPC Updates via Registry ───────────────────────────────
    accepted_npc_updates = []
    raw_npc_updates = resolved.get("update_npc_actors") if isinstance(resolved.get("update_npc_actors"), list) else []
    for index, raw_npc in enumerate(raw_npc_updates):
        if not isinstance(raw_npc, dict):
            continue
        raw_actor_id = raw_npc.get("id") or raw_npc.get("actor_id") or raw_npc.get("actor_ref") or ""
        actor_id = resolve_ref_with_names(raw_actor_id)

        is_known = actor_id in known["npc_ids"] or actor_id in patch_created_npc_ids
        if not actor_id or not is_known:
            unresolved.append({
                "kind": "npc_actor",
                "actor_id": raw_actor_id or "missing_actor_id",
                "reason": "unknown_npc_id",
                "provenance": make_provenance(raw_npc),
                "resolution_mode": "unresolved",
            })
            continue

        supplied_name = clean_text(raw_npc.get("name"), 200)
        name_collision = False
        if supplied_name and actor_id in known["npc_ids"]:
            existing_name = known.get("npc_names", {}).get(actor_id, "")
            if existing_name and supplied_name.lower() != existing_name.lower():
                for other_id, other_name in known.get("npc_names", {}).items():
                    if other_name.lower() == supplied_name.lower() and other_id != actor_id:
                        name_collision = True
                        break
        if name_collision:
            unresolved.append({
                "kind": "npc_actor",
                "actor_id": actor_id,
                "reason": "name_collision_with_different_npc",
                "provenance": make_provenance(raw_npc),
                "resolution_mode": "unresolved",
            })
            continue

        npc_data = {
            "id": actor_id,
            "actor_id": actor_id,
            "name": clean_text(raw_npc.get("name"), 200) or None,
            "role": clean_text(raw_npc.get("role"), 200) or None,
            "public_summary": clean_text(raw_npc.get("public_summary"), 420) or None,
            "voice": clean_text(raw_npc.get("voice"), 240) or None,
            "background": clean_text(raw_npc.get("background"), 700) or None,
            "visibility": _normalize_visibility(raw_npc.get("source_surface"), raw_npc.get("intended_visibility")),
            "certainty": _normalize_certainty(raw_npc.get("certainty")),
            "importance": _normalize_importance(raw_npc.get("importance")),
            "expires_or_retire_condition": clean_text(raw_npc.get("expires_or_retire_condition"), 520) or None,
            "reason": clean_text(raw_npc.get("reason"), 420) or "Resolved staged NPC actor update.",
            "memory_type": clean_text(raw_npc.get("memory_type"), 40).lower() or "npc",
            "provenance": make_provenance(raw_npc, default_tool="resolution_registry"),
            "resolution_mode": raw_npc.get("resolution_mode") or "canonical",
        }
        for field, max_items, limit in (("wants", 6, 180), ("fears", 6, 180), ("secrets", 8, 240)):
            values = raw_npc.get(field)
            if isinstance(values, list):
                npc_data[field] = [clean_text(v, limit) for v in values if clean_text(v, limit)]

        rels = raw_npc.get("relationships")
        if isinstance(rels, dict):
            npc_data["relationships"] = {}
            for target, desc in rels.items():
                resolved_target = resolve_ref_with_names(target)
                if resolved_target and resolved_target in (known["entity_ids"] | patch_created_entity_ids):
                    npc_data["relationships"][resolved_target] = clean_text(desc, 300)
                else:
                    unresolved.append({
                        "kind": "npc_relationship_target",
                        "npc_id": actor_id,
                        "requested_target": target,
                        "reason": "unknown_relationship_target",
                        "provenance": make_provenance(raw_npc),
                        "resolution_mode": "unresolved",
                    })

        roa = raw_npc.get("recent_offscreen_activity")
        if isinstance(roa, list):
            npc_data["recent_offscreen_activity"] = [clean_text(v, 300) for v in roa if clean_text(v, 300)]

        npc_data = {k: v for k, v in npc_data.items() if v is not None}
        accepted_npc_updates.append(npc_data)

    # ── Compile World Events ───────────────────────────────────────────
    accepted_events = []
    raw_events = resolved.get("record_events") if isinstance(resolved.get("record_events"), list) else []
    for raw_event in raw_events:
        if not isinstance(raw_event, dict):
            continue
        summary = clean_text(raw_event.get("summary"), 1200) or "Session memory updated."
        accepted_events.append({
            "event_type": clean_text(raw_event.get("event_type"), 80) or "session_memory",
            "summary": summary,
            "payload": raw_event.get("payload") if isinstance(raw_event.get("payload"), dict) else {},
            "visibility": _normalize_visibility(raw_event.get("source_surface"), raw_event.get("intended_visibility")),
            "certainty": _normalize_certainty(raw_event.get("certainty")),
            "importance": _normalize_importance(raw_event.get("importance")),
            "expires_or_retire_condition": clean_text(raw_event.get("expires_or_retire_condition"), 520) or None,
            "reason": clean_text(raw_event.get("reason"), 420) or "Resolved staged event record.",
            "memory_type": "fact",
            "provenance": make_provenance(raw_event, default_tool="session_memory_record_event"),
            "resolution_mode": raw_event.get("resolution_mode") or "direct",
        })

    # ── Compile Facts via Registry ─────────────────────────────────────
    accepted_facts = []
    skipped_facts = []
    raw_facts = resolved.get("upsert_graph_facts") if isinstance(resolved.get("upsert_graph_facts"), list) else []
    for index, raw_fact in enumerate(raw_facts):
        if not isinstance(raw_fact, dict):
            continue
        text = clean_text(raw_fact.get("text"), 700)
        if not text:
            continue
        raw_entity_ids = raw_fact.get("entity_ids") if isinstance(raw_fact.get("entity_ids"), list) else []
        entity_ids = []
        unknown_entity_ids = []
        for raw_entity_id in raw_entity_ids:
            entity_id = resolve_ref_with_names(raw_entity_id)
            if not entity_id:
                unknown_entity_ids.append(raw_entity_id)
                continue
            if entity_id not in entity_ids:
                entity_ids.append(entity_id)
        if raw_entity_ids and not entity_ids:
            skipped_facts.append({
                "index": index,
                "text": text,
                "reason": "all_entity_ids_unresolved",
                "requested_entity_ids": [clean_id(item, "") for item in raw_entity_ids if clean_id(item, "")],
            })
            unresolved.append({
                "kind": "fact",
                "text": text,
                "reason": "all_entity_ids_unresolved",
                "requested_entity_ids": [clean_id(item, "") for item in raw_entity_ids if clean_id(item, "")],
                "provenance": make_provenance(raw_fact),
                "resolution_mode": "unresolved",
            })
            continue
        if unknown_entity_ids:
            skipped_facts.append({
                "index": index,
                "text": text,
                "reason": "partial_entity_ids_unresolved",
                "requested_entity_ids": [clean_id(item, "") for item in raw_entity_ids if clean_id(item, "")],
                "unknown_entity_ids": unknown_entity_ids,
            })
            unresolved.append({
                "kind": "fact",
                "text": text,
                "reason": "partial_entity_ids_unresolved",
                "requested_entity_ids": [clean_id(item, "") for item in raw_entity_ids if clean_id(item, "")],
                "unknown_entity_ids": unknown_entity_ids,
                "provenance": make_provenance(raw_fact),
                "resolution_mode": "unresolved",
            })
            continue
        fact_id = clean_id(raw_fact.get("id"), "")
        if fact_id and fact_id not in known["fact_ids"]:
            fact_id = ""
        accepted_facts.append({
            "id": fact_id or f"fact_{index + 1}",
            "entity_ids": entity_ids,
            "text": text,
            "visibility": _normalize_visibility(raw_fact.get("source_surface"), raw_fact.get("intended_visibility")),
            "certainty": _normalize_certainty(raw_fact.get("certainty")),
            "importance": _normalize_importance(raw_fact.get("importance")),
            "expires_or_retire_condition": clean_text(raw_fact.get("expires_or_retire_condition"), 520) or None,
            "reason": clean_text(raw_fact.get("reason"), 420) or "Resolved staged memory fact.",
            "memory_type": clean_text(raw_fact.get("memory_type"), 40).lower() or "fact",
            "provenance": make_provenance(raw_fact, default_tool="resolution_registry"),
            "resolution_mode": raw_fact.get("resolution_mode") or ("canonical" if entity_ids else "direct"),
        })
        if unknown_entity_ids:
            # Already skipped above via continue
            pass

    # ── Compile Clocks ─────────────────────────────────────────────────
    create_clocks = []
    for raw_clock in resolved.get("create_clocks") or []:
        if isinstance(raw_clock, dict):
            create_clocks.append({
                **raw_clock,
                "provenance": make_provenance(raw_clock, default_tool="session_memory_update_clocks"),
                "resolution_mode": raw_clock.get("resolution_mode") or "inferred",
            })

    retire_clocks = []
    for raw_clock in resolved.get("retire_clocks") or []:
        if isinstance(raw_clock, dict):
            retire_clocks.append({
                **raw_clock,
                "provenance": make_provenance(raw_clock, default_tool="session_memory_update_clocks"),
                "resolution_mode": raw_clock.get("resolution_mode") or "inferred",
            })

    # ── Build Compiled Patch ───────────────────────────────────────────
    compiled_patch = {
        "running_summary": running_summary,
        "memory_anchors": compiled_anchors,
        "scene_patch": compiled_scene,
        "scene_reason": clean_text(
            resolved.get("scene_reason") or extracted.get("scene_reason"),
            420,
        ) or None,
        "upsert_graph_entities": accepted_entities,
        "upsert_graph_relations": accepted_relations,
        "upsert_graph_facts": accepted_facts,
        "create_clocks": create_clocks,
        "retire_clocks": retire_clocks,
        "update_npc_actors": accepted_npc_updates,
        "record_events": accepted_events,
        "unresolved_items": unresolved,
        "compile_summary": {
            "accepted_fact_count": len(accepted_facts),
            "skipped_fact_count": len(skipped_facts),
            "accepted_entity_count": len(accepted_entities),
            "accepted_relation_count": len(accepted_relations),
            "accepted_npc_count": len(accepted_npc_updates),
            "registry_size": len(registry),
            "clarification_count": len(clarification_requests),
        },
        "resolution_diagnostics": diagnostics,
        "resolution_records": resolution_records,
        "clarification_requests": clarification_requests,
        "registry": registry,
        "source_contract": SOURCE_CONTRACT_COMPILED_V2,
        "base_memory_revision": _get_memory_revision(campaign),
        "evidence_basis": resolved.get("evidence_basis") if isinstance(resolved.get("evidence_basis"), list) else [],
        "resolved_entity_refs": resolved.get("resolved_entity_refs") if isinstance(resolved.get("resolved_entity_refs"), list) else [],
        "resolved_location_refs": resolved.get("resolved_location_refs") if isinstance(resolved.get("resolved_location_refs"), list) else [],
    }

    # ── Final-State Validation ─────────────────────────────────────────
    validation_errors = _validate_final_memory_state(compiled_patch, registry_map, known, campaign)
    if validation_errors:
        raise MemoryPipelineError(
            stage="compilation",
            code="final_state_validation_failed",
            message=f"Final-state validation failed: {'; '.join(validation_errors[:5])}",
            telemetry={"validation_errors": validation_errors},
        )

    # ── Validate diagnostics ───────────────────────────────────────────
    diag_valid, diag_error = validate_diagnostics(diagnostics)
    if not diag_valid:
        compiled_patch["diagnostics_error"] = diag_error

    return compiled_patch

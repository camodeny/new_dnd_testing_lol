import json
from datetime import timedelta

from sqlalchemy.exc import IntegrityError

from time_utils import utcnow
from models import (
    db,
    CampaignMember,
    CampaignClock,
    CampaignWorld,
    CampaignWorldIdentity,
    Character,
    NPCActor,
    WorldEvent,
)
from openrouter import get_world_genesis_package
from services.audit_service import log_audit_event
from services.embedding_service import upsert_memory_embedding
from services.entity_types import normalize_world_entity_type
from services.planning_service import can_start_session, planning_context


WORLD_GENERATION_STALE_AFTER = timedelta(minutes=20)


PUBLIC_INTRO_FIELDS = (
    'title',
    'elevator_pitch',
    'starting_location',
    'campaign_tone',
    'party_hook',
)

PRIVATE_MARKERS = (
    'secret',
    'villain',
    'traitor',
    'hidden',
    'true ',
    'secretly',
    'unknown to',
    'dm_private',
)


def json_dumps(value):
    return json.dumps(value, ensure_ascii=False)


def json_loads(value, fallback):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def clean_text(value, max_length=1200):
    return ' '.join(str(value or '').strip().split())[:max_length]


def normalize_text(value):
    return ' '.join(str(value or '').strip().split())


def clean_id(value, fallback):
    raw = clean_text(value, 100).lower()
    safe = ''.join(ch if ch.isalnum() else '_' for ch in raw).strip('_')
    while '__' in safe:
        safe = safe.replace('__', '_')
    return safe or fallback


def sanitize_public_intro(raw_intro, campaign):
    raw_intro = raw_intro if isinstance(raw_intro, dict) else {}
    default_elevator_pitch = (
        'A newly formed party arrives at the edge of an unfolding adventure, where local trouble '
        'and personal stakes are already beginning to collide.'
    )
    default_party_hook = (
        'The party begins together as a nearby situation demands quick choices and reveals larger stakes.'
    )
    title = normalize_text(raw_intro.get('title')) or normalize_text(campaign.name) or 'Untitled Campaign'
    elevator_pitch = normalize_text(raw_intro.get('elevator_pitch'))
    if not elevator_pitch:
        elevator_pitch = normalize_text(campaign.description) or default_elevator_pitch

    starting_location = normalize_text(raw_intro.get('starting_location')) or 'A tense local crossroads'
    party_hook = normalize_text(raw_intro.get('party_hook')) or default_party_hook
    tone = raw_intro.get('campaign_tone', [])
    if not isinstance(tone, list):
        tone = [tone]
    campaign_tone = []
    for item in tone:
        text = normalize_text(item)
        if text and text not in campaign_tone:
            campaign_tone.append(text)
        if len(campaign_tone) >= 5:
            break
    if not campaign_tone:
        campaign_tone = ['adventure', 'mystery', 'character-driven stakes']

    intro = {
        'title': title,
        'elevator_pitch': elevator_pitch,
        'starting_location': starting_location,
        'campaign_tone': campaign_tone,
        'party_hook': party_hook,
    }

    # Last-resort leak guard. Prompting should avoid this; this prevents obvious private framing.
    for field in ('elevator_pitch', 'party_hook'):
        lowered = intro[field].lower()
        if any(marker in lowered for marker in PRIVATE_MARKERS):
            intro[field] = (
                'The party is drawn into a volatile local situation where public tensions, personal '
                'histories, and dangerous opportunities are converging.'
            )
    return intro


def _selected_character_entities(campaign):
    if campaign is None or campaign.id is None:
        return []
    members = (
        CampaignMember.query
        .filter_by(campaign_id=campaign.id)
        .filter(CampaignMember.selected_character_id.isnot(None))
        .order_by(CampaignMember.id.asc())
        .all()
    )
    selected_ids = []
    for member in members:
        if (member.role or 'player') == 'spectator':
            continue
        if member.selected_character_id not in selected_ids:
            selected_ids.append(member.selected_character_id)
    if not selected_ids:
        return []

    characters = {
        character.id: character
        for character in Character.query.filter(Character.id.in_(selected_ids)).all()
        if character.campaign_id == campaign.id
    }
    result = []
    for character_id in selected_ids:
        character = characters.get(character_id)
        if character is None or not clean_text(character.name, 160):
            continue
        class_names = [
            clean_text(character_class.class_name, 40)
            for character_class in character.classes
            if clean_text(character_class.class_name, 40)
        ]
        description = ' '.join(
            part for part in (
                clean_text(character.subrace or character.race, 80),
                '/'.join(class_names[:3]),
                'adventurer',
            )
            if part
        )
        result.append({
            'id': clean_id(character.name, f'character_{character.id}'),
            'name': clean_text(character.name, 160),
            'summary': description or 'A player character in the campaign.',
            'visibility': 'public',
            'tags': ['player_character'] + [clean_id(name, '') for name in class_names[:3] if clean_id(name, '')],
        })
    return result


def _normalize_entities_with_reference_map(raw_entities, selected_characters=None):
    entities = raw_entities if isinstance(raw_entities, list) else []
    normalized = []
    seen = set()
    reference_map = {}
    character_specs = selected_characters if isinstance(selected_characters, list) else []
    characters_by_id = {spec['id']: spec for spec in character_specs if spec.get('id')}
    characters_by_name = {
        clean_text(spec.get('name'), 160).casefold(): spec
        for spec in character_specs
        if clean_text(spec.get('name'), 160)
    }
    for index, entity in enumerate(entities):
        if not isinstance(entity, dict):
            continue
        raw_entity_id = clean_id(entity.get('id'), f'entity_{index + 1}')
        entity_name = clean_text(entity.get('name'), 160)
        character_spec = characters_by_id.get(raw_entity_id)
        if character_spec is None and entity_name:
            character_spec = characters_by_name.get(entity_name.casefold())
        entity_id = character_spec['id'] if character_spec else raw_entity_id
        reference_map[raw_entity_id] = entity_id
        if entity_id in seen:
            continue
        seen.add(entity_id)
        tags = [clean_text(tag, 40) for tag in entity.get('tags', [])[:8] if clean_text(tag, 40)] \
            if isinstance(entity.get('tags'), list) else []
        if character_spec and 'player_character' not in tags:
            tags.insert(0, 'player_character')
        normalized.append({
            'id': entity_id,
            'type': 'character' if character_spec else normalize_world_entity_type(entity.get('type')),
            'name': character_spec['name'] if character_spec else (entity_name or entity_id.replace('_', ' ').title()),
            'summary': clean_text(entity.get('summary'), 500) or (character_spec or {}).get('summary', ''),
            'visibility': 'public' if character_spec else (clean_text(entity.get('visibility'), 30) or 'dm_private'),
            'tags': tags,
        })
    for character_spec in character_specs:
        entity_id = character_spec.get('id')
        if not entity_id or entity_id in seen:
            continue
        seen.add(entity_id)
        reference_map[entity_id] = entity_id
        normalized.append({
            'id': entity_id,
            'type': 'character',
            'name': character_spec['name'],
            'summary': character_spec.get('summary', ''),
            'visibility': 'public',
            'tags': list(character_spec.get('tags') or ['player_character']),
        })
    return normalized, reference_map


def normalize_entities(raw_entities, selected_characters=None):
    normalized, _reference_map = _normalize_entities_with_reference_map(raw_entities, selected_characters)
    return normalized


def normalize_relations(raw_relations, reference_map=None):
    relations = raw_relations if isinstance(raw_relations, list) else []
    reference_map = reference_map if isinstance(reference_map, dict) else {}
    normalized = []
    for index, relation in enumerate(relations):
        if not isinstance(relation, dict):
            continue
        normalized.append({
            'id': clean_id(relation.get('id'), f'relation_{index + 1}'),
            'source_id': reference_map.get(
                clean_id(relation.get('source_id'), 'unknown_source'),
                clean_id(relation.get('source_id'), 'unknown_source'),
            ),
            'target_id': reference_map.get(
                clean_id(relation.get('target_id'), 'unknown_target'),
                clean_id(relation.get('target_id'), 'unknown_target'),
            ),
            'type': clean_text(relation.get('type'), 80) or 'related_to',
            'summary': clean_text(relation.get('summary'), 500),
            'visibility': clean_text(relation.get('visibility'), 30) or 'dm_private',
        })
    return normalized


def normalize_facts(raw_facts, reference_map=None):
    facts = raw_facts if isinstance(raw_facts, list) else []
    reference_map = reference_map if isinstance(reference_map, dict) else {}
    normalized = []
    for index, fact in enumerate(facts):
        if not isinstance(fact, dict):
            continue
        entity_ids = fact.get('entity_ids', [])
        if not isinstance(entity_ids, list):
            entity_ids = [entity_ids]
        normalized.append({
            'id': clean_id(fact.get('id'), f'fact_{index + 1}'),
            'entity_ids': [
                reference_map.get(clean_id(entity_id, 'unknown_entity'), clean_id(entity_id, 'unknown_entity'))
                for entity_id in entity_ids[:8]
            ],
            'text': clean_text(fact.get('text'), 700),
            'certainty': clean_text(fact.get('certainty'), 40) or 'confirmed',
            'visibility': clean_text(fact.get('visibility'), 30) or 'dm_private',
        })
    return [fact for fact in normalized if fact['text']]


def normalize_knowledge_graph(raw_graph, selected_characters=None):
    raw_graph = raw_graph if isinstance(raw_graph, dict) else {}
    entities, reference_map = _normalize_entities_with_reference_map(
        raw_graph.get('entities', []),
        selected_characters,
    )
    return {
        'schema_version': '1.0',
        'entities': entities,
        'relations': normalize_relations(raw_graph.get('relations', []), reference_map),
        'facts': normalize_facts(raw_graph.get('facts', []), reference_map),
    }


def normalize_world_state(raw_state, public_intro):
    raw_state = raw_state if isinstance(raw_state, dict) else {}
    current_scene = raw_state.get('current_scene') if isinstance(raw_state.get('current_scene'), dict) else {}
    party = raw_state.get('party') if isinstance(raw_state.get('party'), dict) else {}
    open_threads = raw_state.get('open_threads', [])
    if not isinstance(open_threads, list):
        open_threads = [open_threads]
    return {
        'schema_version': '1.0',
        'current_arc': clean_text(raw_state.get('current_arc'), 160) or public_intro['title'],
        'current_scene': {
            'location_id': clean_id(current_scene.get('location_id'), 'starting_location'),
            'location_name': clean_text(current_scene.get('location_name'), 160) or public_intro['starting_location'],
            'time_of_day': clean_text(current_scene.get('time_of_day'), 80) or 'the opening moments',
            'active_npc_ids': current_scene.get('active_npc_ids', []) if isinstance(current_scene.get('active_npc_ids'), list) else [],
            'immediate_tension': clean_text(current_scene.get('immediate_tension'), 420) or public_intro['party_hook'],
        },
        'party': {
            'known_location_id': clean_id(party.get('known_location_id'), 'starting_location'),
            'public_reputation': clean_text(party.get('public_reputation'), 180) or 'new arrivals',
        },
        'open_threads': [clean_text(thread, 240) for thread in open_threads[:8] if clean_text(thread, 240)],
    }


def normalize_npc_actors(raw_actors, selected_characters=None):
    actors = raw_actors if isinstance(raw_actors, list) else []
    character_specs = selected_characters if isinstance(selected_characters, list) else []
    character_ids = {
        clean_id(spec.get('id'), '')
        for spec in character_specs
        if isinstance(spec, dict) and clean_id(spec.get('id'), '')
    }
    character_names = {
        clean_text(spec.get('name'), 160).casefold()
        for spec in character_specs
        if isinstance(spec, dict) and clean_text(spec.get('name'), 160)
    }
    normalized = []
    seen = set()
    for index, actor in enumerate(actors):
        if not isinstance(actor, dict):
            continue
        actor_id = clean_id(actor.get('id'), f'npc_{index + 1}')
        actor_name = clean_text(actor.get('name'), 160)
        if actor_id in character_ids or (actor_name and actor_name.casefold() in character_names):
            continue
        if actor_id in seen:
            continue
        seen.add(actor_id)
        normalized.append({
            'id': actor_id,
            'name': actor_name or actor_id.replace('_', ' ').title(),
            'role': clean_text(actor.get('role'), 180),
            'public_summary': clean_text(actor.get('public_summary'), 420),
            'voice': clean_text(actor.get('voice'), 240),
            'background': clean_text(actor.get('background'), 700),
            'wants': [clean_text(item, 180) for item in actor.get('wants', [])[:6] if clean_text(item, 180)]
            if isinstance(actor.get('wants'), list) else [],
            'fears': [clean_text(item, 180) for item in actor.get('fears', [])[:6] if clean_text(item, 180)]
            if isinstance(actor.get('fears'), list) else [],
            'secrets': [clean_text(item, 240) for item in actor.get('secrets', [])[:8] if clean_text(item, 240)]
            if isinstance(actor.get('secrets'), list) else [],
            'relationships': actor.get('relationships') if isinstance(actor.get('relationships'), dict) else {},
            'recent_offscreen_activity': actor.get('recent_offscreen_activity')
            if isinstance(actor.get('recent_offscreen_activity'), list) else [],
        })
    return normalized


def build_world_identity_pairs(package):
    """Derive authoritative graph NPC entity <-> npc_actors row pairs.

    World generation produces two persistence records for one fictional
    identity: a knowledge-graph entity (type 'npc') and an npc_actors row,
    using distinct ID namespaces (e.g. `the_candlewright` vs
    `npc_the_candlewright`). The pairing is derived here from the
    authoritative generated package by exact, case-insensitive name match.
    A pair is only recorded when the normalized name is unique on BOTH the
    graph-NPC side and the actor side, so a genuinely one-to-one mapping is
    persisted; ambiguous names are left unpaired rather than guessed.
    """
    graph = package.get('knowledge_graph') if isinstance(package.get('knowledge_graph'), dict) else {}
    entities = graph.get('entities') if isinstance(graph.get('entities'), list) else []
    actors = package.get('npc_actors') if isinstance(package.get('npc_actors'), list) else []
    npc_name_counts = {}
    npc_by_name = {}
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        if clean_text(entity.get('type'), 40).lower() != 'npc':
            continue
        name = clean_text(entity.get('name'), 160).lower()
        if not name:
            continue
        npc_name_counts[name] = npc_name_counts.get(name, 0) + 1
        npc_by_name.setdefault(name, entity)
    actor_name_counts = {}
    for actor in actors:
        if not isinstance(actor, dict):
            continue
        name = clean_text(actor.get('name'), 160).lower()
        if not name:
            continue
        actor_name_counts[name] = actor_name_counts.get(name, 0) + 1
    pairs = []
    seen_actors = set()
    for actor in actors:
        if not isinstance(actor, dict):
            continue
        actor_id = clean_id(actor.get('id'), '')
        actor_name = clean_text(actor.get('name'), 160).lower()
        if not actor_id or not actor_name or actor_id in seen_actors:
            continue
        if actor_name_counts.get(actor_name, 0) != 1:
            continue
        if npc_name_counts.get(actor_name, 0) != 1:
            continue
        entity = npc_by_name.get(actor_name)
        if entity is not None and clean_id(entity.get('id'), ''):
            pairs.append({
                'graph_entity_id': entity['id'],
                'actor_id': actor_id,
            })
            seen_actors.add(actor_id)
    return pairs


def persist_world_identity_pairs(campaign, pairs):
    """Replace the campaign's authoritative graph<->actor identity mapping."""
    CampaignWorldIdentity.query.filter_by(campaign_id=campaign.id).delete()
    for pair in pairs:
        graph_entity_id = clean_id(pair.get('graph_entity_id'), '')
        actor_id = clean_id(pair.get('actor_id'), '')
        if not graph_entity_id or not actor_id:
            continue
        db.session.add(CampaignWorldIdentity(
            campaign_id=campaign.id,
            graph_entity_id=graph_entity_id,
            actor_id=actor_id,
        ))


def normalize_clocks(raw_clocks):
    clocks = raw_clocks if isinstance(raw_clocks, list) else []
    normalized = []
    seen = set()
    for index, clock in enumerate(clocks):
        if not isinstance(clock, dict):
            continue
        clock_id = clean_id(clock.get('id'), f'clock_{index + 1}')
        if clock_id in seen:
            continue
        seen.add(clock_id)
        try:
            segments = int(clock.get('segments', 4))
        except (TypeError, ValueError):
            segments = 4
        segments = min(max(segments, 2), 12)
        try:
            filled = int(clock.get('filled', 0))
        except (TypeError, ValueError):
            filled = 0
        normalized.append({
            'id': clock_id,
            'name': clean_text(clock.get('name'), 180) or clock_id.replace('_', ' ').title(),
            'segments': segments,
            'filled': min(max(filled, 0), segments),
            'pressure_type': clean_text(clock.get('pressure_type'), 80) or 'story',
            'visibility': clean_text(clock.get('visibility'), 30) or 'dm_private',
            'summary': clean_text(clock.get('summary'), 420),
            'trigger': clean_text(clock.get('trigger'), 420),
            'on_complete': clean_text(clock.get('on_complete'), 520),
            'completion_criteria': clock.get('completion_criteria') if isinstance(clock.get('completion_criteria'), list) else [],
            'completion_state': clock.get('completion_state') if isinstance(clock.get('completion_state'), dict) else {},
            'status': clean_text(clock.get('status'), 30) or 'active',
        })
    return normalized


def normalize_dm_private(raw_private):
    raw_private = raw_private if isinstance(raw_private, dict) else {}
    rules = raw_private.get('authorized_rules') if isinstance(raw_private.get('authorized_rules'), list) else []
    normalized_rules = []
    for rule in rules[:40]:
        if isinstance(rule, dict):
            rule_id = clean_id(rule.get('id') or rule.get('rule_id'), '')
            description = clean_text(rule.get('description'), 240)
        else:
            rule_id = clean_id(rule, '')
            description = ''
        if rule_id:
            normalized_rules.append({'id': rule_id, 'description': description})
    return {
        'schema_version': '1.0',
        'true_inciting_incident': clean_text(raw_private.get('true_inciting_incident'), 900),
        'villain_plan': clean_text(raw_private.get('villain_plan'), 900),
        'hidden_factions': raw_private.get('hidden_factions') if isinstance(raw_private.get('hidden_factions'), list) else [],
        'npc_secrets': raw_private.get('npc_secrets') if isinstance(raw_private.get('npc_secrets'), list) else [],
        'opening_scene_private_notes': clean_text(raw_private.get('opening_scene_private_notes'), 900),
        'authorized_rules': normalized_rules,
    }


def normalize_world_package(raw_package, campaign):
    raw_package = raw_package if isinstance(raw_package, dict) else {}
    public_intro = sanitize_public_intro(raw_package.get('public_intro'), campaign)
    selected_characters = _selected_character_entities(campaign)
    return {
        'public_intro': public_intro,
        'knowledge_graph': normalize_knowledge_graph(
            raw_package.get('knowledge_graph'),
            selected_characters,
        ),
        'world_state': normalize_world_state(raw_package.get('world_state'), public_intro),
        'dm_private': normalize_dm_private(raw_package.get('dm_private')),
        'npc_actors': normalize_npc_actors(raw_package.get('npc_actors'), selected_characters),
        'clocks': normalize_clocks(raw_package.get('clocks')),
    }


def get_campaign_world(campaign_id):
    return CampaignWorld.query.filter_by(campaign_id=campaign_id).first()


def world_generation_status(world):
    if not world:
        return None
    public_intro = json_loads(world.public_intro, {})
    if not isinstance(public_intro, dict):
        return None
    return public_intro.get('generation_status')


def world_generation_in_progress(world):
    return world_generation_status(world) == 'building'


def world_generation_is_stale(world):
    if not world_generation_in_progress(world):
        return False
    updated_at = world.updated_at or world.created_at
    if not updated_at:
        return False
    return utcnow() - updated_at > WORLD_GENERATION_STALE_AFTER


def clear_world_generation_claim(campaign_id):
    """Remove a placeholder left behind by an interrupted generation attempt."""
    placeholder = get_campaign_world(campaign_id)
    if not world_generation_in_progress(placeholder):
        return False
    db.session.delete(placeholder)
    db.session.commit()
    return True


def world_public_payload(campaign, clean_ready_states=True):
    world = get_campaign_world(campaign.id)
    if world_generation_in_progress(world):
        if world_generation_is_stale(world):
            clear_world_generation_claim(campaign.id)
            world = None
        else:
            return {
                'world': None,
                'is_ready': False,
                'can_generate': False,
                'generation_in_progress': True,
                'planning': None,
            }

    if not world:
        ready, details = can_start_session(campaign, clean_ready_states=clean_ready_states)
        return {
            'world': None,
            'is_ready': False,
            'can_generate': ready,
            'generation_in_progress': False,
            'planning': details,
        }

    return {
        'world': world.to_public_dict(),
        'is_ready': True,
        'can_generate': False,
        'generation_in_progress': False,
        'planning': None,
    }


def persist_world_package(campaign, package):
    world = get_campaign_world(campaign.id)
    if not world:
        world = CampaignWorld(campaign_id=campaign.id, public_intro='{}', knowledge_graph='{}', world_state='{}', dm_private='{}')
        db.session.add(world)

    world.public_intro = json_dumps(package['public_intro'])
    world.knowledge_graph = json_dumps(package['knowledge_graph'])
    world.world_state = json_dumps(package['world_state'])
    world.dm_private = json_dumps(package['dm_private'])
    world.updated_at = utcnow()
    db.session.flush()
    for entity in package['knowledge_graph'].get('entities', []):
        upsert_memory_embedding(campaign, 'entity', entity.get('id'), entity)
    for relation in package['knowledge_graph'].get('relations', []):
        upsert_memory_embedding(campaign, 'relation', relation.get('id'), relation)
    for fact in package['knowledge_graph'].get('facts', []):
        upsert_memory_embedding(campaign, 'fact', fact.get('id'), fact)
    upsert_memory_embedding(campaign, 'world_state', 'current', package['world_state'])
    upsert_memory_embedding(campaign, 'dm_private', 'current', package['dm_private'])
    log_audit_event(
        campaign.id,
        'knowledge_graph_write',
        'Persisted generated world package and knowledge graph.',
        {
            'world_id': world.id,
            'public_intro': package['public_intro'],
            'knowledge_graph': package['knowledge_graph'],
            'world_state': package['world_state'],
            'dm_private': package['dm_private'],
        },
        source='campaign_worlds',
        actor='world_architect',
        commit=False,
    )

    NPCActor.query.filter_by(campaign_id=campaign.id).delete()
    for actor in package['npc_actors']:
        db.session.add(NPCActor(
            campaign_id=campaign.id,
            actor_id=actor['id'],
            name=actor['name'],
            role=actor.get('role'),
            public_summary=actor.get('public_summary'),
            dossier=json_dumps(actor),
        ))
        upsert_memory_embedding(campaign, 'npc_actor', actor['id'], actor)
    log_audit_event(
        campaign.id,
        'npc_actor_write',
        'Persisted generated NPC actor dossiers.',
        {'npc_actors': package['npc_actors']},
        source='npc_actors',
        actor='world_architect',
        commit=False,
    )

    persist_world_identity_pairs(campaign, build_world_identity_pairs(package))

    CampaignClock.query.filter_by(campaign_id=campaign.id).delete()
    for clock in package['clocks']:
        db.session.add(CampaignClock(
            campaign_id=campaign.id,
            clock_id=clock['id'],
            name=clock['name'],
            entity_ids=clock.get('entity_ids'),
            location_ids=clock.get('location_ids'),
            segments=clock['segments'],
            filled=clock['filled'],
            pressure_type=clock.get('pressure_type'),
            visibility=clock.get('visibility') or 'dm_private',
            summary=clock.get('summary'),
            trigger=clock.get('trigger'),
            on_complete=clock.get('on_complete'),
            completion_criteria=clock.get('completion_criteria') or [],
            completion_state=clock.get('completion_state') or {},
            status=clock.get('status') or 'active',
        ))
        upsert_memory_embedding(campaign, 'clock', clock['id'], clock)
    log_audit_event(
        campaign.id,
        'clock_write',
        'Persisted generated campaign clocks.',
        {'clocks': package['clocks']},
        source='campaign_clocks',
        actor='world_architect',
        commit=False,
    )

    world_event = WorldEvent(
        campaign_id=campaign.id,
        event_type='world_generated',
        summary=f'World package generated for {campaign.name}.',
        payload=json_dumps({
            'public_intro': package['public_intro'],
            'npc_actor_ids': [actor['id'] for actor in package['npc_actors']],
            'clock_ids': [clock['id'] for clock in package['clocks']],
        }),
        visibility='dm_private',
    )
    db.session.add(world_event)
    db.session.flush()
    upsert_memory_embedding(campaign, 'world_event', str(world_event.id), world_event.to_dict(include_private=True))
    log_audit_event(
        campaign.id,
        'world_event_write',
        'Persisted world_generated event.',
        {
            'event_type': 'world_generated',
            'summary': f'World package generated for {campaign.name}.',
        },
        source='world_events',
        actor='world_architect',
        commit=False,
    )
    return world


def claim_world_generation(campaign):
    existing = get_campaign_world(campaign.id)
    if existing and not world_generation_in_progress(existing):
        return existing, None
    if existing and world_generation_is_stale(existing):
        db.session.delete(existing)
        db.session.flush()
    elif existing:
        return None, {
            'error': 'Campaign world generation is already in progress',
            'generation_in_progress': True,
            'status': 409,
        }

    world = CampaignWorld(
        campaign_id=campaign.id,
        public_intro=json_dumps({
            'generation_status': 'building',
            'title': campaign.name,
        }),
        knowledge_graph='{}',
        world_state='{}',
        dm_private='{}',
    )
    db.session.add(world)
    try:
        db.session.commit()
        return None, None
    except IntegrityError:
        db.session.rollback()
        existing = get_campaign_world(campaign.id)
        if existing and not world_generation_in_progress(existing):
            return existing, None
        return None, {
            'error': 'Campaign world generation is already in progress',
            'generation_in_progress': True,
            'status': 409,
        }


def ensure_world_generated(campaign, current_user):
    existing = get_campaign_world(campaign.id)
    if existing:
        if world_generation_in_progress(existing):
            if world_generation_is_stale(existing):
                db.session.delete(existing)
                db.session.commit()
            else:
                return None, {
                    'error': 'Campaign world generation is already in progress',
                    'generation_in_progress': True,
                    'status': 409,
                }
        else:
            return existing, None

    claimed_world, claim_error = claim_world_generation(campaign)
    if claimed_world or claim_error:
        return claimed_world, claim_error

    campaign_id = campaign.id
    try:
        campaign = db.session.get(type(campaign), campaign_id)
        existing = get_campaign_world(campaign_id)
        if existing and not world_generation_in_progress(existing):
            return existing, None

        ready, details = can_start_session(campaign)
        if not ready:
            clear_world_generation_claim(campaign_id)
            return None, {
                'error': 'Every party member must select and ready a character before building the world',
                'planning': details,
                'status': 400,
            }

        context = planning_context(campaign, current_user)
        log_audit_event(
            campaign_id,
            'planning_context_read',
            'Read planning context for world generation.',
            {'context': context},
            source='planning_context',
            actor='server',
            commit=True,
        )
        raw_package = get_world_genesis_package(
            context,
            audit_context={
                'campaign_id': campaign_id,
                'operation': 'world_genesis',
                'actor': 'world_architect',
            },
        )
        if not raw_package:
            clear_world_generation_claim(campaign_id)
            return None, {
                'error': 'The DM could not build the world package',
                'status': 500,
            }

        package = normalize_world_package(raw_package, campaign)
        world = persist_world_package(campaign, package)
        db.session.commit()
        return world, None
    except Exception:
        db.session.rollback()
        try:
            clear_world_generation_claim(campaign_id)
        except Exception:
            db.session.rollback()
        raise


def approve_world(world):
    if world.approved_at is None:
        world.approved_at = utcnow()
        world.updated_at = utcnow()


def dm_world_context(campaign, audit=False, reason='dm_world_context', audit_context=None):
    audit_context = audit_context or {}
    world = get_campaign_world(campaign.id)
    if not world:
        if audit:
            log_audit_event(
                campaign.id,
                'knowledge_graph_read',
                'Attempted to read DM world context, but no world package exists.',
                {'reason': reason, 'world': None},
                source='campaign_worlds',
                actor='server',
                trace_id=audit_context.get('trace_id'),
                parent_trace_id=audit_context.get('parent_trace_id'),
                trace_label=audit_context.get('trace_label'),
                commit=True,
            )
        return None

    npcs = NPCActor.query.filter_by(campaign_id=campaign.id).order_by(NPCActor.id.asc()).all()
    clocks = CampaignClock.query.filter_by(campaign_id=campaign.id).order_by(CampaignClock.id.asc()).all()
    recent_events = WorldEvent.query.filter_by(campaign_id=campaign.id).order_by(
        WorldEvent.created_at.desc(),
    ).limit(12).all()

    context = {
        'public_intro': json_loads(world.public_intro, {}),
        'knowledge_graph': json_loads(world.knowledge_graph, {}),
        'world_state': json_loads(world.world_state, {}),
        'dm_private': json_loads(world.dm_private, {}),
        'npc_actors': [npc.to_dict(include_private=True) for npc in npcs],
        'clocks': [clock.to_dict(include_private=True) for clock in clocks],
        'recent_events': [event.to_dict(include_private=True) for event in reversed(recent_events)],
    }
    if audit:
        log_audit_event(
            campaign.id,
            'knowledge_graph_read',
            'Read DM world context for model input.',
            {'reason': reason, 'context': context},
            source='campaign_worlds',
            actor='server',
            trace_id=audit_context.get('trace_id'),
            parent_trace_id=audit_context.get('parent_trace_id'),
            trace_label=audit_context.get('trace_label'),
            commit=True,
        )
    return context

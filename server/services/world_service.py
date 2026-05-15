import json
from datetime import datetime

from models import (
    db,
    CampaignClock,
    CampaignWorld,
    NPCActor,
    WorldEvent,
)
from openrouter import get_world_genesis_package
from services.audit_service import log_audit_event
from services.planning_service import can_start_session, planning_context


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


def clean_id(value, fallback):
    raw = clean_text(value, 100).lower()
    safe = ''.join(ch if ch.isalnum() else '_' for ch in raw).strip('_')
    while '__' in safe:
        safe = safe.replace('__', '_')
    return safe or fallback


def sanitize_public_intro(raw_intro, campaign):
    raw_intro = raw_intro if isinstance(raw_intro, dict) else {}
    title = clean_text(raw_intro.get('title'), 90) or clean_text(campaign.name, 90) or 'Untitled Campaign'
    elevator_pitch = clean_text(raw_intro.get('elevator_pitch'), 420)
    if not elevator_pitch:
        elevator_pitch = clean_text(campaign.description, 420) or (
            'A newly formed party arrives at the edge of an unfolding adventure, where local trouble '
            'and personal stakes are already beginning to collide.'
        )

    starting_location = clean_text(raw_intro.get('starting_location'), 120) or 'A tense local crossroads'
    party_hook = clean_text(raw_intro.get('party_hook'), 360) or (
        'The party begins together as a nearby situation demands quick choices and reveals larger stakes.'
    )
    tone = raw_intro.get('campaign_tone', [])
    if not isinstance(tone, list):
        tone = [tone]
    campaign_tone = []
    for item in tone:
        text = clean_text(item, 40)
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


def normalize_entities(raw_entities):
    entities = raw_entities if isinstance(raw_entities, list) else []
    normalized = []
    seen = set()
    for index, entity in enumerate(entities):
        if not isinstance(entity, dict):
            continue
        entity_id = clean_id(entity.get('id'), f'entity_{index + 1}')
        if entity_id in seen:
            continue
        seen.add(entity_id)
        normalized.append({
            'id': entity_id,
            'type': clean_text(entity.get('type'), 40) or 'other',
            'name': clean_text(entity.get('name'), 160) or entity_id.replace('_', ' ').title(),
            'summary': clean_text(entity.get('summary'), 500),
            'visibility': clean_text(entity.get('visibility'), 30) or 'dm_private',
            'tags': [clean_text(tag, 40) for tag in entity.get('tags', [])[:8] if clean_text(tag, 40)]
            if isinstance(entity.get('tags'), list) else [],
        })
    return normalized


def normalize_relations(raw_relations):
    relations = raw_relations if isinstance(raw_relations, list) else []
    normalized = []
    for index, relation in enumerate(relations):
        if not isinstance(relation, dict):
            continue
        normalized.append({
            'id': clean_id(relation.get('id'), f'relation_{index + 1}'),
            'source_id': clean_id(relation.get('source_id'), 'unknown_source'),
            'target_id': clean_id(relation.get('target_id'), 'unknown_target'),
            'type': clean_text(relation.get('type'), 80) or 'related_to',
            'summary': clean_text(relation.get('summary'), 500),
            'visibility': clean_text(relation.get('visibility'), 30) or 'dm_private',
        })
    return normalized


def normalize_facts(raw_facts):
    facts = raw_facts if isinstance(raw_facts, list) else []
    normalized = []
    for index, fact in enumerate(facts):
        if not isinstance(fact, dict):
            continue
        entity_ids = fact.get('entity_ids', [])
        if not isinstance(entity_ids, list):
            entity_ids = [entity_ids]
        normalized.append({
            'id': clean_id(fact.get('id'), f'fact_{index + 1}'),
            'entity_ids': [clean_id(entity_id, 'unknown_entity') for entity_id in entity_ids[:8]],
            'text': clean_text(fact.get('text'), 700),
            'certainty': clean_text(fact.get('certainty'), 40) or 'confirmed',
            'visibility': clean_text(fact.get('visibility'), 30) or 'dm_private',
        })
    return [fact for fact in normalized if fact['text']]


def normalize_knowledge_graph(raw_graph):
    raw_graph = raw_graph if isinstance(raw_graph, dict) else {}
    return {
        'schema_version': '1.0',
        'entities': normalize_entities(raw_graph.get('entities', [])),
        'relations': normalize_relations(raw_graph.get('relations', [])),
        'facts': normalize_facts(raw_graph.get('facts', [])),
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


def normalize_npc_actors(raw_actors):
    actors = raw_actors if isinstance(raw_actors, list) else []
    normalized = []
    seen = set()
    for index, actor in enumerate(actors):
        if not isinstance(actor, dict):
            continue
        actor_id = clean_id(actor.get('id'), f'npc_{index + 1}')
        if actor_id in seen:
            continue
        seen.add(actor_id)
        normalized.append({
            'id': actor_id,
            'name': clean_text(actor.get('name'), 160) or actor_id.replace('_', ' ').title(),
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
            'status': clean_text(clock.get('status'), 30) or 'active',
        })
    return normalized


def normalize_dm_private(raw_private):
    raw_private = raw_private if isinstance(raw_private, dict) else {}
    return {
        'schema_version': '1.0',
        'true_inciting_incident': clean_text(raw_private.get('true_inciting_incident'), 900),
        'villain_plan': clean_text(raw_private.get('villain_plan'), 900),
        'hidden_factions': raw_private.get('hidden_factions') if isinstance(raw_private.get('hidden_factions'), list) else [],
        'npc_secrets': raw_private.get('npc_secrets') if isinstance(raw_private.get('npc_secrets'), list) else [],
        'opening_scene_private_notes': clean_text(raw_private.get('opening_scene_private_notes'), 900),
    }


def normalize_world_package(raw_package, campaign):
    raw_package = raw_package if isinstance(raw_package, dict) else {}
    public_intro = sanitize_public_intro(raw_package.get('public_intro'), campaign)
    return {
        'public_intro': public_intro,
        'knowledge_graph': normalize_knowledge_graph(raw_package.get('knowledge_graph')),
        'world_state': normalize_world_state(raw_package.get('world_state'), public_intro),
        'dm_private': normalize_dm_private(raw_package.get('dm_private')),
        'npc_actors': normalize_npc_actors(raw_package.get('npc_actors')),
        'clocks': normalize_clocks(raw_package.get('clocks')),
    }


def get_campaign_world(campaign_id):
    return CampaignWorld.query.filter_by(campaign_id=campaign_id).first()


def world_public_payload(campaign):
    world = get_campaign_world(campaign.id)
    if not world:
        ready, details = can_start_session(campaign)
        return {
            'world': None,
            'is_ready': False,
            'can_generate': ready,
            'planning': details,
        }

    return {
        'world': world.to_public_dict(),
        'is_ready': True,
        'can_generate': False,
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
    world.updated_at = datetime.utcnow()
    db.session.flush()
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
    log_audit_event(
        campaign.id,
        'npc_actor_write',
        'Persisted generated NPC actor dossiers.',
        {'npc_actors': package['npc_actors']},
        source='npc_actors',
        actor='world_architect',
        commit=False,
    )

    CampaignClock.query.filter_by(campaign_id=campaign.id).delete()
    for clock in package['clocks']:
        db.session.add(CampaignClock(
            campaign_id=campaign.id,
            clock_id=clock['id'],
            name=clock['name'],
            segments=clock['segments'],
            filled=clock['filled'],
            pressure_type=clock.get('pressure_type'),
            visibility=clock.get('visibility') or 'dm_private',
            summary=clock.get('summary'),
            trigger=clock.get('trigger'),
            on_complete=clock.get('on_complete'),
            status=clock.get('status') or 'active',
        ))
    log_audit_event(
        campaign.id,
        'clock_write',
        'Persisted generated campaign clocks.',
        {'clocks': package['clocks']},
        source='campaign_clocks',
        actor='world_architect',
        commit=False,
    )

    db.session.add(WorldEvent(
        campaign_id=campaign.id,
        event_type='world_generated',
        summary=f'World package generated for {campaign.name}.',
        payload=json_dumps({
            'public_intro': package['public_intro'],
            'npc_actor_ids': [actor['id'] for actor in package['npc_actors']],
            'clock_ids': [clock['id'] for clock in package['clocks']],
        }),
        visibility='dm_private',
    ))
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


def ensure_world_generated(campaign, current_user):
    existing = get_campaign_world(campaign.id)
    if existing:
        return existing, None

    ready, details = can_start_session(campaign)
    if not ready:
        return None, {
            'error': 'Every party member must select and ready a character before building the world',
            'planning': details,
            'status': 400,
        }

    context = planning_context(campaign, current_user)
    log_audit_event(
        campaign.id,
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
            'campaign_id': campaign.id,
            'operation': 'world_genesis',
            'actor': 'world_architect',
        },
    )
    if not raw_package:
        return None, {
            'error': 'The DM could not build the world package',
            'status': 500,
        }

    package = normalize_world_package(raw_package, campaign)
    world = persist_world_package(campaign, package)
    db.session.commit()
    return world, None


def approve_world(world):
    if world.approved_at is None:
        world.approved_at = datetime.utcnow()
        world.updated_at = datetime.utcnow()


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

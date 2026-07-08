import json
from services.world_service import clean_id, clean_text, get_campaign_world, json_loads

def resolve_scene_location_patch(scene_patch, campaign, current_scene):
    if not isinstance(scene_patch, dict):
        return {}

    proposed_id = clean_id(scene_patch.get('location_id'), '') if scene_patch.get('location_id') is not None else None
    proposed_name = clean_text(scene_patch.get('location_name'), 160) if scene_patch.get('location_name') is not None else None

    # If neither is proposed, this is a no-op: preserve by omission.
    if not proposed_id and not proposed_name:
        return {}

    current_scene = current_scene if isinstance(current_scene, dict) else {}
    current_id = clean_id(current_scene.get('location_id'), '')
    current_name = clean_text(current_scene.get('location_name'), 160)

    # If the proposed fields exactly match the current ones, it's also a no-op.
    if proposed_id == current_id and proposed_name == current_name:
        return {}

    # Build canonical locations from:
    # 1. Knowledge graph entities of type 'location'
    # 2. The current scene location, if it has both location_id and location_name.
    canonical_locations = []

    # 1. KG entities
    world = get_campaign_world(campaign.id) if campaign else None
    if world:
        kg = json_loads(world.knowledge_graph, {'entities': [], 'relations': [], 'facts': []})
        for entity in kg.get('entities', []):
            if isinstance(entity, dict) and clean_text(entity.get('type'), 40).lower() == 'location':
                eid = clean_id(entity.get('id'), '')
                ename = clean_text(entity.get('name'), 160)
                if eid and ename:
                    canonical_locations.append({'id': eid, 'name': ename})

    # 2. Current scene
    if current_id and current_name:
        if not any(loc['id'] == current_id for loc in canonical_locations):
            canonical_locations.append({'id': current_id, 'name': current_name})

    # Try to resolve the proposed fields.
    resolved = None

    for loc in canonical_locations:
        loc_id = loc['id']
        loc_name = loc['name']

        # Case A: both proposed
        if proposed_id and proposed_name:
            if proposed_id == loc_id and proposed_name.lower() == loc_name.lower():
                resolved = loc
                break
        # Case B: only ID proposed
        elif proposed_id and not proposed_name:
            if proposed_id == loc_id:
                resolved = loc
                break
        # Case C: only name proposed
        elif proposed_name and not proposed_id:
            if proposed_name.lower() == loc_name.lower():
                resolved = loc
                break

    if resolved:
        return {
            'location_id': resolved['id'],
            'location_name': resolved['name']
        }

    return None

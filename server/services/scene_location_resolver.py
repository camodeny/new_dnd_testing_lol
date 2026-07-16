from services.world_service import clean_id, clean_text, get_campaign_world, json_loads


def resolve_scene_location_patch(scene_patch, campaign, current_scene):
    if not isinstance(scene_patch, dict):
        return {"status": "noop"}

    proposed_id = clean_id(scene_patch.get("location_id"), "") if scene_patch.get("location_id") is not None else None
    proposed_name = clean_text(scene_patch.get("location_name"), 160) if scene_patch.get("location_name") is not None else None

    if not proposed_id and not proposed_name:
        return {"status": "noop"}

    current_scene = current_scene if isinstance(current_scene, dict) else {}
    current_id = clean_id(current_scene.get("location_id"), "")
    current_name = clean_text(current_scene.get("location_name"), 160)

    if proposed_id == current_id and proposed_name == current_name:
        return {"status": "noop"}

    canonical_locations = []

    world = get_campaign_world(campaign.id) if campaign else None
    if world:
        kg = json_loads(world.knowledge_graph, {"entities": [], "relations": [], "facts": []})
        for entity in kg.get("entities", []):
            if isinstance(entity, dict) and clean_text(entity.get("type"), 40).lower() == "location":
                eid = clean_id(entity.get("id"), "")
                ename = clean_text(entity.get("name"), 160)
                if eid and ename:
                    canonical_locations.append({"id": eid, "name": ename})

    if current_id and current_name:
        if not any(loc["id"] == current_id for loc in canonical_locations):
            canonical_locations.append({"id": current_id, "name": current_name})

    resolved = None
    for loc in canonical_locations:
        loc_id = loc["id"]
        loc_name = loc["name"]

        if proposed_id and proposed_name:
            if proposed_id == loc_id and proposed_name.lower() == loc_name.lower():
                resolved = loc
                break
        elif proposed_id and not proposed_name:
            if proposed_id == loc_id:
                resolved = loc
                break
        elif proposed_name and not proposed_id:
            if proposed_name.lower() == loc_name.lower():
                resolved = loc
                break

    if resolved:
        return {
            "status": "canonical",
            "location_id": resolved["id"],
            "location_name": resolved["name"],
        }

    if proposed_id == current_id and proposed_name and proposed_name != current_name:
        return {
            "status": "direct",
            "location_id": current_id,
            "location_name": proposed_name,
        }

    if proposed_name and not proposed_id:
        name_lower = proposed_name.lower()
        for loc in canonical_locations:
            if loc.get("name", "").lower() == name_lower:
                continue
        return {
            "status": "new",
            "location_id": proposed_id or clean_id(proposed_name.lower().replace(" ", "_"), ""),
            "location_name": proposed_name,
        }

    if proposed_id and proposed_name:
        return {
            "status": "new",
            "location_id": proposed_id,
            "location_name": proposed_name,
        }

    return {
        "status": "unresolved",
        "location_id": proposed_id,
        "location_name": proposed_name,
    }

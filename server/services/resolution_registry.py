"""Canonical session-memory identity resolver.

The core module contains the original registry mechanics.  This facade owns the
semantic safety rules that depend on durable campaign types and clarification
actions, while keeping the public import surface stable.
"""

from services import resolution_registry_core as _core
from services.world_service import clean_id, clean_text, get_campaign_world, json_loads


# Preserve the existing public/private import surface for callers and tests.
for _name, _value in vars(_core).items():
    if not _name.startswith("__"):
        globals()[_name] = _value


def _enrich_known_ids_with_entity_types(campaign, known_ids):
    enriched = dict(known_ids) if isinstance(known_ids, dict) else {}
    enriched["entity_ids"] = set(enriched.get("entity_ids", set()))
    enriched["npc_ids"] = set(enriched.get("npc_ids", set()))
    enriched["entity_names"] = dict(enriched.get("entity_names", {}))
    enriched["npc_names"] = dict(enriched.get("npc_names", {}))

    entity_types = dict(enriched.get("entity_types", {}))
    for npc_id in enriched["npc_ids"]:
        entity_types[npc_id] = "npc"

    world = get_campaign_world(campaign.id) if campaign else None
    graph = (
        json_loads(world.knowledge_graph, {"entities": []})
        if world is not None
        else {"entities": []}
    )
    entities = graph.get("entities") if isinstance(graph, dict) else None
    if not isinstance(entities, list):
        entities = []

    for entity in entities:
        if not isinstance(entity, dict):
            continue
        entity_id = clean_id(entity.get("id"), "")
        if not entity_id:
            continue
        entity_type = clean_text(entity.get("type"), 40).lower() or "other"
        entity_types[entity_id] = (
            "npc" if entity_id in enriched["npc_ids"] else entity_type
        )

    enriched["entity_types"] = entity_types
    return enriched


def _canonical_entity_type(known_ids, canonical_id):
    if canonical_id in known_ids.get("npc_ids", set()):
        return "npc"
    return known_ids.get("entity_types", {}).get(canonical_id, "other") or "other"


def _entity_types_compatible(expected_type, candidate_type):
    expected = clean_text(expected_type, 40).lower() or "other"
    candidate = clean_text(candidate_type, 40).lower() or "other"

    if expected in ("npc", "person"):
        return candidate in ("npc", "person")
    if expected in ("entity", "other"):
        return candidate not in ("npc", "person")
    return expected == candidate


def find_all_matching_candidates(name, known_ids, prior_resolutions, expected_type):
    candidates = {}
    name_lower = clean_text(name, 400).strip().lower()
    if not name_lower:
        return candidates

    for npc_id, npc_name in known_ids.get("npc_names", {}).items():
        if (
            clean_text(npc_name, 400).strip().lower() == name_lower
            and _entity_types_compatible(expected_type, "npc")
        ):
            candidates[npc_id] = "npc"

    for entity_id, entity_name in known_ids.get("entity_names", {}).items():
        candidate_type = _canonical_entity_type(known_ids, entity_id)
        if (
            clean_text(entity_name, 400).strip().lower() == name_lower
            and _entity_types_compatible(expected_type, candidate_type)
        ):
            candidates[entity_id] = candidate_type

    if isinstance(prior_resolutions, list):
        for resolution in prior_resolutions:
            if (
                not isinstance(resolution, dict)
                or resolution.get("resolution_action") != "add_alias"
            ):
                continue
            alias_name = clean_text(resolution.get("mention_name"), 400)
            canonical_id = clean_id(resolution.get("canonical_id"), "")
            if (
                not alias_name
                or alias_name.strip().lower() != name_lower
                or not canonical_id
                or (
                    canonical_id not in known_ids.get("entity_ids", set())
                    and canonical_id not in known_ids.get("npc_ids", set())
                )
            ):
                continue
            candidate_type = _canonical_entity_type(known_ids, canonical_id)
            if _entity_types_compatible(expected_type, candidate_type):
                candidates[canonical_id] = candidate_type

    return candidates


def _resolve_entity_claim(
    claim,
    known_ids,
    prior_resolution_map,
    allocated_ids,
    index,
):
    name = clean_text(claim.get("name"), 160) or clean_text(
        claim.get("surface_form"), 160
    )
    mention_ref = clean_id(claim.get("mention_ref"), "") or f"entity_{index}"
    entity_type = clean_text(claim.get("type"), 40).lower() or "other"

    entry = {
        "mention_ref": mention_ref,
        "surface_form": name,
        "identity_status": "provisional_unknown",
        "visibility": "party_known",
        "evidence": [],
        "canonical_id": None,
        "canonical_name": None,
        "decision": None,
        "blocked_operations": [],
        "resolution_state": None,
        "entity_type": entity_type,
    }

    prior = prior_resolution_map.get(mention_ref) or prior_resolution_map.get(
        name.lower()
    )
    if prior and isinstance(prior, dict):
        canonical_id = clean_id(prior.get("canonical_id"), "")
        canonical_name = clean_text(prior.get("canonical_name"), 200)
        canonical_type = _canonical_entity_type(known_ids, canonical_id)
        if (
            canonical_id
            and canonical_id in known_ids.get("entity_ids", set())
            and _entity_types_compatible(entity_type, canonical_type)
        ):
            entry["canonical_id"] = canonical_id
            entry["canonical_name"] = (
                known_ids.get("entity_names", {}).get(canonical_id)
                or known_ids.get("npc_names", {}).get(canonical_id)
                or canonical_name
                or name
            )
            entry["entity_type"] = canonical_type
            entry["decision"] = "reuse_existing"
            entry["resolution_state"] = "resolved"
            entry["identity_status"] = (
                "known_hidden"
                if prior.get("visibility") == "dm_private"
                else "known_public"
            )
            entry["evidence"].append(
                {
                    "source": "prior_durable_memory",
                    "field": "identity_resolution_record",
                }
            )
            return entry

    prior_list = (
        list(prior_resolution_map.values())
        if isinstance(prior_resolution_map, dict)
        else prior_resolution_map
    )
    candidates = find_all_matching_candidates(
        name,
        known_ids,
        prior_list,
        expected_type=entity_type,
    )
    if len(candidates) > 1:
        entry["candidate_ids"] = sorted(candidates)
        entry["decision"] = "request_clarification"
        entry["resolution_state"] = "clarification_requested"
        entry["blocked_operations"] = ["identity_merge", "npc_update"]
        return entry

    if len(candidates) == 1:
        canonical_id, canonical_type = next(iter(candidates.items()))
        existing_name = (
            known_ids.get("entity_names", {}).get(canonical_id)
            or known_ids.get("npc_names", {}).get(canonical_id)
        )
        entry["canonical_id"] = canonical_id
        entry["canonical_name"] = existing_name or name
        entry["entity_type"] = canonical_type
        entry["decision"] = (
            "add_alias"
            if existing_name and name.lower() != existing_name.lower()
            else "reuse_existing"
        )
        entry["resolution_state"] = "resolved"
        entry["identity_status"] = "known_public"
        entry["evidence"].append(
            {"source": "prior_durable_memory", "field": "entity_name_match"}
        )
        return entry

    entry["canonical_id"] = allocate_durable_id(name, allocated_ids)
    entry["canonical_name"] = name
    entry["decision"] = "create_provisional"
    entry["resolution_state"] = "provisional"
    entry["evidence"].append(
        {"source": "visible_transcript", "field": "surface_form_claim"}
    )
    return entry


def _clarification_patch_entity_type(pending_clarification):
    patch = pending_clarification.get("resolution_patch_json")
    if not isinstance(patch, dict):
        patch = pending_clarification.get("resolution_patch")
    if not isinstance(patch, dict):
        return "other"

    npc_updates = patch.get("update_npc_actors")
    if isinstance(npc_updates, list) and any(
        isinstance(item, dict) for item in npc_updates
    ):
        return "npc"

    graph_entities = patch.get("upsert_graph_entities")
    if isinstance(graph_entities, list):
        for entity in graph_entities:
            if not isinstance(entity, dict):
                continue
            entity_type = clean_text(entity.get("type"), 40).lower()
            if entity_type:
                return entity_type
    return "other"


def _apply_clarification_answer(pending_clarification, known_ids, allocated_ids):
    if not isinstance(pending_clarification, dict):
        return None
    if pending_clarification.get("status") != "answered":
        return None

    mention_ref = pending_clarification.get("mention_ref", "")
    if not mention_ref:
        return None

    surface_form = (
        pending_clarification.get("surface_form")
        or pending_clarification.get("mention_entity_id")
        or mention_ref
    )
    canonical_id = clean_id(
        pending_clarification.get("resolved_canonical_id"), ""
    )
    action = pending_clarification.get("resolution_action", "")
    clarification_id = pending_clarification.get("clarification_id")

    if action == "same_identity" and canonical_id:
        if canonical_id not in known_ids.get("entity_ids", set()):
            return None
        canonical_name = (
            known_ids.get("npc_names", {}).get(canonical_id)
            or known_ids.get("entity_names", {}).get(canonical_id)
            or surface_form
        )
        return {
            "mention_ref": mention_ref,
            "surface_form": surface_form,
            "identity_status": "known_hidden",
            "visibility": "dm_private",
            "evidence": [
                {
                    "source": "clarification_answer",
                    "field": "resolved_canonical_id",
                    "clarification_id": clarification_id,
                }
            ],
            "canonical_id": canonical_id,
            "canonical_name": canonical_name,
            "decision": "reuse_existing",
            "blocked_operations": [],
            "resolution_state": "resolved",
            "entity_type": _canonical_entity_type(known_ids, canonical_id),
        }

    if action == "new_entity":
        entity_type = _clarification_patch_entity_type(pending_clarification)
        new_id = allocate_durable_id(surface_form, allocated_ids)
        return {
            "mention_ref": mention_ref,
            "surface_form": surface_form,
            "identity_status": "provisional_new_entity",
            "visibility": "party_known",
            "evidence": [
                {
                    "source": "clarification_answer",
                    "field": "new_entity",
                    "clarification_id": clarification_id,
                }
            ],
            "canonical_id": new_id,
            "canonical_name": surface_form,
            "decision": "create_new",
            "blocked_operations": [],
            "resolution_state": "resolved",
            "entity_type": entity_type,
        }

    if action == "ignore":
        # The compiler consumes this same dict after registry construction.
        # Clearing both accepted patch keys guarantees that ignore is a no-write
        # terminal action even for legacy or manually-created rows.
        pending_clarification["resolution_patch_json"] = None
        pending_clarification["resolution_patch"] = None
        return {
            "mention_ref": mention_ref,
            "surface_form": surface_form,
            "identity_status": "intentionally_undetermined",
            "visibility": "dm_private",
            "evidence": [
                {
                    "source": "clarification_answer",
                    "field": "ignore",
                    "clarification_id": clarification_id,
                }
            ],
            "canonical_id": None,
            "canonical_name": None,
            "decision": "reject",
            "blocked_operations": [],
            "resolution_state": "resolved",
            "entity_type": "other",
        }

    return None


def build_canonical_resolution_registry(
    campaign,
    memory_context,
    extracted,
    resolver_packet,
    prior_resolutions,
    known_ids,
    resolved_entity_refs=None,
):
    enriched_known_ids = _enrich_known_ids_with_entity_types(campaign, known_ids)
    return _core.build_canonical_resolution_registry(
        campaign,
        memory_context,
        extracted,
        resolver_packet,
        prior_resolutions,
        enriched_known_ids,
        resolved_entity_refs=resolved_entity_refs,
    )


def fetch_prior_resolutions(campaign):
    if not campaign:
        return []
    from models import CampaignIdentityResolution

    rows = CampaignIdentityResolution.query.filter_by(
        campaign_id=campaign.id
    ).all()
    return [row.to_dict() for row in rows]


def fetch_pending_clarifications(campaign):
    if not campaign:
        return []
    from models import CampaignClarification

    rows = (
        CampaignClarification.query
        .filter_by(campaign_id=campaign.id)
        .filter(CampaignClarification.status.in_(["pending", "answered"]))
        .all()
    )
    return [row.to_dict() for row in rows]


# The core builder resolves helpers through its own module globals.
_core.find_all_matching_candidates = find_all_matching_candidates
_core._resolve_entity_claim = _resolve_entity_claim
_core._apply_clarification_answer = _apply_clarification_answer
_core.fetch_prior_resolutions = fetch_prior_resolutions
_core.fetch_pending_clarifications = fetch_pending_clarifications

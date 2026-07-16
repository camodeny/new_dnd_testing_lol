import hashlib
import uuid

from services.memory_resolver_schemas import (
    AUTHORITY_PRECEDENCE,
    IDENTITY_STATUSES,
    RESOLUTION_DECISIONS,
    RESOLUTION_STATES,
    CLARIFICATION_KINDS,
    CLARIFICATION_STATUSES,
    BLOCKING_SCOPES,
    DIAGNOSTICS_TEMPLATE,
    make_diagnostics,
    is_identity_worthy,
    can_reuse_existing,
    validate_registry_entry,
    validate_clarification_request,
)
from services.world_service import clean_id, clean_text, get_campaign_world, json_loads


def allocate_durable_id(base_name, existing_ids, prefix="entity"):
    base = clean_id(base_name.lower().replace(" ", "_"), "") or prefix
    if base not in existing_ids:
        return base
    for i in range(2, 100):
        candidate = f"{base}_{i}"
        if candidate not in existing_ids:
            return candidate
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def build_canonical_resolution_registry(
    campaign,
    memory_context,
    extracted,
    resolver_packet,
    prior_resolutions,
    known_ids,
):
    registry = []
    diagnostics = make_diagnostics()
    clarification_requests = []
    pending_clarifications = memory_context.get("pending_clarifications") if isinstance(memory_context, dict) else None

    if not isinstance(pending_clarifications, list):
        pending_clarifications = []

    all_existing_ids = known_ids.get("entity_ids", set()) if isinstance(known_ids, dict) else set()
    all_npc_ids = known_ids.get("npc_ids", set()) if isinstance(known_ids, dict) else set()
    all_location_ids = known_ids.get("location_ids", set()) if isinstance(known_ids, dict) else set()

    packet_mentions = _index_packet_mentions(resolver_packet)
    prior_resolution_map = _index_prior_resolutions(prior_resolutions)
    pending_clarification_map = _index_pending_clarifications(pending_clarifications)

    extracted_entities = extracted.get("entity_claims") if isinstance(extracted, dict) else None
    if not isinstance(extracted_entities, list):
        extracted_entities = []
    extracted_npcs = extracted.get("npc_claims") if isinstance(extracted, dict) else None
    if not isinstance(extracted_npcs, list):
        extracted_npcs = []

    mention_index = 0
    allocated_ids = set(all_existing_ids)

    for mention in packet_mentions:
        entry = _resolve_packet_mention(
            mention, known_ids, prior_resolution_map, allocated_ids, mention_index, campaign
        )
        registry.append(entry)
        if entry.get("decision") == "request_clarification":
            cr = _build_clarification_record(entry, campaign, memory_context)
            if cr:
                clarification_requests.append(cr)
                diagnostics["clarification_requests"].append(cr)
        if entry["canonical_id"]:
            allocated_ids.add(entry["canonical_id"])
        _track_diagnostics(entry, diagnostics)
        mention_index += 1

    for claim in extracted_entities:
        if not isinstance(claim, dict):
            continue
        name = clean_text(claim.get("name"), 160) or clean_text(claim.get("surface_form"), 160)
        if not name:
            continue
        if _registry_has_surface_form(registry, name):
            continue
        if not is_identity_worthy(name):
            continue

        entry = _resolve_entity_claim(
            claim, known_ids, prior_resolution_map, allocated_ids, mention_index
        )
        registry.append(entry)
        if entry.get("decision") == "request_clarification":
            cr = _build_clarification_record(entry, campaign, memory_context)
            if cr:
                clarification_requests.append(cr)
                diagnostics["clarification_requests"].append(cr)
        if entry["canonical_id"]:
            allocated_ids.add(entry["canonical_id"])
        _track_diagnostics(entry, diagnostics)
        mention_index += 1

    for claim in extracted_npcs:
        if not isinstance(claim, dict):
            continue
        name = clean_text(claim.get("name"), 160) or clean_text(claim.get("surface_form"), 160)
        if not name:
            continue
        if _registry_has_surface_form(registry, name):
            continue
        if not is_identity_worthy(name):
            continue

        entry = _resolve_npc_claim(
            claim, known_ids, prior_resolution_map, allocated_ids, mention_index
        )
        registry.append(entry)
        if entry.get("decision") == "request_clarification":
            cr = _build_clarification_record(entry, campaign, memory_context)
            if cr:
                clarification_requests.append(cr)
                diagnostics["clarification_requests"].append(cr)
        if entry["canonical_id"]:
            allocated_ids.add(entry["canonical_id"])
        _track_diagnostics(entry, diagnostics)
        mention_index += 1

    for pending in pending_clarifications:
        if not isinstance(pending, dict):
            continue
        resolved_entry = _apply_clarification_answer(pending, known_ids, allocated_ids)
        if resolved_entry:
            registry.append(resolved_entry)
            if resolved_entry["canonical_id"]:
                allocated_ids.add(resolved_entry["canonical_id"])
            _track_diagnostics(resolved_entry, diagnostics)

    registry_map = {entry["mention_ref"]: entry for entry in registry}

    return registry, registry_map, clarification_requests, diagnostics


def resolve_ref(ref, registry_map, known_ids):
    if not ref:
        return ""
    ref_cleaned = clean_id(ref, "")
    if ref_cleaned in registry_map:
        return registry_map[ref_cleaned].get("canonical_id") or ref_cleaned
    if isinstance(known_ids, dict) and ref_cleaned in known_ids.get("entity_ids", set()):
        return ref_cleaned
    ref_lower = str(ref).strip().lower()
    if ref_lower in registry_map:
        return registry_map[ref_lower].get("canonical_id") or ref_lower
    return ""


def _index_packet_mentions(resolver_packet):
    packet = resolver_packet if isinstance(resolver_packet, dict) else {}
    mentions = packet.get("entity_mentions")
    if not isinstance(mentions, list):
        return []
    return mentions


def _index_prior_resolutions(prior_resolutions):
    result = {}
    if not isinstance(prior_resolutions, list):
        return result
    for res in prior_resolutions:
        if not isinstance(res, dict):
            continue
        mention_entity_id = clean_id(res.get("mention_entity_id"), "")
        if mention_entity_id:
            result[mention_entity_id] = res
            result[mention_entity_id.lower()] = res
        # Only index by mention_name globally if it's an explicit alias (resolution_action == "add_alias")
        if res.get("resolution_action") == "add_alias":
            mention_name = clean_text(res.get("mention_name"), 400)
            if mention_name:
                result[mention_name.lower()] = res
    return result


def _index_pending_clarifications(pending_clarifications):
    result = {}
    for pc in pending_clarifications:
        if not isinstance(pc, dict):
            continue
        mention_ref = pc.get("mention_ref", "")
        if mention_ref:
            result[mention_ref] = pc
        mention_entity_id = pc.get("mention_entity_id", "")
        if mention_entity_id:
            result[mention_entity_id] = pc
    return result


def _registry_has_surface_form(registry, surface_form):
    form_lower = surface_form.strip().lower()
    for entry in registry:
        if entry.get("surface_form", "").strip().lower() == form_lower:
            return True
        if entry.get("canonical_name", "").strip().lower() == form_lower:
            return True
    return False


def find_all_matching_candidates(name, known_ids, prior_resolutions, expected_type):
    candidates = {}  # Map canonical_id -> type
    name_lower = name.strip().lower()

    if expected_type == "npc":
        # Check NPC names
        for npc_id, npc_name in known_ids.get("npc_names", {}).items():
            if npc_name.strip().lower() == name_lower:
                candidates[npc_id] = "npc"

        # Check prior resolutions (aliases) for NPC
        if isinstance(prior_resolutions, list):
            for res in prior_resolutions:
                if not isinstance(res, dict):
                    continue
                if res.get("resolution_action") == "add_alias":
                    res_name = res.get("mention_name", "")
                    if res_name and res_name.strip().lower() == name_lower:
                        can_id = res.get("canonical_id")
                        if can_id and can_id in known_ids.get("npc_ids", set()):
                            candidates[can_id] = "npc"

    elif expected_type == "entity":
        # Check entity names
        for ent_id, ent_name in known_ids.get("entity_names", {}).items():
            if ent_name.strip().lower() == name_lower:
                candidates[ent_id] = "entity"

        # Check prior resolutions (aliases) for entity
        if isinstance(prior_resolutions, list):
            for res in prior_resolutions:
                if not isinstance(res, dict):
                    continue
                if res.get("resolution_action") == "add_alias":
                    res_name = res.get("mention_name", "")
                    if res_name and res_name.strip().lower() == name_lower:
                        can_id = res.get("canonical_id")
                        if can_id and can_id in known_ids.get("entity_ids", set()):
                            candidates[can_id] = "entity"

    return candidates


def _resolve_packet_mention(mention, known_ids, prior_resolution_map, allocated_ids, index, campaign):
    mention_ref = mention.get("mention_ref", f"packet_mention_{index}")
    surface_form = clean_text(mention.get("surface_form"), 200) or mention_ref
    identity_status = mention.get("identity_status", "provisional_new_entity")
    canonical_id_from_packet = clean_id(mention.get("canonical_id"), "")
    public_name = clean_text(mention.get("public_name"), 200) or surface_form
    visibility = mention.get("visibility", "dm_private")
    evidence = [{"source": "memory_resolver_packet", "field": f"entity_mentions[{index}]"}]

    entry = {
        "mention_ref": mention_ref,
        "surface_form": surface_form,
        "identity_status": identity_status,
        "visibility": visibility,
        "evidence": evidence,
        "canonical_id": None,
        "canonical_name": None,
        "decision": None,
        "blocked_operations": [],
        "resolution_state": None,
    }

    if identity_status == "known_hidden":
        if canonical_id_from_packet and canonical_id_from_packet in known_ids.get("entity_ids", set()):
            # Check if the surface_form matches a DIFFERENT known entity (name collision)
            surface_lower = surface_form.strip().lower()
            for npc_id, npc_name in known_ids.get("npc_names", {}).items():
                if npc_name.lower() == surface_lower and npc_id.lower() != canonical_id_from_packet.lower():
                    # The surface form is the proper name of a different NPC — reject this mapping
                    entry["decision"] = "reject"
                    entry["resolution_state"] = "rejected"
                    entry["canonical_id"] = None
                    entry["blocked_operations"] = ["identity_merge", "npc_update", "hidden_identity_reveal"]
                    return entry

            for ent_id, ent_name in known_ids.get("entity_names", {}).items():
                if ent_name.lower() == surface_lower and ent_id.lower() != canonical_id_from_packet.lower():
                    entry["decision"] = "reject"
                    entry["resolution_state"] = "rejected"
                    entry["canonical_id"] = None
                    entry["blocked_operations"] = ["identity_merge", "npc_update", "hidden_identity_reveal"]
                    return entry

            entry["canonical_id"] = canonical_id_from_packet
            existing_name = known_ids.get("npc_names", {}).get(canonical_id_from_packet) or known_ids.get("entity_names", {}).get(canonical_id_from_packet)
            entry["canonical_name"] = existing_name or public_name
            entry["decision"] = "reuse_existing"
            entry["resolution_state"] = "resolved"
        else:
            entry["decision"] = "request_clarification"
            entry["resolution_state"] = "clarification_requested"
            entry["blocked_operations"] = ["identity_merge", "npc_update", "hidden_identity_reveal"]

    elif identity_status == "known_public":
        if canonical_id_from_packet and canonical_id_from_packet in known_ids.get("entity_ids", set()):
            surface_lower = surface_form.strip().lower()
            for npc_id, npc_name in known_ids.get("npc_names", {}).items():
                if npc_name.lower() == surface_lower and npc_id.lower() != canonical_id_from_packet.lower():
                    entry["decision"] = "reject"
                    entry["resolution_state"] = "rejected"
                    entry["canonical_id"] = None
                    entry["blocked_operations"] = ["identity_merge", "npc_update"]
                    return entry
            for ent_id, ent_name in known_ids.get("entity_names", {}).items():
                if ent_name.lower() == surface_lower and ent_id.lower() != canonical_id_from_packet.lower():
                    entry["decision"] = "reject"
                    entry["resolution_state"] = "rejected"
                    entry["canonical_id"] = None
                    entry["blocked_operations"] = ["identity_merge", "npc_update"]
                    return entry
            entry["canonical_id"] = canonical_id_from_packet
            existing_name = known_ids.get("npc_names", {}).get(canonical_id_from_packet) or known_ids.get("entity_names", {}).get(canonical_id_from_packet)
            entry["canonical_name"] = existing_name or public_name
            entry["decision"] = "reuse_existing"
            entry["resolution_state"] = "resolved"
            entry["visibility"] = "public"
        else:
            entry["canonical_id"] = canonical_id_from_packet or allocate_durable_id(
                public_name, allocated_ids
            )
            entry["canonical_name"] = public_name
            entry["decision"] = "create_new"
            entry["resolution_state"] = "resolved"
            entry["visibility"] = "public"

    elif identity_status == "intentionally_undetermined":
        entry["canonical_id"] = allocate_durable_id(surface_form, allocated_ids)
        entry["canonical_name"] = surface_form
        entry["decision"] = "create_provisional"
        entry["resolution_state"] = "provisional"

    elif identity_status == "provisional_new_entity":
        entry["canonical_id"] = canonical_id_from_packet or allocate_durable_id(
            surface_form, allocated_ids
        )
        entry["canonical_name"] = public_name
        entry["decision"] = "create_new"
        entry["resolution_state"] = "resolved"

    elif identity_status == "candidate_existing_entity":
        # Candidate status is not a confirmed identity — always request clarification
        entry["decision"] = "request_clarification"
        entry["resolution_state"] = "clarification_requested"
        entry["blocked_operations"] = ["identity_merge", "npc_update"]

    else:
        entry["decision"] = "create_provisional"
        entry["resolution_state"] = "provisional"
        entry["canonical_id"] = allocate_durable_id(surface_form, allocated_ids)
        entry["canonical_name"] = surface_form

    return entry


def _resolve_entity_claim(claim, known_ids, prior_resolution_map, allocated_ids, index):
    name = clean_text(claim.get("name"), 160) or clean_text(claim.get("surface_form"), 160)
    mention_ref = clean_id(claim.get("mention_ref"), "") or f"entity_{index}"
    surface_form = name
    entity_type = clean_text(claim.get("type"), 40).lower() or "other"

    entry = {
        "mention_ref": mention_ref,
        "surface_form": surface_form,
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

    prior = prior_resolution_map.get(mention_ref) or prior_resolution_map.get(name.lower())
    if prior and isinstance(prior, dict):
        canonical_id = clean_id(prior.get("canonical_id"), "")
        canonical_name = clean_text(prior.get("canonical_name"), 200)
        if canonical_id and canonical_id in known_ids.get("entity_ids", set()):
            entry["canonical_id"] = canonical_id
            existing_name = known_ids.get("entity_names", {}).get(canonical_id) or known_ids.get("npc_names", {}).get(canonical_id)
            entry["canonical_name"] = existing_name or canonical_name or name
            entry["decision"] = "reuse_existing"
            entry["resolution_state"] = "resolved"
            entry["identity_status"] = prior.get("visibility", "") == "dm_private" and "known_hidden" or "known_public"
            entry["evidence"].append({"source": "prior_durable_memory", "field": "identity_resolution_record"})
            return entry

    prior_res_list = list(prior_resolution_map.values()) if isinstance(prior_resolution_map, dict) else prior_resolution_map
    candidates = find_all_matching_candidates(name, known_ids, prior_res_list, expected_type="entity")
    if len(candidates) > 1:
        entry["decision"] = "request_clarification"
        entry["resolution_state"] = "clarification_requested"
        entry["blocked_operations"] = ["identity_merge", "npc_update"]
        return entry
    elif len(candidates) == 1:
        can_id, can_type = list(candidates.items())[0]
        entry["canonical_id"] = can_id
        existing_name = known_ids.get("entity_names", {}).get(can_id) or known_ids.get("npc_names", {}).get(can_id)
        if existing_name and name.lower() != existing_name.lower():
            entry["decision"] = "add_alias"
            entry["canonical_name"] = existing_name
        else:
            entry["decision"] = "reuse_existing"
            entry["canonical_name"] = existing_name or name
        entry["resolution_state"] = "resolved"
        entry["identity_status"] = "known_public"
        entry["evidence"].append({"source": "prior_durable_memory", "field": "entity_name_match"})
        return entry
    else:
        if is_identity_worthy(name):
            new_id = allocate_durable_id(name, allocated_ids)
            entry["canonical_id"] = new_id
            entry["canonical_name"] = name
            entry["decision"] = "create_provisional"
            entry["resolution_state"] = "provisional"
            entry["evidence"].append({"source": "visible_transcript", "field": "surface_form_claim"})
        else:
            entry["decision"] = "reject"
            entry["resolution_state"] = "rejected"

        return entry


def _resolve_npc_claim(claim, known_ids, prior_resolution_map, allocated_ids, index):
    name = clean_text(claim.get("name"), 160) or clean_text(claim.get("surface_form"), 160)
    mention_ref = clean_id(claim.get("mention_ref"), "") or f"npc_{index}"
    surface_form = name
    proposed_id = clean_id(claim.get("id") or claim.get("actor_id"), "")

    entry = {
        "mention_ref": mention_ref,
        "surface_form": surface_form,
        "identity_status": "provisional_unknown",
        "visibility": "party_known",
        "evidence": [],
        "canonical_id": None,
        "canonical_name": None,
        "decision": None,
        "blocked_operations": [],
        "resolution_state": None,
        "entity_type": "npc",
    }

    if proposed_id and proposed_id in known_ids.get("npc_ids", set()):
        # Check for name collision: does the proposed name match a DIFFERENT known NPC?
        if name:
            existing_name = known_ids.get("npc_names", {}).get(proposed_id, "")
            name_lower = name.strip().lower()
            if existing_name and existing_name.lower() != name_lower:
                # Check if the supplied name matches a DIFFERENT known entity
                for other_id, other_name in known_ids.get("npc_names", {}).items():
                    if other_name.lower() == name_lower and other_id != proposed_id:
                        entry["decision"] = "reject"
                        entry["resolution_state"] = "rejected"
                        entry["blocked_operations"] = ["identity_merge", "npc_update"]
                        return entry
        existing_name = known_ids.get("npc_names", {}).get(proposed_id, "")
        entry["canonical_id"] = proposed_id
        entry["canonical_name"] = existing_name or name
        entry["decision"] = "reuse_existing"
        entry["resolution_state"] = "resolved"
        entry["identity_status"] = "known_public"
        entry["evidence"].append({"source": "prior_durable_memory", "field": "npc_id_match"})
        return entry

    prior = prior_resolution_map.get(mention_ref) or prior_resolution_map.get(name.lower())
    if prior and isinstance(prior, dict):
        canonical_id = clean_id(prior.get("canonical_id"), "")
        canonical_name = clean_text(prior.get("canonical_name"), 200)
        if canonical_id and canonical_id in known_ids.get("npc_ids", set()):
            entry["canonical_id"] = canonical_id
            existing_name = known_ids.get("npc_names", {}).get(canonical_id) or known_ids.get("entity_names", {}).get(canonical_id)
            entry["canonical_name"] = existing_name or canonical_name or name
            entry["decision"] = "reuse_existing"
            entry["resolution_state"] = "resolved"
            entry["identity_status"] = prior.get("visibility", "") == "dm_private" and "known_hidden" or "known_public"
            entry["evidence"].append({"source": "prior_durable_memory", "field": "identity_resolution_record"})
            return entry

    prior_res_list = list(prior_resolution_map.values()) if isinstance(prior_resolution_map, dict) else prior_resolution_map
    candidates = find_all_matching_candidates(name, known_ids, prior_res_list, expected_type="npc")
    if len(candidates) > 1:
        entry["decision"] = "request_clarification"
        entry["resolution_state"] = "clarification_requested"
        entry["blocked_operations"] = ["identity_merge", "npc_update"]
        return entry
    elif len(candidates) == 1:
        can_id, can_type = list(candidates.items())[0]
        entry["canonical_id"] = can_id
        existing_name = known_ids.get("npc_names", {}).get(can_id) or known_ids.get("entity_names", {}).get(can_id)
        if existing_name and name.lower() != existing_name.lower():
            entry["decision"] = "add_alias"
            entry["canonical_name"] = existing_name
        else:
            entry["decision"] = "reuse_existing"
            entry["canonical_name"] = existing_name or name
        entry["resolution_state"] = "resolved"
        entry["identity_status"] = "known_public"
        entry["evidence"].append({"source": "prior_durable_memory", "field": "npc_name_match"})
        return entry
    else:
        if is_identity_worthy(name):
            new_id = allocate_durable_id(name, allocated_ids)
            entry["canonical_id"] = new_id
            entry["canonical_name"] = name
            entry["decision"] = "create_provisional"
            entry["resolution_state"] = "provisional"
            entry["evidence"].append({"source": "visible_transcript", "field": "surface_form_claim"})
        else:
            entry["decision"] = "reject"
            entry["resolution_state"] = "rejected"

        return entry


def _apply_clarification_answer(pending_clarification, known_ids, allocated_ids):
    if not isinstance(pending_clarification, dict):
        return None
    status = pending_clarification.get("status", "")
    if status not in ("answered",):
        return None

    mention_ref = pending_clarification.get("mention_ref", "")
    if not mention_ref:
        return None

    resolved_canonical_id = clean_id(pending_clarification.get("resolved_canonical_id"), "")
    resolution_action = pending_clarification.get("resolution_action", "")

    if resolution_action == "same_identity" and resolved_canonical_id:
        if resolved_canonical_id not in known_ids.get("entity_ids", set()):
            return None
        existing_name = known_ids.get("npc_names", {}).get(resolved_canonical_id) or known_ids.get("entity_names", {}).get(resolved_canonical_id)
        return {
            "mention_ref": mention_ref,
            "surface_form": pending_clarification.get("mention_entity_id", mention_ref),
            "identity_status": "known_hidden",
            "visibility": "dm_private",
            "evidence": [{
                "source": "clarification_answer",
                "field": "resolved_canonical_id",
                "clarification_id": pending_clarification.get("clarification_id")
            }],
            "canonical_id": resolved_canonical_id,
            "canonical_name": existing_name or pending_clarification.get("mention_entity_id", resolved_canonical_id),
            "decision": "reuse_existing",
            "blocked_operations": [],
            "resolution_state": "resolved",
        }

    if resolution_action == "new_entity":
        surface_form_val = pending_clarification.get("surface_form") or pending_clarification.get("mention_entity_id") or mention_ref
        new_id = allocate_durable_id(
            surface_form_val, allocated_ids
        )
        return {
            "mention_ref": mention_ref,
            "surface_form": surface_form_val,
            "identity_status": "provisional_new_entity",
            "visibility": "party_known",
            "evidence": [{
                "source": "clarification_answer",
                "field": "new_entity",
                "clarification_id": pending_clarification.get("clarification_id")
            }],
            "canonical_id": new_id,
            "canonical_name": surface_form_val,
            "decision": "create_new",
            "blocked_operations": [],
            "resolution_state": "resolved",
        }

    if resolution_action == "ignore":
        return {
            "mention_ref": mention_ref,
            "surface_form": pending_clarification.get("mention_entity_id", mention_ref),
            "identity_status": "intentionally_undetermined",
            "visibility": "dm_private",
            "evidence": [{
                "source": "clarification_answer",
                "field": "ignore",
                "clarification_id": pending_clarification.get("clarification_id")
            }],
            "canonical_id": None,
            "canonical_name": None,
            "decision": "reject",
            "blocked_operations": [],
            "resolution_state": "resolved",
        }

    return None


def _build_clarification_record(entry, campaign, memory_context):
    if entry.get("decision") != "request_clarification":
        return None

    mention_ref = entry["mention_ref"]
    surface_form = entry["surface_form"]
    campaign_id = getattr(campaign, "id", 0) if campaign else 0

    turn_id = (memory_context or {}).get("hot_context", {}).get("turn_id", "")
    idempotency_key_raw = f"{campaign_id}:{mention_ref}:{turn_id}:identity:{','.join(sorted(entry.get('blocked_operations', [])))}"
    idempotency_key = hashlib.sha256(idempotency_key_raw.encode("utf-8")).hexdigest()[:64]
    clar_id = f"clar_{hashlib.md5(idempotency_key_raw.encode('utf-8')).hexdigest()[:12]}"

    return {
        "clarification_id": clar_id,
        "idempotency_key": idempotency_key,
        "kind": "identity",
        "mention_ref": mention_ref,
        "mention_entity_id": entry.get("canonical_id") or mention_ref,
        "surface_form": surface_form,
        "question": f"Is {surface_form} an existing NPC or entity?",
        "candidate_ids": [],
        "blocking_scope": entry.get("blocked_operations", []),
        "status": "pending",
        "source_turn_id": turn_id,
    }


def _track_diagnostics(entry, diagnostics):
    decision = entry.get("decision", "")
    if decision == "reuse_existing":
        diagnostics["reused_existing"].append({
            "mention_ref": entry["mention_ref"],
            "surface_form": entry["surface_form"],
            "canonical_id": entry.get("canonical_id"),
            "canonical_name": entry.get("canonical_name"),
            "evidence": entry.get("evidence"),
        })
    elif decision == "create_new":
        diagnostics["created_new"].append({
            "mention_ref": entry["mention_ref"],
            "surface_form": entry["surface_form"],
            "canonical_id": entry.get("canonical_id"),
        })
    elif decision == "create_provisional":
        diagnostics["created_provisional"].append({
            "mention_ref": entry["mention_ref"],
            "surface_form": entry["surface_form"],
            "canonical_id": entry.get("canonical_id"),
            "identity_status": entry.get("identity_status"),
        })
    elif decision == "add_alias":
        diagnostics["aliases_added"].append({
            "mention_ref": entry["mention_ref"],
            "surface_form": entry["surface_form"],
            "canonical_id": entry.get("canonical_id"),
        })
    elif decision == "defer_resolution":
        diagnostics["deferred_resolutions"].append({
            "mention_ref": entry["mention_ref"],
            "surface_form": entry["surface_form"],
        })
    elif decision == "reject":
        diagnostics["rejected_mutations"].append({
            "mention_ref": entry["mention_ref"],
            "surface_form": entry["surface_form"],
            "reason": entry.get("reason", "rejected_by_registry"),
        })

    blocked = entry.get("blocked_operations")
    if isinstance(blocked, list) and len(blocked) > 0:
        diagnostics["blocked_mutations"].append({
            "mention_ref": entry["mention_ref"],
            "surface_form": entry["surface_form"],
            "blocked_operations": blocked,
        })


def fetch_prior_resolutions(campaign):
    if not campaign:
        return []
    try:
        from models import CampaignIdentityResolution
        rows = CampaignIdentityResolution.query.filter_by(campaign_id=campaign.id).all()
        return [row.to_dict() for row in rows]
    except Exception:
        return []


def fetch_pending_clarifications(campaign):
    if not campaign:
        return []
    try:
        from models import CampaignClarification
        rows = (
            CampaignClarification.query
            .filter_by(campaign_id=campaign.id)
            .filter(CampaignClarification.status.in_(["pending", "answered"]))
            .all()
        )
        return [row.to_dict() for row in rows]
    except Exception:
        return []


def persist_clarification_requests(clarification_requests, campaign):
    if not campaign or not clarification_requests:
        return
    from models import CampaignClarification, db
    existing = CampaignClarification.query.filter_by(campaign_id=campaign.id).all()
    existing_keys = {
        row.idempotency_key
        for row in existing
        if row.idempotency_key and row.status in ("pending", "answered")
    }
    for cr in clarification_requests:
        if not isinstance(cr, dict):
            continue
        idempotency_key = cr.get("idempotency_key", "")
        if idempotency_key in existing_keys:
            continue
        new_id = cr["clarification_id"]
        mention_ref = cr["mention_ref"]
        # Obsolete older pending clarifications for the same mention_ref
        for row in existing:
            if row.mention_ref == mention_ref and row.status == "pending" and row.clarification_id != new_id:
                row.status = "obsolete"
                row.obsoleted_by_clarification_id = new_id
        model = CampaignClarification(
            campaign_id=campaign.id,
            clarification_id=new_id,
            idempotency_key=idempotency_key,
            kind=cr["kind"],
            mention_ref=mention_ref,
            mention_entity_id=cr.get("mention_entity_id"),
            surface_form=cr.get("surface_form"),
            question=cr["question"],
            candidate_ids=cr.get("candidate_ids"),
            blocking_scope=cr.get("blocking_scope"),
            status="pending",
            source_memory_run_id=cr.get("source_memory_run_id"),
            source_turn_id=cr.get("source_turn_id"),
        )
        db.session.add(model)
        existing_keys.add(idempotency_key)


def persist_identity_resolutions(resolution_records, campaign):
    if not campaign or not resolution_records:
        return
    from models import CampaignIdentityResolution, db
    existing_ids = {
        row.resolution_id
        for row in CampaignIdentityResolution.query.filter_by(campaign_id=campaign.id).all()
        if row.resolution_id
    }
    for record in resolution_records:
        if not isinstance(record, dict):
            continue
        res_id = record.get("resolution_id", "")
        if res_id in existing_ids:
            continue
        model = CampaignIdentityResolution(
            campaign_id=campaign.id,
            resolution_id=res_id,
            mention_entity_id=record["mention_entity_id"],
            mention_name=record.get("mention_name"),
            resolution_action=record["resolution_action"],
            canonical_id=record["canonical_id"],
            canonical_name=record.get("canonical_name"),
            visibility=record.get("visibility", "dm_private"),
            resolved_by=record.get("resolved_by"),
            source_clarification_id=record.get("source_clarification_id"),
            source_turn_id=record.get("source_turn_id"),
            source_trace_id=record.get("source_trace_id"),
            evidence_json=record.get("evidence"),
        )
        db.session.add(model)
        existing_ids.add(res_id)

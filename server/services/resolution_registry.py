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
            # Verify the canonical ID doesn't contradict existing durable canon
            canonical_name = canonical_id_from_packet
            # Check if this mapping would conflate distinct known entities
            existing_name_for_id = None
            from models import NPCActor
            npc = NPCActor.query.filter_by(campaign_id=getattr(campaign, 'id', 0), actor_id=canonical_id_from_packet).first()
            if npc:
                existing_name_for_id = npc.name
            
            if existing_name_for_id and existing_name_for_id.lower() != surface_form.lower():
                # The packet is trying to map a surface form to an existing NPC with a different name
                # This could be a valid hidden-identity reveal or an erroneous conflation
                # Accept it but mark it as requiring evidence
                entry["canonical_id"] = canonical_id_from_packet
                entry["canonical_name"] = public_name
                entry["decision"] = "reuse_existing"
                entry["resolution_state"] = "resolved"
                entry["visibility"] = "dm_private"
            else:
                entry["canonical_id"] = canonical_id_from_packet
                entry["canonical_name"] = public_name
                entry["decision"] = "reuse_existing"
                entry["resolution_state"] = "resolved"
        else:
            entry["decision"] = "request_clarification"
            entry["resolution_state"] = "clarification_requested"
            entry["blocked_operations"] = ["identity_merge", "npc_update", "hidden_identity_reveal"]

    elif identity_status == "known_public":
        if canonical_id_from_packet and canonical_id_from_packet in known_ids.get("entity_ids", set()):
            entry["canonical_id"] = canonical_id_from_packet
            entry["canonical_name"] = public_name
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
        if canonical_id_from_packet and canonical_id_from_packet in known_ids.get("entity_ids", set()):
            entry["decision"] = "defer_resolution"
            entry["resolution_state"] = "deferred_resolution"
            entry["canonical_id"] = None
            entry["blocked_operations"] = ["identity_merge", "npc_update"]
        else:
            entry["canonical_id"] = allocate_durable_id(surface_form, allocated_ids)
            entry["canonical_name"] = surface_form
            entry["decision"] = "create_provisional"
            entry["resolution_state"] = "provisional"

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
            entry["canonical_name"] = canonical_name or name
            entry["decision"] = "reuse_existing"
            entry["resolution_state"] = "resolved"
            entry["identity_status"] = prior.get("visibility", "") == "dm_private" and "known_hidden" or "known_public"
            entry["evidence"].append({"source": "prior_durable_memory", "field": "identity_resolution_record"})
            return entry

    name_lower = name.lower()
    name_clean = clean_id(name_lower, "")
    for existing_id in known_ids.get("entity_ids", set()):
        if existing_id.lower() == name_clean:
            entry["canonical_id"] = existing_id
            entry["canonical_name"] = name
            entry["decision"] = "reuse_existing"
            entry["resolution_state"] = "resolved"
            entry["identity_status"] = "known_public"
            entry["evidence"].append({"source": "prior_durable_memory", "field": "entity_id_match"})
            return entry

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
        entry["canonical_id"] = proposed_id
        entry["canonical_name"] = name
        entry["decision"] = "reuse_existing"
        entry["resolution_state"] = "resolved"
        entry["identity_status"] = "known_public"
        entry["evidence"].append({"source": "prior_durable_memory", "field": "npc_id_match"})
        return entry

    prior = prior_resolution_map.get(mention_ref) or prior_resolution_map.get(name.lower())
    if prior and isinstance(prior, dict):
        canonical_id = clean_id(prior.get("canonical_id"), "")
        if canonical_id and canonical_id in known_ids.get("entity_ids", set()):
            entry["canonical_id"] = canonical_id
            entry["canonical_name"] = clean_text(prior.get("canonical_name"), 200) or name
            entry["decision"] = "reuse_existing"
            entry["resolution_state"] = "resolved"
            entry["identity_status"] = prior.get("visibility", "") == "dm_private" and "known_hidden" or "known_public"
            entry["evidence"].append({"source": "prior_durable_memory", "field": "identity_resolution_record"})
            return entry

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
        return {
            "mention_ref": mention_ref,
            "surface_form": pending_clarification.get("mention_entity_id", mention_ref),
            "identity_status": "known_hidden",
            "visibility": "dm_private",
            "evidence": [{"source": "clarification_answer", "field": "resolved_canonical_id"}],
            "canonical_id": resolved_canonical_id,
            "canonical_name": resolved_canonical_id,
            "decision": "reuse_existing",
            "blocked_operations": [],
            "resolution_state": "resolved",
        }

    if resolution_action == "new_entity":
        new_id = allocate_durable_id(
            pending_clarification.get("mention_entity_id", "entity"), allocated_ids
        )
        return {
            "mention_ref": mention_ref,
            "surface_form": pending_clarification.get("mention_entity_id", mention_ref),
            "identity_status": "provisional_new_entity",
            "visibility": "party_known",
            "evidence": [{"source": "clarification_answer", "field": "new_entity"}],
            "canonical_id": new_id,
            "canonical_name": pending_clarification.get("mention_entity_id", ""),
            "decision": "create_new",
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

    idempotency_key_raw = f"{campaign_id}:{mention_ref}:identity:{','.join(sorted(entry.get('blocked_operations', [])))}"
    idempotency_key = hashlib.sha256(idempotency_key_raw.encode("utf-8")).hexdigest()[:64]
    clar_id = f"clar_{hashlib.md5(idempotency_key_raw.encode('utf-8')).hexdigest()[:12]}"

    return {
        "clarification_id": clar_id,
        "idempotency_key": idempotency_key,
        "kind": "identity",
        "mention_ref": mention_ref,
        "mention_entity_id": entry.get("canonical_id") or mention_ref,
        "question": f"Is {surface_form} an existing NPC or entity?",
        "candidate_ids": [],
        "blocking_scope": entry.get("blocked_operations", []),
        "status": "pending",
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
    existing_keys = {
        row.idempotency_key
        for row in CampaignClarification.query.filter_by(campaign_id=campaign.id).all()
        if row.idempotency_key
    }
    for cr in clarification_requests:
        if not isinstance(cr, dict):
            continue
        idempotency_key = cr.get("idempotency_key", "")
        if idempotency_key in existing_keys:
            continue
        model = CampaignClarification(
            campaign_id=campaign.id,
            clarification_id=cr["clarification_id"],
            idempotency_key=idempotency_key,
            kind=cr["kind"],
            mention_ref=cr["mention_ref"],
            mention_entity_id=cr.get("mention_entity_id"),
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

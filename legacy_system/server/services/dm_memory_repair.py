"""DM-arbitrated repair for failed post-turn memory patches.

When the staged memory compiler rejects a patch because the resolver produced a
duplicate identity ("split brain": one surface form resolving to a freshly
allocated id while the resolver's own refs claim a different canonical id), the
old behaviour was to blind-retry the identical patch. For a deterministic
validation failure that always fails again, so the turn and the whole
automation run died (see run 55: ``unrecovered_dm_turn:dm_post_turn_failed``).

This module replaces that dead end with a bounded repair chain: the AI DM is
asked a scoped, structured identity-arbitration question. The DM may decide to
merge the duplicate into the canonical identity, keep it as a genuinely
distinct entity, or drop the whole memory write for the turn (clocks and
running summary still complete). The decision is applied to the failed compiled
patch, identity resolutions are persisted so the split does not recur, and the
patch is re-validated before it is returned for application.
"""

import json
import re

from services.world_service import clean_id, clean_text


REPAIR_ELIGIBLE_CODES = {"final_state_validation_failed"}

MAX_DM_REPAIR_ATTEMPTS = 2

# A decision to drop the memory write for this turn while still completing the
# clock and running-summary tail. Returned by :func:`attempt_dm_repair` as the
# ``patch`` value (None) with ``decision == 'skip_memory_write'``.
SKIP_MEMORY_WRITE = "skip_memory_write"

_SPLIT_BRAIN_RE = re.compile(
    r"resolved_ref_split_brain: (?P<name>.+?) -> (?P<duplicate_id>\S+) \(canonical (?P<canonical_id>\S+)\)"
)
_NPC_SPLIT_BRAIN_RE = re.compile(
    r"npc_actor_split_brain: (?P<name>.+?) -> (?P<duplicate_id>\S+) \(canonical actor (?P<canonical_id>\S+)\)"
)
_CONFLICT_RE = re.compile(r"resolved_ref_conflict: (?P<term>.+)")


def _validation_errors(err):
    telemetry = getattr(err, "telemetry", None)
    if not isinstance(telemetry, dict):
        return []
    errors = telemetry.get("validation_errors") or []
    if not isinstance(errors, list):
        return []
    return [str(item) for item in errors]


def _failed_patch(err):
    telemetry = getattr(err, "telemetry", None)
    if not isinstance(telemetry, dict):
        return None
    patch = telemetry.get("compiled_patch")
    return patch if isinstance(patch, dict) else None


def parse_identity_conflicts(err):
    """Parse repair-eligible identity conflicts from a failed memory patch.

    Returns a list of conflict dicts with the shape::

        {
            "kind": "resolved_ref_split_brain" | "npc_actor_split_brain" | "resolved_ref_conflict",
            "name": <surface form>,
            "duplicate_id": <allocated id>,
            "canonical_id": <existing canonical id>,
            "term": <conflicting term>  # conflict kind only
        }

    Empty list when the error is not a repair-eligible identity conflict.
    """
    conflicts = []
    for error in _validation_errors(err):
        match = _SPLIT_BRAIN_RE.search(error)
        if match:
            conflicts.append({
                "kind": "resolved_ref_split_brain",
                "name": clean_text(match.group("name"), 200),
                "duplicate_id": clean_id(match.group("duplicate_id"), ""),
                "canonical_id": clean_id(match.group("canonical_id"), ""),
            })
            continue
        match = _NPC_SPLIT_BRAIN_RE.search(error)
        if match:
            conflicts.append({
                "kind": "npc_actor_split_brain",
                "name": clean_text(match.group("name"), 200),
                "duplicate_id": clean_id(match.group("duplicate_id"), ""),
                "canonical_id": clean_id(match.group("canonical_id"), ""),
            })
            continue
        match = _CONFLICT_RE.search(error)
        if match:
            conflicts.append({
                "kind": "resolved_ref_conflict",
                "term": clean_text(match.group("term"), 200),
                "name": clean_text(match.group("term"), 200),
                "duplicate_id": None,
                "canonical_id": None,
            })
    return conflicts


def is_repair_eligible(err):
    """True when the error is a repair-eligible identity conflict with a patch."""
    if getattr(err, "code", None) not in REPAIR_ELIGIBLE_CODES:
        return False
    if _failed_patch(err) is None:
        return False
    return bool(parse_identity_conflicts(err))


def _build_arbitration_context(campaign, session, err, conflicts):
    from services.session_memory_agent import _known_ids
    from services.world_service import get_campaign_world, json_loads

    patch = _failed_patch(err) or {}
    known = {}
    try:
        known = _known_ids(campaign) if campaign else {}
    except Exception:
        known = {}

    entity_names = dict(known.get("entity_names", {}))
    npc_names = dict(known.get("npc_names", {}))
    names = {**npc_names, **entity_names}

    graph_entities = {}
    try:
        world = get_campaign_world(campaign.id) if campaign else None
        graph = json_loads(world.knowledge_graph, {"entities": []}) if world is not None else {}
        for entity in graph.get("entities", []) if isinstance(graph, dict) else []:
            if isinstance(entity, dict) and entity.get("id"):
                graph_entities[clean_id(entity.get("id"), "")] = {
                    "name": clean_text(entity.get("name"), 160),
                    "type": clean_text(entity.get("type"), 40),
                    "summary": clean_text(entity.get("summary"), 300),
                }
    except Exception:
        graph_entities = {}

    summary_lines = []
    for conflict in conflicts:
        summary_lines.append({
            "surface_form": conflict.get("name"),
            "allocated_id": conflict.get("duplicate_id"),
            "canonical_id": conflict.get("canonical_id"),
            "canonical_name": names.get(conflict.get("canonical_id")),
            "allocated_entity": graph_entities.get(conflict.get("duplicate_id")),
            "canonical_entity": graph_entities.get(conflict.get("canonical_id")),
        })

    transcript = []
    if session is not None:
        try:
            for message in (session.messages or [])[-8:]:
                if isinstance(message, dict) and message.get("content"):
                    transcript.append({
                        "role": message.get("role"),
                        "content": clean_text(message.get("content"), 500),
                    })
                else:
                    content = getattr(message, "content", None)
                    if content:
                        transcript.append({
                            "role": getattr(message, "role", "?"),
                            "content": clean_text(content, 500),
                        })
        except Exception:
            transcript = []

    patch_entities = []
    for entity in patch.get("upsert_graph_entities", []) if isinstance(patch.get("upsert_graph_entities"), list) else []:
        if isinstance(entity, dict) and entity.get("name"):
            patch_entities.append({
                "id": clean_id(entity.get("id"), ""),
                "name": clean_text(entity.get("name"), 160),
                "type": clean_text(entity.get("type"), 40),
            })

    return {
        "campaign_id": campaign.id if campaign else None,
        "session_id": session.id if session else None,
        "conflicts": summary_lines,
        "patch_entities": patch_entities[:30],
        "recent_transcript": transcript,
    }


ARBITRATION_SYSTEM_PROMPT = (
    "You are the Dungeon Master arbitrating a memory-integrity conflict in the campaign's "
    "durable knowledge graph. The memory compiler refused a write because the resolver created a "
    "duplicate identity: a surface form (character/NPC/entity name) was allocated a brand-new id while "
    "the resolver's own references claim a different canonical id already exists for it. Writing the patch "
    "as-is would leave two memory records for one entity (a 'split brain'), so nothing was persisted.\n\n"
    "Decide, as the DM, what should happen. You have full latitude: the identities may be the same person "
    "(merge), genuinely distinct characters who happen to share a name (keep the new one as a separate "
    "entity), or the safest action for this turn may be to drop the memory write entirely (the visible "
    "turn still happened; only the durable memory update is skipped).\n\n"
    "Return strict JSON only:\n"
    "{\n"
    '  "decision": "merge" | "keep_distinct" | "skip_memory_write",\n'
    '  "conflicts": [\n'
    "    {\n"
    '      "surface_form": "<name>",\n'
    '      "duplicate_id": "<allocated id>",\n'
    '      "canonical_id": "<existing canonical id>",\n'
    '      "resolution": "merge" | "keep_distinct" | "skip",\n'
    '      "target_id": "<id the surface form should resolve to>",\n'
    '      "renamed_to": "<optional new display name for a kept entity, or null>",\n'
    '      "reason": "<short reason>"\n'
    "    }\n"
    "  ],\n"
    '  "reason": "<overall short reason>"\n'
    "}\n"
    'Do not wrap the JSON in markdown fences or add any extra text.'
)


def _build_arbitration_prompt(context):
    return (
        "A durable campaign memory write was rejected due to duplicate-identity conflicts.\n\n"
        "Structured context:\n"
        f"{json.dumps(context, indent=2)}\n\n"
        "Resolve each conflict. If the same-entity resolution differs from the resolver's canonical choice, "
        "you may override it via target_id. Return the strict JSON arbitration described in the system prompt."
    )


def _request_dm_decision(context, audit_context=None):
    """Ask the AI DM for an identity-arbitration decision. Returns parsed dict or None."""
    from openrouter import _json_loads_with_repair, _post_chat

    prompt = _build_arbitration_prompt(context)
    raw = _post_chat(
        [
            {"role": "system", "content": ARBITRATION_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        json_mode=True,
        audit_context={
            **(audit_context or {}),
            "operation": "dm_memory_identity_arbitration",
            "actor": "session_dm",
        },
        allow_thinking=False,
        timeout_seconds=90,
        max_attempts=1,
    )
    if not isinstance(raw, str) or not raw.strip():
        return None
    data = _json_loads_with_repair(raw, audit_context=audit_context or {})
    if not isinstance(data, dict):
        return None
    decision = clean_text(data.get("decision"), 40).lower()
    if decision not in ("merge", "keep_distinct", "skip_memory_write"):
        return None
    return data


def _normalize_dm_decision(conflicts, decision):
    """Validate and normalize the DM's decision into a list of per-conflict resolutions."""
    if decision is None:
        return None
    top = clean_text(decision.get("decision"), 40).lower()
    if top not in ("merge", "keep_distinct", SKIP_MEMORY_WRITE):
        return None
    if top == SKIP_MEMORY_WRITE:
        return {"decision": SKIP_MEMORY_WRITE, "resolutions": []}

    by_dup = {}
    raw_conflicts = decision.get("conflicts") if isinstance(decision.get("conflicts"), list) else []
    for raw in raw_conflicts:
        if not isinstance(raw, dict):
            continue
        dup_id = clean_id(raw.get("duplicate_id"), "")
        if not dup_id:
            continue
        resolution = clean_text(raw.get("resolution"), 40).lower()
        if resolution not in ("merge", "keep_distinct", "skip"):
            continue
        by_dup[dup_id] = {
            "resolution": resolution,
            "target_id": clean_id(raw.get("target_id"), ""),
            "renamed_to": clean_text(raw.get("renamed_to"), 160) or None,
        }

    resolutions = []
    for conflict in conflicts:
        dup_id = conflict.get("duplicate_id")
        canonical_id = conflict.get("canonical_id")
        if not dup_id:
            continue
        chosen = by_dup.get(dup_id) or {}
        resolution = chosen.get("resolution")
        if resolution is None:
            # Fail closed toward the DM's explicit top-level decision rather
            # than silently executing the opposite arbitration when structured
            # output omits a specific conflict.
            resolution = "keep_distinct" if top == "keep_distinct" else "merge"
        target_id = chosen.get("target_id") or (canonical_id if resolution == "merge" else dup_id)
        resolutions.append({
            "surface_form": conflict.get("name"),
            "duplicate_id": dup_id,
            "canonical_id": canonical_id,
            "resolution": resolution,
            "target_id": target_id or dup_id,
            "renamed_to": chosen.get("renamed_to"),
        })
    return {"decision": top, "resolutions": resolutions}


def _remap_patch_ids(patch, id_remap):
    """Rewrite every reference to a remapped id inside a compiled patch (deep copy)."""
    from services.session_memory_agent import _remap_event_reference_fields

    remapped = json.loads(json.dumps(patch, default=str))

    def _rewrite_entity_ids(items):
        if not isinstance(items, list):
            return items
        for item in items:
            if not isinstance(item, dict):
                continue
            for field in ("id", "actor_id", "entity_id"):
                current = item.get(field)
                if isinstance(current, str) and current in id_remap:
                    item[field] = id_remap[current]
            for field in ("source_id", "target_id"):
                current = item.get(field)
                if isinstance(current, str) and current in id_remap:
                    item[field] = id_remap[current]
            for field in ("entity_ids", "participant_ids", "related_ids", "source_ids", "target_ids"):
                values = item.get(field)
                if isinstance(values, list):
                    item[field] = [id_remap.get(value, value) for value in values]
        return items

    remapped["upsert_graph_entities"] = _rewrite_entity_ids(remapped.get("upsert_graph_entities"))
    remapped["upsert_graph_relations"] = _rewrite_entity_ids(remapped.get("upsert_graph_relations"))
    remapped["upsert_graph_facts"] = _rewrite_entity_ids(remapped.get("upsert_graph_facts"))
    remapped["update_npc_actors"] = _rewrite_entity_ids(remapped.get("update_npc_actors"))

    scene = remapped.get("scene_patch")
    if isinstance(scene, dict):
        for field in ("location_id",):
            current = scene.get(field)
            if isinstance(current, str) and current in id_remap:
                scene[field] = id_remap[current]
        for field in ("active_npc_ids", "departed_npc_ids"):
            values = scene.get(field)
            if isinstance(values, list):
                scene[field] = [id_remap.get(value, value) for value in values]

    refs = remapped.get("resolved_entity_refs")
    if isinstance(refs, list):
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            for field in ("entity_id", "proposed_id"):
                current = ref.get(field)
                if isinstance(current, str) and current in id_remap:
                    ref[field] = id_remap[current]

    events = remapped.get("record_events")
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, dict):
                continue
            if isinstance(event.get("payload"), dict):
                event["payload"] = _remap_event_reference_fields(event["payload"], id_remap)

    return remapped


def _drop_entity(patch, entity_id):
    """Remove an upsert entity and any references to it from a compiled patch."""
    patch = json.loads(json.dumps(patch, default=str))
    entities = patch.get("upsert_graph_entities")
    if isinstance(entities, list):
        patch["upsert_graph_entities"] = [
            entity for entity in entities
            if not (isinstance(entity, dict) and clean_id(entity.get("id"), "") == entity_id)
        ]
    return _drop_references(patch, entity_id)


def _drop_references(patch, entity_id):
    """Remove all references to an entity id from relations, facts, scene, and refs."""
    def _filter_ids(value):
        return [item for item in value if item != entity_id] if isinstance(value, list) else value

    relations = patch.get("upsert_graph_relations")
    if isinstance(relations, list):
        patch["upsert_graph_relations"] = [
            relation for relation in relations
            if not (
                isinstance(relation, dict)
                and (relation.get("source_id") == entity_id or relation.get("target_id") == entity_id)
            )
        ]
    facts = patch.get("upsert_graph_facts")
    if isinstance(facts, list):
        for fact in facts:
            if isinstance(fact, dict) and isinstance(fact.get("entity_ids"), list):
                fact["entity_ids"] = _filter_ids(fact["entity_ids"])
    npc_updates = patch.get("update_npc_actors")
    if isinstance(npc_updates, list):
        patch["update_npc_actors"] = [
            item for item in npc_updates
            if not (isinstance(item, dict) and clean_id(item.get("id") or item.get("actor_id"), "") == entity_id)
        ]
    scene = patch.get("scene_patch")
    if isinstance(scene, dict):
        active = scene.get("active_npc_ids")
        if isinstance(active, list) and entity_id in active:
            scene["active_npc_ids"] = [item for item in active if item != entity_id]
            departed = scene.get("departed_npc_ids")
            if not isinstance(departed, list):
                departed = []
                scene["departed_npc_ids"] = departed
            if entity_id not in departed:
                departed.append(entity_id)
    refs = patch.get("resolved_entity_refs")
    if isinstance(refs, list):
        patch["resolved_entity_refs"] = [
            ref for ref in refs
            if not (isinstance(ref, dict) and clean_id(ref.get("entity_id"), "") == entity_id)
        ]
    return patch


def _resolution_records_for(conflict, action, source_trace_id=None, target_id=None, renamed_to=None):
    import hashlib

    dup_id = conflict.get("duplicate_id") or ""
    surface_form = conflict.get("name") or ""
    canonical_id = conflict.get("canonical_id") or ""
    if action == "merge":
        canonical = target_id or canonical_id
        record = {
            "resolution_id": f"dm_repair_merge_{hashlib.md5(f'{dup_id}:{canonical}'.encode()).hexdigest()[:12]}",
            "mention_entity_id": dup_id,
            "mention_name": surface_form,
            "resolution_action": "same_identity",
            "canonical_id": canonical,
            "canonical_name": renamed_to or None,
            "visibility": "dm_private",
            "resolved_by": "dm_memory_repair",
            "source_trace_id": source_trace_id,
            "evidence": {"source": "dm_memory_repair", "field": "identity_arbitration", "action": "merge"},
        }
        return [record]
    if action == "keep_distinct":
        new_id = target_id or dup_id
        record = {
            "resolution_id": f"dm_repair_keep_{hashlib.md5(f'{surface_form}:{new_id}'.encode()).hexdigest()[:12]}",
            "mention_entity_id": surface_form,
            "mention_name": surface_form,
            "resolution_action": "new_entity",
            "canonical_id": new_id,
            "canonical_name": renamed_to or surface_form,
            "visibility": "party_known",
            "resolved_by": "dm_memory_repair",
            "source_trace_id": source_trace_id,
            "evidence": {"source": "dm_memory_repair", "field": "identity_arbitration", "action": "keep_distinct"},
        }
        return [record]
    return []


def _apply_resolutions(campaign, patch, resolutions, source_trace_id=None):
    """Rewrite a compiled patch according to per-conflict DM resolutions.

    Returns ``(repaired_patch, new_resolution_records)``. Identity-resolution
    records are NOT persisted here; they are attached to the repaired patch's
    ``resolution_records`` list so the caller's memory-write transaction (via
    ``apply_compiled_session_memory_patch``) commits them atomically with the
    accepted patch. A rejected/abandoned repair therefore leaves no durable
    resolution behind.
    """
    repaired = json.loads(json.dumps(patch, default=str))
    resolution_records = []

    id_remap = {}
    keep_rename = {}
    drop_ids = set()
    for resolution in resolutions:
        dup_id = resolution.get("duplicate_id")
        action = resolution.get("resolution")
        if action == "skip":
            drop_ids.add(dup_id)
            continue
        target_id = resolution.get("target_id") or dup_id
        if action == "merge":
            id_remap[dup_id] = target_id
        elif action == "keep_distinct":
            keep_rename[dup_id] = resolution.get("renamed_to")
        if dup_id and action in ("merge", "keep_distinct"):
            records = _resolution_records_for(
                resolution,
                action,
                source_trace_id=source_trace_id,
                target_id=target_id,
                renamed_to=resolution.get("renamed_to"),
            )
            resolution_records.extend(records)

    if id_remap:
        repaired = _remap_patch_ids(repaired, id_remap)
    for entity_id in drop_ids:
        repaired = _drop_references(repaired, entity_id)
        repaired = _drop_entity(repaired, entity_id)

    # keep_distinct: the surface form now resolves to the kept (new) id instead
    # of the resolver's canonical id, otherwise final-state validation would
    # still flag the mismatch between the term's canonical and the upsert id.
    if keep_rename or any(res.get("resolution") == "keep_distinct" for res in resolutions):
        ref_term_remap = {}
        for resolution in resolutions:
            if resolution.get("resolution") != "keep_distinct":
                continue
            dup_id = resolution.get("duplicate_id")
            target_id = resolution.get("target_id") or dup_id
            surface_form = (resolution.get("surface_form") or "").strip().lower()
            if not surface_form:
                continue
            # Only the term that collided (surface form) moves to the kept id;
            # unrelated refs stay untouched.
            ref_term_remap[surface_form] = target_id
        refs = repaired.get("resolved_entity_refs")
        if isinstance(refs, list) and ref_term_remap:
            for ref in refs:
                if not isinstance(ref, dict):
                    continue
                label_lower = clean_text(ref.get("label"), 200).strip().lower()
                if label_lower in ref_term_remap:
                    ref["entity_id"] = ref_term_remap[label_lower]
                    ref["resolution"] = "reuse_existing"
                    ref["canonical_name"] = ref.get("canonical_name") or ref.get("label")

    if keep_rename:
        entities = repaired.get("upsert_graph_entities")
        if isinstance(entities, list):
            for entity in entities:
                if isinstance(entity, dict) and clean_id(entity.get("id"), "") in keep_rename:
                    new_name = keep_rename[clean_id(entity.get("id"), "")]
                    if new_name:
                        entity["name"] = new_name

    if resolution_records:
        existing = repaired.get("resolution_records")
        if not isinstance(existing, list):
            existing = []
        # The compiled patch carries prior resolver-produced resolution records;
        # append (do not overwrite) the DM-arbitrated records.
        repaired["resolution_records"] = list(existing) + resolution_records

    return repaired, resolution_records


def _revalidate(campaign, patch):
    """Re-run the compiled-patch final-state validation against the repaired patch."""
    from services.session_memory_agent import _known_ids, _validate_final_memory_state

    known = _known_ids(campaign) if campaign else {}
    registry_map = {}
    registry = patch.get("registry")
    if isinstance(registry, list):
        registry_map = {
            entry.get("mention_ref"): entry
            for entry in registry
            if isinstance(entry, dict) and entry.get("mention_ref")
        }
    return _validate_final_memory_state(patch, registry_map, known, campaign)


def _log_repair(campaign_id, event, payload, source_trace_id=None, parent_trace_id=None):
    from services.audit_service import log_audit_event

    try:
        log_audit_event(
            campaign_id,
            event,
            payload.get("summary") or "DM-arbitrated memory repair.",
            payload,
            source="dm_memory_repair",
            actor="session_dm",
            trace_id=source_trace_id,
            parent_trace_id=parent_trace_id,
            trace_label=f"dm_memory_repair: campaign {campaign_id}",
            audit_role="tools",
            # Deliberately NOT commit=True: a failed/rejected repair must not
            # flush pending identity-resolution rows (or any other staged state)
            # into the database. The caller's memory-write transaction owns the
            # commit, so resolution rows land only when the repaired patch is
            # actually accepted.
            commit=False,
        )
    except Exception:
        pass


def attempt_dm_repair(campaign, session, err, audit_context=None, max_attempts=MAX_DM_REPAIR_ATTEMPTS):
    """Run the DM-arbitrated repair chain for a failed memory patch.

    Returns a dict with ``decision`` and ``patch``:

    - ``{"decision": "merge" | "keep_distinct", "patch": <repaired compiled patch>}``
    - ``{"decision": "skip_memory_write", "patch": None}``  (drop the memory write)

    Returns ``None`` when the error is not repair-eligible or the DM could not
    produce a usable decision within ``max_attempts``.
    """
    if not is_repair_eligible(err):
        return None
    patch = _failed_patch(err)
    if patch is None:
        return None

    conflicts = parse_identity_conflicts(err)
    if not conflicts:
        return None

    source_trace_id = None
    parent_trace_id = None
    if isinstance(audit_context, dict):
        source_trace_id = audit_context.get("trace_id")
        parent_trace_id = audit_context.get("parent_trace_id")

    campaign_id = campaign.id if campaign else None

    context = _build_arbitration_context(campaign, session, err, conflicts)
    decision = None
    last_error = None
    for _attempt in range(max(1, int(max_attempts))):
        try:
            decision = _request_dm_decision(context, audit_context=audit_context)
        except Exception as exc:
            last_error = repr(exc)
            decision = None
        normalized = _normalize_dm_decision(conflicts, decision)
        if normalized is not None:
            break

    if normalized is None:
        _log_repair(
            campaign_id,
            "dm_memory_repair_failed",
            {"summary": "DM memory repair chain produced no usable arbitration decision.",
             "error": last_error or "no_usable_decision", "conflicts": conflicts},
            source_trace_id=source_trace_id,
            parent_trace_id=parent_trace_id,
        )
        return None

    if normalized["decision"] == SKIP_MEMORY_WRITE:
        _log_repair(
            campaign_id,
            "dm_memory_repair_skipped",
            {"summary": "DM chose to drop this turn's durable memory write after identity conflict.",
             "decision": normalized, "conflicts": conflicts},
            source_trace_id=source_trace_id,
            parent_trace_id=parent_trace_id,
        )
        return {"decision": SKIP_MEMORY_WRITE, "patch": None}

    repaired, resolution_records = _apply_resolutions(
        campaign,
        patch,
        normalized["resolutions"],
        source_trace_id=source_trace_id,
    )
    validation_errors = _revalidate(campaign, repaired)
    if validation_errors:
        _log_repair(
            campaign_id,
            "dm_memory_repair_rejected",
            {"summary": "DM-repaired memory patch still failed final-state validation.",
             "validation_errors": validation_errors, "decision": normalized},
            source_trace_id=source_trace_id,
            parent_trace_id=parent_trace_id,
        )
        return None

    _log_repair(
        campaign_id,
        "dm_memory_repair_applied",
        {"summary": "DM-arbitrated memory repair resolved identity conflicts.",
         "decision": normalized, "conflicts": conflicts,
         "resolution_records": resolution_records},
        source_trace_id=source_trace_id,
        parent_trace_id=parent_trace_id,
    )
    return {"decision": normalized["decision"], "patch": repaired}

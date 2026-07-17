import re

# ── Authority Precedence ──────────────────────────────────────────────
# Higher index = weaker authority. The compiler must prefer lower indexes.
AUTHORITY_PRECEDENCE = {
    "HUMAN_CLARIFICATION": 1,
    "EXISTING_CANONICAL_IDENTITY": 2,
    "RESOLVER_PACKET": 3,
    "VISIBLE_TRANSCRIPT": 4,
    "ALIASES_AND_SCENE_CONTINUITY": 5,
    "HEURISTIC_CANDIDATE": 6,
}

# ── Identity Statuses ─────────────────────────────────────────────────
IDENTITY_STATUSES = {
    "known_hidden",
    "known_public",
    "intentionally_undetermined",
    "provisional_new_entity",
    "provisional_unknown",
    "candidate_existing_entity",
}

# ── Resolution Decisions ──────────────────────────────────────────────
RESOLUTION_DECISIONS = {
    "reuse_existing",
    "create_new",
    "add_alias",
    "rename_existing",
    "create_provisional",
    "defer_resolution",
    "request_clarification",
    "reject",
}

# ── Resolution States ─────────────────────────────────────────────────
RESOLUTION_STATES = {
    "rejected",
    "unresolved_error",
    "deferred_resolution",
    "clarification_requested",
    "provisional",
    "resolved",
}

# ── Clarification Kinds ───────────────────────────────────────────────
CLARIFICATION_KINDS = {
    "identity",
    "location",
    "causality",
    "ownership",
    "hidden_canon",
}

# ── Clarification Statuses ────────────────────────────────────────────
CLARIFICATION_STATUSES = {
    "pending",
    "answered",
    "dismissed",
    "obsolete",
    "resolved",
}

# ── Memory Run Statuses ───────────────────────────────────────────────
MEMORY_RUN_STATUSES = {
    "completed",
    "completed_with_pending_clarifications",
    "failed",
}

# ── Source Contract Markers ───────────────────────────────────────────
SOURCE_CONTRACT_COMPILED_V2 = "compiled_session_memory_v2"

# ── Compilation Order (for documentation + runtime validation) ────────
COMPILATION_ORDER = [
    "extract_mentions",
    "ingest_resolver_packet",
    "build_registry",
    "allocate_durable_ids",
    "compile_scene",
    "compile_cast",
    "compile_npc_updates",
    "compile_graph_entities",
    "compile_relations",
    "compile_facts",
    "compile_clocks",
    "compile_events",
    "compile_summary",
    "compile_anchors",
    "validate_final_state",
]

# ── Blocking Scope Sets (for clarifications) ─────────────────────────
BLOCKING_SCOPES = {
    "identity_merge",
    "npc_update",
    "relation_create",
    "fact_assert",
    "cast_membership",
    "location_ownership",
    "causality_chain",
    "hidden_identity_reveal",
}

# ── Diagnostics Categories ────────────────────────────────────────────
DIAGNOSTICS_TEMPLATE = {
    "reused_existing": [],
    "created_new": [],
    "created_provisional": [],
    "aliases_added": [],
    "deferred_resolutions": [],
    "clarification_requests": [],
    "rejected_mutations": [],
    "blocked_mutations": [],
    "substitutions": [],
}


def make_diagnostics():
    import json
    return json.loads(json.dumps(DIAGNOSTICS_TEMPLATE))

# ── Valid Evidence Sources ────────────────────────────────────────────
EVIDENCE_SOURCES = {
    "memory_resolver_packet",
    "visible_transcript",
    "prior_durable_memory",
    "existing_alias",
    "prior_scene_cast",
    "clarification_answer",
    "resolver_tool_result",
}

# ── Resolver Packet Schema Validation ─────────────────────────────────

_VALID_MENTION_REF = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")


def validate_resolver_packet(packet):
    if not isinstance(packet, dict):
        return False, "resolver_packet must be a dict"
    entity_mentions = packet.get("entity_mentions")
    if entity_mentions is None:
        return True, None
    if not isinstance(entity_mentions, list):
        return False, "entity_mentions must be a list"
    for idx, mention in enumerate(entity_mentions):
        ok, err = validate_entity_mention(mention)
        if not ok:
            return False, f"entity_mentions[{idx}]: {err}"
    return True, None


def validate_entity_mention(mention):
    if not isinstance(mention, dict):
        return False, "must be a dict"
    mention_ref = mention.get("mention_ref", "")
    if not _VALID_MENTION_REF.match(str(mention_ref)):
        return False, f"invalid mention_ref: {mention_ref!r}"
    identity_status = mention.get("identity_status", "")
    if identity_status not in IDENTITY_STATUSES:
        return False, f"invalid identity_status: {identity_status!r}"
    surface_form = mention.get("surface_form", "")
    if not isinstance(surface_form, str) or not surface_form.strip():
        return False, "surface_form is required and must be non-empty"
    visibility = mention.get("visibility", "dm_private")
    if visibility not in ("public", "party_known", "dm_private"):
        return False, f"invalid visibility: {visibility!r}"
    canonical_id = mention.get("canonical_id")
    if canonical_id is not None and not isinstance(canonical_id, str):
        return False, "canonical_id must be a string or null"
    evidence_refs = mention.get("evidence_refs")
    if evidence_refs is not None and not isinstance(evidence_refs, list):
        return False, "evidence_refs must be a list or null"
    return True, None


# ── Clarification Request Schema ──────────────────────────────────────


def validate_clarification_request(req):
    if not isinstance(req, dict):
        return False, "must be a dict"
    if req.get("kind") not in CLARIFICATION_KINDS:
        return False, f"invalid kind: {req.get('kind')!r}"
    if not isinstance(req.get("mention_ref"), str) or not req["mention_ref"].strip():
        return False, "mention_ref is required"
    if not isinstance(req.get("question"), str) or not req["question"].strip():
        return False, "question is required"
    blocking_scope = req.get("blocking_scope")
    if isinstance(blocking_scope, list):
        for s in blocking_scope:
            if s not in BLOCKING_SCOPES:
                return False, f"invalid blocking_scope item: {s!r}"
    return True, None


# ── Resolution Registry Entry Schema ──────────────────────────────────


def validate_registry_entry(entry):
    if not isinstance(entry, dict):
        return False, "must be a dict"
    required = ["mention_ref", "surface_form", "decision", "identity_status"]
    for field in required:
        if field not in entry:
            return False, f"missing required field: {field}"
    if entry["decision"] not in RESOLUTION_DECISIONS:
        return False, f"invalid decision: {entry['decision']!r}"
    if entry["identity_status"] not in IDENTITY_STATUSES:
        return False, f"invalid identity_status: {entry['identity_status']!r}"
    if "canonical_id" in entry and entry["canonical_id"] is not None:
        if not isinstance(entry["canonical_id"], str):
            return False, "canonical_id must be a string or None"
    return True, None


# ── Identity Resolution Record Schema ─────────────────────────────────


def validate_identity_resolution(record):
    if not isinstance(record, dict):
        return False, "must be a dict"
    required = ["mention_entity_id", "canonical_id", "resolution_action"]
    for field in required:
        if field not in record:
            return False, f"missing required field: {field}"
    if record["resolution_action"] not in ("same_identity", "distinct_identity", "retconned", "add_alias"):
        return False, f"invalid resolution_action: {record['resolution_action']!r}"
    return True, None


# ── Diagnostics Validation ────────────────────────────────────────────


def validate_diagnostics(diagnostics):
    if not isinstance(diagnostics, dict):
        return False, "must be a dict"
    for key in DIAGNOSTICS_TEMPLATE:
        if key in diagnostics and not isinstance(diagnostics[key], list):
            return False, f"{key} must be a list"
    substitutions = diagnostics.get("substitutions", [])
    if not isinstance(substitutions, list):
        return False, "substitutions must be a list"
    if len(substitutions) > 0:
        return False, "substitutions must be empty for compiled session-memory patches"
    return True, None


# ── Identity-Worthy Check ─────────────────────────────────────────────
# Heuristic to determine if a noun phrase is identity-worthy (should get
# a provisional entity) vs a generic descriptor that can be ignored.


_IDENTITY_WORTHY_PATTERNS = [
    r"\bthe\s+[\w'-]+(?:\s+[\w'-]+){0,3}\b",  # "the grey-cloaked figure", "the innkeeper", "the hooded figure"
    r"\ba\s+(?:tall|short|hooded|masked|robed|armored|cloaked|mysterious)\s+[\w'-]+\b",
    r"\b[A-Z][a-z']+(?:\s+[A-Z][a-z']+)+\b",  # Proper names like "Kaelen Morwen"
    r"\bthe\s+[\w'-]+\b",                        # "the guard", "the innkeeper"
]


def is_identity_worthy(phrase):
    if not isinstance(phrase, str) or len(phrase.strip()) < 4:
        return False
    phrase_lower = phrase.strip().lower()
    generic_terms = {"someone", "something", "a person", "people", "them", "everyone", "nobody", "it", "that"}
    if phrase_lower in generic_terms:
        return False
    for pattern in _IDENTITY_WORTHY_PATTERNS:
        if re.search(pattern, phrase):
            return True
    proper_name = re.match(r"^[A-Z][a-z']+(\s+[A-Z][a-z']+)*$", phrase.strip())
    if proper_name:
        return True
    return False


# ── Evidence-Aware Identity Matching ──────────────────────────────────

def identity_in_evidence(registry_entry, evidence_dict):
    evidence = registry_entry.get("evidence") if isinstance(registry_entry.get("evidence"), list) else []
    for ev in evidence:
        if not isinstance(ev, dict):
            continue
        source = ev.get("source", "")
        if source in evidence_dict:
            return evidence_dict[source]
    return False


def can_reuse_existing(
    proposed_name,
    proposed_id,
    canonical_name,
    canonical_id,
    decision,
    evidence,
):
    if decision not in RESOLUTION_DECISIONS:
        return False, "unknown decision"
    if decision not in ("reuse_existing", "add_alias", "rename_existing"):
        return False, "decision does not authorize reuse of existing identity"

    if not canonical_id or not canonical_name:
        return False, "missing canonical identity"

    if proposed_id and proposed_id.lower() == canonical_id.lower():
        return True, None

    evidence_list = evidence if isinstance(evidence, list) else []
    if not evidence_list:
        return False, "no evidence provided for identity reuse"

    for ev in evidence_list:
        if not isinstance(ev, dict):
            continue
        source = ev.get("source", "")
        if source == "memory_resolver_packet":
            return True, "resolver packet explicitly authorizes reuse"
        if source == "visible_transcript":
            transcript_ref = ev.get("field", "")
            if canonical_name.lower() in str(transcript_ref).lower():
                return True, "canonical name found in visible transcript"
        if source == "prior_durable_memory":
            return True, "prior durable memory supports reuse"
        if source == "existing_alias":
            return True, "existing alias supports reuse"
        if source == "clarification_answer":
            return True, "clarification answer explicitly resolves identity"

    return False, f"evidence chain does not support reusing {canonical_name!r} for {proposed_name!r}"


# ── Final-State Invariants ────────────────────────────────────────────


FINAL_STATE_INVARIANTS = [
    "active_cast_ids_exist",
    "relation_endpoints_exist",
    "fact_entity_refs_exist",
    "npc_updates_target_canonical_id",
    "scene_location_exists",
    "no_incompatible_canonical_names",
    "no_provisional_silent_merge",
    "no_clarification_blocked_mutation",
    "alias_visibility_consistent",
    "summary_scene_graph_consistent",
    "cast_sets_no_overlap",
    "substitutions_empty",
]


# ── Evidence Status Model ─────────────────────────────────────────────

EVIDENCE_STATUSES = {
    "contradicted",
    "insufficiently_supported",
    "supported_by_evidence",
    "supported_by_rules",
}

EVIDENCE_SOURCE_TYPES = {
    "transcript_message",
    "prior_memory_record",
    "world_event",
    "clock_rule",
    "resolver_output",
    "clarification_answer",
    "deterministic_rule",
}

PIPELINE_STAGES = {
    "proposed",
    "resolved",
    "validated",
    "applied",
    "rejected",
    "unresolved",
}


def make_evidence_source(source_type, source_id, description=None, confidence=None, ambiguity=None):
    source = {
        "source_type": source_type,
        "source_id": source_id,
    }
    if description:
        source["description"] = description
    if confidence is not None:
        source["confidence"] = confidence
    if ambiguity is not None:
        source["ambiguity"] = ambiguity
    return source


_STRONG_EVIDENCE_SOURCES = frozenset({
    "memory_resolver_packet",
    "prior_durable_memory",
    "prior_memory_record",
    "clarification_answer",
    "existing_alias",
    "prior_scene_cast",
})

_WEAK_EVIDENCE_SOURCES = frozenset({
    "visible_transcript",
    "transcript_message",
    "resolver_output",
    "resolver_tool_result",
    "world_event",
})

_RULE_DERIVED_SOURCES = frozenset({
    "clock_rule",
    "deterministic_rule",
})


def determine_evidence_status(evidence_sources, is_rule_derived=False):
    if not isinstance(evidence_sources, list):
        evidence_sources = []
    if is_rule_derived:
        return "supported_by_rules"
    if not evidence_sources:
        return "insufficiently_supported"
    for ev in evidence_sources:
        if isinstance(ev, dict):
            src_type = ev.get("source_type") or ev.get("source", "")
            if src_type in _RULE_DERIVED_SOURCES:
                return "supported_by_rules"
            if src_type in _STRONG_EVIDENCE_SOURCES:
                return "supported_by_evidence"
            if src_type in _WEAK_EVIDENCE_SOURCES:
                conf = ev.get("confidence")
                if conf is None or conf >= 0.3:
                    return "supported_by_evidence"
    for ev in evidence_sources:
        if isinstance(ev, (str, int)):
            return "supported_by_evidence"
    return "insufficiently_supported"


def normalize_evidence_status(value):
    status = str(value or "").strip().lower()
    return status if status in EVIDENCE_STATUSES else None


def make_provenance_record(
    source_player_message_id=None,
    source_dm_message_id=None,
    prior_memory_record_ids=None,
    world_event_ids=None,
    clock_trigger_id=None,
    tool_name=None,
    tool_result_id=None,
    evidence_sources=None,
    evidence_status=None,
    pipeline_stage=None,
    resolution_confidence=None,
    ambiguity_status=None,
    trace_id=None,
    parent_trace_id=None,
    build_sha=None,
):
    record = {}
    if source_player_message_id is not None:
        record["source_player_message_id"] = source_player_message_id
    if source_dm_message_id is not None:
        record["source_dm_message_id"] = source_dm_message_id
    if prior_memory_record_ids:
        record["prior_memory_record_ids"] = prior_memory_record_ids if isinstance(prior_memory_record_ids, list) else [prior_memory_record_ids]
    if world_event_ids:
        record["world_event_ids"] = world_event_ids if isinstance(world_event_ids, list) else [world_event_ids]
    if clock_trigger_id:
        record["clock_trigger_id"] = clock_trigger_id
    if tool_name:
        record["tool_name"] = tool_name
    if tool_result_id:
        record["tool_result_id"] = tool_result_id
    if evidence_sources:
        record["evidence_sources"] = evidence_sources if isinstance(evidence_sources, list) else [evidence_sources]
    if evidence_status:
        record["evidence_status"] = evidence_status
    if pipeline_stage:
        record["pipeline_stage"] = pipeline_stage
    if resolution_confidence is not None:
        record["resolution_confidence"] = resolution_confidence
    if ambiguity_status is not None:
        record["ambiguity_status"] = ambiguity_status
    if trace_id:
        record["trace_id"] = trace_id
    if parent_trace_id:
        record["parent_trace_id"] = parent_trace_id
    if build_sha:
        record["build_sha"] = build_sha
    return record

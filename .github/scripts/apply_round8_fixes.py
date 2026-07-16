from pathlib import Path
import re


def read(path):
    return Path(path).read_text()


def write(path, text):
    Path(path).write_text(text)


def replace_once(path, old, new):
    text = read(path)
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one match, found {count}: {old[:120]!r}")
    write(path, text.replace(old, new, 1))


def replace_regex_once(path, pattern, replacement):
    text = read(path)
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise RuntimeError(f"{path}: regex expected one match, found {count}: {pattern[:120]!r}")
    write(path, updated)


# CampaignClarification persists the kind of identity being resolved.
replace_once(
    "server/models.py",
    "    surface_form = db.Column(db.Text, nullable=True)\n\n    question = db.Column(db.Text, nullable=False)",
    "    surface_form = db.Column(db.Text, nullable=True)\n    entity_type = db.Column(db.String(40), nullable=True)\n\n    question = db.Column(db.Text, nullable=False)",
)
replace_once(
    "server/models.py",
    '            "surface_form": self.surface_form,\n            "question": self.question,',
    '            "surface_form": self.surface_form,\n            "entity_type": self.entity_type,\n            "question": self.question,',
)

# Lightweight SQLite setup must match the model for fresh and existing dev DBs.
replace_once(
    "server/app.py",
    "                mention_entity_id VARCHAR(200),\n                question TEXT NOT NULL,",
    "                mention_entity_id VARCHAR(200),\n                surface_form TEXT,\n                entity_type VARCHAR(40),\n                question TEXT NOT NULL,",
)
replace_once(
    "server/app.py",
    "        db.session.execute(text('CREATE INDEX ix_campaign_clarifications_idempotency ON campaign_clarifications (idempotency_key)'))\n\n    identity_res_columns",
    "        db.session.execute(text('CREATE INDEX ix_campaign_clarifications_idempotency ON campaign_clarifications (idempotency_key)'))\n    if clarification_columns:\n        if 'surface_form' not in clarification_columns:\n            db.session.execute(text('ALTER TABLE campaign_clarifications ADD COLUMN surface_form TEXT'))\n        if 'entity_type' not in clarification_columns:\n            db.session.execute(text('ALTER TABLE campaign_clarifications ADD COLUMN entity_type VARCHAR(40)'))\n\n    identity_res_columns",
)

# Expose graph types as part of durable canon.
replace_once(
    "server/services/session_memory_agent.py",
    "        'entity_names': {\n            clean_id(entity.get('id'), ''): clean_text(entity.get('name'), 160) or ''\n            for entity in graph.get('entities', [])\n            if isinstance(entity, dict) and clean_id(entity.get('id'), '') and clean_text(entity.get('name'), 160)\n        },\n    }",
    "        'entity_names': {\n            clean_id(entity.get('id'), ''): clean_text(entity.get('name'), 160) or ''\n            for entity in graph.get('entities', [])\n            if isinstance(entity, dict) and clean_id(entity.get('id'), '') and clean_text(entity.get('name'), 160)\n        },\n        'entity_types': {\n            clean_id(entity.get('id'), ''): clean_text(entity.get('type'), 40).lower() or 'other'\n            for entity in graph.get('entities', [])\n            if isinstance(entity, dict) and clean_id(entity.get('id'), '')\n        },\n    }",
)

registry_path = "server/services/resolution_registry.py"
registry = read(registry_path)

# NPC/person claims can arrive through entity_claims; use NPC authority for them.
old_dispatch = """        entry = _resolve_entity_claim(
            claim, known_ids, prior_resolution_map, allocated_ids, mention_index
        )
"""
new_dispatch = """        claim_type = clean_text(claim.get("type"), 40).lower() or "other"
        resolver = _resolve_npc_claim if claim_type in ("npc", "person") else _resolve_entity_claim
        entry = resolver(
            claim, known_ids, prior_resolution_map, allocated_ids, mention_index
        )
"""
if registry.count(old_dispatch) != 1:
    raise RuntimeError("entity-claim resolver dispatch pattern changed")
registry = registry.replace(old_dispatch, new_dispatch, 1)

# Candidate matching is keyed by canonical ID and filtered by durable type.
start = registry.index("def find_all_matching_candidates(")
end = registry.index("\n\ndef _resolve_packet_mention", start)
registry = registry[:start] + '''def _canonical_entity_type(known_ids, canonical_id):
    if canonical_id in known_ids.get("npc_ids", set()):
        return "npc"
    return known_ids.get("entity_types", {}).get(canonical_id, "other") or "other"


def _entity_types_compatible(expected_type, candidate_type):
    expected = clean_text(expected_type, 40).lower() or "other"
    candidate = clean_text(candidate_type, 40).lower() or "other"
    if expected in ("npc", "person"):
        return candidate in ("npc", "person")
    if expected == "entity":
        return candidate not in ("npc", "person")
    if expected == "other":
        return candidate not in ("npc", "person")
    return expected == candidate


def find_all_matching_candidates(name, known_ids, prior_resolutions, expected_type):
    candidates = {}
    name_lower = name.strip().lower()

    for npc_id, npc_name in known_ids.get("npc_names", {}).items():
        if npc_name.strip().lower() == name_lower and _entity_types_compatible(expected_type, "npc"):
            candidates[npc_id] = "npc"

    for ent_id, ent_name in known_ids.get("entity_names", {}).items():
        candidate_type = _canonical_entity_type(known_ids, ent_id)
        if ent_name.strip().lower() == name_lower and _entity_types_compatible(expected_type, candidate_type):
            candidates[ent_id] = candidate_type

    if isinstance(prior_resolutions, list):
        for res in prior_resolutions:
            if not isinstance(res, dict) or res.get("resolution_action") != "add_alias":
                continue
            res_name = clean_text(res.get("mention_name"), 400)
            canonical_id = clean_id(res.get("canonical_id"), "")
            if not res_name or res_name.lower() != name_lower or not canonical_id:
                continue
            candidate_type = _canonical_entity_type(known_ids, canonical_id)
            if canonical_id in known_ids.get("entity_ids", set()) and _entity_types_compatible(expected_type, candidate_type):
                candidates[canonical_id] = candidate_type

    return candidates
''' + registry[end:]

# Prior durable resolutions must also satisfy the claimed type.
old_prior = '''    prior = prior_resolution_map.get(mention_ref) or prior_resolution_map.get(name.lower())
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
'''
new_prior = '''    prior = prior_resolution_map.get(mention_ref) or prior_resolution_map.get(name.lower())
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
            entry["entity_type"] = canonical_type
            existing_name = known_ids.get("entity_names", {}).get(canonical_id) or known_ids.get("npc_names", {}).get(canonical_id)
            entry["canonical_name"] = existing_name or canonical_name or name
            entry["decision"] = "reuse_existing"
            entry["resolution_state"] = "resolved"
            entry["identity_status"] = prior.get("visibility", "") == "dm_private" and "known_hidden" or "known_public"
            entry["evidence"].append({"source": "prior_durable_memory", "field": "identity_resolution_record"})
            return entry
'''
if registry.count(old_prior) != 1:
    raise RuntimeError("entity prior-resolution block changed")
registry = registry.replace(old_prior, new_prior, 1)
registry = registry.replace(
    '    candidates = find_all_matching_candidates(name, known_ids, prior_res_list, expected_type="entity")',
    '    candidates = find_all_matching_candidates(name, known_ids, prior_res_list, expected_type=entity_type)',
    1,
)
old_candidate = '''        entry["canonical_id"] = can_id
        existing_name = known_ids.get("entity_names", {}).get(can_id) or known_ids.get("npc_names", {}).get(can_id)
'''
new_candidate = '''        entry["canonical_id"] = can_id
        entry["entity_type"] = can_type
        existing_name = known_ids.get("entity_names", {}).get(can_id) or known_ids.get("npc_names", {}).get(can_id)
'''
if registry.count(old_candidate) < 1:
    raise RuntimeError("entity candidate assignment block changed")
registry = registry.replace(old_candidate, new_candidate, 1)

# Replace clarification answer compilation as a complete typed, no-write state machine.
start = registry.index("def _apply_clarification_answer(")
end = registry.index("\n\ndef _build_clarification_record", start)
registry = registry[:start] + '''def _apply_clarification_answer(pending_clarification, known_ids, allocated_ids):
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
    resolved_canonical_id = clean_id(pending_clarification.get("resolved_canonical_id"), "")
    resolution_action = pending_clarification.get("resolution_action", "")

    if resolution_action == "same_identity" and resolved_canonical_id:
        if resolved_canonical_id not in known_ids.get("entity_ids", set()):
            return None
        existing_name = (
            known_ids.get("npc_names", {}).get(resolved_canonical_id)
            or known_ids.get("entity_names", {}).get(resolved_canonical_id)
        )
        return {
            "mention_ref": mention_ref,
            "surface_form": surface_form,
            "identity_status": "known_hidden",
            "visibility": "dm_private",
            "evidence": [{
                "source": "clarification_answer",
                "field": "resolved_canonical_id",
                "clarification_id": pending_clarification.get("clarification_id"),
            }],
            "canonical_id": resolved_canonical_id,
            "canonical_name": existing_name or surface_form,
            "decision": "reuse_existing",
            "blocked_operations": [],
            "resolution_state": "resolved",
            "entity_type": _canonical_entity_type(known_ids, resolved_canonical_id),
        }

    if resolution_action == "new_entity":
        entity_type = clean_text(pending_clarification.get("entity_type"), 40).lower() or "other"
        new_id = allocate_durable_id(surface_form, allocated_ids)
        return {
            "mention_ref": mention_ref,
            "surface_form": surface_form,
            "identity_status": "provisional_new_entity",
            "visibility": "party_known",
            "evidence": [{
                "source": "clarification_answer",
                "field": "new_entity",
                "clarification_id": pending_clarification.get("clarification_id"),
            }],
            "canonical_id": new_id,
            "canonical_name": surface_form,
            "decision": "create_new",
            "blocked_operations": [],
            "resolution_state": "resolved",
            "entity_type": entity_type,
        }

    if resolution_action == "ignore":
        return {
            "mention_ref": mention_ref,
            "surface_form": surface_form,
            "identity_status": "intentionally_undetermined",
            "visibility": "dm_private",
            "evidence": [{
                "source": "clarification_answer",
                "field": "ignore",
                "clarification_id": pending_clarification.get("clarification_id"),
            }],
            "canonical_id": None,
            "canonical_name": None,
            "decision": "reject",
            "blocked_operations": [],
            "resolution_state": "resolved",
            "entity_type": clean_text(pending_clarification.get("entity_type"), 40).lower() or "other",
        }

    return None
''' + registry[end:]

old_record = '''        "mention_entity_id": entry.get("canonical_id") or mention_ref,
        "surface_form": surface_form,
        "question": f"Is {surface_form} an existing NPC or entity?",
'''
new_record = '''        "mention_entity_id": entry.get("canonical_id") or mention_ref,
        "surface_form": surface_form,
        "entity_type": entry.get("entity_type", "other"),
        "question": f"Is {surface_form} an existing NPC or entity?",
'''
if registry.count(old_record) != 1:
    raise RuntimeError("clarification record block changed")
registry = registry.replace(old_record, new_record, 1)
replace_persist_old = '''            mention_entity_id=cr.get("mention_entity_id"),
            surface_form=cr.get("surface_form"),
            question=cr["question"],
'''
replace_persist_new = '''            mention_entity_id=cr.get("mention_entity_id"),
            surface_form=cr.get("surface_form"),
            entity_type=cr.get("entity_type"),
            question=cr["question"],
'''
if registry.count(replace_persist_old) != 1:
    raise RuntimeError("clarification persistence block changed")
registry = registry.replace(replace_persist_old, replace_persist_new, 1)

# Human clarifications and durable aliases are authority; query errors must fail closed.
start = registry.index("def fetch_prior_resolutions(")
end = registry.index("\n\ndef persist_clarification_requests", start)
registry = registry[:start] + '''def fetch_prior_resolutions(campaign):
    if not campaign:
        return []
    from models import CampaignIdentityResolution
    rows = CampaignIdentityResolution.query.filter_by(campaign_id=campaign.id).all()
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
''' + registry[end:]
write(registry_path, registry)

# Ignore means no mutation, even if a malformed caller supplied a patch.
replace_once(
    "server/routes/sessions.py",
    "    if resolution_action == \"same_identity\":\n",
    "    if resolution_action == \"ignore\" and resolution_patch is not None:\n        return jsonify({'error': 'ignore clarifications cannot include a resolution_patch.'}), 400\n\n    if resolution_action == \"same_identity\":\n",
)

# Compiler defense in depth and canonical type preservation.
agent_path = "server/services/session_memory_agent.py"
agent = read(agent_path)
old_patch_merge = '                    if patch_json:\n                        patch_json = copy.deepcopy(patch_json)'
new_patch_merge = '                    if patch_json and pc.get("resolution_action") in ("same_identity", "new_entity"):\n                        patch_json = copy.deepcopy(patch_json)'
if agent.count(old_patch_merge) != 1:
    raise RuntimeError("clarification patch merge block changed")
agent = agent.replace(old_patch_merge, new_patch_merge, 1)
old_type = '''        entity_type = entry.get("entity_type", "other")
        if entity_type in ("npc", "person"):
'''
new_type = '''        requested_type = clean_text(entry.get("entity_type"), 40).lower() or "other"
        durable_type = "npc" if entity_id in known.get("npc_ids", set()) else known.get("entity_types", {}).get(entity_id)
        entity_type = durable_type or requested_type
        if entity_type in ("npc", "person"):
'''
if agent.count(old_type) != 1:
    raise RuntimeError("entity type compilation block changed")
agent = agent.replace(old_type, new_type, 1)
write(agent_path, agent)

# Application cannot downgrade an established canonical type to "other".
dm_path = "server/services/dm_tools.py"
dm = read(dm_path)
old_collision = '''                if existing_type and new_type and existing_type != new_type and existing_type != "other" and new_type != "other":
                    raise MemoryPipelineError(
                        stage="validation",
                        code="exact_id_collision",
                        message=f"Entity ID {entity_id!r} already exists with incompatible type {existing_type!r} vs {new_type!r}.",
                    )
'''
new_collision = '''                if existing_type and new_type:
                    if new_type == "other":
                        entity["type"] = existing_type
                    elif existing_type != "other" and existing_type != new_type:
                        raise MemoryPipelineError(
                            stage="validation",
                            code="exact_id_collision",
                            message=f"Entity ID {entity_id!r} already exists with incompatible type {existing_type!r} vs {new_type!r}.",
                        )
'''
if dm.count(old_collision) != 1:
    raise RuntimeError("application canonical type validation block changed")
write(dm_path, dm.replace(old_collision, new_collision, 1))

# Use the personal runner for the PR checks going forward.
workflow_path = ".github/workflows/pr_review.yml"
workflow = read(workflow_path)
if "runs-on: ubuntu-latest" not in workflow:
    raise RuntimeError("PR workflow runner pattern changed")
write(workflow_path, workflow.replace("runs-on: ubuntu-latest", "runs-on: self-hosted"))

# Focused regressions for the five remaining integrity gaps.
test_path = "server/tests/test_session_memory_integrity.py"
tests = read(test_path)
marker = "\n\nif __name__ == '__main__':\n"
if marker not in tests:
    raise RuntimeError("test class terminator not found")
additions = r'''

    def test_entity_claim_typed_npc_reuses_canonical_npc(self):
        known = _known_ids(self.campaign)
        registry, _, _, _ = build_canonical_resolution_registry(
            self.campaign,
            self._base_context(),
            {"entity_claims": [{"name": "Mira", "type": "npc", "mention_ref": "mira_claim"}]},
            None,
            [],
            known,
        )
        entry = next(item for item in registry if item.get("mention_ref") == "mira_claim")
        self.assertEqual(entry.get("canonical_id"), "mira")
        self.assertEqual(entry.get("entity_type"), "npc")
        self.assertEqual(entry.get("decision"), "reuse_existing")

    def test_graph_subtype_name_match_does_not_cross_types(self):
        known = _known_ids(self.campaign)
        registry, _, _, _ = build_canonical_resolution_registry(
            self.campaign,
            self._base_context(),
            {"entity_claims": [{"name": "Waterdeep", "type": "organization", "mention_ref": "waterdeep_org"}]},
            None,
            [],
            known,
        )
        entry = next(item for item in registry if item.get("mention_ref") == "waterdeep_org")
        self.assertNotEqual(entry.get("canonical_id"), "waterdeep")
        self.assertEqual(entry.get("entity_type"), "organization")

    def test_ignore_clarification_never_merges_resolution_patch(self):
        clar = CampaignClarification(
            campaign_id=self.campaign.id,
            clarification_id="clar_ignore_patch",
            idempotency_key="key_ignore_patch",
            kind="identity",
            mention_ref="ignored_figure",
            mention_entity_id="ignored_figure",
            surface_form="Ignored Figure",
            entity_type="npc",
            question="Ignore?",
            status="answered",
            resolution_action="ignore",
            resolution_patch_json={"upsert_graph_entities": [{"name": "Injected Entity", "type": "organization"}]},
        )
        db.session.add(clar)
        db.session.commit()
        compiled = compile_staged_memory_patch(self._base_context(), {}, {})
        self.assertIn("clar_ignore_patch", compiled.get("consumed_clarification_ids", []))
        self.assertFalse(any(item.get("name") == "Injected Entity" for item in compiled.get("upsert_graph_entities", [])))

    def test_new_entity_clarification_preserves_npc_type_and_patch(self):
        clar = CampaignClarification(
            campaign_id=self.campaign.id,
            clarification_id="clar_new_npc_typed",
            idempotency_key="key_new_npc_typed",
            kind="identity",
            mention_ref="masked_guard_ref",
            mention_entity_id="masked_guard_ref",
            surface_form="Masked Guard",
            entity_type="npc",
            question="New NPC?",
            status="answered",
            resolution_action="new_entity",
            resolution_patch_json={"update_npc_actors": [{"id": "masked_guard_ref", "role": "Guard"}]},
        )
        db.session.add(clar)
        db.session.commit()
        compiled = compile_staged_memory_patch(self._base_context(), {}, {})
        self.assertIn("clar_new_npc_typed", compiled.get("consumed_clarification_ids", []))
        npc_updates = compiled.get("update_npc_actors", [])
        self.assertEqual(len(npc_updates), 1)
        new_id = npc_updates[0]["id"]
        entity = next(item for item in compiled.get("upsert_graph_entities", []) if item.get("id") == new_id)
        self.assertEqual(entity.get("type"), "npc")

    def test_same_identity_clarification_preserves_durable_entity_type(self):
        clar = CampaignClarification(
            campaign_id=self.campaign.id,
            clarification_id="clar_same_location",
            idempotency_key="key_same_location",
            kind="identity",
            mention_ref="city_reference",
            mention_entity_id="city_reference",
            surface_form="the city",
            entity_type="other",
            question="Waterdeep?",
            status="answered",
            resolution_action="same_identity",
            resolved_canonical_id="waterdeep",
            resolution_patch_json={"upsert_graph_entities": [{"id": "waterdeep", "type": "other", "summary": "Updated"}]},
        )
        db.session.add(clar)
        db.session.commit()
        compiled = compile_staged_memory_patch(self._base_context(), {}, {})
        entity = next(item for item in compiled.get("upsert_graph_entities", []) if item.get("id") == "waterdeep")
        self.assertEqual(entity.get("type"), "location")
'''
write(test_path, tests.replace(marker, additions + marker, 1))

print("Round 8 patch applied successfully.")

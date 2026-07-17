import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(relative_path):
    return (ROOT / relative_path).read_text()


def _write(relative_path, content):
    (ROOT / relative_path).write_text(content)


def _replace_once(relative_path, old, new):
    content = _read(relative_path)
    count = content.count(old)
    if count != 1:
        raise AssertionError(
            f"{relative_path}: expected exactly one match, found {count}: {old[:120]!r}"
        )
    _write(relative_path, content.replace(old, new, 1))


def _apply_round8_patch():
    registry_path = "server/services/resolution_registry.py"
    registry = _read(registry_path)

    old_dispatch = '''        entry = _resolve_entity_claim(
            claim, known_ids, prior_resolution_map, allocated_ids, mention_index
        )
'''
    new_dispatch = '''        claim_type = clean_text(claim.get("type"), 40).lower() or "other"
        resolver = _resolve_npc_claim if claim_type in ("npc", "person") else _resolve_entity_claim
        entry = resolver(
            claim, known_ids, prior_resolution_map, allocated_ids, mention_index
        )
'''
    if registry.count(old_dispatch) != 1:
        raise AssertionError("entity-claim resolver dispatch pattern changed")
    registry = registry.replace(old_dispatch, new_dispatch, 1)

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
    if expected in ("entity", "other"):
        return candidate not in ("npc", "person")
    return expected == candidate


def find_all_matching_candidates(name, known_ids, prior_resolutions, expected_type):
    candidates = {}
    name_lower = name.strip().lower()

    for npc_id, npc_name in known_ids.get("npc_names", {}).items():
        if npc_name.strip().lower() == name_lower and _entity_types_compatible(expected_type, "npc"):
            candidates[npc_id] = "npc"

    for entity_id, entity_name in known_ids.get("entity_names", {}).items():
        candidate_type = _canonical_entity_type(known_ids, entity_id)
        if entity_name.strip().lower() == name_lower and _entity_types_compatible(expected_type, candidate_type):
            candidates[entity_id] = candidate_type

    if isinstance(prior_resolutions, list):
        for resolution in prior_resolutions:
            if not isinstance(resolution, dict) or resolution.get("resolution_action") != "add_alias":
                continue
            alias = clean_text(resolution.get("mention_name"), 400)
            canonical_id = clean_id(resolution.get("canonical_id"), "")
            if not alias or alias.lower() != name_lower or not canonical_id:
                continue
            candidate_type = _canonical_entity_type(known_ids, canonical_id)
            if canonical_id in known_ids.get("entity_ids", set()) and _entity_types_compatible(expected_type, candidate_type):
                candidates[canonical_id] = candidate_type

    return candidates
''' + registry[end:]

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
        raise AssertionError("entity prior-resolution block changed")
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
        raise AssertionError("entity candidate assignment block changed")
    registry = registry.replace(old_candidate, new_candidate, 1)

    start = registry.index("def _apply_clarification_answer(")
    end = registry.index("\n\ndef _build_clarification_record", start)
    registry = registry[:start] + '''def _clarification_entity_type(pending_clarification):
    blocking_scope = pending_clarification.get("blocking_scope")
    if isinstance(blocking_scope, list) and "npc_update" in blocking_scope:
        return "npc"

    patch = pending_clarification.get("resolution_patch_json") or pending_clarification.get("resolution_patch")
    if isinstance(patch, dict):
        if isinstance(patch.get("update_npc_actors"), list) and patch["update_npc_actors"]:
            return "npc"
        entities = patch.get("upsert_graph_entities")
        if isinstance(entities, list):
            for entity in entities:
                if isinstance(entity, dict):
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
        entity_type = _clarification_entity_type(pending_clarification)
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
            "entity_type": _clarification_entity_type(pending_clarification),
        }

    return None
''' + registry[end:]

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
    _write(registry_path, registry)

    agent_path = "server/services/session_memory_agent.py"
    agent = _read(agent_path)
    old_known = '''        'entity_names': {
            clean_id(entity.get('id'), ''): clean_text(entity.get('name'), 160) or ''
            for entity in graph.get('entities', [])
            if isinstance(entity, dict) and clean_id(entity.get('id'), '') and clean_text(entity.get('name'), 160)
        },
    }'''
    new_known = '''        'entity_names': {
            clean_id(entity.get('id'), ''): clean_text(entity.get('name'), 160) or ''
            for entity in graph.get('entities', [])
            if isinstance(entity, dict) and clean_id(entity.get('id'), '') and clean_text(entity.get('name'), 160)
        },
        'entity_types': {
            clean_id(entity.get('id'), ''): clean_text(entity.get('type'), 40).lower() or 'other'
            for entity in graph.get('entities', [])
            if isinstance(entity, dict) and clean_id(entity.get('id'), '')
        },
    }'''
    if agent.count(old_known) != 1:
        raise AssertionError("known entity type block changed")
    agent = agent.replace(old_known, new_known, 1)

    old_patch_merge = '                    if patch_json:\n                        patch_json = copy.deepcopy(patch_json)'
    new_patch_merge = '                    if patch_json and pc.get("resolution_action") in ("same_identity", "new_entity"):\n                        patch_json = copy.deepcopy(patch_json)'
    if agent.count(old_patch_merge) != 1:
        raise AssertionError("clarification patch merge block changed")
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
        raise AssertionError("entity type compilation block changed")
    agent = agent.replace(old_type, new_type, 1)
    _write(agent_path, agent)

    tests_path = "server/tests/test_session_memory_integrity.py"
    tests = _read(tests_path)
    marker = "\n\nif __name__ == '__main__':\n"
    if marker not in tests:
        raise AssertionError("test module terminator not found")
    if "test_round8_entity_claim_typed_npc_reuses_canonical_npc" not in tests:
        additions = r'''

    def test_round8_entity_claim_typed_npc_reuses_canonical_npc(self):
        known = _known_ids(self.campaign)
        registry, _, _, _ = build_canonical_resolution_registry(
            self.campaign,
            self._base_context(),
            {"entity_claims": [{"name": "Mira", "type": "npc", "mention_ref": "mira_typed_claim"}]},
            None,
            [],
            known,
        )
        entry = next(item for item in registry if item.get("mention_ref") == "mira_typed_claim")
        self.assertEqual(entry.get("canonical_id"), "mira")
        self.assertEqual(entry.get("entity_type"), "npc")
        self.assertEqual(entry.get("decision"), "reuse_existing")

    def test_round8_graph_subtype_name_match_does_not_cross_types(self):
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

    def test_round8_ignore_clarification_never_merges_resolution_patch(self):
        clarification = CampaignClarification(
            campaign_id=self.campaign.id,
            clarification_id="clar_round8_ignore_patch",
            idempotency_key="key_round8_ignore_patch",
            kind="identity",
            mention_ref="ignored_figure",
            mention_entity_id="ignored_figure",
            surface_form="Ignored Figure",
            question="Ignore?",
            blocking_scope=["npc_update"],
            status="answered",
            resolution_action="ignore",
            resolution_patch_json={"upsert_graph_entities": [{"name": "Injected Entity", "type": "organization"}]},
        )
        db.session.add(clarification)
        db.session.commit()
        compiled = compile_staged_memory_patch(self._base_context(), {}, {})
        self.assertIn("clar_round8_ignore_patch", compiled.get("consumed_clarification_ids", []))
        self.assertFalse(any(
            item.get("name") == "Injected Entity"
            for item in compiled.get("upsert_graph_entities", [])
        ))

    def test_round8_new_entity_clarification_preserves_npc_type_and_patch(self):
        clarification = CampaignClarification(
            campaign_id=self.campaign.id,
            clarification_id="clar_round8_new_npc",
            idempotency_key="key_round8_new_npc",
            kind="identity",
            mention_ref="masked_guard_ref",
            mention_entity_id="masked_guard_ref",
            surface_form="Masked Guard",
            question="New NPC?",
            blocking_scope=["npc_update"],
            status="answered",
            resolution_action="new_entity",
            resolution_patch_json={"update_npc_actors": [{"id": "masked_guard_ref", "role": "Guard"}]},
        )
        db.session.add(clarification)
        db.session.commit()
        compiled = compile_staged_memory_patch(self._base_context(), {}, {})
        self.assertIn("clar_round8_new_npc", compiled.get("consumed_clarification_ids", []))
        npc_updates = compiled.get("update_npc_actors", [])
        self.assertEqual(len(npc_updates), 1)
        new_id = npc_updates[0]["id"]
        entity = next(
            item for item in compiled.get("upsert_graph_entities", [])
            if item.get("id") == new_id
        )
        self.assertEqual(entity.get("type"), "npc")

    def test_round8_same_identity_preserves_durable_entity_type(self):
        clarification = CampaignClarification(
            campaign_id=self.campaign.id,
            clarification_id="clar_round8_same_location",
            idempotency_key="key_round8_same_location",
            kind="identity",
            mention_ref="city_reference",
            mention_entity_id="city_reference",
            surface_form="the city",
            question="Waterdeep?",
            status="answered",
            resolution_action="same_identity",
            resolved_canonical_id="waterdeep",
            resolution_patch_json={
                "upsert_graph_entities": [{"id": "waterdeep", "type": "other", "summary": "Updated"}]
            },
        )
        db.session.add(clarification)
        db.session.commit()
        compiled = compile_staged_memory_patch(self._base_context(), {}, {})
        entity = next(
            item for item in compiled.get("upsert_graph_entities", [])
            if item.get("id") == "waterdeep"
        )
        self.assertEqual(entity.get("type"), "location")

    def test_round8_durable_identity_reads_fail_closed(self):
        from services.resolution_registry import (
            fetch_pending_clarifications,
            fetch_prior_resolutions,
        )
        with unittest.mock.patch(
            'models.CampaignIdentityResolution.query',
            new_callable=unittest.mock.PropertyMock,
        ) as prior_query:
            prior_query.return_value.filter_by.return_value.all.side_effect = RuntimeError("db unavailable")
            with self.assertRaises(RuntimeError):
                fetch_prior_resolutions(self.campaign)
        with unittest.mock.patch(
            'models.CampaignClarification.query',
            new_callable=unittest.mock.PropertyMock,
        ) as clarification_query:
            clarification_query.return_value.filter_by.return_value.filter.return_value.all.side_effect = RuntimeError("db unavailable")
            with self.assertRaises(RuntimeError):
                fetch_pending_clarifications(self.campaign)
'''
        tests = tests.replace(marker, additions + marker, 1)
        _write(tests_path, tests)


def test_generate_round8_patch_artifact():
    if os.environ.get("ROUND8_PATCH_CHILD") == "1":
        return

    _apply_round8_patch()

    output_dir = ROOT / "test-logs" / "round8-patch"
    output_dir.mkdir(parents=True, exist_ok=True)
    patched_files = [
        "server/services/resolution_registry.py",
        "server/services/session_memory_agent.py",
        "server/tests/test_session_memory_integrity.py",
    ]
    for relative_path in patched_files:
        destination = output_dir / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative_path, destination)

    environment = os.environ.copy()
    environment["ROUND8_PATCH_CHILD"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "server/tests/test_session_memory_integrity.py",
            "server/tests/test_000_round8_patch_artifact.py",
            "-q",
            "-W",
            "error",
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=240,
    )
    (output_dir / "focused-tests.stdout.log").write_text(result.stdout)
    (output_dir / "focused-tests.stderr.log").write_text(result.stderr)
    assert result.returncode == 0, result.stdout + "\n" + result.stderr

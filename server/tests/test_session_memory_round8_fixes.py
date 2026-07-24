import json
import os
import sys
import unittest
from unittest import mock

from flask import Flask

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from models import (
    Campaign,
    CampaignClarification,
    CampaignIdentityResolution,
    CampaignSession,
    CampaignWorld,
    NPCActor,
    User,
    db,
)
from services.resolution_registry import (
    build_canonical_resolution_registry,
    fetch_pending_clarifications,
    fetch_prior_resolutions,
    find_all_matching_candidates,
)
from services.session_memory_agent import _known_ids, compile_staged_memory_patch
from services.dm_tools import apply_compiled_session_memory_patch
from services.memory_resolver_schemas import SOURCE_CONTRACT_COMPILED_V2


class SessionMemoryRound8FixesTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
        self.app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
        self.app.config["SECRET_KEY"] = "test-secret"
        self.app.config["JWT_EXPIRATION_HOURS"] = 24
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        user = User(username="round8_dm", email="round8@example.com")
        user.set_password("password")
        db.session.add(user)
        db.session.commit()

        self.campaign = Campaign(
            name="Round 8 Integrity",
            description="Test",
            user_id=user.id,
        )
        db.session.add(self.campaign)
        db.session.commit()

        graph = {
            "entities": [
                {"id": "waterdeep", "name": "Waterdeep", "type": "location"},
                {
                    "id": "watchful_order",
                    "name": "Watchful Order",
                    "type": "organization",
                },
            ],
            "relations": [],
            "facts": [],
        }
        self.world = CampaignWorld(
            campaign_id=self.campaign.id,
            public_intro="{}",
            knowledge_graph=json.dumps(graph),
            world_state=json.dumps(
                {
                    "current_scene": {
                        "location_id": "waterdeep",
                        "location_name": "Waterdeep",
                    }
                }
            ),
            dm_private="{}",
        )
        db.session.add(self.world)

        self.session = CampaignSession(campaign_id=self.campaign.id)
        db.session.add(self.session)

        db.session.add(
            NPCActor(
                campaign_id=self.campaign.id,
                actor_id="mira",
                name="Mira",
                dossier="{}",
            )
        )
        db.session.commit()

    def tearDown(self):
        db.session.rollback()
        db.drop_all()
        self.ctx.pop()

    def _context(self):
        return {
            "campaign_id": self.campaign.id,
            "session_id": self.session.id,
            "hot_context": {
                "current_scene": {
                    "location_id": "waterdeep",
                    "location_name": "Waterdeep",
                }
            },
        }

    def test_typed_entity_claim_reuses_npc_canon(self):
        registry, _, _, _ = build_canonical_resolution_registry(
            self.campaign,
            self._context(),
            {
                "entity_claims": [
                    {
                        "name": "Mira",
                        "type": "npc",
                        "mention_ref": "mira_claim",
                    }
                ]
            },
            None,
            [],
            _known_ids(self.campaign),
        )
        entry = next(
            item for item in registry if item.get("mention_ref") == "mira_claim"
        )
        self.assertEqual(entry.get("canonical_id"), "mira")
        self.assertEqual(entry.get("entity_type"), "npc")
        self.assertEqual(entry.get("decision"), "reuse_existing")

    def test_graph_subtype_match_does_not_cross_types(self):
        registry, _, _, _ = build_canonical_resolution_registry(
            self.campaign,
            self._context(),
            {
                "entity_claims": [
                    {
                        "name": "Waterdeep",
                        "type": "organization",
                        "mention_ref": "waterdeep_org_claim",
                    }
                ]
            },
            None,
            [],
            _known_ids(self.campaign),
        )
        entry = next(
            item
            for item in registry
            if item.get("mention_ref") == "waterdeep_org_claim"
        )
        self.assertNotEqual(entry.get("canonical_id"), "waterdeep")
        self.assertEqual(entry.get("entity_type"), "organization")
        self.assertEqual(entry.get("decision"), "create_provisional")

    def test_candidate_identity_is_deduplicated_by_canonical_id(self):
        known = {
            "entity_ids": {"mira"},
            "npc_ids": {"mira"},
            "entity_names": {"mira": "Mira"},
            "npc_names": {"mira": "Mira"},
            "entity_types": {"mira": "npc"},
        }
        candidates = find_all_matching_candidates(
            "Mira",
            known,
            [],
            expected_type="npc",
        )
        self.assertEqual(candidates, {"mira": "npc"})

    def test_private_plot_names_are_pruned_from_player_facing_memory_anchors(self):
        graph = json.loads(self.world.knowledge_graph)
        graph["entities"].extend([
            {
                "id": "lady_seraphine_vane",
                "name": "Lady Seraphine Vane",
                "type": "npc",
                "visibility": "dm_private",
            },
            {
                "id": "mirror_of_drowned_kings",
                "name": "Mirror of Drowned Kings",
                "type": "item",
                "visibility": "dm_private",
            },
        ])
        self.world.knowledge_graph = json.dumps(graph)
        db.session.commit()

        context = {
            **self._context(),
            "latest_player_message": "We inspect the canal for scrape marks.",
            "latest_dm_message": "Fresh scrapes and an oil sheen mark the stone.",
        }
        compiled = compile_staged_memory_patch(
            context,
            {},
            {
                "memory_anchors": {
                    "current_goal": "Trace the skiff through the canal.",
                    "current_scene": "At the canal mouth.",
                    "open_clues": ["Fresh scrape marks", "An oil sheen"],
                    "unresolved_questions": [
                        "What is Lady Seraphine Vane's plan involving the Mirror of Drowned Kings?"
                    ],
                    "npc_observations": [],
                    "recent_offers_promises": [],
                },
            },
        )

        self.assertEqual(compiled["memory_anchors"]["unresolved_questions"], [])
        self.assertEqual(
            compiled["memory_anchors"]["open_clues"],
            ["Fresh scrape marks", "An oil sheen"],
        )

    def test_npc_update_materializes_relation_endpoint_before_validation(self):
        patch = {
            "source_contract": SOURCE_CONTRACT_COMPILED_V2,
            "base_memory_revision": self.world.memory_revision or 0,
            "upsert_graph_entities": [],
            "upsert_graph_relations": [{
                "id": "rel_mira_knows_lyra",
                "type": "knows",
                "source_id": "mira",
                "target_id": "lyra_sunfall",
            }],
            "upsert_graph_facts": [],
            "update_npc_actors": [{
                "id": "lyra_sunfall",
                "name": "Lyra Sunfall",
                "role": "Canal guide",
                "visibility": "party_known",
            }],
            "record_events": [],
        }
        graph = json.loads(self.world.knowledge_graph)
        graph["entities"].append({"id": "mira", "name": "Mira", "type": "npc"})
        self.world.knowledge_graph = json.dumps(graph)
        db.session.commit()

        result = apply_compiled_session_memory_patch(
            self.campaign,
            self.session,
            patch,
        )
        db.session.commit()

        graph = json.loads(self.world.knowledge_graph)
        self.assertIn("lyra_sunfall", {item["id"] for item in graph["entities"]})
        self.assertEqual(result["graph_changes"][-1]["id"], "rel_mira_knows_lyra")

    def test_ignore_clarification_cannot_merge_patch(self):
        clarification = CampaignClarification(
            campaign_id=self.campaign.id,
            clarification_id="clar_ignore_write",
            idempotency_key="clar_ignore_write_key",
            kind="identity",
            mention_ref="ignored_ref",
            mention_entity_id="ignored_ref",
            surface_form="Ignored Figure",
            question="Ignore this figure?",
            status="answered",
            resolution_action="ignore",
            resolution_patch_json={
                "upsert_graph_entities": [
                    {
                        "name": "Injected Organization",
                        "type": "organization",
                    }
                ]
            },
        )
        db.session.add(clarification)
        db.session.commit()

        compiled = compile_staged_memory_patch(self._context(), {}, {})
        self.assertIn(
            "clar_ignore_write",
            compiled.get("consumed_clarification_ids", []),
        )
        self.assertFalse(
            any(
                entity.get("name") == "Injected Organization"
                for entity in compiled.get("upsert_graph_entities", [])
            )
        )

    def test_new_entity_clarification_preserves_npc_type(self):
        clarification = CampaignClarification(
            campaign_id=self.campaign.id,
            clarification_id="clar_new_masked_guard",
            idempotency_key="clar_new_masked_guard_key",
            kind="identity",
            mention_ref="masked_guard_ref",
            mention_entity_id="masked_guard_ref",
            surface_form="Masked Guard",
            question="Is this a new NPC?",
            status="answered",
            resolution_action="new_entity",
            resolution_patch_json={
                "update_npc_actors": [
                    {"id": "masked_guard_ref", "role": "Guard"}
                ]
            },
        )
        db.session.add(clarification)
        db.session.commit()

        compiled = compile_staged_memory_patch(self._context(), {}, {})
        self.assertIn(
            "clar_new_masked_guard",
            compiled.get("consumed_clarification_ids", []),
        )
        npc_updates = compiled.get("update_npc_actors", [])
        self.assertEqual(len(npc_updates), 1)
        new_id = npc_updates[0]["id"]
        entity = next(
            item
            for item in compiled.get("upsert_graph_entities", [])
            if item.get("id") == new_id
        )
        self.assertEqual(entity.get("type"), "npc")

    def test_same_identity_clarification_preserves_canonical_type(self):
        clarification = CampaignClarification(
            campaign_id=self.campaign.id,
            clarification_id="clar_same_waterdeep",
            idempotency_key="clar_same_waterdeep_key",
            kind="identity",
            mention_ref="city_ref",
            mention_entity_id="city_ref",
            surface_form="the city",
            question="Is this Waterdeep?",
            status="answered",
            resolution_action="same_identity",
            resolved_canonical_id="waterdeep",
            resolution_patch_json={
                "upsert_graph_entities": [
                    {
                        "id": "waterdeep",
                        "type": "other",
                        "summary": "Updated summary",
                    }
                ]
            },
        )
        db.session.add(clarification)
        db.session.commit()

        compiled = compile_staged_memory_patch(self._context(), {}, {})
        entity = next(
            item
            for item in compiled.get("upsert_graph_entities", [])
            if item.get("id") == "waterdeep"
        )
        self.assertEqual(entity.get("type"), "location")

    def test_durable_resolution_read_errors_fail_closed(self):
        query = mock.Mock()
        query.filter_by.side_effect = RuntimeError("identity DB unavailable")
        with mock.patch.object(CampaignIdentityResolution, "query", query):
            with self.assertRaisesRegex(RuntimeError, "identity DB unavailable"):
                fetch_prior_resolutions(self.campaign)

    def test_clarification_read_errors_fail_closed(self):
        query = mock.Mock()
        query.filter_by.side_effect = RuntimeError(
            "clarification DB unavailable"
        )
        with mock.patch.object(CampaignClarification, "query", query):
            with self.assertRaisesRegex(
                RuntimeError,
                "clarification DB unavailable",
            ):
                fetch_pending_clarifications(self.campaign)


if __name__ == "__main__":
    unittest.main()

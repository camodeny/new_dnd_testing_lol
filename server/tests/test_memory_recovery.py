import json
import os
import sys
import unittest

from flask import Flask

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from models import (
    Campaign,
    CampaignSession,
    CampaignWorld,
    Character,
    NPCActor,
    SessionMemoryRecoveryTask,
    User,
    db,
)
from services.memory_recovery import (
    create_memory_recovery_task,
    has_pending_memory_recovery,
    pending_memory_recovery_tasks,
    resolve_memory_recovery_tasks,
    retry_memory_recovery_task,
)
from services.session_memory_agent import MemoryPipelineError, compile_staged_memory_patch
from services.dm_tools import apply_compiled_session_memory_patch
from services.memory_resolver_schemas import SOURCE_CONTRACT_COMPILED_V2


class SessionMemoryRelationEndpointTest(unittest.TestCase):
    """Regression coverage for issue #100.

    Resolved NPC actors and roster PCs referenced as relation endpoints must
    survive resolution, compilation, validation, and application even when they
    have no graph entity of their own.
    """

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

        user = User(username="issue100_dm", email="issue100@example.com")
        user.set_password("password")
        db.session.add(user)
        db.session.commit()

        self.campaign = Campaign(
            name="Issue 100",
            description="Test",
            user_id=user.id,
        )
        db.session.add(self.campaign)
        db.session.commit()

        graph = {
            "entities": [
                {"id": "waterdeep", "name": "Waterdeep", "type": "location"},
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
        db.session.commit()

    def tearDown(self):
        db.session.rollback()
        db.drop_all()
        self.ctx.pop()

    def _base_patch(self):
        return {
            "source_contract": SOURCE_CONTRACT_COMPILED_V2,
            "base_memory_revision": self.world.memory_revision or 0,
            "upsert_graph_entities": [],
            "upsert_graph_relations": [],
            "upsert_graph_facts": [],
            "update_npc_actors": [],
            "record_events": [],
        }

    def _graph_entity_ids(self):
        graph = json.loads(self.world.knowledge_graph)
        return {entity["id"] for entity in graph["entities"]}

    def test_private_npc_relation_source_materializes_at_application(self):
        # vex_mal is a resolved private NPC known only to the NPC actor registry.
        db.session.add(
            NPCActor(
                campaign_id=self.campaign.id,
                actor_id="vex_mal",
                name="Vex Mal",
                role="Rival",
                public_summary="A shadowy rival.",
                dossier="{}",
            )
        )
        db.session.commit()

        patch = self._base_patch()
        patch["upsert_graph_relations"] = [{
            "id": "rel_vex_mal_rival_waterdeep",
            "type": "rival_of",
            "source_id": "vex_mal",
            "target_id": "waterdeep",
        }]

        result = apply_compiled_session_memory_patch(self.campaign, self.session, patch)
        db.session.commit()

        self.assertIn("vex_mal", self._graph_entity_ids())
        materializations = result.get("relation_endpoint_materializations", [])
        self.assertEqual(materializations[0]["endpoint_id"], "vex_mal")
        self.assertEqual(materializations[0]["source"], "known_npc")
        self.assertEqual(materializations[0]["materialized_type"], "npc")

        graph = json.loads(self.world.knowledge_graph)
        vex = next(e for e in graph["entities"] if e["id"] == "vex_mal")
        self.assertEqual(vex["name"], "Vex Mal")
        self.assertEqual(vex["type"], "npc")

    def test_roster_pc_relation_source_materializes_at_application(self):
        # toren_reed is a roster PC with no redundant new entity proposal.
        db.session.add(
            Character(
                campaign_id=self.campaign.id,
                name="Toren Reed",
                race="Human",
                background="Sage",
            )
        )
        db.session.commit()

        patch = self._base_patch()
        patch["upsert_graph_relations"] = [{
            "id": "rel_toren_reed_studies_waterdeep",
            "type": "studies_at",
            "source_id": "toren_reed",
            "target_id": "waterdeep",
        }]

        result = apply_compiled_session_memory_patch(self.campaign, self.session, patch)
        db.session.commit()

        self.assertIn("toren_reed", self._graph_entity_ids())
        materializations = result.get("relation_endpoint_materializations", [])
        self.assertEqual(materializations[0]["endpoint_id"], "toren_reed")
        self.assertEqual(materializations[0]["source"], "known_roster_pc")
        self.assertEqual(materializations[0]["materialized_type"], "pc")

        graph = json.loads(self.world.knowledge_graph)
        toren = next(e for e in graph["entities"] if e["id"] == "toren_reed")
        self.assertEqual(toren["name"], "Toren Reed")
        self.assertEqual(toren["type"], "pc")

    def test_relation_endpoint_resolves_by_character_name_slug(self):
        # A roster character named "Toren Reed" maps to slug toren_reed; a
        # relation that spells the canonical slug must still materialize.
        db.session.add(
            Character(
                campaign_id=self.campaign.id,
                name="Toren Reed",
                race="Human",
                background="Sage",
            )
        )
        db.session.commit()

        patch = self._base_patch()
        patch["upsert_graph_relations"] = [{
            "id": "rel_toren_ally_waterdeep",
            "type": "allied_with",
            "source_id": "toren_reed",
            "target_id": "waterdeep",
        }]
        apply_compiled_session_memory_patch(self.campaign, self.session, patch)
        db.session.commit()
        self.assertIn("toren_reed", self._graph_entity_ids())

    def test_same_patch_entity_relation_endpoint_applies(self):
        patch = self._base_patch()
        patch["upsert_graph_entities"] = [{
            "id": "mira_wolf",
            "name": "Mira's Wolf",
            "type": "beast",
        }]
        patch["upsert_graph_relations"] = [{
            "id": "rel_wolf_roams_waterdeep",
            "type": "roams",
            "source_id": "mira_wolf",
            "target_id": "waterdeep",
        }]

        result = apply_compiled_session_memory_patch(self.campaign, self.session, patch)
        db.session.commit()

        self.assertIn("mira_wolf", self._graph_entity_ids())
        self.assertEqual(result.get("relation_endpoint_materializations", []), [])
        graph = json.loads(self.world.knowledge_graph)
        rel_ids = {rel["id"] for rel in graph["relations"]}
        self.assertIn("rel_wolf_roams_waterdeep", rel_ids)

    def test_invalid_relation_endpoint_fails_closed_with_telemetry(self):
        patch = self._base_patch()
        patch["upsert_graph_relations"] = [{
            "id": "rel_unknown_thing",
            "type": "allied_with",
            "source_id": "completely_unknown_actor",
            "target_id": "waterdeep",
        }]

        with self.assertRaises(MemoryPipelineError) as ctx:
            apply_compiled_session_memory_patch(self.campaign, self.session, patch)
        err = ctx.exception
        self.assertEqual(err.code, "missing_relation_endpoint")
        self.assertEqual(err.stage, "validation")
        telemetry = err.telemetry or {}
        self.assertEqual(telemetry.get("endpoint_id"), "completely_unknown_actor")
        self.assertEqual(telemetry.get("endpoint_role"), "source")
        self.assertTrue(isinstance(telemetry.get("unresolved_endpoints"), list))
        self.assertTrue(
            any(item.get("endpoint_id") == "completely_unknown_actor" for item in telemetry["unresolved_endpoints"])
        )
        # Fail closed: no partial graph mutation.
        self.assertNotIn("completely_unknown_actor", self._graph_entity_ids())
        self.assertEqual(json.loads(self.world.knowledge_graph)["relations"], [])

    def test_compile_manifest_classifies_endpoint_kinds(self):
        db.session.add(
            NPCActor(
                campaign_id=self.campaign.id,
                actor_id="vex_mal",
                name="Vex Mal",
                dossier="{}",
            )
        )
        db.session.add(
            Character(
                campaign_id=self.campaign.id,
                name="Toren Reed",
                race="Human",
                background="Sage",
            )
        )
        db.session.commit()

        resolved = {
            "upsert_graph_relations": [
                {
                    "type": "rival_of",
                    "source_id": "vex_mal",
                    "target_id": "waterdeep",
                },
                {
                    "type": "studies_at",
                    "source_id": "toren_reed",
                    "target_id": "waterdeep",
                },
                {
                    "type": "roams",
                    "source_id": "mira_wolf",
                    "target_id": "waterdeep",
                },
            ],
            "upsert_graph_entities": [
                {"id": "mira_wolf", "name": "Mira's Wolf", "type": "beast"},
            ],
        }
        compiled = compile_staged_memory_patch(
            {
                "campaign_id": self.campaign.id,
                "session_id": self.session.id,
            },
            {},
            resolved,
        )
        manifest = compiled.get("relation_endpoint_manifest", [])
        by_id = {entry["endpoint_id"]: entry for entry in manifest}
        self.assertEqual(by_id["vex_mal"]["endpoint_kind"], "known_npc")
        self.assertTrue(by_id["vex_mal"]["materialize_required"])
        self.assertEqual(by_id["toren_reed"]["endpoint_kind"], "known_roster_pc")
        self.assertTrue(by_id["toren_reed"]["materialize_required"])
        self.assertEqual(by_id["mira_wolf"]["endpoint_kind"], "same_patch_entity")
        self.assertFalse(by_id["mira_wolf"]["materialize_required"])
        self.assertEqual(by_id["waterdeep"]["endpoint_kind"], "known_entity")
        self.assertFalse(by_id["waterdeep"]["materialize_required"])

        # Compiled patch applies end-to-end without missing_relation_endpoint.
        apply_compiled_session_memory_patch(self.campaign, self.session, compiled)
        db.session.commit()
        graph_ids = self._graph_entity_ids()
        self.assertIn("vex_mal", graph_ids)
        self.assertIn("toren_reed", graph_ids)
        self.assertIn("mira_wolf", graph_ids)

    def test_compiled_patch_applies_with_private_npc_and_roster_pc(self):
        db.session.add(
            NPCActor(
                campaign_id=self.campaign.id,
                actor_id="vex_mal",
                name="Vex Mal",
                dossier="{}",
            )
        )
        db.session.add(
            Character(
                campaign_id=self.campaign.id,
                name="Toren Reed",
                race="Human",
                background="Sage",
            )
        )
        db.session.commit()

        resolved = {
            "running_summary": "The party followed the rival to the library.",
            "upsert_graph_relations": [
                {
                    "type": "rival_of",
                    "source_id": "vex_mal",
                    "target_id": "waterdeep",
                },
                {
                    "type": "studies_at",
                    "source_id": "toren_reed",
                    "target_id": "waterdeep",
                },
            ],
            "upsert_graph_facts": [
                {
                    "text": "Vex Mal is watching the party.",
                    "entity_ids": ["vex_mal"],
                },
            ],
            "record_events": [
                {
                    "event_type": "sighting",
                    "summary": "Vex Mal spotted near the gate.",
                    "payload": {"actor_id": "vex_mal", "entity_ids": ["toren_reed"]},
                },
            ],
        }
        compiled = compile_staged_memory_patch(
            {
                "campaign_id": self.campaign.id,
                "session_id": self.session.id,
            },
            {},
            resolved,
        )
        apply_compiled_session_memory_patch(self.campaign, self.session, compiled)
        db.session.commit()

        self.assertIn("vex_mal", self._graph_entity_ids())
        self.assertIn("toren_reed", self._graph_entity_ids())
        self.assertEqual(self.session.running_summary, "The party followed the rival to the library.")


class SessionMemoryRecoveryTaskTest(unittest.TestCase):
    """Recovery task lifecycle: explicit partial-state record + bounded retry."""

    def setUp(self):
        self.app = Flask(__name__)
        self.app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
        self.app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
        self.app.config["SECRET_KEY"] = "test-secret"
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        user = User(username="recovery_dm", email="recovery@example.com")
        user.set_password("password")
        db.session.add(user)
        db.session.commit()

        self.campaign = Campaign(
            name="Recovery",
            description="Test",
            user_id=user.id,
        )
        db.session.add(self.campaign)
        db.session.commit()

        graph = {
            "entities": [
                {"id": "waterdeep", "name": "Waterdeep", "type": "location"},
            ],
            "relations": [],
            "facts": [],
        }
        self.world = CampaignWorld(
            campaign_id=self.campaign.id,
            public_intro="{}",
            knowledge_graph=json.dumps(graph),
            world_state=json.dumps({}),
            dm_private="{}",
        )
        db.session.add(self.world)

        self.session = CampaignSession(campaign_id=self.campaign.id)
        db.session.add(self.session)
        db.session.commit()

    def tearDown(self):
        db.session.rollback()
        db.drop_all()
        self.ctx.pop()

    def test_create_task_records_recoverable_partial_state(self):
        db.session.add(
            NPCActor(
                campaign_id=self.campaign.id,
                actor_id="vex_mal",
                name="Vex Mal",
                dossier="{}",
            )
        )
        db.session.commit()
        patch = {
            "source_contract": SOURCE_CONTRACT_COMPILED_V2,
            "base_memory_revision": self.world.memory_revision or 0,
            "upsert_graph_entities": [],
            "upsert_graph_relations": [{
                "type": "rival_of",
                "source_id": "vex_mal",
                "target_id": "waterdeep",
            }],
        }
        err = MemoryPipelineError(
            stage="validation",
            code="missing_relation_endpoint",
            message="Relation source 'vex_mal' not found in graph entities.",
        )
        task = create_memory_recovery_task(
            self.campaign.id,
            self.session,
            player_message_id=1,
            dm_message_id=2,
            err=err,
            patch=patch,
            trace_id="trace_1",
        )
        self.assertEqual(task.status, "pending")
        self.assertEqual(task.error_code, "missing_relation_endpoint")
        self.assertTrue(task.patch_json)
        self.assertTrue(has_pending_memory_recovery(self.campaign.id))
        self.assertEqual(len(pending_memory_recovery_tasks(self.campaign.id)), 1)
        self.assertTrue(task.to_dict()["has_patch"])

    def test_retry_reapplies_patch_and_repairs_endpoint(self):
        db.session.add(
            NPCActor(
                campaign_id=self.campaign.id,
                actor_id="vex_mal",
                name="Vex Mal",
                dossier="{}",
            )
        )
        db.session.commit()
        patch = {
            "source_contract": SOURCE_CONTRACT_COMPILED_V2,
            "base_memory_revision": self.world.memory_revision or 0,
            "upsert_graph_entities": [],
            "upsert_graph_relations": [{
                "type": "rival_of",
                "source_id": "vex_mal",
                "target_id": "waterdeep",
            }],
            "running_summary": "Recovered summary after retry.",
        }
        task = create_memory_recovery_task(
            self.campaign.id,
            self.session,
            player_message_id=7,
            dm_message_id=8,
            err=MemoryPipelineError(stage="validation", code="missing_relation_endpoint", message="missing"),
            patch=patch,
            trace_id="trace_7",
        )

        result = retry_memory_recovery_task(self.campaign.id, task.id)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "resolved")

        graph = json.loads(self.world.knowledge_graph)
        entity_ids = {e["id"] for e in graph["entities"]}
        self.assertIn("vex_mal", entity_ids)
        self.assertEqual(self.session.running_summary, "Recovered summary after retry.")
        self.assertFalse(has_pending_memory_recovery(self.campaign.id))

    def test_retry_fails_closed_for_invalid_patch(self):
        patch = {
            "source_contract": SOURCE_CONTRACT_COMPILED_V2,
            "base_memory_revision": self.world.memory_revision or 0,
            "upsert_graph_relations": [{
                "type": "rival_of",
                "source_id": "ghost_actor",
                "target_id": "waterdeep",
            }],
        }
        task = create_memory_recovery_task(
            self.campaign.id,
            self.session,
            player_message_id=9,
            dm_message_id=10,
            err=MemoryPipelineError(stage="validation", code="missing_relation_endpoint", message="missing"),
            patch=patch,
            trace_id="trace_9",
        )
        result = retry_memory_recovery_task(self.campaign.id, task.id)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "Relation source 'ghost_actor' not found in graph entities.")
        refreshed = db.session.get(SessionMemoryRecoveryTask, task.id)
        self.assertEqual(refreshed.status, "pending")
        self.assertEqual(refreshed.attempts, 1)
        self.assertTrue(has_pending_memory_recovery(self.campaign.id))

    def test_resolve_marks_pending_task_resolved(self):
        task = create_memory_recovery_task(
            self.campaign.id,
            self.session,
            player_message_id=11,
            dm_message_id=12,
            err=MemoryPipelineError(stage="validation", code="missing_relation_endpoint", message="missing"),
            patch={"source_contract": SOURCE_CONTRACT_COMPILED_V2, "upsert_graph_entities": []},
            trace_id="trace_11",
        )
        resolved = resolve_memory_recovery_tasks(self.campaign.id, player_message_id=11)
        self.assertEqual(resolved, 1)
        refreshed = db.session.get(SessionMemoryRecoveryTask, task.id)
        self.assertEqual(refreshed.status, "resolved")
        self.assertIsNotNone(refreshed.resolved_at)
        self.assertFalse(has_pending_memory_recovery(self.campaign.id))

    def test_retry_exhaustion_marks_task_failed(self):
        from services import memory_recovery as recovery_module

        patch = {
            "source_contract": SOURCE_CONTRACT_COMPILED_V2,
            "base_memory_revision": self.world.memory_revision or 0,
            "upsert_graph_relations": [{
                "type": "rival_of",
                "source_id": "ghost_actor",
                "target_id": "waterdeep",
            }],
        }
        task = create_memory_recovery_task(
            self.campaign.id,
            self.session,
            player_message_id=13,
            dm_message_id=14,
            err=MemoryPipelineError(stage="validation", code="missing_relation_endpoint", message="missing"),
            patch=patch,
            trace_id="trace_13",
        )
        original_max = recovery_module.MAX_RECOVERY_ATTEMPTS
        recovery_module.MAX_RECOVERY_ATTEMPTS = 1
        try:
            result = retry_memory_recovery_task(self.campaign.id, task.id)
        finally:
            recovery_module.MAX_RECOVERY_ATTEMPTS = original_max
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        refreshed = db.session.get(SessionMemoryRecoveryTask, task.id)
        self.assertEqual(refreshed.status, "failed")
        self.assertFalse(has_pending_memory_recovery(self.campaign.id))


class MemoryRecoveryRouteTest(unittest.TestCase):
    """API surface: pending listing and bounded retry routes."""

    def setUp(self):
        from auth import generate_token
        from routes.sessions import sessions_bp

        self.app = Flask(__name__)
        self.app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
        self.app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
        self.app.config["SECRET_KEY"] = "test-secret"
        self.app.config["JWT_EXPIRATION_HOURS"] = 24
        self.app.config["AUTH_SESSION_COOKIE_NAME"] = "dnd_session"
        db.init_app(self.app)
        self.app.register_blueprint(sessions_bp)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        user = User(username="route_dm", email="route@example.com")
        user.set_password("password")
        db.session.add(user)
        db.session.commit()

        self.campaign = Campaign(name="Routes", description="Test", user_id=user.id)
        db.session.add(self.campaign)
        db.session.commit()
        db.session.add(
            CampaignWorld(
                campaign_id=self.campaign.id,
                public_intro="{}",
                knowledge_graph=json.dumps({
                    "entities": [{"id": "waterdeep", "name": "Waterdeep", "type": "location"}],
                    "relations": [],
                    "facts": [],
                }),
                world_state=json.dumps({}),
                dm_private="{}",
            )
        )
        db.session.add(CampaignSession(campaign_id=self.campaign.id))
        db.session.commit()

        self.token = generate_token(user.id)
        self.client = self.app.test_client()
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def tearDown(self):
        db.session.rollback()
        db.drop_all()
        self.ctx.pop()

    def test_pending_route_lists_empty(self):
        response = self.client.get(
            f"/api/campaigns/{self.campaign.id}/memory-recovery/pending",
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["count"], 0)
        self.assertEqual(data["tasks"], [])

    def test_pending_route_lists_pending_task(self):
        task = SessionMemoryRecoveryTask(
            campaign_id=self.campaign.id,
            player_message_id=1,
            status="pending",
            error_code="missing_relation_endpoint",
            patch_json="{}",
        )
        db.session.add(task)
        db.session.commit()

        response = self.client.get(
            f"/api/campaigns/{self.campaign.id}/memory-recovery/pending",
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["tasks"][0]["id"], task.id)
        self.assertEqual(data["tasks"][0]["status"], "pending")

    def test_retry_route_resolves_valid_patch(self):
        db.session.add(
            NPCActor(
                campaign_id=self.campaign.id,
                actor_id="vex_mal",
                name="Vex Mal",
                dossier="{}",
            )
        )
        world = CampaignWorld.query.filter_by(campaign_id=self.campaign.id).first()
        patch = {
            "source_contract": SOURCE_CONTRACT_COMPILED_V2,
            "base_memory_revision": world.memory_revision or 0,
            "upsert_graph_entities": [],
            "upsert_graph_relations": [{
                "type": "rival_of",
                "source_id": "vex_mal",
                "target_id": "waterdeep",
            }],
        }
        task = SessionMemoryRecoveryTask(
            campaign_id=self.campaign.id,
            player_message_id=1,
            status="pending",
            error_code="missing_relation_endpoint",
            patch_json=json.dumps(patch),
        )
        db.session.add(task)
        db.session.commit()

        response = self.client.post(
            f"/api/campaigns/{self.campaign.id}/memory-recovery/{task.id}/retry",
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["status"], "resolved")

        refreshed = db.session.get(SessionMemoryRecoveryTask, task.id)
        self.assertEqual(refreshed.status, "resolved")

    def test_retry_route_fails_closed_for_unknown_endpoint(self):
        world = CampaignWorld.query.filter_by(campaign_id=self.campaign.id).first()
        patch = {
            "source_contract": SOURCE_CONTRACT_COMPILED_V2,
            "base_memory_revision": world.memory_revision or 0,
            "upsert_graph_entities": [],
            "upsert_graph_relations": [{
                "type": "rival_of",
                "source_id": "ghost_actor",
                "target_id": "waterdeep",
            }],
        }
        task = SessionMemoryRecoveryTask(
            campaign_id=self.campaign.id,
            player_message_id=2,
            status="pending",
            error_code="missing_relation_endpoint",
            patch_json=json.dumps(patch),
        )
        db.session.add(task)
        db.session.commit()

        response = self.client.post(
            f"/api/campaigns/{self.campaign.id}/memory-recovery/{task.id}/retry",
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 400)
        data = response.get_json()
        self.assertFalse(data["ok"])

        refreshed = db.session.get(SessionMemoryRecoveryTask, task.id)
        self.assertEqual(refreshed.status, "pending")
        self.assertEqual(refreshed.attempts, 1)


if __name__ == "__main__":
    unittest.main()

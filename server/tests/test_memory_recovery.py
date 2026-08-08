import json
import os
import sys
import unittest
from unittest import mock

from flask import Flask

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from models import (
    Campaign,
    CampaignAuditEvent,
    CampaignClock,
    CampaignMember,
    CampaignSession,
    CampaignWorld,
    Character,
    NPCActor,
    SessionDmTurn,
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
from services.dm_turns import mark_session_dm_turn_error
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

    def test_private_npc_endpoint_materializes_fail_closed_to_dm_private(self):
        # A DM-private canonical NPC used only as a relation endpoint must never
        # be promoted into the party-visible graph. The materialized placeholder
        # defaults to dm_private when the referencing relation carries no
        # party-visible evidence (and even a private relation must stay private).
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
            "visibility": "dm_private",
        }]

        result = apply_compiled_session_memory_patch(self.campaign, self.session, patch)
        db.session.commit()

        graph = json.loads(self.world.knowledge_graph)
        vex = next(e for e in graph["entities"] if e["id"] == "vex_mal")
        self.assertEqual(vex["visibility"], "dm_private")
        self.assertNotIn(vex["visibility"], {"public", "party_known"})

    def test_private_npc_endpoint_with_no_visibility_evidence_stays_private(self):
        # Without any referencing-relation visibility signal, the endpoint
        # placeholder must fail closed to dm_private rather than party_known.
        db.session.add(
            NPCActor(
                campaign_id=self.campaign.id,
                actor_id="vex_mal",
                name="Vex Mal",
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

        apply_compiled_session_memory_patch(self.campaign, self.session, patch)
        db.session.commit()

        graph = json.loads(self.world.knowledge_graph)
        vex = next(e for e in graph["entities"] if e["id"] == "vex_mal")
        self.assertEqual(vex["visibility"], "dm_private")

    def test_party_visible_relation_materializes_endpoint_party_known(self):
        # Only positive party-visible evidence on every referencing relation
        # promotes the placeholder to party_known.
        db.session.add(
            NPCActor(
                campaign_id=self.campaign.id,
                actor_id="vex_mal",
                name="Vex Mal",
                dossier="{}",
            )
        )
        db.session.commit()

        patch = self._base_patch()
        patch["upsert_graph_relations"] = [{
            "id": "rel_vex_mal_seen_waterdeep",
            "type": "seen_at",
            "source_id": "vex_mal",
            "target_id": "waterdeep",
            "visibility": "party_known",
        }]

        apply_compiled_session_memory_patch(self.campaign, self.session, patch)
        db.session.commit()

        graph = json.loads(self.world.knowledge_graph)
        vex = next(e for e in graph["entities"] if e["id"] == "vex_mal")
        self.assertEqual(vex["visibility"], "party_known")

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

    def _summary_with_clock_state(self):
        """A mock summary-finalizer payload that reflects the committed clock
        state at finalization time, proving the summary sees recovered clocks."""
        clock = CampaignClock.query.filter_by(
            campaign_id=self.campaign.id,
            clock_id="trapped_ferrymen",
        ).first()
        if clock is None:
            return "No clock state."
        return f"Trapped Ferrymen now at {clock.filled}/{clock.segments}."

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

        with mock.patch(
            "services.dm_tools.build_session_clock_context",
            return_value={"allowed_evidence_sources": []},
        ), mock.patch(
            "openrouter.get_session_clock_updates",
            return_value={"advance_clocks": [], "retire_clocks": [], "create_clocks": []},
        ), mock.patch(
            "routes.sessions._repair_post_turn_clocks",
            return_value=(7, None),
        ), mock.patch(
            "services.dm_tools.build_session_summary_finalize_context",
            return_value={"summary_context": True},
        ), mock.patch(
            "openrouter.get_session_running_summary_finalize",
            return_value={"running_summary": "Recovered summary after retry."},
        ), mock.patch(
            "routes.sessions._verify_post_turn_state",
            return_value=(True, None),
        ):
            result = retry_memory_recovery_task(self.campaign.id, task.id)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "resolved")
        self.assertTrue(result["clock_recovered"])
        self.assertEqual(result["post_turn_revision"], 7)

        graph = json.loads(self.world.knowledge_graph)
        entity_ids = {e["id"] for e in graph["entities"]}
        self.assertIn("vex_mal", entity_ids)
        self.assertEqual(self.session.running_summary, "Recovered summary after retry.")
        self.assertFalse(has_pending_memory_recovery(self.campaign.id))

    def test_retry_replays_clock_adjudication_and_repairs_turn(self):
        # A failed turn that should advance a clock must replay clock
        # adjudication and flip the durable SessionDmTurn out of error state
        # before the next visible turn can rely on it.
        db.session.add(
            NPCActor(
                campaign_id=self.campaign.id,
                actor_id="vex_mal",
                name="Vex Mal",
                dossier="{}",
            )
        )
        db.session.add(
            CampaignClock(
                campaign_id=self.campaign.id,
                clock_id="trapped_ferrymen",
                name="Trapped Ferrymen",
                segments=4,
                filled=1,
                status="active",
                visibility="party_known",
            )
        )
        db.session.add(mark_session_dm_turn_error(
            self.campaign.id,
            self.session.id,
            player_message_id=7,
            trace_id="trace_7",
            error_text="missing_relation_endpoint",
            memory_status="error",
            clock_status="skipped",
        ))
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
        task = create_memory_recovery_task(
            self.campaign.id,
            self.session,
            player_message_id=7,
            dm_message_id=8,
            err=MemoryPipelineError(stage="validation", code="missing_relation_endpoint", message="missing"),
            patch=patch,
            trace_id="trace_7",
            context={
                "current_user_id": self.campaign.user_id,
                "current_scene_before": {"location_id": "waterdeep"},
            },
        )

        with mock.patch(
            "services.dm_tools.build_session_clock_context",
            return_value={"allowed_evidence_sources": []},
        ) as clock_context_mock, mock.patch(
            "openrouter.get_session_clock_updates",
            return_value={"advance_clocks": [
                {"clock_id": "trapped_ferrymen", "delta": 1, "reason": "The party freed a trapped crewman."},
            ]},
        ) as clock_updates_mock, mock.patch(
            "routes.sessions._repair_post_turn_clocks",
            return_value=(7, None),
        ), mock.patch(
            "services.dm_tools.build_session_summary_finalize_context",
            return_value={"summary_context": True},
        ), mock.patch(
            "openrouter.get_session_running_summary_finalize",
            side_effect=lambda summary_context, audit_context=None: {"running_summary": self._summary_with_clock_state()},
        ), mock.patch(
            "routes.sessions._verify_post_turn_state",
            return_value=(True, None),
        ):
            result = retry_memory_recovery_task(self.campaign.id, task.id)

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["clock_recovered"], result.get("clock_error"))
        self.assertIsNone(result.get("clock_error"))
        self.assertEqual(result["post_turn_revision"], 7)

        clock = CampaignClock.query.filter_by(
            campaign_id=self.campaign.id,
            clock_id="trapped_ferrymen",
        ).first()
        self.assertEqual(clock.filled, 2)

        # The finalized summary is authored after clock replay, so it must see
        # the recovered clock state (2/4), not the pre-recovery 1/4.
        self.assertIn("2/4", self.session.running_summary)
        self.assertNotIn("1/4", self.session.running_summary)

        turn = SessionDmTurn.query.filter_by(
            campaign_id=self.campaign.id,
            player_message_id=7,
        ).first()
        self.assertEqual(turn.post_turn_status, "complete")
        self.assertEqual(turn.memory_status, "complete")
        self.assertEqual(turn.clock_status, "complete")
        self.assertEqual(turn.post_turn_revision, 7)
        self.assertIsNone(turn.error_text)

        refreshed = db.session.get(SessionMemoryRecoveryTask, task.id)
        self.assertEqual(refreshed.status, "resolved")
        self.assertFalse(has_pending_memory_recovery(self.campaign.id))

    def test_clock_replay_failure_blocks_recovery(self):
        # A failed clock replay must keep recovery blocking (fail closed) rather
        # than returning success: the task stays pending so the worker cannot
        # continue play on stale clocks, and the durable turn stays in error.
        db.session.add(
            NPCActor(
                campaign_id=self.campaign.id,
                actor_id="vex_mal",
                name="Vex Mal",
                dossier="{}",
            )
        )
        db.session.add(mark_session_dm_turn_error(
            self.campaign.id,
            self.session.id,
            player_message_id=7,
            trace_id="trace_7",
            error_text="missing_relation_endpoint",
            memory_status="error",
            clock_status="skipped",
        ))
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
        task = create_memory_recovery_task(
            self.campaign.id,
            self.session,
            player_message_id=7,
            dm_message_id=8,
            err=MemoryPipelineError(stage="validation", code="missing_relation_endpoint", message="missing"),
            patch=patch,
            trace_id="trace_7",
        )

        with mock.patch(
            "services.dm_tools.build_session_clock_context",
            side_effect=RuntimeError("provider unavailable"),
        ):
            result = retry_memory_recovery_task(self.campaign.id, task.id)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "pending")
        self.assertIn("clock_replay_failed", result["error"])

        # Memory is durable but the turn is NOT reported repaired, and the task
        # stays pending so later cycles keep blocking instead of resuming play
        # on a stale clock.
        graph = json.loads(self.world.knowledge_graph)
        self.assertIn("vex_mal", {e["id"] for e in graph["entities"]})
        turn = SessionDmTurn.query.filter_by(
            campaign_id=self.campaign.id,
            player_message_id=7,
        ).first()
        self.assertEqual(turn.post_turn_status, "error")
        self.assertTrue(has_pending_memory_recovery(self.campaign.id))
        refreshed = db.session.get(SessionMemoryRecoveryTask, task.id)
        self.assertEqual(refreshed.status, "pending")

    def test_summary_finalize_failure_keeps_recovery_pending(self):
        # The finalized summary must come from the repaired committed state. A
        # finalization that returns no summary keeps the task pending (fail
        # closed) and the turn in error instead of marking it complete with the
        # stale pre-recovery summary.
        db.session.add(
            NPCActor(
                campaign_id=self.campaign.id,
                actor_id="vex_mal",
                name="Vex Mal",
                dossier="{}",
            )
        )
        db.session.add(mark_session_dm_turn_error(
            self.campaign.id,
            self.session.id,
            player_message_id=7,
            trace_id="trace_7",
            error_text="missing_relation_endpoint",
            memory_status="error",
            clock_status="skipped",
        ))
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
        task = create_memory_recovery_task(
            self.campaign.id,
            self.session,
            player_message_id=7,
            dm_message_id=8,
            err=MemoryPipelineError(stage="validation", code="missing_relation_endpoint", message="missing"),
            patch=patch,
            trace_id="trace_7",
        )

        with mock.patch(
            "services.dm_tools.build_session_clock_context",
            return_value={"allowed_evidence_sources": []},
        ), mock.patch(
            "openrouter.get_session_clock_updates",
            return_value={"advance_clocks": [], "retire_clocks": [], "create_clocks": []},
        ), mock.patch(
            "routes.sessions._repair_post_turn_clocks",
            return_value=(7, None),
        ), mock.patch(
            "services.dm_tools.build_session_summary_finalize_context",
            return_value={"summary_context": True},
        ), mock.patch(
            "openrouter.get_session_running_summary_finalize",
            return_value={"running_summary": None},
        ):
            result = retry_memory_recovery_task(self.campaign.id, task.id)

        self.assertFalse(result["ok"])
        self.assertIn("summary_finalize_failed", result["error"])
        self.assertEqual(result["status"], "pending")

        # Memory and clock replay are durable even though finalization failed,
        # so the next retry must not replay the non-idempotent clock advance.
        refreshed = db.session.get(SessionMemoryRecoveryTask, task.id)
        self.assertTrue(refreshed.memory_applied)
        self.assertTrue(refreshed.clock_applied)

        turn = SessionDmTurn.query.filter_by(
            campaign_id=self.campaign.id,
            player_message_id=7,
        ).first()
        self.assertEqual(turn.post_turn_status, "error")
        self.assertTrue(has_pending_memory_recovery(self.campaign.id))

    def test_verify_incident_keeps_recovery_pending(self):
        # A read-only verification contradiction must not be resolved: the task
        # stays pending and the turn stays in error.
        db.session.add(
            NPCActor(
                campaign_id=self.campaign.id,
                actor_id="vex_mal",
                name="Vex Mal",
                dossier="{}",
            )
        )
        db.session.add(mark_session_dm_turn_error(
            self.campaign.id,
            self.session.id,
            player_message_id=7,
            trace_id="trace_7",
            error_text="missing_relation_endpoint",
            memory_status="error",
            clock_status="skipped",
        ))
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
        task = create_memory_recovery_task(
            self.campaign.id,
            self.session,
            player_message_id=7,
            dm_message_id=8,
            err=MemoryPipelineError(stage="validation", code="missing_relation_endpoint", message="missing"),
            patch=patch,
            trace_id="trace_7",
        )

        with mock.patch(
            "services.dm_tools.build_session_clock_context",
            return_value={"allowed_evidence_sources": []},
        ), mock.patch(
            "openrouter.get_session_clock_updates",
            return_value={"advance_clocks": [], "retire_clocks": [], "create_clocks": []},
        ), mock.patch(
            "routes.sessions._repair_post_turn_clocks",
            return_value=(7, None),
        ), mock.patch(
            "services.dm_tools.build_session_summary_finalize_context",
            return_value={"summary_context": True},
        ), mock.patch(
            "openrouter.get_session_running_summary_finalize",
            return_value={"running_summary": "Recovered summary."},
        ), mock.patch(
            "routes.sessions._verify_post_turn_state",
            return_value=(False, "summary contradicts committed clock state"),
        ):
            result = retry_memory_recovery_task(self.campaign.id, task.id)

        self.assertFalse(result["ok"])
        self.assertIn("verify_incident", result["error"])
        self.assertEqual(result["status"], "pending")

        # Clock replay is durable even though verification failed.
        refreshed = db.session.get(SessionMemoryRecoveryTask, task.id)
        self.assertTrue(refreshed.memory_applied)
        self.assertTrue(refreshed.clock_applied)

        turn = SessionDmTurn.query.filter_by(
            campaign_id=self.campaign.id,
            player_message_id=7,
        ).first()
        self.assertEqual(turn.post_turn_status, "error")
        self.assertTrue(has_pending_memory_recovery(self.campaign.id))

    def test_retry_does_not_replay_clock_after_finalization_failure(self):
        # Clock starts 1/4. First retry replays adjudication (1/4 -> 2/4) then
        # summary finalization fails. The clock_applied marker must be durable so
        # the second retry skips the non-idempotent clock replay, resumes at the
        # finalization tail, and the final clock is still 2/4 (not 3/4).
        db.session.add(
            NPCActor(
                campaign_id=self.campaign.id,
                actor_id="vex_mal",
                name="Vex Mal",
                dossier="{}",
            )
        )
        db.session.add(
            CampaignClock(
                campaign_id=self.campaign.id,
                clock_id="trapped_ferrymen",
                name="Trapped Ferrymen",
                segments=4,
                filled=1,
                status="active",
                visibility="party_known",
            )
        )
        db.session.add(mark_session_dm_turn_error(
            self.campaign.id,
            self.session.id,
            player_message_id=7,
            trace_id="trace_7",
            error_text="missing_relation_endpoint",
            memory_status="error",
            clock_status="skipped",
        ))
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
        task = create_memory_recovery_task(
            self.campaign.id,
            self.session,
            player_message_id=7,
            dm_message_id=8,
            err=MemoryPipelineError(stage="validation", code="missing_relation_endpoint", message="missing"),
            patch=patch,
            trace_id="trace_7",
        )

        finalize_calls = {"count": 0}

        def _fake_finalize(summary_context, audit_context=None):
            finalize_calls["count"] += 1
            if finalize_calls["count"] == 1:
                return {"running_summary": None}
            clock = CampaignClock.query.filter_by(
                campaign_id=self.campaign.id,
                clock_id="trapped_ferrymen",
            ).first()
            return {"running_summary": f"Trapped Ferrymen at {clock.filled}/4."}

        with mock.patch(
            "services.dm_tools.build_session_clock_context",
            return_value={"allowed_evidence_sources": []},
        ), mock.patch(
            "openrouter.get_session_clock_updates",
            return_value={"advance_clocks": [
                {"clock_id": "trapped_ferrymen", "delta": 1, "reason": "The party freed a trapped crewman."},
            ]},
        ), mock.patch(
            "routes.sessions._repair_post_turn_clocks",
            return_value=(7, None),
        ), mock.patch(
            "services.dm_tools.build_session_summary_finalize_context",
            return_value={"summary_context": True},
        ), mock.patch(
            "openrouter.get_session_running_summary_finalize",
            side_effect=_fake_finalize,
        ), mock.patch(
            "routes.sessions._verify_post_turn_state",
            return_value=(True, None),
        ):
            first = retry_memory_recovery_task(self.campaign.id, task.id)
            self.assertFalse(first["ok"])
            self.assertIn("summary_finalize_failed", first["error"])

            clock_after_first = CampaignClock.query.filter_by(
                campaign_id=self.campaign.id,
                clock_id="trapped_ferrymen",
            ).first()
            self.assertEqual(clock_after_first.filled, 2)

            second = retry_memory_recovery_task(self.campaign.id, task.id)
            self.assertTrue(second["ok"], second)

        clock_final = CampaignClock.query.filter_by(
            campaign_id=self.campaign.id,
            clock_id="trapped_ferrymen",
        ).first()
        # The second retry must NOT re-apply the same adjudication; 2/4 is the
        # durable recovered state, not 3/4.
        self.assertEqual(clock_final.filled, 2)
        self.assertEqual(second["clock_recovered"], True)
        turn = SessionDmTurn.query.filter_by(
            campaign_id=self.campaign.id,
            player_message_id=7,
        ).first()
        self.assertEqual(turn.post_turn_status, "complete")
        self.assertFalse(has_pending_memory_recovery(self.campaign.id))

    def test_retry_skips_reapply_after_task_memory_marker_set(self):
        # A prior attempt may have applied memory but failed the clock replay,
        # leaving the task pending. The per-task memory_applied marker (never the
        # world revision) records that this exact patch is durable, so the retry
        # skips re-applying it and completes the skipped clock replay.
        db.session.add(
            NPCActor(
                campaign_id=self.campaign.id,
                actor_id="vex_mal",
                name="Vex Mal",
                dossier="{}",
            )
        )
        db.session.add(mark_session_dm_turn_error(
            self.campaign.id,
            self.session.id,
            player_message_id=7,
            trace_id="trace_7",
            error_text="missing_relation_endpoint",
            memory_status="error",
            clock_status="skipped",
        ))
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
        task = create_memory_recovery_task(
            self.campaign.id,
            self.session,
            player_message_id=7,
            dm_message_id=8,
            err=MemoryPipelineError(stage="validation", code="missing_relation_endpoint", message="missing"),
            patch=patch,
            trace_id="trace_7",
        )
        # Simulate a prior partial recovery: memory already applied, clock replay
        # failed, task left pending.
        task.memory_applied = True
        db.session.commit()

        with mock.patch(
            "services.dm_tools.build_session_clock_context",
            return_value={"allowed_evidence_sources": []},
        ), mock.patch(
            "openrouter.get_session_clock_updates",
            return_value={"advance_clocks": [], "retire_clocks": [], "create_clocks": []},
        ), mock.patch(
            "services.dm_tools.apply_compiled_session_memory_patch",
        ) as apply_mock, mock.patch(
            "routes.sessions._repair_post_turn_clocks",
            return_value=(7, None),
        ), mock.patch(
            "services.dm_tools.build_session_summary_finalize_context",
            return_value={"summary_context": True},
        ), mock.patch(
            "openrouter.get_session_running_summary_finalize",
            return_value={"running_summary": "Recovered summary."},
        ), mock.patch(
            "routes.sessions._verify_post_turn_state",
            return_value=(True, None),
        ):
            result = retry_memory_recovery_task(self.campaign.id, task.id)

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["memory_already_applied"])
        apply_mock.assert_not_called()

        turn = SessionDmTurn.query.filter_by(
            campaign_id=self.campaign.id,
            player_message_id=7,
        ).first()
        self.assertEqual(turn.post_turn_status, "complete")
        self.assertEqual(turn.clock_status, "complete")
        self.assertFalse(has_pending_memory_recovery(self.campaign.id))

    def test_stale_base_revision_fails_closed_not_silently_discarded(self):
        # An unrelated later memory write advances the campaign revision while a
        # recovery task is pending. The retry must NOT infer that the failed
        # patch was applied; it must attempt the apply, reject the stale base,
        # and keep the task pending so the missing memory is never silently lost.
        db.session.add(
            NPCActor(
                campaign_id=self.campaign.id,
                actor_id="vex_mal",
                name="Vex Mal",
                dossier="{}",
            )
        )
        db.session.add(mark_session_dm_turn_error(
            self.campaign.id,
            self.session.id,
            player_message_id=7,
            trace_id="trace_7",
            error_text="missing_relation_endpoint",
            memory_status="error",
            clock_status="skipped",
        ))
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
        task = create_memory_recovery_task(
            self.campaign.id,
            self.session,
            player_message_id=7,
            dm_message_id=8,
            err=MemoryPipelineError(stage="validation", code="missing_relation_endpoint", message="missing"),
            patch=patch,
            trace_id="trace_7",
        )

        # Unrelated write advances the revision; the failed patch is NOT applied.
        world = CampaignWorld.query.filter_by(campaign_id=self.campaign.id).first()
        world.memory_revision = (world.memory_revision or 0) + 1
        db.session.commit()

        result = retry_memory_recovery_task(self.campaign.id, task.id)

        self.assertFalse(result["ok"])
        self.assertIn("stale", result["error"])
        refreshed = db.session.get(SessionMemoryRecoveryTask, task.id)
        self.assertEqual(refreshed.status, "pending")
        self.assertFalse(refreshed.memory_applied)
        self.assertTrue(has_pending_memory_recovery(self.campaign.id))

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
        self.session = CampaignSession(campaign_id=self.campaign.id)
        db.session.add(self.session)

        self.player_user = User(username="route_player", email="player@example.com")
        self.player_user.set_password("password")
        db.session.add(self.player_user)
        self.dm_user = User(username="route_codm", email="codm@example.com")
        self.dm_user.set_password("password")
        db.session.add(self.dm_user)
        db.session.flush()
        db.session.add(
            CampaignMember(
                campaign_id=self.campaign.id,
                user_id=self.player_user.id,
                role="player",
            )
        )
        db.session.add(
            CampaignMember(
                campaign_id=self.campaign.id,
                user_id=self.dm_user.id,
                role="dm",
            )
        )
        db.session.commit()

        self.token = generate_token(user.id)
        self.client = self.app.test_client()
        self.headers = {"Authorization": f"Bearer {self.token}"}
        self.player_headers = {"Authorization": f"Bearer {generate_token(self.player_user.id)}"}
        self.dm_headers = {"Authorization": f"Bearer {generate_token(self.dm_user.id)}"}

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
            session_id=self.session.id,
            player_message_id=1,
            status="pending",
            error_code="missing_relation_endpoint",
            patch_json=json.dumps(patch),
        )
        db.session.add(task)
        db.session.commit()

        with mock.patch(
            "services.dm_tools.build_session_clock_context",
            return_value={"allowed_evidence_sources": []},
        ), mock.patch(
            "openrouter.get_session_clock_updates",
            return_value={"advance_clocks": [], "retire_clocks": [], "create_clocks": []},
        ), mock.patch(
            "routes.sessions._repair_post_turn_clocks",
            return_value=(7, None),
        ), mock.patch(
            "services.dm_tools.build_session_summary_finalize_context",
            return_value={"summary_context": True},
        ), mock.patch(
            "openrouter.get_session_running_summary_finalize",
            return_value={"running_summary": "Recovered summary."},
        ), mock.patch(
            "routes.sessions._verify_post_turn_state",
            return_value=(True, None),
        ):
            response = self.client.post(
                f"/api/campaigns/{self.campaign.id}/memory-recovery/{task.id}/retry",
                headers=self.headers,
            )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["status"], "resolved")
        self.assertTrue(data["clock_recovered"])

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

    def test_retry_route_requires_dm_or_owner(self):
        from auth import generate_token

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
            player_message_id=3,
            status="pending",
            error_code="missing_relation_endpoint",
            patch_json=json.dumps(patch),
        )
        db.session.add(task)
        db.session.commit()

        player_response = self.client.post(
            f"/api/campaigns/{self.campaign.id}/memory-recovery/{task.id}/retry",
            headers=self.player_headers,
        )
        self.assertEqual(player_response.status_code, 403)

        dm_response = self.client.post(
            f"/api/campaigns/{self.campaign.id}/memory-recovery/{task.id}/retry",
            headers=self.dm_headers,
        )
        self.assertEqual(dm_response.status_code, 400)

        # A non-member user is also denied.
        stranger = User(username="stranger", email="stranger@example.com")
        stranger.set_password("password")
        db.session.add(stranger)
        db.session.commit()
        stranger_headers = {"Authorization": f"Bearer {generate_token(stranger.id)}"}
        stranger_response = self.client.post(
            f"/api/campaigns/{self.campaign.id}/memory-recovery/{task.id}/retry",
            headers=stranger_headers,
        )
        self.assertEqual(stranger_response.status_code, 403)

        # The task was not silently resolved by the denied retry; the DM retry
        # above did legitimately attempt and fail the stale/invalid patch.
        refreshed = db.session.get(SessionMemoryRecoveryTask, task.id)
        self.assertEqual(refreshed.status, "pending")
        self.assertEqual(refreshed.attempts, 1)

    def test_pending_route_hides_recovery_metadata_from_players(self):
        task = SessionMemoryRecoveryTask(
            campaign_id=self.campaign.id,
            player_message_id=4,
            status="pending",
            error_code="missing_relation_endpoint",
            error_text="Relation source 'vex_mal' not found in graph entities.",
            patch_json="{}",
        )
        db.session.add(task)
        db.session.commit()

        player_response = self.client.get(
            f"/api/campaigns/{self.campaign.id}/memory-recovery/pending",
            headers=self.player_headers,
        )
        self.assertEqual(player_response.status_code, 403)
        self.assertNotIn("vex_mal", player_response.get_data(as_text=True))

        dm_response = self.client.get(
            f"/api/campaigns/{self.campaign.id}/memory-recovery/pending",
            headers=self.dm_headers,
        )
        self.assertEqual(dm_response.status_code, 200)
        data = dm_response.get_json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["tasks"][0]["error_code"], "missing_relation_endpoint")

    def test_player_post_turn_status_does_not_leak_recovery_metadata(self):
        # A memory failure surfaces in the ordinary player-facing dm-turn-status
        # payload. It must stay minimal (recoverable flag + opaque task id) and
        # must not carry DM-internal recovery fields or private entity ids.
        player_message_id = 5
        task = SessionMemoryRecoveryTask(
            campaign_id=self.campaign.id,
            session_id=self.session.id,
            player_message_id=player_message_id,
            dm_message_id=6,
            trace_id="trace_secret",
            status="pending",
            error_code="missing_relation_endpoint",
            error_text="Relation source 'vex_mal' not found in graph entities.",
            last_error_text="clock_replay_failed: provider unavailable",
            patch_json='{"upsert_graph_relations":[{"source_id":"vex_mal"}]}',
        )
        db.session.add(task)
        db.session.add(CampaignAuditEvent(
            campaign_id=self.campaign.id,
            event_type='memory_update_error',
            source='session_memory',
            actor='session_memory_writer',
            trace_id=f"session_memory_writer:session_{self.session.id}:message_{player_message_id}",
            summary='Post-turn memory update failed.',
            payload=json.dumps({'telemetry': {'pipeline_error_stage': 'validation'}}),
        ))
        db.session.add(CampaignAuditEvent(
            campaign_id=self.campaign.id,
            event_type='dm_output_stored',
            source='session_messages',
            actor='session_dm',
            summary='DM response stored.',
            payload=json.dumps({'player_message_id': player_message_id, 'dm_message_id': 6}),
        ))
        db.session.commit()

        response = self.client.get(
            f"/api/sessions/{self.session.id}/dm-turn-status",
            headers=self.player_headers,
            query_string={'after_message_id': player_message_id},
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["has_pending_recovery"])
        self.assertEqual(data["recovery_task"], {"id": task.id})

        body_text = response.get_data(as_text=True)
        self.assertNotIn("vex_mal", body_text)
        self.assertNotIn("trace_secret", body_text)
        self.assertNotIn("error_text", body_text)
        self.assertNotIn("last_error_text", body_text)
        self.assertNotIn("patch_json", body_text)


if __name__ == "__main__":
    unittest.main()

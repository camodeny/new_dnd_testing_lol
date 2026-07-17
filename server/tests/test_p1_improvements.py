import os
import sys
import json
import unittest
import unittest.mock
from datetime import datetime
from flask import Flask

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from models import (
    db,
    User,
    Campaign,
    CampaignSession,
    AutomationRun,
    AutomationRunAuditCycle,
    AutomationRunAuditorJob,
    AutomationRunEvent,
    AutomationRunProviderCall,
    CampaignAuditEvent,
    SessionMessage,
    AutomationRunAuditAttempt,
    AutomationSnapshot,
    CampaignWorld,
    NPCActor
)
from services.automation_service import create_audit_cycle
from services.automation_auditor import execute_auditor_tool, _cycle_boundaries
from services.session_memory_agent import compile_staged_memory_patch
from services.dm_tools import _validate_memory_scene_patch, apply_memory_patch

class P1ImprovementsTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SECRET_KEY'] = 'test-secret'
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

        # Register blueprint
        from routes.automation import automation_bp
        self.app.register_blueprint(automation_bp)

        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        # Seed basic models
        self.user = User(username='testuser', email='test@example.com', password_hash='hash')
        db.session.add(self.user)
        db.session.flush()

        self.campaign = Campaign(name='P1 Test Campaign', user_id=self.user.id)
        db.session.add(self.campaign)
        db.session.flush()

        self.session = CampaignSession(campaign_id=self.campaign.id, running_summary="Summary")
        db.session.add(self.session)
        db.session.flush()

        self.world = CampaignWorld(
            campaign_id=self.campaign.id,
            public_intro='{}',
            knowledge_graph='{"entities":[],"relations":[],"facts":[]}',
            world_state='{"current_scene":{"location_id":"waterdeep","location_name":"Waterdeep"}}',
            dm_private='{}'
        )
        db.session.add(self.world)
        db.session.flush()

        self.snapshot = AutomationSnapshot(
            scenario_id=1,
            source_campaign_id=self.campaign.id,
            label="Test Snapshot",
            snapshot_json={}
        )
        db.session.add(self.snapshot)
        db.session.flush()

        self.run = AutomationRun(
            scenario_id=1,
            snapshot_id=self.snapshot.id,
            user_id=self.user.id,
            status='active',
            derived_campaign_id=self.campaign.id
        )
        db.session.add(self.run)
        db.session.commit()

        # Create client for route testing
        self.client = self.app.test_client()

        # Mock authenticate_request to return our user
        self.auth_patcher = unittest.mock.patch('auth.authenticate_request', return_value=(self.user, None))
        self.auth_patcher.start()

        # Mock _run_owned_by_user to return True
        self.owner_patcher = unittest.mock.patch('routes.automation._run_owned_by_user', return_value=True)
        self.owner_patcher.start()

    def tearDown(self):
        self.auth_patcher.stop()
        self.owner_patcher.stop()
        db.session.rollback()
        db.drop_all()
        self.ctx.pop()

    # 1. test_boundary_excludes_future_transcript_messages
    def test_boundary_excludes_future_transcript_messages(self):
        m1 = SessionMessage(session_id=self.session.id, role="player", content="Message 1")
        db.session.add(m1)
        db.session.commit()

        # Create cycle with boundary at message 1
        cycle = create_audit_cycle(self.run, 'after_player', player_message_id=m1.id)

        # Create future message
        m2 = SessionMessage(session_id=self.session.id, role="dm", content="Message 2")
        db.session.add(m2)
        db.session.commit()

        res = execute_auditor_tool(self.run, 'get_transcript', {'limit': 10})
        messages = res.get('messages', [])
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]['id'], m1.id)

    # 2. test_boundary_excludes_future_audit_events
    def test_boundary_excludes_future_audit_events(self):
        ae1 = CampaignAuditEvent(campaign_id=self.campaign.id, event_type="test", summary="Event 1")
        db.session.add(ae1)
        db.session.commit()

        cycle = create_audit_cycle(self.run, 'after_player')

        ae2 = CampaignAuditEvent(campaign_id=self.campaign.id, event_type="test", summary="Event 2")
        db.session.add(ae2)
        db.session.commit()

        res = execute_auditor_tool(self.run, 'get_audit_events')
        events = res.get('events', [])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['id'], ae1.id)

    # 3. test_boundary_excludes_future_provider_calls
    def test_boundary_excludes_future_provider_calls(self):
        pc1 = AutomationRunProviderCall(run_id=self.run.id, dedupe_key="pc1", phase="after_player", request_json={})
        db.session.add(pc1)
        db.session.commit()

        cycle = create_audit_cycle(self.run, 'after_player')

        pc2 = AutomationRunProviderCall(run_id=self.run.id, dedupe_key="pc2", phase="after_player", request_json={})
        db.session.add(pc2)
        db.session.commit()

        res = execute_auditor_tool(self.run, 'get_provider_calls')
        calls = res.get('provider_calls', [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['id'], pc1.id)

    # 4. test_boundary_excludes_future_run_events
    def test_boundary_excludes_future_run_events(self):
        # Flushes boundary pause run event during creation
        cycle = create_audit_cycle(self.run, 'after_player')

        # Create future run event
        re2 = AutomationRunEvent(run_id=self.run.id, event_type="future_event", sequence_number=99, payload_json={})
        db.session.add(re2)
        db.session.commit()

        res = execute_auditor_tool(self.run, 'get_run_events')
        events = res.get('events', [])
        # Should include pause run event but not the future run event
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['event_type'], 'audit_cycle_paused')

    # 5. test_boundary_excludes_future_detail_fetch_by_id
    def test_boundary_excludes_future_detail_fetch_by_id(self):
        pc1 = AutomationRunProviderCall(run_id=self.run.id, dedupe_key="pc1", phase="after_player", request_json={})
        db.session.add(pc1)
        db.session.commit()

        cycle = create_audit_cycle(self.run, 'after_player')

        pc2 = AutomationRunProviderCall(run_id=self.run.id, dedupe_key="pc2", phase="after_player", request_json={})
        db.session.add(pc2)
        db.session.commit()

        # Fetch detail of post-boundary pc2
        res = execute_auditor_tool(self.run, 'get_provider_call_detail', {'provider_call_id': pc2.id})
        self.assertIn('error', res)

        # Detail of pre-boundary pc1 should succeed
        res2 = execute_auditor_tool(self.run, 'get_provider_call_detail', {'provider_call_id': pc1.id})
        self.assertNotIn('error', res2)

    # 6. test_boundary_includes_audit_cycle_paused_event
    def test_boundary_includes_audit_cycle_paused_event(self):
        cycle = create_audit_cycle(self.run, 'after_player')
        boundaries = _cycle_boundaries(cycle)
        self.assertIsNotNone(boundaries.get("run_event_id"))

        # Verify pause event exists
        paused_event = db.session.get(AutomationRunEvent, boundaries["run_event_id"])
        self.assertEqual(paused_event.event_type, 'audit_cycle_paused')

    # 7. test_old_unbounded_cycle_preserves_backward_compatibility
    def test_old_unbounded_cycle_preserves_backward_compatibility(self):
        # Create cycle without boundary markers in payload
        cycle = AutomationRunAuditCycle(
            run_id=self.run.id,
            cycle_number=99,
            phase='after_player',
            status='pending',
            payload_json={}
        )
        db.session.add(cycle)
        db.session.flush()
        self.run.awaiting_audit_cycle_id = cycle.id
        db.session.commit()

        # Add provider call
        pc1 = AutomationRunProviderCall(run_id=self.run.id, dedupe_key="pc1", phase="after_player", request_json={})
        db.session.add(pc1)
        db.session.commit()

        # Should fetch without boundaries blocking it
        res = execute_auditor_tool(self.run, 'get_provider_calls')
        self.assertEqual(len(res.get('provider_calls', [])), 1)

    # 8. test_failed_attempt_survives_after_submit_validation_exception
    def test_failed_attempt_survives_after_submit_validation_exception(self):
        cycle = create_audit_cycle(self.run, 'after_player')
        
        # Mock submit_audit_cycle_feedback to raise a validation exception
        with unittest.mock.patch('routes.automation.submit_audit_cycle_feedback', side_effect=ValueError("Validation exception")):
            payload = {
                'scorecard': {
                    'criteria': []
                }
            }
            resp = self.client.post(
                f'/api/automation/runs/{self.run.id}/audit-cycles/{cycle.id}/audit',
                json=payload
            )
            self.assertEqual(resp.status_code, 500)

        # Check attempt was persisted with status failed
        attempt = AutomationRunAuditAttempt.query.filter_by(cycle_id=cycle.id).first()
        self.assertIsNotNone(attempt)
        self.assertEqual(attempt.status, 'failed')
        self.assertEqual(attempt.error_class, 'ValueError')

    # 9. test_failed_attempt_rolls_back_dirty_submission_transaction_first
    def test_failed_attempt_rolls_back_dirty_submission_transaction_first(self):
        cycle = create_audit_cycle(self.run, 'after_player')

        # Insert a dummy record that shouldn't be committed if the request crashes
        # Mock submit_audit_cycle_feedback to dirty session and raise exception
        def dirty_and_fail(*args, **kwargs):
            ae = CampaignAuditEvent(campaign_id=self.campaign.id, event_type="dirty_event", summary="should rollback")
            db.session.add(ae)
            raise ValueError("validation failed")

        with unittest.mock.patch('routes.automation.submit_audit_cycle_feedback', side_effect=dirty_and_fail):
            resp = self.client.post(
                f'/api/automation/runs/{self.run.id}/audit-cycles/{cycle.id}/audit',
                json={}
            )
            self.assertEqual(resp.status_code, 500)

        # Check dirty event was rolled back
        dirty_event = CampaignAuditEvent.query.filter_by(event_type="dirty_event").first()
        self.assertIsNone(dirty_event)

        # Attempt logging itself must be successfully committed
        attempt = AutomationRunAuditAttempt.query.filter_by(cycle_id=cycle.id).first()
        self.assertIsNotNone(attempt)
        self.assertEqual(attempt.status, 'failed')
        self.assertEqual(attempt.error_class, 'ValueError')

    # 10. test_success_attempt_created_once_on_valid_submission
    def test_success_attempt_created_once_on_valid_submission(self):
        cycle = create_audit_cycle(self.run, 'after_player')

        # Valid submission (mock scorecard criteria checking)
        resp = self.client.post(
            f'/api/automation/runs/{self.run.id}/audit-cycles/{cycle.id}/audit',
            json={'summary': 'Pass', 'notes': 'All good', 'scorecard': {'criteria': []}}
        )
        self.assertEqual(resp.status_code, 200)

        attempts = AutomationRunAuditAttempt.query.filter_by(cycle_id=cycle.id).all()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].status, 'success')

    # 11. test_success_attempt_records_normalized_payload
    def test_success_attempt_records_normalized_payload(self):
        cycle = create_audit_cycle(self.run, 'after_player')
        resp = self.client.post(
            f'/api/automation/runs/{self.run.id}/audit-cycles/{cycle.id}/audit',
            json={'summary': 'Approved summary', 'notes': 'Fine notes', 'scorecard': {'criteria': []}}
        )
        self.assertEqual(resp.status_code, 200)

        attempt = AutomationRunAuditAttempt.query.filter_by(cycle_id=cycle.id).first()
        self.assertIsNotNone(attempt.normalized_payload_json)
        self.assertEqual(attempt.normalized_payload_json['summary'], 'Approved summary')
        self.assertEqual(attempt.normalized_payload_json['notes'], 'Fine notes')

    # 12. test_memory_normalizers_preserve_provenance
    def test_memory_normalizers_preserve_provenance(self):
        memory_context = {
            'campaign_id': self.campaign.id,
            'source_player_message_id': 12,
            'source_dm_message_id': 15,
        }
        extracted = {}
        resolved = {
            'upsert_graph_facts': [
                {
                    'text': 'The red dragon sleeps.',
                    'provenance': {
                        'source_player_message_id': 12,
                        'source_dm_message_id': 15,
                        'tool_name': 'test_tool',
                        'evidence_basis': ['The dragon is sleeping.']
                    }
                }
            ]
        }
        compiled = compile_staged_memory_patch(memory_context, extracted, resolved)
        fact = compiled['upsert_graph_facts'][0]
        self.assertIn('provenance', fact)
        self.assertEqual(fact['provenance']['source_player_message_id'], 12)
        self.assertEqual(fact['provenance']['tool_name'], 'test_tool')

    # 13. test_memory_normalizers_preserve_resolution_mode
    def test_memory_normalizers_preserve_resolution_mode(self):
        memory_context = {
            'campaign_id': self.campaign.id,
            'source_player_message_id': 12,
            'source_dm_message_id': 15,
        }
        extracted = {}
        resolved = {
            'upsert_graph_facts': [
                {
                    'text': 'Waterdeep is a city.',
                    'resolution_mode': 'canonical'
                }
            ]
        }
        compiled = compile_staged_memory_patch(memory_context, extracted, resolved)
        fact = compiled['upsert_graph_facts'][0]
        self.assertEqual(fact['resolution_mode'], 'canonical')

    # 14. test_scene_location_resolution_marks_canonical_only_after_successful_resolution
    def test_scene_location_resolution_marks_canonical_only_after_successful_resolution(self):
        # Set up a known location in knowledge graph
        self.world.knowledge_graph = '{"entities":[{"id":"phandalin","type":"location","name":"Phandalin"}],"relations":[],"facts":[]}'
        db.session.add(self.world)
        db.session.commit()

        memory_context = {
            'campaign_id': self.campaign.id,
            'hot_context': {
                'current_scene': {
                    'location_id': 'phandalin',
                    'location_name': 'Phandalin'
                }
            }
        }

        # Case A: Matches Phandalin (canonical)
        extracted = {'scene_patch': {'location_name': 'Phandalin'}}
        resolved = {'scene_patch': {'location_name': 'Phandalin'}}
        compiled_canonical = compile_staged_memory_patch(memory_context, extracted, resolved)
        self.assertEqual(compiled_canonical['scene_patch']['resolution_mode'], 'canonical')

        # Case B: Unknown location (now treated as 'new' — creating a new location)
        extracted_unresolved = {'scene_patch': {'location_name': 'Neverwinter Wood'}}
        resolved_unresolved = {'scene_patch': {'location_name': 'Neverwinter Wood'}}
        compiled_unresolved = compile_staged_memory_patch(memory_context, extracted_unresolved, resolved_unresolved)
        self.assertEqual(compiled_unresolved['scene_patch']['resolution_mode'], 'new')

    def test_boundary_with_zero_excludes_all_future_provider_calls(self):
        # Create cycle when there are absolutely NO provider calls
        cycle = create_audit_cycle(self.run, 'after_player')
        boundaries = _cycle_boundaries(cycle)
        self.assertEqual(boundaries.get("provider_call_id"), 0)

        # Create a future provider call
        pc1 = AutomationRunProviderCall(run_id=self.run.id, dedupe_key="pc1", phase="after_player", request_json={})
        db.session.add(pc1)
        db.session.commit()

        # Auditor tool shouldn't return pc1, because max provider_call_id is 0
        res = execute_auditor_tool(self.run, 'get_provider_calls')
        calls = res.get('provider_calls', [])
        self.assertEqual(len(calls), 0)

    def test_metadata_only_scene_patch_does_not_mutate_or_trigger_events(self):
        patch = {
            'scene_patch': {
                'provenance': {'source_player_message_id': 1},
                'resolution_mode': 'canonical'
            }
        }
        result = apply_memory_patch(self.campaign, self.session, patch)
        self.assertNotIn('scene_updated', result.get('world_event_ids', []))

        db.session.refresh(self.world)
        import json
        state = json.loads(self.world.world_state)
        current_scene = state.get('current_scene', {})
        self.assertNotIn('provenance', current_scene)
        self.assertNotIn('resolution_mode', current_scene)

    def test_staged_entity_upsert_survives_compilation(self):
        memory_context = {'campaign_id': self.campaign.id}
        extracted = {}
        resolved = {
            'upsert_graph_entities': [
                {
                    'id': 'gundren_rockseeker',
                    'name': 'Gundren Rockseeker',
                    'type': 'npc',
                    'summary': 'A dwarf patron.',
                    'tags': ['dwarf', 'patron'],
                    'certainty': 'confirmed',
                    'importance': 4,
                    'intended_visibility': 'party_known',
                    'source_surface': 'visible_transcript',
                    'reason': 'Introduced by DM.',
                    'provenance': {'evidence_basis': ['Dwarf named Gundren']}
                }
            ]
        }
        compiled = compile_staged_memory_patch(memory_context, extracted, resolved)
        entities = compiled.get('upsert_graph_entities', [])
        self.assertEqual(len(entities), 1)
        ent = entities[0]
        self.assertEqual(ent['id'], 'gundren_rockseeker')
        self.assertEqual(ent['name'], 'Gundren Rockseeker')
        self.assertEqual(ent['type'], 'npc')
        self.assertEqual(ent['summary'], 'A dwarf patron.')
        self.assertEqual(ent['importance'], 4)
        self.assertEqual(ent['certainty'], 'confirmed')
        self.assertEqual(ent['visibility'], 'party_known')
        self.assertEqual(ent['provenance']['evidence_basis'], ['Dwarf named Gundren'])

    def test_staged_relation_valid_endpoints_survives(self):
        # Entity already in KG
        self.world.knowledge_graph = '{"entities":[{"id":"phandalin","type":"location","name":"Phandalin"}],"relations":[],"facts":[]}'
        db.session.add(self.world)
        db.session.commit()

        memory_context = {'campaign_id': self.campaign.id}
        extracted = {}
        resolved = {
            'upsert_graph_entities': [
                {
                    'id': 'stonehill_inn',
                    'name': 'Stonehill Inn',
                    'type': 'location'
                }
            ],
            'upsert_graph_relations': [
                {
                    'type': 'located_in',
                    'source_ref': 'stonehill_inn', # created in same patch
                    'target_ref': 'phandalin',     # exists in campaign world
                    'summary': 'The Inn is located in Phandalin.',
                    'certainty': 'confirmed',
                    'importance': 3,
                    'provenance': {'evidence_basis': ['Located in Phandalin.']}
                }
            ]
        }
        compiled = compile_staged_memory_patch(memory_context, extracted, resolved)
        relations = compiled.get('upsert_graph_relations', [])
        self.assertEqual(len(relations), 1)
        rel = relations[0]
        self.assertEqual(rel['source_id'], 'stonehill_inn')
        self.assertEqual(rel['target_id'], 'phandalin')
        self.assertEqual(rel['type'], 'located_in')
        self.assertEqual(rel['provenance']['evidence_basis'], ['Located in Phandalin.'])

    def test_staged_relation_unresolved_endpoint_rejected(self):
        memory_context = {'campaign_id': self.campaign.id}
        extracted = {}
        resolved = {
            'upsert_graph_relations': [
                {
                    'type': 'located_in',
                    'source_ref': 'unknown_inn',
                    'target_ref': 'unknown_town',
                    'provenance': {'evidence_basis': ['Some proof.']}
                }
            ]
        }
        compiled = compile_staged_memory_patch(memory_context, extracted, resolved)
        self.assertEqual(len(compiled.get('upsert_graph_relations', [])), 0)
        unresolved = compiled.get('unresolved_items', [])
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0]['kind'], 'relation')
        self.assertEqual(unresolved[0]['reason'], 'unresolved_relation_endpoints')
        self.assertIn('unknown_inn', unresolved[0]['unresolved_endpoints'])

    def test_staged_npc_update_valid_survives(self):
        # Known NPC in DB
        npc = NPCActor(campaign_id=self.campaign.id, actor_id='sildar_hallwinter', name='Sildar Hallwinter', public_summary='A human warrior.', dossier='{}')
        db.session.add(npc)
        db.session.commit()

        # Check update to existing NPC, and check update to new NPC created in the same patch
        memory_context = {'campaign_id': self.campaign.id}
        extracted = {}
        resolved = {
            'upsert_graph_entities': [
                {
                    'id': 'gundren_rockseeker',
                    'name': 'Gundren Rockseeker',
                    'type': 'npc'
                }
            ],
            'update_npc_actors': [
                {
                    'actor_ref': 'sildar_hallwinter',
                    'role': 'Member of Lord\'s Alliance',
                    'voice': 'Gruff'
                },
                {
                    'actor_ref': 'gundren_rockseeker', # created in same patch
                    'role': 'Patron dwarf'
                }
            ]
        }
        compiled = compile_staged_memory_patch(memory_context, extracted, resolved)
        npcs = compiled.get('update_npc_actors', [])
        self.assertEqual(len(npcs), 2)
        sildar = next(n for n in npcs if n['id'] == 'sildar_hallwinter')
        gundren = next(n for n in npcs if n['id'] == 'gundren_rockseeker')
        self.assertEqual(sildar['role'], 'Member of Lord\'s Alliance')
        self.assertEqual(sildar['voice'], 'Gruff')
        self.assertEqual(gundren['role'], 'Patron dwarf')

    def test_staged_npc_update_unknown_rejected(self):
        memory_context = {'campaign_id': self.campaign.id}
        extracted = {}
        resolved = {
            'update_npc_actors': [
                {
                    'actor_ref': 'unknown_npc_id',
                    'role': 'Rogue'
                }
            ]
        }
        compiled = compile_staged_memory_patch(memory_context, extracted, resolved)
        self.assertEqual(len(compiled.get('update_npc_actors', [])), 0)
        unresolved = compiled.get('unresolved_items', [])
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0]['kind'], 'npc_actor')
        self.assertEqual(unresolved[0]['actor_id'], 'unknown_npc_id')

    def test_staged_world_event_survives(self):
        memory_context = {'campaign_id': self.campaign.id}
        extracted = {}
        resolved = {
            'record_events': [
                {
                    'event_type': 'combat_started',
                    'summary': 'The party engaged Goblins.',
                    'payload': {'enemy': 'goblin', 'count': 4},
                    'source_surface': 'visible_transcript',
                    'intended_visibility': 'public'
                }
            ]
        }
        compiled = compile_staged_memory_patch(memory_context, extracted, resolved)
        events = compiled.get('record_events', [])
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev['event_type'], 'combat_started')
        self.assertEqual(ev['summary'], 'The party engaged Goblins.')
        self.assertEqual(ev['payload'], {'enemy': 'goblin', 'count': 4})
        self.assertEqual(ev['visibility'], 'public')
        self.assertEqual(ev['provenance']['tool_name'], 'session_memory_record_event')

    def test_missing_campaign_path_safe(self):
        from services.session_memory_agent import MemoryPipelineError
        memory_context = {'campaign_id': None}
        extracted = {'running_summary': 'No campaign summary'}
        resolved = {}
        with self.assertRaises(MemoryPipelineError) as ctx:
            compile_staged_memory_patch(memory_context, extracted, resolved)
        self.assertEqual(ctx.exception.stage, 'compilation')
        self.assertEqual(ctx.exception.code, 'missing_campaign')

    def test_persistence_applies_new_mutations(self):
        # Known NPC in DB
        npc = NPCActor(campaign_id=self.campaign.id, actor_id='sildar_hallwinter', name='Sildar Hallwinter', public_summary='A human warrior.', dossier='{}')
        db.session.add(npc)
        db.session.commit()

        compiled_patch = {
            'running_summary': 'New Summary',
            'upsert_graph_entities': [
                {
                    'id': 'gundren_rockseeker',
                    'name': 'Gundren Rockseeker',
                    'type': 'npc',
                    'summary': 'A dwarf patron.'
                }
            ],
            'upsert_graph_relations': [
                {
                    'id': 'rel_gundren_sildar',
                    'type': 'friends',
                    'source_id': 'gundren_rockseeker',
                    'target_id': 'sildar_hallwinter',
                    'summary': 'They are friends.'
                }
            ],
            'update_npc_actors': [
                {
                    'id': 'sildar_hallwinter',
                    'role': 'Warrior member of Lord\'s Alliance'
                }
            ],
            'record_events': [
                {
                    'event_type': 'story_beat',
                    'summary': 'Gundren hired the party.',
                    'payload': {'gold': 10}
                }
            ]
        }
        res = apply_memory_patch(self.campaign, self.session, compiled_patch)
        self.assertTrue(res['running_summary_updated'])
        self.assertEqual(len(res['graph_changes']), 2) # entity + relation
        self.assertEqual(len(res['npc_changes']), 1)
        self.assertEqual(len(res['world_event_ids']), 2) # NPC update records an event, and the manual event record

        # Verify NPC got updated in DB
        db.session.refresh(npc)
        self.assertEqual(npc.role, 'Warrior member of Lord\'s Alliance')

        # Verify entity got created in CampaignWorld KG
        db.session.refresh(self.world)
        import json
        kg = json.loads(self.world.knowledge_graph)
        self.assertTrue(any(e['id'] == 'gundren_rockseeker' for e in kg['entities']))
        self.assertTrue(any(r['id'] == 'rel_gundren_sildar' for r in kg['relations']))

    def test_relation_id_collision_across_patches(self):
        # 1. First patch: Stonehill Inn located in Phandalin
        self.world.knowledge_graph = '{"entities":[{"id":"stonehill_inn","type":"location","name":"Stonehill Inn"},{"id":"phandalin","type":"location","name":"Phandalin"}],"relations":[],"facts":[]}'
        db.session.add(self.world)
        db.session.commit()

        compiled_patch_1 = {
            'upsert_graph_relations': [
                {
                    'type': 'located_in',
                    'source_id': 'stonehill_inn',
                    'target_id': 'phandalin',
                    'summary': 'The Inn is in Phandalin.'
                }
            ]
        }
        res1 = apply_memory_patch(self.campaign, self.session, compiled_patch_1)
        self.assertEqual(len(res1['graph_changes']), 1)

        # 2. Second patch: Gundren friends with Sildar
        db.session.refresh(self.world)
        kg1 = json.loads(self.world.knowledge_graph)
        kg1['entities'].extend([
            {'id': 'gundren', 'type': 'npc', 'name': 'Gundren'},
            {'id': 'sildar', 'type': 'npc', 'name': 'Sildar'}
        ])
        self.world.knowledge_graph = json.dumps(kg1)
        db.session.add(self.world)
        db.session.commit()

        compiled_patch_2 = {
            'upsert_graph_relations': [
                {
                    'type': 'friends',
                    'source_id': 'gundren',
                    'target_id': 'sildar',
                    'summary': 'They are friends.'
                }
            ]
        }
        res2 = apply_memory_patch(self.campaign, self.session, compiled_patch_2)
        self.assertEqual(len(res2['graph_changes']), 1)

        # Confirm both relations exist in DB
        db.session.refresh(self.world)
        kg2 = json.loads(self.world.knowledge_graph)
        self.assertEqual(len(kg2['relations']), 2)
        self.assertTrue(any(r['type'] == 'located_in' for r in kg2['relations']))
        self.assertTrue(any(r['type'] == 'friends' for r in kg2['relations']))

    def test_npc_metadata_completeness(self):
        # Seed an NPC actor to update
        npc = NPCActor(campaign_id=self.campaign.id, actor_id='gundren_rockseeker', name='Gundren', public_summary='Dwarf', dossier='{}')
        db.session.add(npc)
        
        # Seed sildar so relationship validation passes
        sildar = NPCActor(campaign_id=self.campaign.id, actor_id='sildar_hallwinter', name='Sildar', dossier='{}')
        db.session.add(sildar)
        db.session.commit()

        memory_context = {'campaign_id': self.campaign.id}
        extracted = {}
        resolved = {
            'update_npc_actors': [
                {
                    'id': 'gundren_rockseeker',
                    'relationships': {'sildar_hallwinter': 'Friends'},
                    'recent_offscreen_activity': ['Traveled to Phandalin'],
                    'expires_or_retire_condition': 'Retires when campaign ends',
                    'memory_type': 'npc'
                }
            ]
        }
        compiled = compile_staged_memory_patch(memory_context, extracted, resolved)
        npcs = compiled.get('update_npc_actors', [])
        self.assertEqual(len(npcs), 1)
        self.assertEqual(npcs[0]['relationships'], {'sildar_hallwinter': 'Friends'})
        self.assertEqual(npcs[0]['recent_offscreen_activity'], ['Traveled to Phandalin'])
        self.assertEqual(npcs[0]['expires_or_retire_condition'], 'Retires when campaign ends')
        self.assertEqual(npcs[0]['memory_type'], 'npc')

        # Persist it
        apply_memory_patch(self.campaign, self.session, compiled)
        db.session.refresh(npc)
        dossier = json.loads(npc.dossier)
        self.assertEqual(dossier.get('relationships'), {'sildar_hallwinter': 'Friends'})
        self.assertEqual(dossier.get('recent_offscreen_activity'), ['Traveled to Phandalin'])
        self.assertEqual(dossier.get('expires_or_retire_condition'), 'Retires when campaign ends')
        self.assertEqual(dossier.get('memory_type'), 'npc')

    def test_party_known_event_visibility(self):
        memory_context = {'campaign_id': self.campaign.id}
        extracted = {}
        resolved = {
            'record_events': [
                {
                    'event_type': 'story_milestone',
                    'summary': 'The party cleared Cragmaw Hideout.',
                    'source_surface': 'visible_transcript',
                    'intended_visibility': 'party_known'
                }
            ]
        }
        compiled = compile_staged_memory_patch(memory_context, extracted, resolved)
        events = compiled.get('record_events', [])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['visibility'], 'party_known')

    def test_visibility_fail_closed(self):
        memory_context = {'campaign_id': self.campaign.id}
        extracted = {}
        resolved = {
            'record_events': [
                {
                    'event_type': 'story_milestone',
                    'summary': 'The party cleared Cragmaw Hideout.',
                    'intended_visibility': 'party_known' # Omitted source_surface
                }
            ]
        }
        compiled = compile_staged_memory_patch(memory_context, extracted, resolved)
        events = compiled.get('record_events', [])
        self.assertEqual(len(events), 1)
        # Without visible_transcript source_surface, it MUST fail closed to dm_private
        self.assertEqual(events[0]['visibility'], 'dm_private')

    def test_npc_update_remapping_after_entity_dedupe(self):
        # Sildar is already in the campaign knowledge graph
        self.world.knowledge_graph = '{"entities":[{"id":"sildar_hallwinter_canonical","type":"npc","name":"Sildar Hallwinter"}],"relations":[],"facts":[]}'
        db.session.add(self.world)
        db.session.commit()

        # Update NPC actor loop remaps temporary Same-Patch Entity IDs to their Deduplicated IDs
        compiled_patch = {
            'upsert_graph_entities': [
                {
                    'id': 'sildar_hallwinter_temp',
                    'name': 'Sildar Hallwinter', # same name, will deduplicate to sildar_hallwinter_canonical
                    'type': 'npc'
                }
            ],
            'update_npc_actors': [
                {
                    'id': 'sildar_hallwinter_temp',
                    'role': 'Warrior of Phandalin'
                }
            ]
        }
        
        # Create an NPCActor under the canonical ID so it can be updated
        npc = NPCActor(campaign_id=self.campaign.id, actor_id='sildar_hallwinter_canonical', name='Sildar', public_summary='Warrior', dossier='{}')
        db.session.add(npc)
        db.session.commit()

        res = apply_memory_patch(self.campaign, self.session, compiled_patch)
        self.assertEqual(len(res['npc_changes']), 1)
        self.assertEqual(res['npc_changes'][0]['npc_actor']['actor_id'], 'sildar_hallwinter_canonical')
        
        # Verify the actual NPCActor was updated in DB
        db.session.refresh(npc)
        self.assertEqual(npc.role, 'Warrior of Phandalin')

    def test_entity_id_trust_and_same_patch_compilation_remapping(self):
        memory_context = {'campaign_id': self.campaign.id}
        extracted = {}
        resolved = {
            'upsert_graph_entities': [
                {
                    'id': 'invented_hallucinated_id',
                    'name': 'Gundren Rockseeker',
                    'type': 'npc'
                }
            ],
            'update_npc_actors': [
                {
                    'id': 'invented_hallucinated_id',
                    'role': 'Dwarf Patron'
                }
            ],
            'upsert_graph_relations': [
                {
                    'type': 'allied_with',
                    'source_ref': 'invented_hallucinated_id',
                    'target_ref': 'sildar_hallwinter_canonical',
                    'summary': 'Allied'
                }
            ]
        }
        
        # Seed canonical sildar entity and NPCActor
        self.world.knowledge_graph = '{"entities":[{"id":"sildar_hallwinter_canonical","type":"npc","name":"Sildar Hallwinter"},{"id":"gundren_rockseeker","type":"npc","name":"Gundren Rockseeker"}],"relations":[],"facts":[]}'
        db.session.add(self.world)
        sildar = NPCActor(campaign_id=self.campaign.id, actor_id='sildar_hallwinter_canonical', name='Sildar', public_summary='Warrior', dossier='{}')
        db.session.add(sildar)
        
        # Seed NPCActor for the new gundren generated ID so persistence passes
        gundren = NPCActor(campaign_id=self.campaign.id, actor_id='gundren_rockseeker', name='Gundren Rockseeker', public_summary='Dwarf', dossier='{}')
        db.session.add(gundren)
        db.session.commit()

        compiled = compile_staged_memory_patch(memory_context, extracted, resolved)
        
        # Verify compilation remapped the invented ID to gundren_rockseeker
        entities = compiled.get('upsert_graph_entities', [])
        self.assertEqual(len(entities), 1)
        self.assertEqual(entities[0]['id'], 'gundren_rockseeker')
        
        npcs = compiled.get('update_npc_actors', [])
        self.assertEqual(len(npcs), 1)
        self.assertEqual(npcs[0]['id'], 'gundren_rockseeker')
        
        rels = compiled.get('upsert_graph_relations', [])
        self.assertEqual(len(rels), 1)
        self.assertEqual(rels[0]['source_id'], 'gundren_rockseeker')
        self.assertEqual(rels[0]['target_id'], 'sildar_hallwinter_canonical')

        # Run persistence to verify it applies fully
        res = apply_memory_patch(self.campaign, self.session, compiled)
        self.assertEqual(len(res['npc_changes']), 1)
        self.assertEqual(res['npc_changes'][0]['npc_actor']['actor_id'], 'gundren_rockseeker')

    def test_npc_relationship_target_validation(self):
        memory_context = {'campaign_id': self.campaign.id}
        extracted = {}
        resolved = {
            'update_npc_actors': [
                {
                    'id': 'gundren_rockseeker',
                    'relationships': {
                        'sildar_hallwinter_canonical': 'Friends',
                        'invented_non_existent_npc': 'Enemies' # Unknown target
                    }
                }
            ]
        }
        
        # Seed gundren and sildar
        gundren = NPCActor(campaign_id=self.campaign.id, actor_id='gundren_rockseeker', name='Gundren', dossier='{}')
        db.session.add(gundren)
        sildar = NPCActor(campaign_id=self.campaign.id, actor_id='sildar_hallwinter_canonical', name='Sildar', dossier='{}')
        db.session.add(sildar)
        db.session.commit()

        compiled = compile_staged_memory_patch(memory_context, extracted, resolved)
        npcs = compiled.get('update_npc_actors', [])
        self.assertEqual(len(npcs), 1)
        # Canonical target should be kept
        self.assertEqual(npcs[0]['relationships'].get('sildar_hallwinter_canonical'), 'Friends')
        # Hallucinated non-existent target should be omitted
        self.assertNotIn('invented_non_existent_npc', npcs[0]['relationships'])
        
        # Verified that the unresolved item was reported
        unresolved = compiled.get('unresolved_items', [])
        self.assertTrue(any(u.get('kind') == 'npc_relationship_target' for u in unresolved))

    def test_noid_entity_by_name_same_patch_resolution(self):
        memory_context = {'campaign_id': self.campaign.id}
        extracted = {}
        resolved = {
            'upsert_graph_entities': [
                {
                    'name': 'Gundren Rockseeker',
                    'type': 'npc'
                }
            ],
            'update_npc_actors': [
                {
                    'actor_ref': 'Gundren Rockseeker',
                    'role': 'Patron'
                }
            ],
            'upsert_graph_relations': [
                {
                    'type': 'allied_with',
                    'source_ref': 'Gundren Rockseeker',
                    'target_ref': 'sildar_hallwinter_canonical',
                    'summary': 'Allied'
                }
            ]
        }

        # Seed canonical sildar entity and NPCActor
        self.world.knowledge_graph = '{"entities":[{"id":"sildar_hallwinter_canonical","type":"npc","name":"Sildar Hallwinter"},{"id":"gundren_rockseeker","type":"npc","name":"Gundren Rockseeker"}],"relations":[],"facts":[]}'
        db.session.add(self.world)
        sildar = NPCActor(campaign_id=self.campaign.id, actor_id='sildar_hallwinter_canonical', name='Sildar', public_summary='Warrior', dossier='{}')
        db.session.add(sildar)

        # Seed NPCActor for the new gundren generated ID so persistence passes
        gundren = NPCActor(campaign_id=self.campaign.id, actor_id='gundren_rockseeker', name='Gundren Rockseeker', public_summary='Dwarf', dossier='{}')
        db.session.add(gundren)
        db.session.commit()

        compiled = compile_staged_memory_patch(memory_context, extracted, resolved)

        # Verify compilation remapped the no-ID entity by name
        entities = compiled.get('upsert_graph_entities', [])
        self.assertEqual(len(entities), 1)
        self.assertEqual(entities[0]['id'], 'gundren_rockseeker')

        npcs = compiled.get('update_npc_actors', [])
        self.assertEqual(len(npcs), 1)
        self.assertEqual(npcs[0]['id'], 'gundren_rockseeker')

        rels = compiled.get('upsert_graph_relations', [])
        self.assertEqual(len(rels), 1)
        self.assertEqual(rels[0]['source_id'], 'gundren_rockseeker')
        self.assertEqual(rels[0]['target_id'], 'sildar_hallwinter_canonical')

        # Run persistence to verify it applies fully
        res = apply_memory_patch(self.campaign, self.session, compiled)
        self.assertEqual(len(res['npc_changes']), 1)
        self.assertEqual(res['npc_changes'][0]['npc_actor']['actor_id'], 'gundren_rockseeker')

    def test_evidence_status_determination(self):
        from services.memory_resolver_schemas import determine_evidence_status, make_evidence_source

        self.assertEqual(
            determine_evidence_status([], is_rule_derived=False),
            'insufficiently_supported'
        )
        self.assertEqual(
            determine_evidence_status([], is_rule_derived=True),
            'supported_by_rules'
        )
        self.assertEqual(
            determine_evidence_status([
                make_evidence_source('prior_durable_memory', 'ent_123')
            ]),
            'supported_by_evidence'
        )
        self.assertEqual(
            determine_evidence_status([
                make_evidence_source('transcript_message', 'msg_1', confidence=0.8)
            ]),
            'supported_by_evidence'
        )
        self.assertEqual(
            determine_evidence_status([
                make_evidence_source('transcript_message', 'msg_1', confidence=0.2)
            ]),
            'insufficiently_supported'
        )

    def test_make_provenance_record(self):
        from services.memory_resolver_schemas import make_provenance_record

        record = make_provenance_record(
            source_player_message_id=42,
            source_dm_message_id=43,
            prior_memory_record_ids=['mem_1', 'mem_2'],
            world_event_ids=['evt_1'],
            clock_trigger_id='clock_guard_spotted',
            tool_name='resolution_registry',
            evidence_sources=[{'source_type': 'transcript_message', 'source_id': 'msg_99'}],
            evidence_status='supported_by_evidence',
            pipeline_stage='applied',
            resolution_confidence=0.9,
            ambiguity_status=False,
            trace_id='tr_abc',
        )
        self.assertEqual(record['source_player_message_id'], 42)
        self.assertEqual(record['source_dm_message_id'], 43)
        self.assertEqual(record['prior_memory_record_ids'], ['mem_1', 'mem_2'])
        self.assertEqual(record['world_event_ids'], ['evt_1'])
        self.assertEqual(record['clock_trigger_id'], 'clock_guard_spotted')
        self.assertEqual(record['tool_name'], 'resolution_registry')
        self.assertEqual(record['evidence_status'], 'supported_by_evidence')
        self.assertEqual(record['pipeline_stage'], 'applied')
        self.assertEqual(record['resolution_confidence'], 0.9)
        self.assertFalse(record['ambiguity_status'])
        self.assertEqual(record['trace_id'], 'tr_abc')

    def test_compiled_patch_includes_enhanced_provenance(self):
        memory_context = {
            'campaign_id': self.campaign.id,
            'source_player_message_id': 100,
            'source_dm_message_id': 200,
            'trace_id': 'trace_test',
        }
        resolved = {
            'upsert_graph_entities': [
                {
                    'id': 'gundren_rockseeker',
                    'name': 'Gundren Rockseeker',
                    'type': 'npc',
                    'summary': 'A dwarf patron.',
                    'intended_visibility': 'party_known',
                    'source_surface': 'visible_transcript',
                    'provenance': {
                        'evidence_sources': [
                            {'source_type': 'transcript_message', 'source_id': 'msg_200'}
                        ],
                        'evidence_status': 'supported_by_evidence',
                        'pipeline_stage': 'resolved',
                        'resolution_confidence': 0.95,
                    },
                }
            ],
            'upsert_graph_facts': [
                {
                    'text': 'The red dragon sleeps.',
                    'evidence': [
                        {'source': 'visible_transcript', 'field': 'DM said the dragon sleeps.'}
                    ],
                }
            ],
            'upsert_graph_relations': [
                {
                    'type': 'allied_with',
                    'source_id': 'gundren_rockseeker',
                    'target_id': 'sildar_hallwinter',
                    'provenance': {
                        'evidence_status': 'insufficiently_supported',
                        'ambiguity_status': True,
                    },
                }
            ],
            'create_clocks': [
                {
                    'id': 'clock_danger',
                    'name': 'Danger Clock',
                    'segments': 4,
                    'provenance': {
                        'evidence_sources': [
                            {'source_type': 'transcript_message', 'source_id': 'msg_200'}
                        ],
                        'evidence_status': 'supported_by_evidence',
                    },
                }
            ],
        }

        self.world.knowledge_graph = '{"entities":[{"id":"gundren_rockseeker","type":"npc","name":"Gundren Rockseeker"},{"id":"sildar_hallwinter","type":"npc","name":"Sildar Hallwinter"}],"relations":[],"facts":[]}'
        gundren = NPCActor(campaign_id=self.campaign.id, actor_id='gundren_rockseeker', name='Gundren Rockseeker', public_summary='Dwarf', dossier='{}')
        sildar = NPCActor(campaign_id=self.campaign.id, actor_id='sildar_hallwinter', name='Sildar Hallwinter', public_summary='Warrior', dossier='{}')
        db.session.add(gundren)
        db.session.add(sildar)
        db.session.commit()

        compiled = compile_staged_memory_patch(memory_context, {}, resolved)

        entities = compiled.get('upsert_graph_entities', [])
        self.assertEqual(len(entities), 1)
        self.assertIn('evidence_status', entities[0]['provenance'])
        self.assertEqual(entities[0]['provenance']['evidence_status'], 'supported_by_evidence')
        self.assertIn('pipeline_stage', entities[0]['provenance'])
        self.assertEqual(entities[0]['provenance']['resolution_confidence'], 0.95)

        facts = compiled.get('upsert_graph_facts', [])
        self.assertEqual(len(facts), 1)
        self.assertIn('provenance', facts[0])
        self.assertIn('evidence_sources', facts[0]['provenance'])

        rels = compiled.get('upsert_graph_relations', [])
        self.assertEqual(len(rels), 1)
        self.assertEqual(rels[0]['provenance']['evidence_status'], 'insufficiently_supported')
        self.assertTrue(rels[0]['provenance']['ambiguity_status'])

        clocks = compiled.get('create_clocks', [])
        self.assertEqual(len(clocks), 1)
        self.assertEqual(clocks[0]['provenance']['evidence_status'], 'supported_by_evidence')
        self.assertIn('evidence_sources', clocks[0]['provenance'])

    def test_memory_log_persistence_stores_provenance(self):
        from models import CampaignMemoryLog, CampaignClock

        patch = {
            'create_clocks': [
                {
                    'id': 'clock_prov_test',
                    'name': 'Provenance Test Clock',
                    'segments': 4,
                    'filled': 1,
                    'visibility': 'party_known',
                    'provenance': {
                        'source_player_message_id': 300,
                        'source_dm_message_id': 400,
                        'tool_name': 'test_tool',
                        'evidence_sources': [
                            {'source_type': 'transcript_message', 'source_id': 'msg_400'}
                        ],
                        'evidence_status': 'supported_by_evidence',
                        'pipeline_stage': 'applied',
                    },
                }
            ],
        }

        apply_memory_patch(self.campaign, self.session, patch)

        logs = CampaignMemoryLog.query.filter_by(
            campaign_id=self.campaign.id,
            target_table='campaign_clocks',
            memory_id='clock_prov_test',
        ).all()
        self.assertGreaterEqual(len(logs), 1)

        log = logs[0]
        self.assertEqual(log.evidence_status, 'supported_by_evidence')
        self.assertIsNotNone(log.provenance_json)
        self.assertEqual(log.provenance_json.get('tool_name'), 'test_tool')
        self.assertEqual(log.provenance_json.get('source_player_message_id'), 300)
        self.assertEqual(log.provenance_json.get('pipeline_stage'), 'applied')
        self.assertIn('evidence_sources', log.provenance_json)

        clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='clock_prov_test').first()
        self.assertIsNotNone(clock)
        self.assertEqual(clock.name, 'Provenance Test Clock')

    def test_clock_advance_records_provenance(self):
        from models import CampaignClock, WorldEvent
        from services.dm_tools import _tool_advance_clock

        clock = CampaignClock(
            campaign_id=self.campaign.id,
            clock_id='clock_advance_test',
            name='Advance Test Clock',
            segments=6,
            filled=0,
            visibility='party_known',
        )
        db.session.add(clock)
        db.session.commit()

        result = _tool_advance_clock(self.campaign, None, {
            'clock_id': 'clock_advance_test',
            'delta': 2,
            'reason': 'Test advance',
            'evidence': ['Transcript evidence'],
        })

        self.assertNotIn('error', result)
        self.assertEqual(result['clock']['filled'], 2)

        event = WorldEvent.query.filter_by(
            campaign_id=self.campaign.id,
            event_type='clock_advanced',
        ).order_by(WorldEvent.id.desc()).first()
        self.assertIsNotNone(event)
        payload = json.loads(event.payload) if event.payload else {}
        self.assertIn('provenance', payload)
        self.assertEqual(payload['provenance']['tool_name'], 'session_dm_advance_clock')
        self.assertIn('evidence_sources', payload['provenance'])
        self.assertEqual(payload['provenance']['delta'], 2)

    def test_clock_create_and_retire_record_provenance(self):
        from models import CampaignClock, WorldEvent
        from services.dm_tools import _create_clock_from_patch, _retire_clock_from_patch

        result = _create_clock_from_patch(self.campaign, {
            'id': 'clock_create_test',
            'name': 'Create Test Clock',
            'segments': 6,
            'filled': 0,
            'visibility': 'party_known',
            'provenance': {
                'tool_name': 'test_create',
                'evidence_status': 'supported_by_evidence',
                'pipeline_stage': 'proposed',
            },
        })
        self.assertNotIn('error', result)
        clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='clock_create_test').first()
        self.assertIsNotNone(clock)

        create_event = WorldEvent.query.filter_by(
            campaign_id=self.campaign.id,
            event_type='clock_created',
        ).order_by(WorldEvent.id.desc()).first()
        self.assertIsNotNone(create_event)
        create_payload = json.loads(create_event.payload) if create_event.payload else {}
        self.assertIn('provenance', create_payload)
        self.assertEqual(create_payload['provenance']['tool_name'], 'test_create')

        retire_result = _retire_clock_from_patch(self.campaign, {
            'clock_id': 'clock_create_test',
            'reason': 'Test retire',
            'provenance': {
                'tool_name': 'test_retire',
                'evidence_status': 'supported_by_evidence',
                'pipeline_stage': 'applied',
            },
        })
        self.assertNotIn('error', retire_result)

        retire_event = WorldEvent.query.filter_by(
            campaign_id=self.campaign.id,
            event_type='clock_retired',
        ).order_by(WorldEvent.id.desc()).first()
        self.assertIsNotNone(retire_event)
        retire_payload = json.loads(retire_event.payload) if retire_event.payload else {}
        self.assertIn('provenance', retire_payload)
        self.assertEqual(retire_payload['provenance']['tool_name'], 'test_retire')

    def test_provenance_survives_roundtrip_in_memory_log(self):
        from models import CampaignMemoryLog

        patch = {
            'update_npc_actors': [
                {
                    'id': 'test_npc_prov',
                    'name': 'Test NPC',
                    'role': 'Test',
                    'provenance': {
                        'source_player_message_id': 888,
                        'tool_name': 'test_resolver',
                        'evidence_sources': [
                            {'source_type': 'prior_memory_record', 'source_id': 'mem_abc'}
                        ],
                        'evidence_status': 'supported_by_evidence',
                        'pipeline_stage': 'applied',
                        'resolution_confidence': 0.85,
                    },
                }
            ],
        }
        result = apply_memory_patch(self.campaign, self.session, patch)
        self.assertEqual(len(result['npc_changes']), 1)

        logs = CampaignMemoryLog.query.filter_by(
            campaign_id=self.campaign.id,
            target_table='npc_actors',
        ).all()
        log = next((log_entry for log_entry in logs if log_entry.memory_id == 'test_npc_prov'), None)
        self.assertIsNotNone(log, 'Memory log should exist for test NPC')
        self.assertEqual(log.evidence_status, 'supported_by_evidence')
        self.assertIsNotNone(log.provenance_json)
        self.assertEqual(log.provenance_json.get('tool_name'), 'test_resolver')
        self.assertEqual(log.provenance_json.get('source_player_message_id'), 888)
        self.assertEqual(log.provenance_json.get('pipeline_stage'), 'applied')
        self.assertEqual(log.provenance_json.get('resolution_confidence'), 0.85)

    def test_identity_resolution_provenance_in_resolution_records(self):
        memory_context = {
            'campaign_id': self.campaign.id,
            'source_player_message_id': 1,
            'source_dm_message_id': 2,
        }
        self.world.knowledge_graph = '{"entities":[{"id":"existing_entity","type":"npc","name":"Existing Entity"}],"relations":[],"facts":[]}'
        db.session.add(self.world)
        existing_npc = NPCActor(campaign_id=self.campaign.id, actor_id='existing_entity', name='Existing Entity', public_summary='Known NPC', dossier='{}')
        db.session.add(existing_npc)
        db.session.commit()

        resolved = {
            'upsert_graph_entities': [
                {
                    'id': 'existing_entity',
                    'name': 'Existing Entity',
                    'type': 'npc',
                    'intended_visibility': 'party_known',
                    'source_surface': 'visible_transcript',
                    'provenance': {
                        'evidence_sources': [
                            {'source_type': 'prior_memory_record', 'source_id': 'existing_entity'}
                        ],
                        'evidence_status': 'supported_by_evidence',
                    },
                }
            ],
        }
        compiled = compile_staged_memory_patch(memory_context, {}, resolved)
        registry_entries = compiled.get('registry', [])
        self.assertTrue(any(
            e.get('evidence') is not None
            for e in registry_entries
        ), 'Registry entries should preserve evidence')

    def test_evidence_status_unsupported_vs_supported(self):
        memory_context = {
            'campaign_id': self.campaign.id,
            'source_player_message_id': 1,
        }
        self.world.knowledge_graph = '{"entities":[{"id":"phandalin","type":"location","name":"Phandalin"}],"relations":[],"facts":[]}'
        db.session.add(self.world)
        db.session.commit()

        resolved_unsupported = {
            'upsert_graph_facts': [
                {
                    'text': 'An unverified claim.',
                    'provenance': {
                        'evidence_basis': [],
                        'evidence_status': 'insufficiently_supported',
                    },
                }
            ],
        }
        compiled_unsupported = compile_staged_memory_patch(memory_context, {}, resolved_unsupported)
        fact_unsupported = compiled_unsupported['upsert_graph_facts'][0]
        self.assertEqual(fact_unsupported['provenance']['evidence_status'], 'insufficiently_supported')

        resolved_supported = {
            'upsert_graph_facts': [
                {
                    'text': 'A verified claim.',
                    'provenance': {
                        'evidence_basis': ['Transcript says verified claim.'],
                        'evidence_status': 'supported_by_evidence',
                    },
                }
            ],
        }
        compiled_supported = compile_staged_memory_patch(memory_context, {}, resolved_supported)
        fact_supported = compiled_supported['upsert_graph_facts'][0]
        self.assertEqual(fact_supported['provenance']['evidence_status'], 'supported_by_evidence')

if __name__ == '__main__':
    unittest.main()

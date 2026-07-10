import os
import sys
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

        # Case B: Unknown location (unresolved)
        extracted_unresolved = {'scene_patch': {'location_name': 'Neverwinter Wood'}}
        resolved_unresolved = {'scene_patch': {'location_name': 'Neverwinter Wood'}}
        compiled_unresolved = compile_staged_memory_patch(memory_context, extracted_unresolved, resolved_unresolved)
        self.assertEqual(compiled_unresolved['unresolved_items'][0]['resolution_mode'], 'unresolved')

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
        memory_context = {'campaign_id': None}
        extracted = {'running_summary': 'No campaign summary'}
        resolved = {}
        compiled = compile_staged_memory_patch(memory_context, extracted, resolved)
        self.assertEqual(compiled['running_summary'], 'No campaign summary')
        self.assertEqual(len(compiled['unresolved_items']), 1)
        self.assertEqual(compiled['unresolved_items'][0]['reason'], 'missing_campaign')

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

if __name__ == '__main__':
    unittest.main()

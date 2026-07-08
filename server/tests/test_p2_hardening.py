import os
import sys
import json
import unittest
from flask import Flask

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from models import (
    db,
    User,
    Campaign,
    CampaignSession,
    CampaignMemoryLog,
    CampaignAuditEvent,
    CampaignMemoryRun,
    CampaignWorld
)
from services.dm_tools import apply_memory_patch, _validate_memory_scene_patch
from services.session_memory_agent import compile_staged_memory_patch
from openrouter import _fallback_session_memory_patch, _compile_telemetry_summary

class P2HardeningTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SECRET_KEY'] = 'test-secret'
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        # Seed basic models
        self.user = User(username='testuser', email='test@example.com', password_hash='hash')
        db.session.add(self.user)
        db.session.flush()

        self.campaign = Campaign(name='P2 Test Campaign', user_id=self.user.id)
        db.session.add(self.campaign)
        db.session.flush()

        self.session = CampaignSession(campaign_id=self.campaign.id, running_summary="Summary")
        db.session.add(self.session)
        db.session.flush()

        # Seed CampaignWorld for resolver context
        self.world = CampaignWorld(
            campaign_id=self.campaign.id,
            public_intro='{}',
            world_state='{}',
            dm_private='{}',
            knowledge_graph=json.dumps({
                'entities': [
                    {'id': 'waterdeep', 'name': 'Waterdeep', 'type': 'location'},
                    {'id': 'neverwinter', 'name': 'Neverwinter', 'type': 'location'}
                ],
                'relations': [],
                'facts': []
            })
        )
        db.session.add(self.world)
        db.session.flush()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_memory_anchors_persistence(self):
        # 1. memory_anchors persists on CampaignSession
        anchors = {
            "current_goal": "Find the lost mine",
            "current_scene": "Inside the tavern",
            "open_clues": ["Map fragment"],
            "unresolved_questions": ["Where is Gundren?"],
            "npc_observations": ["Sildar looks tired"],
            "recent_offers_promises": ["100gp reward"]
        }
        patch = {
            'running_summary': 'Summary updated',
            'memory_anchors': anchors
        }
        apply_memory_patch(self.campaign, self.session, patch)
        db.session.commit()

        session_db = db.session.get(CampaignSession, self.session.id)
        self.assertEqual(session_db.memory_anchors, anchors)

    def test_to_dict_returns_normalized_anchor_shape(self):
        # 2. CampaignSession.to_dict() returns the normalized anchor shape
        # Test case: memory_anchors is None
        self.session.memory_anchors = None
        db.session.commit()
        
        d = self.session.to_dict()
        self.assertIn('memory_anchors', d)
        self.assertEqual(d['memory_anchors']['current_goal'], None)
        self.assertEqual(d['memory_anchors']['open_clues'], [])

        # Test case: memory_anchors is partial
        self.session.memory_anchors = {"current_goal": "Goal"}
        db.session.commit()
        d2 = self.session.to_dict()
        self.assertEqual(d2['memory_anchors']['current_goal'], "Goal")
        self.assertEqual(d2['memory_anchors']['open_clues'], [])

    def test_memory_writer_fallback_preserves_scene_location(self):
        # 3. memory_writer fallback preserves existing scene location
        memory_context = {
            'prior_running_summary': 'Old summary',
            'prior_memory_anchors': {'current_goal': 'Existing Goal'},
            'hot_context': {
                'current_scene': {
                    'location_id': 'waterdeep',
                    'location_name': 'Waterdeep'
                }
            }
        }
        telemetry = {}
        fallback = _fallback_session_memory_patch(memory_context, telemetry)
        self.assertEqual(fallback['scene_patch']['location_id'], 'waterdeep')
        self.assertEqual(fallback['scene_patch']['location_name'], 'Waterdeep')
        self.assertEqual(fallback['memory_anchors']['current_goal'], 'Existing Goal')

    def test_unresolved_scene_location_does_not_overwrite_current_scene(self):
        # 4. unresolved scene-location proposals do not overwrite the current scene
        current_scene = {'location_id': 'waterdeep', 'location_name': 'Waterdeep'}
        # proposed location is unknown (not in CampaignWorld list)
        patch = {'location_id': 'baldurs_gate', 'location_name': 'Baldur\'s Gate'}
        validated, skipped = _validate_memory_scene_patch(self.campaign, current_scene, patch, {})
        
        self.assertNotIn('location_id', validated)
        self.assertNotIn('location_name', validated)
        self.assertEqual(skipped['location_id'], 'baldurs_gate')

    def test_scene_mutation_warnings(self):
        # 5. rejected/unresolved/repaired scene mutations emit warning/audit artifacts
        # We perform apply_memory_patch with unresolved location
        patch = {
            'scene_patch': {
                'location_id': 'baldurs_gate',
                'location_name': 'Baldur\'s Gate'
            }
        }
        # Clear existing logs/events
        CampaignMemoryLog.query.delete()
        CampaignAuditEvent.query.delete()
        db.session.commit()

        apply_memory_patch(self.campaign, self.session, patch)
        db.session.commit()

        # Should log scene_mutation_warning for unresolved location change
        log = CampaignMemoryLog.query.filter_by(operation='warning').first()
        self.assertIsNotNone(log)
        self.assertEqual(log.error, 'scene_location_unresolved')
        self.assertIn('baldurs_gate', log.patch_json['unresolved_items'][0])

        event = CampaignAuditEvent.query.filter_by(event_type='scene_mutation_warning').first()
        self.assertIsNotNone(event)
        payload_data = json.loads(event.payload)
        self.assertEqual(payload_data['warning_type'], 'scene_location_unresolved')

    def test_telemetry_summary_classification(self):
        # 6. telemetry summary correctly classifies provider retry, parse repair, etc.
        # Test Case A: Parser Failure
        telemetry_a = {'error': 'JSONDecodeError: Expecting value'}
        audit_context_a = {'telemetry_tracker': {}}
        summary_a = _compile_telemetry_summary(telemetry_a, audit_context_a)
        self.assertEqual(summary_a['status'], 'parser_failure')
        self.assertEqual(summary_a['failure_category'], 'parser')

        # Test Case B: Success with retries and repairs
        telemetry_b = {}
        audit_context_b = {
            'telemetry_tracker': {
                'provider_retries': 2,
                'parse_repairs': 1,
                'guard_retries': 0,
                'fallback_active': False
            }
        }
        summary_b = _compile_telemetry_summary(telemetry_b, audit_context_b)
        self.assertEqual(summary_b['status'], 'success')
        self.assertEqual(summary_b['provider_retries'], 2)
        self.assertEqual(summary_b['parse_repairs'], 1)

        # Test Case C: Partial Fallback
        telemetry_c = {'fallback_active': True}
        audit_context_c = {'telemetry_tracker': {'fallback_active': True}}
        summary_c = _compile_telemetry_summary(telemetry_c, audit_context_c)
        self.assertEqual(summary_c['status'], 'partial_fallback')
        self.assertEqual(summary_c['fallback_active'], True)

    def test_substance_check_includes_anchors(self):
        from openrouter import _session_memory_patch_has_substance
        # Patch with empty anchors has no substance
        self.assertFalse(_session_memory_patch_has_substance({'memory_anchors': {}}))
        self.assertFalse(_session_memory_patch_has_substance({'memory_anchors': {'current_goal': None}}))
        # Patch with non-empty anchors has substance
        self.assertTrue(_session_memory_patch_has_substance({'memory_anchors': {'current_goal': 'Find the lost mine'}}))

    def test_staged_unresolved_scene_location_emits_warning(self):
        patch = {
            'unresolved_items': [{
                'kind': 'scene_location',
                'location_id': 'baldurs_gate',
                'location_name': 'Baldur\'s Gate',
                'reason': 'unresolved_scene_location'
            }]
        }
        # Clear existing logs/events
        CampaignMemoryLog.query.delete()
        CampaignAuditEvent.query.delete()
        db.session.commit()

        apply_memory_patch(self.campaign, self.session, patch)
        db.session.commit()

        # Should log scene_mutation_warning for staged unresolved location change
        log = CampaignMemoryLog.query.filter_by(operation='warning').first()
        self.assertIsNotNone(log)
        self.assertEqual(log.error, 'scene_location_unresolved')
        self.assertIn('baldurs_gate', log.patch_json['unresolved_items'][0])

        event = CampaignAuditEvent.query.filter_by(event_type='scene_mutation_warning').first()
        self.assertIsNotNone(event)
        payload_data = json.loads(event.payload)
        self.assertEqual(payload_data['warning_type'], 'scene_location_unresolved')

    def test_compile_telemetry_summary_classifies_empty_patch_as_model_failure(self):
        telemetry = {'error': 'empty_patch'}
        audit_context = {'telemetry_tracker': {}}
        summary = _compile_telemetry_summary(telemetry, audit_context)
        self.assertEqual(summary['status'], 'model_output_failure')
        self.assertEqual(summary['failure_category'], 'model')

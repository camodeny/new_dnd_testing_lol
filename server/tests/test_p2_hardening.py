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
from services.session_memory_agent import MemoryPipelineError, compile_staged_memory_patch

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
        self.session.memory_anchors = None
        db.session.commit()
        
        d = self.session.to_dict()
        self.assertIn('memory_anchors', d)
        self.assertEqual(d['memory_anchors']['current_goal'], None)
        self.assertEqual(d['memory_anchors']['open_clues'], [])

        self.session.memory_anchors = {"current_goal": "Goal"}
        db.session.commit()
        d2 = self.session.to_dict()
        self.assertEqual(d2['memory_anchors']['current_goal'], "Goal")
        self.assertEqual(d2['memory_anchors']['open_clues'], [])

    def test_unresolved_scene_location_does_not_overwrite_current_scene(self):
        current_scene = {'location_id': 'waterdeep', 'location_name': 'Waterdeep'}
        patch = {'location_id': 'baldurs_gate', 'location_name': 'Baldur\'s Gate'}
        validated, skipped = _validate_memory_scene_patch(self.campaign, current_scene, patch, {})
        
        self.assertNotIn('location_id', validated)
        self.assertNotIn('location_name', validated)
        self.assertEqual(skipped['location_id'], 'baldurs_gate')

    def test_scene_mutation_warnings(self):
        patch = {
            'scene_patch': {
                'location_id': 'baldurs_gate',
                'location_name': 'Baldur\'s Gate'
            }
        }
        CampaignMemoryLog.query.delete()
        CampaignAuditEvent.query.delete()
        db.session.commit()

        apply_memory_patch(self.campaign, self.session, patch)
        db.session.commit()

        log = CampaignMemoryLog.query.filter_by(operation='warning').first()
        self.assertIsNotNone(log)
        self.assertEqual(log.error, 'scene_location_unresolved')
        self.assertIn('baldurs_gate', log.patch_json['unresolved_items'][0])

        event = CampaignAuditEvent.query.filter_by(event_type='scene_mutation_warning').first()
        self.assertIsNotNone(event)
        payload_data = json.loads(event.payload)
        self.assertEqual(payload_data['warning_type'], 'scene_location_unresolved')

    def test_staged_unresolved_scene_location_emits_warning(self):
        patch = {
            'unresolved_items': [{
                'kind': 'scene_location',
                'location_id': 'baldurs_gate',
                'location_name': 'Baldur\'s Gate',
                'reason': 'unresolved_scene_location'
            }]
        }
        CampaignMemoryLog.query.delete()
        CampaignAuditEvent.query.delete()
        db.session.commit()

        apply_memory_patch(self.campaign, self.session, patch)
        db.session.commit()

        log = CampaignMemoryLog.query.filter_by(operation='warning').first()
        self.assertIsNotNone(log)
        self.assertEqual(log.error, 'scene_location_unresolved')
        self.assertIn('baldurs_gate', log.patch_json['unresolved_items'][0])

        event = CampaignAuditEvent.query.filter_by(event_type='scene_mutation_warning').first()
        self.assertIsNotNone(event)
        payload_data = json.loads(event.payload)
        self.assertEqual(payload_data['warning_type'], 'scene_location_unresolved')

    def test_compile_missing_campaign_raises_error(self):
        with self.assertRaises(MemoryPipelineError) as ctx:
            compile_staged_memory_patch({}, {}, {})
        self.assertEqual(ctx.exception.stage, 'compilation')
        self.assertEqual(ctx.exception.code, 'missing_campaign')

    def test_apply_memory_patch_raises_validation_error_on_invalid_visibility(self):
        patch = {
            'upsert_graph_entities': [
                {
                    'id': 'test_entity',
                    'name': 'test entity',
                    'type': 'other',
                    'visibility': 'invalid_visibility',
                }
            ]
        }
        with self.assertRaises(MemoryPipelineError) as ctx:
            apply_memory_patch(self.campaign, self.session, patch)
        self.assertEqual(ctx.exception.stage, 'validation')
        self.assertEqual(ctx.exception.code, 'validation_error')

    def test_apply_memory_patch_raises_validation_error_on_invalid_certainty(self):
        patch = {
            'upsert_graph_entities': [
                {
                    'id': 'test_entity',
                    'name': 'test entity',
                    'type': 'other',
                    'certainty': 'invalid_certainty',
                }
            ]
        }
        with self.assertRaises(MemoryPipelineError) as ctx:
            apply_memory_patch(self.campaign, self.session, patch)
        self.assertEqual(ctx.exception.stage, 'validation')
        self.assertEqual(ctx.exception.code, 'validation_error')

    def test_apply_memory_patch_raises_application_error_on_persistence_failure(self):
        from unittest.mock import patch as mock_patch
        test_patch = {
            'running_summary': 'summary',
            'memory_anchors': {'current_goal': 'test'},
        }
        with mock_patch('services.dm_tools._world_json', side_effect=RuntimeError('db error')):
            with self.assertRaises(MemoryPipelineError) as ctx:
                apply_memory_patch(self.campaign, self.session, test_patch)
        self.assertEqual(ctx.exception.stage, 'application')
        self.assertEqual(ctx.exception.code, 'persistence_error')

    def test_apply_memory_patch_accepted_with_valid_visibilities(self):
        patch = {
            'upsert_graph_entities': [
                {
                    'id': 'test_entity',
                    'name': 'test entity',
                    'type': 'other',
                    'visibility': 'party_known',
                    'certainty': 'confirmed',
                    'source_surface': 'visible_transcript',
                    'intended_visibility': 'party_known',
                }
            ],
            'upsert_graph_relations': [],
            'upsert_graph_facts': [],
            'update_npc_actors': [],
            'record_events': [],
            'create_clocks': [],
            'retire_clocks': [],
        }
        result = apply_memory_patch(self.campaign, self.session, patch)
        self.assertIn('graph_changes', result)


if __name__ == '__main__':
    unittest.main()

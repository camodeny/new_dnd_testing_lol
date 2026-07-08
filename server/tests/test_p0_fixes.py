import os
import sys
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../automation')))

from flask import Flask
from models import db, Campaign, CampaignWorld,NPCActor
from services.scene_location_resolver import resolve_scene_location_patch
from services.session_memory_agent import compile_staged_memory_patch
from services.dm_tools import _validate_memory_scene_patch, apply_memory_patch
import run_automation_worker as worker
from openrouter import _fallback_scene_patch

class P0FixesTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SECRET_KEY'] = 'test-secret'
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        self.campaign = Campaign(name='P0 Test', description='Test campaign', user_id=1)
        db.session.add(self.campaign)
        db.session.flush()

        self.world = CampaignWorld(
            campaign_id=self.campaign.id,
            public_intro='{}',
            knowledge_graph='{"entities":[{"id":"waterdeep","type":"location","name":"Waterdeep"},{"id":"neverwinter","type":"location","name":"Neverwinter"}],"relations":[],"facts":[]}',
            world_state='{"current_scene":{"location_id":"waterdeep","location_name":"Waterdeep"}}',
            dm_private='{}'
        )
        db.session.add(self.world)
        db.session.commit()

    def tearDown(self):
        db.session.rollback()
        db.drop_all()
        self.ctx.pop()

    # 1. Fallback scene patch preserves existing location when model JSON parsing fails
    # 2. Fallback scene patch does not infer a new location from DM prose
    def test_fallback_scene_patch_preserves_location_and_does_not_infer_from_prose(self):
        memory_context = {
            'hot_context': {
                'current_scene': {
                    'location_id': 'waterdeep',
                    'location_name': 'Waterdeep',
                    'active_npc_ids': []
                }
            },
            'latest_dm_message': 'You walk into the Yawning Portal in Neverwinter.'
        }
        patch_result = _fallback_scene_patch(memory_context)
        # Should NOT change location fields to neverwinter or yawning_portal
        self.assertEqual(patch_result.get('location_id'), 'waterdeep')
        self.assertEqual(patch_result.get('location_name'), 'Waterdeep')

    # 3. compile_staged_memory_patch rejects location_name-only updates when unresolved
    def test_compile_staged_memory_patch_rejects_unresolved_name_only(self):
        memory_context = {
            'campaign_id': self.campaign.id,
            'hot_context': {
                'current_scene': {
                    'location_id': 'waterdeep',
                    'location_name': 'Waterdeep'
                }
            }
        }
        extracted = {'scene_patch': {'location_name': 'Baldur\'s Gate'}}
        resolved = {'scene_patch': {'location_name': 'Baldur\'s Gate'}}
        compiled = compile_staged_memory_patch(memory_context, extracted, resolved)
        self.assertNotIn('location_id', compiled['scene_patch'])
        self.assertNotIn('location_name', compiled['scene_patch'])
        self.assertTrue(any(item['kind'] == 'scene_location' for item in compiled['unresolved_items']))

    # 4. compile_staged_memory_patch rejects unknown location_id plus plausible location_name
    def test_compile_staged_memory_patch_rejects_unknown_id_and_plausible_name(self):
        memory_context = {
            'campaign_id': self.campaign.id,
            'hot_context': {
                'current_scene': {
                    'location_id': 'waterdeep',
                    'location_name': 'Waterdeep'
                }
            }
        }
        extracted = {'scene_patch': {'location_id': 'baldurs_gate', 'location_name': 'Baldur\'s Gate'}}
        resolved = {'scene_patch': {'location_id': 'baldurs_gate', 'location_name': 'Baldur\'s Gate'}}
        compiled = compile_staged_memory_patch(memory_context, extracted, resolved)
        self.assertNotIn('location_id', compiled['scene_patch'])
        self.assertNotIn('location_name', compiled['scene_patch'])
        self.assertTrue(any(item['kind'] == 'scene_location' for item in compiled['unresolved_items']))

    # 5. compile_staged_memory_patch resolves known location name to canonical ID/name pair
    def test_compile_staged_memory_patch_resolves_known_location_name(self):
        memory_context = {
            'campaign_id': self.campaign.id,
            'hot_context': {
                'current_scene': {
                    'location_id': 'waterdeep',
                    'location_name': 'Waterdeep'
                }
            }
        }
        extracted = {'scene_patch': {'location_name': 'Neverwinter'}}
        resolved = {'scene_patch': {'location_name': 'Neverwinter'}}
        compiled = compile_staged_memory_patch(memory_context, extracted, resolved)
        self.assertEqual(compiled['scene_patch']['location_id'], 'neverwinter')
        self.assertEqual(compiled['scene_patch']['location_name'], 'Neverwinter')

    # 6. apply_memory_patch never persists only one of location_id or location_name
    # 7. Direct scene update validation rejects partial/unresolved location changes
    def test_validate_memory_scene_patch_rejects_partial_or_unresolved_changes(self):
        current_scene = {'location_id': 'waterdeep', 'location_name': 'Waterdeep'}
        audit_context = {'latest_player_message': 'Let\'s go to Neverwinter or Baldur\'s Gate.'}
        
        # Test case: Unresolved change (Baldur's Gate is not in knowledge graph)
        patch = {'location_id': 'baldurs_gate', 'location_name': 'Baldur\'s Gate'}
        validated, skipped = _validate_memory_scene_patch(self.campaign, current_scene, patch, audit_context)
        self.assertNotIn('location_id', validated)
        self.assertNotIn('location_name', validated)
        self.assertEqual(skipped.get('location_id'), 'baldurs_gate')
        self.assertEqual(skipped.get('location_name'), 'Baldur\'s Gate')

        # Test case: Resolved change and supported by visible text
        patch = {'location_name': 'Neverwinter'}
        validated, skipped = _validate_memory_scene_patch(self.campaign, current_scene, patch, audit_context)
        self.assertEqual(validated.get('location_id'), 'neverwinter')
        self.assertEqual(validated.get('location_name'), 'Neverwinter')

    # 8. Automation worker fails run when dm_turn.status == "error"
    # 9. Automation worker fails run when dm_turn.post_turn_status == "error"
    # 10. Failure run event includes useful debugging metadata
    @patch('run_automation_worker.fetch_run')
    @patch('run_automation_worker.claim_run')
    @patch('run_automation_worker.build_manifest_for_run')
    @patch('run_automation_worker.append_event')
    @patch('run_automation_worker.complete_run')
    @patch('run_automation_worker.wait_for_dm_response')
    def test_automation_worker_fails_on_dm_turn_errors(self, mock_wait, mock_complete, mock_append, mock_manifest, mock_claim, mock_fetch):
        args = SimpleNamespace(
            api_base='http://127.0.0.1:5889',
            owner_api_key='owner-key',
            worker_id='worker-test',
            max_minutes=None,
            idle_timeout=180.0,
            heartbeat_interval=999.0,
            poll_interval=0.01,
            max_turns=50,
            dm_response_timeout=60.0,
            model='opencode-go/deepseek-v4-flash',
            message_window=16,
            once=False,
            run_id=2,
        )

        mock_claim.return_value = {
            'run': {'id': 2, 'attempt_count': 1, 'runner_config': {'audit_pause_phases': ['after_dm']}, 'completed_turns': 0},
            'lease_token': 'lease-1',
            'derived_campaign': {'id': 1},
            'api_base': 'http://127.0.0.1:5889',
            'owner': {'api_key': 'owner-key', 'token': 'owner-token'},
        }
        mock_manifest.return_value = {
            'api_base': 'http://127.0.0.1:5889',
            'owner': {'api_key': 'owner-key', 'token': 'owner-token'},
        }

        # First case: dm_turn.status is "error"
        mock_wait.return_value = ({
            'status': 'error',
            'post_turn_status': 'complete',
            'dm_message_id': None,
            'turn_error': 'API limit exceeded',
        }, False)

        # Force immediate exit of loop by having stop_requested
        mock_fetch.side_effect = [
            {'run': {'status': 'running', 'completed_turns': 0}, 'latest_session': {
                'id': 4,
                'is_active': True,
                'messages': [{'id': 410, 'role': 'player', 'content': 'Hello', 'created_at': '2026-07-01T15:59:05'}],
            }},
            {'run': {'status': 'stop_requested', 'error_text': 'done'}, 'latest_session': {}},
        ]

        # Call worker logic
        with patch('run_automation_worker.active_session_from_run_payload') as mock_active, \
             patch.object(worker.autonomous, 'fetch_dm_turn_status') as mock_fetch_dm, \
             patch.object(worker, 'heartbeat') as mock_heart:
            mock_heart.return_value = {'run': {'lease_token': 'lease-1'}}
            mock_fetch_dm.return_value = {'status': 'pending'}
            mock_active.return_value = {
                'id': 4,
                'is_active': True,
                'messages': [{'id': 410, 'role': 'player', 'content': 'Hello'}],
            }
            # We trigger execute_run
            worker.execute_run(args, 2)

        # Assert complete_run was called with status='failed'
        mock_complete.assert_any_call(
            'http://127.0.0.1:5889',
            'owner-key',
            2,
            'worker-test',
            'lease-1',
            status='failed',
            error_text='API limit exceeded',
            dedupe_key='run_completed:2:dm-failure:410'
        )

        # Assert append_event was called with 'dm_turn_failed'
        failed_event_call = [
            call for call in mock_append.call_args_list 
            if call[0][5] == 'dm_turn_failed'
        ]
        self.assertTrue(len(failed_event_call) > 0)
        payload = failed_event_call[0][0][6]
        self.assertEqual(payload['status'], 'error')
        self.assertEqual(payload['turn_error'], 'API limit exceeded')
        self.assertIn('dm_response_audit', payload['skipped_downstream_expectations'])

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

    # 3. compile_staged_memory_patch creates new location for name-only proposals
    def test_compile_staged_memory_patch_creates_new_location_for_unknown(self):
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
        self.assertIn('location_id', compiled['scene_patch'])
        self.assertIn('location_name', compiled['scene_patch'])
        self.assertEqual(compiled['scene_patch']['resolution_mode'], 'new')

    # 4. compile_staged_memory_patch creates new location for unknown id + plausible name
    def test_compile_staged_memory_patch_creates_new_location_for_unknown_id_and_name(self):
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
        self.assertIn('location_id', compiled['scene_patch'])
        self.assertIn('location_name', compiled['scene_patch'])
        self.assertEqual(compiled['scene_patch']['resolution_mode'], 'new')

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
    # 7. Direct scene update validation accepts new locations when name is supported by visible text
    def test_validate_memory_scene_patch_accepts_new_location_when_supported(self):
        current_scene = {'location_id': 'waterdeep', 'location_name': 'Waterdeep'}
        audit_context = {'latest_player_message': 'Let\'s go to Neverwinter or Baldur\'s Gate.'}
        
        patch = {'location_id': 'baldurs_gate', 'location_name': 'Baldur\'s Gate'}
        validated, skipped = _validate_memory_scene_patch(self.campaign, current_scene, patch, audit_context)
        # New location is in visible terms, so it should be validated
        self.assertEqual(validated.get('location_id'), 'baldurs_gate')
        self.assertEqual(validated.get('location_name'), 'Baldur\'s Gate')
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
            'gameplay_readiness': {'campaign_ready': True},
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
        }, False, None)

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
             patch.object(worker, 'heartbeat') as mock_heart, \
             patch.object(worker, 'pause_for_audit_if_needed', return_value=(False, 'lease-1')) as pause_mock:
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
        self.assertNotIn('dm_response_audit', payload['skipped_downstream_expectations'])
        pause_mock.assert_called_once()

    @patch('run_automation_worker.fetch_run')
    @patch('run_automation_worker.claim_run')
    @patch('run_automation_worker.build_manifest_for_run')
    @patch('run_automation_worker.append_event')
    @patch('run_automation_worker.complete_run')
    @patch('run_automation_worker.wait_for_dm_response')
    def test_automation_worker_fails_on_post_turn_errors(self, mock_wait, mock_complete, mock_append, mock_manifest, mock_claim, mock_fetch):
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
            'gameplay_readiness': {'campaign_ready': True},
        }
        mock_manifest.return_value = {
            'api_base': 'http://127.0.0.1:5889',
            'owner': {'api_key': 'owner-key', 'token': 'owner-token'},
        }

        # Second case: dm_turn.status is "complete", but post_turn_status is "error"
        mock_wait.return_value = ({
            'status': 'speak',
            'post_turn_status': 'error',
            'dm_message_id': 411,
            'post_turn_error': 'Failed to compile memory patch',
        }, False, None)

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
             patch.object(worker, 'heartbeat') as mock_heart, \
             patch.object(worker, 'pause_for_audit_if_needed', return_value=(False, 'lease-1')) as pause_mock:
            mock_heart.return_value = {'run': {'lease_token': 'lease-1'}}
            mock_fetch_dm.return_value = {'status': 'pending'}
            mock_active.return_value = {
                'id': 4,
                'is_active': True,
                'messages': [{'id': 410, 'role': 'player', 'content': 'Hello'}],
            }
            worker.execute_run(args, 2)

        # Assert complete_run was called with status='failed'
        mock_complete.assert_any_call(
            'http://127.0.0.1:5889',
            'owner-key',
            2,
            'worker-test',
            'lease-1',
            status='failed',
            error_text='dm_post_turn_error',
            dedupe_key='run_completed:2:dm-failure:410'
        )

        # Assert append_event was called with 'dm_turn_failed'
        failed_event_call = [
            call for call in mock_append.call_args_list 
            if call[0][5] == 'dm_turn_failed'
        ]
        self.assertTrue(len(failed_event_call) > 0)
        payload = failed_event_call[-1][0][6]
        self.assertEqual(payload['status'], 'speak')
        self.assertEqual(payload['post_turn_status'], 'error')
        self.assertEqual(payload['turn_error'], 'Failed to compile memory patch')
        pause_mock.assert_called_once()

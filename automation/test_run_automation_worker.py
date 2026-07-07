import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch


sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
import run_automation_worker as worker


class RunAutomationWorkerTests(unittest.TestCase):
    def test_parse_args_defaults_to_no_wall_clock_cap(self):
        with patch.object(sys, 'argv', ['run_automation_worker.py']):
            args = worker.parse_args()

        self.assertIsNone(args.max_minutes)

    def test_parse_args_defaults_dm_response_timeout_to_five_minutes(self):
        with patch.object(sys, 'argv', ['run_automation_worker.py']):
            args = worker.parse_args()

        self.assertEqual(args.dm_response_timeout, 300.0)

    def test_wait_for_dm_response_waits_for_post_turn_completion(self):
        args = SimpleNamespace(poll_interval=0.01, dm_response_timeout=5.0)
        manifest = {'session': {'id': 4}}
        statuses = [
            {
                'status': 'speak',
                'player_message_id': 411,
                'dm_message_id': 412,
                'post_turn_complete': False,
                'post_turn_status': 'pending',
            },
            {
                'status': 'speak',
                'player_message_id': 411,
                'dm_message_id': 412,
                'post_turn_complete': True,
                'post_turn_status': 'complete',
            },
        ]

        with patch.object(worker.autonomous, 'fetch_dm_turn_status', side_effect=statuses) as fetch_status, \
                patch.object(worker.time, 'sleep') as sleep:
            result, timed_out = worker.wait_for_dm_response(args, manifest, 411, lambda: None)

        self.assertFalse(timed_out)
        self.assertEqual(result, statuses[-1])
        self.assertEqual(fetch_status.call_count, 2)
        sleep.assert_called_once()

    def test_execute_run_resets_idle_timer_after_after_dm_audit_resume(self):
        args = SimpleNamespace(
            api_base='http://127.0.0.1:5889',
            owner_api_key='owner-key',
            worker_id='audit-worker-test',
            max_minutes=None,
            idle_timeout=180.0,
            heartbeat_interval=999.0,
            poll_interval=0.01,
            max_turns=50,
            dm_response_timeout=60.0,
            model='opencode-go/deepseek-v4-flash',
        )
        claim_payload = {
            'run': {'id': 2, 'attempt_count': 1, 'runner_config': {'audit_pause_phases': ['after_dm']}},
            'lease_token': 'lease-1',
            'derived_campaign': {'id': 100003},
            'roster': [
                {
                    'llm_player_id': 33,
                    'user_id': 35,
                    'label': 'Audit Player 3',
                    'character_name': 'Kaelen Shadowstep',
                    'derived_character_id': 38,
                },
            ],
        }
        initial_session = {
            'id': 4,
            'is_active': True,
            'started_at': '2026-07-01T15:58:50.044971',
            'messages': [
                {'id': 410, 'role': 'dm', 'content': 'Opening scene.', 'created_at': '2026-07-01T15:59:05.608343'},
            ],
        }
        stop_requested_run = {
            'run': {'status': 'stop_requested', 'error_text': 'done'},
            'latest_session': initial_session,
        }
        monotonic_values = iter([
            0.0,    # start_time
            0.0,    # initial last_change_at
            0.0,    # maybe_heartbeat loop 1
            0.0,    # deadline loop 1
            0.0,    # fingerprint_changed activity reset
            0.0,    # pre-after_dm pause activity reset
            500.0,  # post-after_dm audit resume activity reset
            500.1,  # idle timeout check after resume
            500.2,  # maybe_heartbeat loop 2
            500.2,  # deadline loop 2
            500.2,
            500.2,
        ])

        def fake_monotonic():
            return next(monotonic_values, 500.2)

        with patch.object(worker, 'claim_run', return_value=claim_payload), \
                patch.object(worker, 'build_manifest_for_run', return_value={'campaign': {'id': 100003}}), \
                patch.object(worker, 'append_event'), \
                patch.object(worker, 'request_overseer_decision', return_value={'action': 'choose_player', 'llm_player_id': 33}), \
                patch.object(worker, 'api_get', side_effect=[{'world': {}}, {'campaign': {'id': 100003}}]), \
                patch.object(worker, 'fetch_campaign_characters', return_value=[{'id': 38, 'name': 'Kaelen Shadowstep'}]), \
                patch.object(worker, 'request_player_decision', return_value=(
                    {'action': 'speak', 'content': 'Kaelen asks about the patrols.'},
                    '{"action":"speak"}',
                    0,
                    'opencode_go',
                    'deepseek-v4-flash',
                )), \
                patch.object(worker, 'submit_decision', return_value={'message': {'id': 411}}), \
                patch.object(worker, 'wait_for_dm_response', return_value=(
                    {'status': 'speak', 'player_message_id': 411, 'dm_message_id': 412},
                    False,
                )), \
                patch.object(worker, 'pause_for_audit_if_needed', side_effect=[(False, 'lease-1'), (False, 'lease-1')]), \
                patch.object(worker, 'fetch_run', side_effect=[
                    {'run': {'status': 'running'}, 'latest_session': initial_session},
                    stop_requested_run,
                ]), \
                patch.object(worker, 'complete_run') as complete_run, \
                patch.object(worker.time, 'sleep'), \
                patch.object(worker.time, 'monotonic', side_effect=fake_monotonic):
            finished = worker.execute_run(args, 2)

        self.assertTrue(finished)
        complete_run.assert_called_once()
        self.assertEqual(complete_run.call_args.kwargs['error_text'], 'done')
        self.assertNotEqual(complete_run.call_args.kwargs['error_text'], 'idle_timeout')

    def test_resolved_dm_on_startup_no_after_dm_cycle(self):
        args = SimpleNamespace(
            api_base='http://127.0.0.1:5889',
            owner_api_key='owner-key',
            worker_id='test-worker',
            max_minutes=None,
            idle_timeout=180.0,
            heartbeat_interval=999.0,
            poll_interval=0.01,
            max_turns=50,
            dm_response_timeout=60.0,
            message_window=16,
            model='test-model',
        )
        initial_session = {
            'id': 4,
            'is_active': True,
            'messages': [
                {'id': 410, 'role': 'player', 'content': 'Hello.'},
                {'id': 411, 'role': 'dm', 'content': 'DM Response.'},
            ],
        }
        claim_payload = {
            'run': {'id': 2, 'attempt_count': 1, 'runner_config': {'audit_pause_phases': ['after_dm']}},
            'lease_token': 'lease-1',
            'derived_campaign': {'id': 100003},
            'latest_session': initial_session,
            'roster': [],
        }
        run_payload_with_empty_cycles = {
            'run': {'status': 'running'},
            'latest_session': initial_session,
            'audit_cycles': []
        }
        dm_status = {
            'status': 'speak',
            'player_message_id': 410,
            'dm_message_id': 411,
            'post_turn_complete': True,
            'post_turn_status': 'complete',
        }

        with patch.object(worker, 'claim_run', return_value=claim_payload), \
             patch.object(worker, 'build_manifest_for_run', return_value={}), \
             patch.object(worker, 'append_event'), \
             patch.object(worker, 'fetch_run', return_value=run_payload_with_empty_cycles), \
             patch.object(worker.autonomous, 'fetch_dm_turn_status', return_value=dm_status), \
             patch.object(worker, 'wait_for_dm_response', return_value=(dm_status, False)), \
             patch.object(worker, 'heartbeat', return_value={'run': {'lease_token': 'lease-1'}}), \
             patch.object(worker, 'complete_run') as complete_mock, \
             patch.object(worker, 'pause_for_audit_if_needed', return_value=(True, 'lease-1')) as pause_mock:
            
            finished = worker.execute_run(args, 2)

        self.assertTrue(finished)
        pause_mock.assert_called_once()
        self.assertEqual(pause_mock.call_args[0][4], 'after_dm')
        complete_mock.assert_not_called()

    def test_resolved_dm_on_startup_with_existing_after_dm_cycle(self):
        args = SimpleNamespace(
            api_base='http://127.0.0.1:5889',
            owner_api_key='owner-key',
            worker_id='test-worker',
            max_minutes=None,
            idle_timeout=180.0,
            heartbeat_interval=999.0,
            poll_interval=0.01,
            max_turns=1,
            dm_response_timeout=60.0,
            message_window=16,
            model='test-model',
        )
        initial_session = {
            'id': 4,
            'is_active': True,
            'messages': [
                {'id': 410, 'role': 'player', 'content': 'Hello.'},
                {'id': 411, 'role': 'dm', 'content': 'DM Response.'},
            ],
        }
        claim_payload = {
            'run': {'id': 2, 'attempt_count': 1, 'completed_turns': 1, 'runner_config': {'audit_pause_phases': ['after_dm']}},
            'lease_token': 'lease-1',
            'derived_campaign': {'id': 100003},
            'latest_session': initial_session,
            'roster': [],
        }
        run_payload_with_existing_cycle = {
            'run': {'status': 'running', 'completed_turns': 1},
            'latest_session': initial_session,
            'audit_cycles': [
                {'phase': 'after_dm', 'player_message_id': 410, 'dm_message_id': 411}
            ]
        }
        dm_status = {
            'status': 'speak',
            'player_message_id': 410,
            'dm_message_id': 411,
            'post_turn_complete': True,
            'post_turn_status': 'complete',
        }

        with patch.object(worker, 'claim_run', return_value=claim_payload), \
             patch.object(worker, 'build_manifest_for_run', return_value={}), \
             patch.object(worker, 'append_event'), \
             patch.object(worker, 'fetch_run', return_value=run_payload_with_existing_cycle), \
             patch.object(worker.autonomous, 'fetch_dm_turn_status', return_value=dm_status), \
             patch.object(worker, 'pause_for_audit_if_needed') as pause_mock, \
             patch.object(worker, 'request_overseer_decision') as overseer_mock, \
             patch.object(worker, 'heartbeat', return_value={'run': {'lease_token': 'lease-1'}}), \
             patch.object(worker, 'complete_run') as complete_mock:
            
            finished = worker.execute_run(args, 2)

        self.assertTrue(finished)
        pause_mock.assert_not_called()
        overseer_mock.assert_not_called()
        complete_mock.assert_called_once()
        self.assertEqual(complete_mock.call_args.kwargs['status'], 'completed')

    def test_max_cycles_missing_after_dm(self):
        args = SimpleNamespace(
            api_base='http://127.0.0.1:5889',
            owner_api_key='owner-key',
            worker_id='test-worker',
            max_minutes=None,
            idle_timeout=180.0,
            heartbeat_interval=999.0,
            poll_interval=0.01,
            max_turns=1,
            dm_response_timeout=60.0,
            message_window=16,
            model='test-model',
        )
        initial_session = {
            'id': 4,
            'is_active': True,
            'messages': [
                {'id': 410, 'role': 'player', 'content': 'Hello.'},
                {'id': 411, 'role': 'dm', 'content': 'DM Response.'},
            ],
        }
        claim_payload = {
            'run': {'id': 2, 'attempt_count': 1, 'completed_turns': 1, 'runner_config': {'audit_pause_phases': ['after_dm']}},
            'lease_token': 'lease-1',
            'derived_campaign': {'id': 100003},
            'latest_session': initial_session,
            'roster': [],
        }
        run_payload_with_empty_cycles = {
            'run': {'status': 'running', 'completed_turns': 1},
            'latest_session': initial_session,
            'audit_cycles': []
        }
        dm_status = {
            'status': 'speak',
            'player_message_id': 410,
            'dm_message_id': 411,
            'post_turn_complete': True,
            'post_turn_status': 'complete',
        }

        with patch.object(worker, 'claim_run', return_value=claim_payload), \
             patch.object(worker, 'build_manifest_for_run', return_value={}), \
             patch.object(worker, 'append_event'), \
             patch.object(worker, 'fetch_run', return_value=run_payload_with_empty_cycles), \
             patch.object(worker.autonomous, 'fetch_dm_turn_status', return_value=dm_status), \
             patch.object(worker, 'wait_for_dm_response', return_value=(dm_status, False)), \
             patch.object(worker, 'heartbeat', return_value={'run': {'lease_token': 'lease-1'}}), \
             patch.object(worker, 'pause_for_audit_if_needed', return_value=(True, 'lease-1')) as pause_mock, \
             patch.object(worker, 'complete_run') as complete_mock:
            
            finished = worker.execute_run(args, 2)

        self.assertTrue(finished)
        pause_mock.assert_called_once()
        self.assertEqual(pause_mock.call_args[0][4], 'after_dm')
        complete_mock.assert_not_called()

    def test_silent_empty_dm_matching_by_player_message_id(self):
        args = SimpleNamespace(
            api_base='http://127.0.0.1:5889',
            owner_api_key='owner-key',
            worker_id='test-worker',
            max_minutes=None,
            idle_timeout=180.0,
            heartbeat_interval=999.0,
            poll_interval=0.01,
            max_turns=50,
            dm_response_timeout=60.0,
            message_window=16,
            model='test-model',
        )
        initial_session = {
            'id': 4,
            'is_active': True,
            'messages': [
                {'id': 410, 'role': 'player', 'content': 'Hello.'},
            ],
        }
        claim_payload = {
            'run': {'id': 2, 'attempt_count': 1, 'runner_config': {'audit_pause_phases': ['after_dm']}},
            'lease_token': 'lease-1',
            'derived_campaign': {'id': 100003},
            'latest_session': initial_session,
            'roster': [],
        }
        run_payload_with_silent_cycle = {
            'run': {'status': 'running'},
            'latest_session': initial_session,
            'audit_cycles': [
                {'phase': 'after_dm', 'player_message_id': 410, 'dm_message_id': None}
            ]
        }
        dm_status = {
            'status': 'silent',
            'player_message_id': 410,
            'dm_message_id': None,
        }

        with patch.object(worker, 'claim_run', return_value=claim_payload), \
             patch.object(worker, 'build_manifest_for_run', return_value={}), \
             patch.object(worker, 'append_event'), \
             patch.object(worker.autonomous, 'fetch_dm_turn_status', return_value=dm_status), \
             patch.object(worker, 'pause_for_audit_if_needed') as pause_mock, \
             patch.object(worker, 'request_overseer_decision', return_value={'action': 'no_action'}), \
             patch.object(worker, 'heartbeat', return_value={'run': {'lease_token': 'lease-1'}}), \
             patch.object(worker, 'complete_run') as complete_mock, \
             patch.object(worker.time, 'sleep'):
            
            run_payload_stop = {'run': {'status': 'stop_requested', 'error_text': 'done'}, 'latest_session': initial_session}
            with patch.object(worker, 'fetch_run', side_effect=[run_payload_with_silent_cycle, run_payload_stop]):
                finished = worker.execute_run(args, 2)

        self.assertTrue(finished)
        pause_mock.assert_not_called()

    def test_max_cycles_missing_after_dm_resumed(self):
        args = SimpleNamespace(
            api_base='http://127.0.0.1:5889',
            owner_api_key='owner-key',
            worker_id='test-worker',
            max_minutes=None,
            idle_timeout=180.0,
            heartbeat_interval=999.0,
            poll_interval=0.01,
            max_turns=1,
            dm_response_timeout=60.0,
            message_window=16,
            model='test-model',
        )
        initial_session = {
            'id': 4,
            'is_active': True,
            'messages': [
                {'id': 410, 'role': 'player', 'content': 'Hello.'},
                {'id': 411, 'role': 'dm', 'content': 'DM Response.'},
            ],
        }
        claim_payload = {
            'run': {'id': 2, 'attempt_count': 1, 'completed_turns': 1, 'runner_config': {'audit_pause_phases': ['after_dm']}},
            'lease_token': 'lease-1',
            'derived_campaign': {'id': 100003},
            'latest_session': initial_session,
            'roster': [],
        }
        run_payload_with_empty_cycles = {
            'run': {'status': 'running', 'completed_turns': 1},
            'latest_session': initial_session,
            'audit_cycles': []
        }
        dm_status = {
            'status': 'speak',
            'player_message_id': 410,
            'dm_message_id': 411,
            'post_turn_complete': True,
            'post_turn_status': 'complete',
        }

        with patch.object(worker, 'claim_run', return_value=claim_payload), \
             patch.object(worker, 'build_manifest_for_run', return_value={}), \
             patch.object(worker, 'append_event'), \
             patch.object(worker, 'fetch_run', return_value=run_payload_with_empty_cycles), \
             patch.object(worker.autonomous, 'fetch_dm_turn_status', return_value=dm_status), \
             patch.object(worker, 'wait_for_dm_response', return_value=(dm_status, False)), \
             patch.object(worker, 'heartbeat', return_value={'run': {'lease_token': 'lease-1'}}), \
             patch.object(worker, 'pause_for_audit_if_needed', return_value=(False, 'lease-1')) as pause_mock, \
             patch.object(worker, 'request_overseer_decision') as overseer_mock, \
             patch.object(worker, 'complete_run') as complete_mock:
            
            finished = worker.execute_run(args, 2)

        self.assertTrue(finished)
        pause_mock.assert_called_once()
        self.assertEqual(pause_mock.call_args[0][4], 'after_dm')
        complete_mock.assert_called_once()
        self.assertEqual(complete_mock.call_args.kwargs['status'], 'completed')
        overseer_mock.assert_not_called()


if __name__ == '__main__':
    unittest.main()

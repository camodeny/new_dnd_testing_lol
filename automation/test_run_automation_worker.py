import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch


sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
import run_automation_worker as worker


class RunAutomationWorkerTests(unittest.TestCase):
    def test_parse_args_defaults_to_no_wall_clock_cap(self):
        with patch.object(sys, 'argv', ['run_automation_worker.py']):
            args = worker.parse_args()

        self.assertIsNone(args.max_minutes)

    def test_parse_args_defaults_dm_response_timeout_to_twelve_minutes(self):
        with patch.object(sys, 'argv', ['run_automation_worker.py']):
            args = worker.parse_args()

        self.assertEqual(args.dm_response_timeout, 720.0)

    def test_wait_for_dm_response_waits_for_post_turn_completion(self):
        args = SimpleNamespace(poll_interval=0.01, dm_response_timeout=5.0,
                               dm_visible_response_timeout=None, dm_post_turn_timeout=None)
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
            result, timed_out, timeout_phase = worker.wait_for_dm_response(args, manifest, 411, lambda: None)

        self.assertFalse(timed_out)
        self.assertIsNone(timeout_phase)
        self.assertEqual(result, statuses[-1])
        self.assertEqual(fetch_status.call_count, 2)
        sleep.assert_not_called()

    def test_wait_for_audit_resume_keeps_worker_alive_until_continue(self):
        args = SimpleNamespace(
            api_base='http://127.0.0.1:5889',
            owner_api_key='owner-key',
            poll_interval=0.01,
        )
        waiting = {
            'run': {
                'status': 'awaiting_audit',
                'awaiting_audit_cycle_id': 17,
            },
        }
        resumed = {
            'run': {
                'status': 'running',
                'awaiting_audit_cycle_id': None,
            },
        }
        heartbeat = Mock()

        with patch.object(worker, 'fetch_run', side_effect=[waiting, resumed]) as fetch_run, \
                patch.object(worker.time, 'sleep') as sleep:
            stopped, lease_token = worker.wait_for_audit_resume(
                args,
                run_id=7,
                lease_token='lease-1',
                maybe_heartbeat_fn=heartbeat,
            )

        self.assertFalse(stopped)
        self.assertEqual(lease_token, 'lease-1')
        self.assertEqual(fetch_run.call_count, 2)
        heartbeat.assert_called_once()
        sleep.assert_called_once_with(0.01)

    def test_pause_for_audit_waits_then_returns_to_same_worker(self):
        args = SimpleNamespace(
            api_base='http://127.0.0.1:5889',
            owner_api_key='owner-key',
            worker_id='audit-worker',
            poll_interval=0.01,
        )
        claim_payload = {'run': {'runner_config': {'audit_pause_phases': ['after_dm']}}}

        with patch.object(worker, 'pause_run', return_value={
            'run': {'lease_token': 'lease-2'},
            'paused': True,
            'audit_cycle': {'id': 17},
        }), patch.object(worker, 'wait_for_audit_resume', return_value=(False, 'lease-3')) as wait_for_resume, \
                patch.object(worker, 'append_event') as append_event:
            should_stop, lease_token = worker.pause_for_audit_if_needed(
                args,
                claim_payload,
                run_id=7,
                lease_token='lease-1',
                phase='after_dm',
                maybe_heartbeat_fn=Mock(),
                summary='Paused after DM response.',
            )

        self.assertFalse(should_stop)
        self.assertEqual(lease_token, 'lease-3')
        wait_for_resume.assert_called_once()
        self.assertEqual(
            [call.args[5] for call in append_event.call_args_list],
            ['worker_waiting_for_audit_resume', 'worker_resumed_after_audit'],
        )

    def test_pause_for_audit_returns_worker_to_queue_when_backend_releases_lease(self):
        args = SimpleNamespace(
            api_base='http://127.0.0.1:5889',
            owner_api_key='owner-key',
            worker_id='audit-worker',
            poll_interval=0.01,
        )
        claim_payload = {'run': {'runner_config': {'audit_pause_phases': ['after_dm']}}}

        with patch.object(worker, 'pause_run', return_value={
            'run': {'status': 'awaiting_audit', 'has_lease_token': False},
            'paused': True,
            'worker_released': True,
            'audit_cycle': {'id': 17},
        }), patch.object(worker, 'wait_for_audit_resume') as wait_for_resume, \
                patch.object(worker, 'append_event') as append_event:
            should_yield, lease_token = worker.pause_for_audit_if_needed(
                args,
                claim_payload,
                run_id=7,
                lease_token='lease-1',
                phase='after_dm',
                maybe_heartbeat_fn=Mock(),
                summary='Paused after DM response.',
            )

        self.assertTrue(should_yield)
        self.assertIsNone(lease_token)
        wait_for_resume.assert_not_called()
        append_event.assert_not_called()

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
        initial_session = {
            'id': 4,
            'is_active': True,
            'started_at': '2026-07-01T15:58:50.044971',
            'messages': [
                {'id': 410, 'role': 'dm', 'content': 'Opening scene.', 'created_at': '2026-07-01T15:59:05.608343'},
            ],
        }
        claim_payload = {
            'run': {'id': 2, 'attempt_count': 1, 'runner_config': {'audit_pause_phases': ['after_dm']}},
            'lease_token': 'lease-1',
            'derived_campaign': {'id': 100003},
            'latest_session': initial_session,
            'gameplay_readiness': {'campaign_ready': True},
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
                patch.object(worker, 'heartbeat', return_value={'run': {'lease_token': 'lease-1'}}), \
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
                    None,
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
            'gameplay_readiness': {'campaign_ready': True},
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
             patch.object(worker, 'wait_for_dm_response', return_value=(dm_status, False, None)), \
             patch.object(worker, 'heartbeat', return_value={'run': {'lease_token': 'lease-1'}}), \
             patch.object(worker, 'complete_run') as complete_mock, \
             patch.object(worker, 'pause_for_audit_if_needed', return_value=(True, 'lease-1')) as pause_mock:
            
            finished = worker.execute_run(args, 2)

        self.assertTrue(finished)
        pause_mock.assert_called_once()
        self.assertEqual(pause_mock.call_args[0][4], 'after_dm')
        complete_mock.assert_called_once()
        self.assertEqual(complete_mock.call_args.kwargs['status'], 'stopped')

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
            'gameplay_readiness': {'campaign_ready': True},
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
            'gameplay_readiness': {'campaign_ready': True},
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
             patch.object(worker, 'wait_for_dm_response', return_value=(dm_status, False, None)), \
             patch.object(worker, 'heartbeat', return_value={'run': {'lease_token': 'lease-1'}}), \
             patch.object(worker, 'pause_for_audit_if_needed', return_value=(True, 'lease-1')) as pause_mock, \
             patch.object(worker, 'complete_run') as complete_mock:
            
            finished = worker.execute_run(args, 2)

        self.assertTrue(finished)
        pause_mock.assert_called_once()
        self.assertEqual(pause_mock.call_args[0][4], 'after_dm')
        complete_mock.assert_called_once()
        self.assertEqual(complete_mock.call_args.kwargs['status'], 'stopped')

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
            'gameplay_readiness': {'campaign_ready': True},
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
             patch.object(worker, 'request_overseer_decision', return_value={'action': 'no_action'}) as overseer_mock, \
             patch.object(worker, 'heartbeat', return_value={'run': {'lease_token': 'lease-1'}}), \
             patch.object(worker, 'complete_run') as complete_mock, \
             patch.object(worker.time, 'sleep'):
            
            run_payload_stop = {'run': {'status': 'stop_requested', 'error_text': 'done'}, 'latest_session': initial_session}
            with patch.object(
                worker,
                'fetch_run',
                side_effect=[run_payload_with_silent_cycle, run_payload_with_silent_cycle, run_payload_stop],
            ):
                finished = worker.execute_run(args, 2)

        self.assertTrue(finished)
        pause_mock.assert_not_called()
        overseer_mock.assert_called_once()
        self.assertEqual(overseer_mock.call_args[0][5], dm_status)

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
            'gameplay_readiness': {'campaign_ready': True},
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
             patch.object(worker, 'wait_for_dm_response', return_value=(dm_status, False, None)), \
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


    def test_pre_overseer_guard_resume_after_after_player_audit(self):
        """When session-on-start resume block is skipped (no session in claim payload),
        the pre-overseer guard should detect a missing after_dm cycle after an
        after_player audit and force the DM wait/pause path before any overseer decision."""
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
            'run': {'id': 2, 'attempt_count': 1, 'runner_config': {'audit_pause_phases': ['after_dm', 'after_player']}},
            'lease_token': 'lease-1',
            'derived_campaign': {'id': 100003},
            'latest_session': initial_session,
            'gameplay_readiness': {'campaign_ready': True},
            'roster': [],
        }
        run_payload = {
            'run': {'status': 'running', 'completed_turns': 1},
            'latest_session': initial_session,
            'audit_cycles': [
                {'phase': 'after_player', 'player_message_id': 410, 'dm_message_id': 411}
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
             patch.object(worker, 'fetch_run', return_value=run_payload), \
             patch.object(worker.autonomous, 'fetch_dm_turn_status', return_value=dm_status), \
             patch.object(worker, 'wait_for_dm_response', return_value=(dm_status, False, None)), \
             patch.object(worker, 'heartbeat', return_value={'run': {'lease_token': 'lease-1'}}), \
             patch.object(worker, 'pause_for_audit_if_needed', return_value=(True, 'lease-1')) as pause_mock, \
             patch.object(worker, 'complete_run') as complete_mock, \
             patch.object(worker, 'request_overseer_decision') as overseer_mock, \
             patch.object(worker.time, 'sleep'):

            finished = worker.execute_run(args, 2)

        self.assertTrue(finished)
        pause_mock.assert_called_once()
        self.assertEqual(pause_mock.call_args[0][4], 'after_dm')
        overseer_mock.assert_not_called()
        complete_mock.assert_called_once()
        self.assertEqual(complete_mock.call_args.kwargs['status'], 'stopped')


    def test_stop_during_audit_finalizes_as_stopped(self):
        """Regression: stopping a run during audit review must finalize as stopped, not leave it as stop_requested."""
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
            model='test-model',
        )
        initial_session = {
            'id': 4,
            'is_active': True,
            'started_at': '2026-07-01T15:58:50.044971',
            'messages': [
                {'id': 410, 'role': 'dm', 'content': 'Opening scene.', 'created_at': '2026-07-01T15:59:05.608343'},
            ],
        }
        claim_payload = {
            'run': {'id': 2, 'attempt_count': 1, 'runner_config': {'audit_pause_phases': ['after_dm']}},
            'lease_token': 'lease-1',
            'derived_campaign': {'id': 100003},
            'latest_session': initial_session,
            'gameplay_readiness': {'campaign_ready': True},
            'roster': [
                {
                    'llm_player_id': 33,
                    'user_id': 35,
                    'label': 'Test Player',
                    'character_name': 'Kaelen Shadowstep',
                    'derived_character_id': 38,
                },
            ],
        }

        monotonic_values = [0.0] * 20
        monotonic_iter = iter(monotonic_values)

        with patch.object(worker, 'claim_run', return_value=claim_payload), \
                patch.object(worker, 'heartbeat', return_value={'run': {'lease_token': 'lease-1'}}), \
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
                    None,
                )), \
                patch.object(worker, 'pause_for_audit_if_needed', side_effect=[(False, 'lease-1'), (True, 'lease-1')]) as pause_mock, \
                patch.object(worker, 'fetch_run', side_effect=[
                    {'run': {'status': 'running'}, 'latest_session': initial_session},
                    {'run': {'status': 'running'}, 'latest_session': initial_session},
                ]), \
                patch.object(worker, 'complete_run') as complete_run_mock, \
                patch.object(worker.time, 'sleep'), \
                patch.object(worker.time, 'monotonic', side_effect=lambda: next(monotonic_iter, 0.0)):
            finished = worker.execute_run(args, 2)

        self.assertTrue(finished)
        self.assertEqual(pause_mock.call_count, 2)
        complete_run_mock.assert_called_once()
        self.assertEqual(complete_run_mock.call_args.kwargs['status'], 'stopped')

    def test_ensure_campaign_initialized_bootstraps_missing_session(self):
        args = SimpleNamespace(
            api_base='http://127.0.0.1:5889',
            owner_api_key='owner-key',
            worker_id='bootstrap-worker',
        )
        claim_payload = {
            'derived_campaign': {'id': 100003},
            'latest_session': None,
        }
        session = {
            'id': 44,
            'is_active': True,
            'messages': [{'id': 101, 'role': 'dm', 'content': 'Opening scene.'}],
        }

        with patch.object(worker, 'start_session', return_value=session) as start_session, \
                patch.object(worker, 'api_get', return_value={'world': {'world_state': {}}}) as api_get:
            ensured_session, world_payload = worker.ensure_campaign_initialized(args, claim_payload)

        self.assertEqual(ensured_session, session)
        self.assertEqual(world_payload, {'world': {'world_state': {}}})
        self.assertEqual(claim_payload['latest_session'], session)
        start_session.assert_called_once()
        api_get.assert_called_once()

    def test_ensure_campaign_initialized_bootstraps_with_stale_gameplay_readiness(self):
        args = SimpleNamespace(
            api_base='http://127.0.0.1:5889',
            owner_api_key='owner-key',
            worker_id='bootstrap-worker',
        )
        claim_payload = {
            'derived_campaign': {'id': 100003},
            'latest_session': None,
            'gameplay_readiness': {
                'world_present': False,
                'active_session_present': False,
                'opening_dm_present': False,
                'campaign_ready': False,
            }
        }
        session = {
            'id': 44,
            'is_active': True,
            'messages': [{'id': 101, 'role': 'dm', 'content': 'Opening scene.'}],
        }

        with patch.object(worker, 'start_session', return_value=session) as start_session, \
                patch.object(worker, 'api_get', return_value={'world': {'world_state': {}}}) as api_get:
            ensured_session, world_payload = worker.ensure_campaign_initialized(args, claim_payload)

        self.assertEqual(ensured_session, session)
        self.assertEqual(world_payload, {'world': {'world_state': {}}})
        self.assertEqual(claim_payload['latest_session'], session)
        start_session.assert_called_once()
        api_get.assert_called_once()

    def test_ensure_campaign_initialized_rejects_unplayable_session(self):
        args = SimpleNamespace(
            api_base='http://127.0.0.1:5889',
            owner_api_key='owner-key',
            worker_id='bootstrap-worker',
        )
        claim_payload = {
            'derived_campaign': {'id': 100003},
            'latest_session': {
                'id': 44,
                'is_active': True,
                'messages': [],
            },
        }

        with patch.object(worker, 'api_get', return_value={'world': {'world_state': {}}}):
            with self.assertRaisesRegex(RuntimeError, 'campaign_not_initialized'):
                worker.ensure_campaign_initialized(args, claim_payload)

    def test_ensure_campaign_initialized_hydrates_compact_claim_session(self):
        args = SimpleNamespace(
            api_base='http://127.0.0.1:5889',
            owner_api_key='owner-key',
            worker_id='bootstrap-worker',
        )
        claim_payload = {
            'derived_campaign': {'id': 100003},
            'latest_session': {'id': 44, 'is_active': True},
            'gameplay_readiness': {'campaign_ready': True},
        }
        hydrated_session = {
            'id': 44,
            'is_active': True,
            'messages': [{'id': 101, 'role': 'dm', 'content': 'Opening scene.'}],
        }

        with patch.object(worker, 'api_get', return_value={'session': hydrated_session}) as api_get:
            ensured_session, world_payload = worker.ensure_campaign_initialized(args, claim_payload)

        self.assertEqual(ensured_session, hydrated_session)
        self.assertEqual(world_payload, {'world': {'world_state': {}}})
        self.assertEqual(claim_payload['latest_session'], hydrated_session)
        api_get.assert_called_once_with(
            'http://127.0.0.1:5889',
            '/api/sessions/44',
            api_key='owner-key',
        )

    def test_ensure_campaign_initialized_no_world(self):
        args = SimpleNamespace(
            api_base='http://127.0.0.1:5889',
            owner_api_key='owner-key',
            worker_id='bootstrap-worker',
        )
        claim_payload = {
            'derived_campaign': {'id': 100003},
            'latest_session': {
                'id': 44,
                'is_active': True,
                'messages': [{'id': 101, 'role': 'dm', 'content': 'Opening scene.'}],
            },
        }
        with patch.object(worker, 'api_get', return_value={}):
            with self.assertRaisesRegex(RuntimeError, 'campaign_not_initialized'):
                worker.ensure_campaign_initialized(args, claim_payload)

    def test_ensure_campaign_initialized_no_dm_message(self):
        args = SimpleNamespace(
            api_base='http://127.0.0.1:5889',
            owner_api_key='owner-key',
            worker_id='bootstrap-worker',
        )
        claim_payload = {
            'derived_campaign': {'id': 100003},
            'latest_session': {
                'id': 44,
                'is_active': True,
                'messages': [{'id': 101, 'role': 'player', 'content': 'I do something.'}],
            },
        }
        with patch.object(worker, 'api_get', return_value={'world': {'world_state': {}}}):
            with self.assertRaisesRegex(RuntimeError, 'campaign_not_initialized'):
                worker.ensure_campaign_initialized(args, claim_payload)

    def test_execute_run_initialization_failure(self):
        args = SimpleNamespace(
            api_base='http://127.0.0.1:5889',
            owner_api_key='owner-key',
            worker_id='test-worker',
            max_turns=5,
            max_minutes=1.0,
            poll_interval=0.1,
            idle_timeout=1.0,
            heartbeat_interval=1.0,
            init_lease_seconds=900,
            dm_response_timeout=1.0,
            message_window=10,
        )
        claim_payload = {
            'run': {'id': 123, 'attempt_count': 1, 'completed_turns': 0},
            'lease_token': 'lease-token-123',
            'derived_campaign': {'id': 100003},
            'latest_session': {
                'id': 44,
                'is_active': True,
                'messages': [],
            },
        }
        with patch.object(worker, 'claim_run', return_value=claim_payload) as mock_claim, \
             patch.object(worker, 'heartbeat', return_value={'run': {'lease_token': 'lease-token-123'}}), \
             patch.object(worker, 'api_get', return_value={'world': {'world_state': {}}}), \
             patch.object(worker, 'complete_run') as mock_complete:
            with self.assertRaises(RuntimeError):
                worker.execute_run(args, 123)
            
            mock_complete.assert_called_once_with(
                'http://127.0.0.1:5889',
                'owner-key',
                123,
                'test-worker',
                'lease-token-123',
                status='failed',
                error_text='campaign_not_initialized',
                dedupe_key='run_completed:123:init-failed'
            )


    def test_execute_run_initialization_failure_with_api_error_details(self):
        args = SimpleNamespace(
            api_base='http://127.0.0.1:5889',
            owner_api_key='owner-key',
            worker_id='test-worker',
            max_turns=5,
            max_minutes=1.0,
            poll_interval=0.1,
            idle_timeout=1.0,
            heartbeat_interval=1.0,
            init_lease_seconds=900,
            dm_response_timeout=1.0,
            message_window=10,
        )
        claim_payload = {
            'run': {'id': 123, 'attempt_count': 1, 'completed_turns': 0},
            'lease_token': 'lease-token-123',
            'derived_campaign': {'id': 100003},
            'latest_session': {
                'id': 44,
                'is_active': True,
                'messages': [],
            },
        }
        from llm_campaign_common import ApiError
        with patch.object(worker, 'claim_run', return_value=claim_payload) as mock_claim, \
             patch.object(worker, 'heartbeat', return_value={'run': {'lease_token': 'lease-token-123'}}), \
             patch.object(worker, 'ensure_campaign_initialized', side_effect=ApiError("POST /api/campaigns/4/sessions -> HTTP 400: Every party member must select and ready a character before starting a session")), \
             patch.object(worker, 'complete_run') as mock_complete:
            with self.assertRaises(ApiError):
                worker.execute_run(args, 123)
            
            mock_complete.assert_called_once_with(
                'http://127.0.0.1:5889',
                'owner-key',
                123,
                'test-worker',
                'lease-token-123',
                status='failed',
                error_text='campaign_not_initialized: POST /api/campaigns/4/sessions -> HTTP 400: Every party member must select and ready a character before starting a session',
                dedupe_key='run_completed:123:init-failed'
            )

    def test_execute_run_initialization_failure_empty_details(self):
        args = SimpleNamespace(
            api_base='http://127.0.0.1:5889',
            owner_api_key='owner-key',
            worker_id='test-worker',
            max_turns=5,
            max_minutes=1.0,
            poll_interval=0.1,
            idle_timeout=1.0,
            heartbeat_interval=1.0,
            init_lease_seconds=900,
            dm_response_timeout=1.0,
            message_window=10,
        )
        claim_payload = {
            'run': {'id': 123, 'attempt_count': 1, 'completed_turns': 0},
            'lease_token': 'lease-token-123',
            'derived_campaign': {'id': 100003},
            'latest_session': {
                'id': 44,
                'is_active': True,
                'messages': [],
            },
        }
        with patch.object(worker, 'claim_run', return_value=claim_payload) as mock_claim, \
             patch.object(worker, 'heartbeat', return_value={'run': {'lease_token': 'lease-token-123'}}), \
             patch.object(worker, 'ensure_campaign_initialized', side_effect=RuntimeError("")), \
             patch.object(worker, 'complete_run') as mock_complete:
            with self.assertRaises(RuntimeError):
                worker.execute_run(args, 123)
            
            mock_complete.assert_called_once_with(
                'http://127.0.0.1:5889',
                'owner-key',
                123,
                'test-worker',
                'lease-token-123',
                status='failed',
                error_text='campaign_not_initialized',
                dedupe_key='run_completed:123:init-failed'
            )


    def test_default_worker_id_includes_hostname_pid_and_nonce(self):
        wid = worker.default_worker_id()
        parts = wid.split('-')
        self.assertGreaterEqual(len(parts), 3)
        self.assertTrue(wid.startswith('automation-'))

    def test_default_worker_ids_are_unique_with_same_pid(self):
        with patch.object(os, 'getpid', return_value=12345):
            w1 = worker.default_worker_id()
            w2 = worker.default_worker_id()
            self.assertNotEqual(w1, w2)

    def test_resolve_worker_id_cli_overrides_env(self):
        os.environ.pop('DND_AUTOMATION_WORKER_ID', None)
        result = worker.resolve_worker_id('cli-worker', 'DND_AUTOMATION_WORKER_ID')
        self.assertEqual(result, 'cli-worker')

    def test_resolve_worker_id_env_overrides_generated(self):
        os.environ['DND_AUTOMATION_WORKER_ID'] = 'env-worker'
        result = worker.resolve_worker_id(None, 'DND_AUTOMATION_WORKER_ID')
        self.assertEqual(result, 'env-worker')
        del os.environ['DND_AUTOMATION_WORKER_ID']

    def test_resolve_worker_id_generated_when_no_override(self):
        os.environ.pop('DND_AUTOMATION_WORKER_ID', None)
        result = worker.resolve_worker_id(None, 'DND_AUTOMATION_WORKER_ID')
        self.assertTrue(result.startswith('automation-'))
        self.assertNotEqual(result, '')


    def test_execute_run_initialization_lease_covers_window_when_heartbeat_blocked(self):
        args = SimpleNamespace(
            api_base='http://127.0.0.1:5889',
            owner_api_key='owner-key',
            worker_id='test-worker',
            max_turns=5,
            max_minutes=1.0,
            poll_interval=0.01,
            idle_timeout=1.0,
            heartbeat_interval=0.01,
            init_lease_seconds=900,
            dm_response_timeout=1.0,
            message_window=10,
        )
        claim_payload = {
            'run': {'id': 123, 'attempt_count': 1, 'completed_turns': 0},
            'lease_token': 'lease-token-123',
            'derived_campaign': {'id': 100003},
            'latest_session': None,
            'gameplay_readiness': {
                'world_present': False,
                'active_session_present': False,
                'opening_dm_present': False,
                'campaign_ready': False,
            },
        }

        # The first call (initial long-lease heartbeat) succeeds.  Subsequent
        # calls from the background thread (which carry lease_seconds) fail to
        # model the single-threaded SQLite deployment where the server cannot
        # service heartbeats during world generation.  Main-loop heartbeats
        # (no lease_seconds) must succeed so the run loop can exit cleanly.
        first_heartbeat = [True]
        def heartbeat_side_effect(*a, **kw):
            if first_heartbeat[0]:
                first_heartbeat[0] = False
                return {'run': {'lease_token': 'lease-token-123'}}
            if kw.get('lease_seconds') is not None:
                raise worker.ApiError('POST /heartbeat -> timed out')
            return {'run': {'lease_token': 'lease-token-123'}}

        init_delay = 0.15

        def slow_ensure(args, claim_payload, **kw):
            time.sleep(init_delay)
            session = {
                'id': 44,
                'is_active': True,
                'messages': [{'id': 101, 'role': 'dm', 'content': 'Opening scene.'}],
            }
            claim_payload['latest_session'] = session
            return session, {'world': {'world_state': {}}}

        with patch.object(worker, 'claim_run', return_value=claim_payload), \
             patch.object(worker, 'heartbeat', side_effect=heartbeat_side_effect) as mock_heartbeat, \
             patch.object(worker, 'ensure_campaign_initialized', side_effect=slow_ensure), \
             patch.object(worker, 'build_manifest_for_run', return_value={'session': {'id': 44}, 'llm_players': [], 'campaign': {}, 'owner': {}, 'api_base': ''}), \
             patch.object(worker, 'append_event'), \
             patch.object(worker, 'fetch_run', return_value={'run': {'status': 'stop_requested'}}), \
             patch.object(worker, 'complete_run') as mock_complete:
            worker.execute_run(args, 123)

        first_call_kwargs = mock_heartbeat.call_args_list[0][1]
        self.assertEqual(
            first_call_kwargs.get('lease_seconds'), 900,
            'Initial heartbeat must carry lease_seconds=args.init_lease_seconds before initialization',
        )
        self.assertNotEqual(
            mock_complete.call_args[1].get('status'),
            'failed',
            msg='Run must not fail when background heartbeats are blocked, '
            'because the initial long lease covers the initialization window',
        )

    def test_start_session_polls_on_generation_in_progress(self):
        from llm_campaign_common import start_session, ApiError

        session = {'id': 55, 'is_active': True, 'messages': []}
        get_call_count = [0]
        world_check_count = [0]

        def mock_get(base, path, **kwargs):
            if '/sessions' in path and 'campaigns' in path:
                get_call_count[0] += 1
                if get_call_count[0] == 1:
                    return {'sessions': []}
                return {'sessions': [session]}
            if '/world' in path:
                world_check_count[0] += 1
                return {'generation_in_progress': True}
            return {}

        def mock_post(base, path, payload=None, **kwargs):
            raise ApiError(
                'POST /api/campaigns/42/sessions -> HTTP 409: '
                '{"error": "Campaign world generation is already in progress", '
                '"generation_in_progress": true}'
            )

        with patch('llm_campaign_common.api_get', side_effect=mock_get), \
             patch('llm_campaign_common.api_post', side_effect=mock_post), \
             patch('llm_campaign_common.time.sleep'), \
             patch('llm_campaign_common.default_session_start_timeout', return_value=5):
            result = start_session(
                'http://127.0.0.1:5889', 42, api_key='key',
                timeout=5, poll_interval=0.01,
            )

        self.assertEqual(
            result, session,
            'Worker must poll for the active session when world generation is in progress, '
            'rather than raising a terminal error',
        )
        self.assertGreaterEqual(
            get_call_count[0], 2,
            'Must make multiple GET calls to poll for session to appear',
        )

    def test_start_session_polls_and_retries_when_world_done_but_no_session(self):
        from llm_campaign_common import start_session, ApiError

        session = {'id': 66, 'is_active': True, 'messages': []}
        call_plan = [
            ('get', '/campaigns/42/sessions', {'sessions': []}),           # find_active_session (first)
            ('post', '/campaigns/42/sessions', ApiError('HTTP 409: generation_in_progress')),  # POST fails
            ('get', '/campaigns/42/sessions', {'sessions': []}),           # find_active_session (poll)
            ('get', '/campaigns/42/world', {'generation_in_progress': False}),  # world done!
            ('post', '/campaigns/42/sessions', {'session': session}),       # retry POST succeeds
        ]
        step = [0]

        def mock_get(base, path, **kwargs):
            while step[0] < len(call_plan):
                kind, url_fragment, result = call_plan[step[0]]
                if kind == 'get' and url_fragment in path:
                    step[0] += 1
                    return result
                break
            return {}

        def mock_post(base, path, payload=None, **kwargs):
            while step[0] < len(call_plan):
                kind, url_fragment, result = call_plan[step[0]]
                if kind == 'post' and url_fragment in path:
                    step[0] += 1
                    if isinstance(result, ApiError):
                        raise result
                    return result
                break
            raise ApiError('POST /campaigns/42/sessions -> unexpected call')

        with patch('llm_campaign_common.api_get', side_effect=mock_get), \
             patch('llm_campaign_common.api_post', side_effect=mock_post), \
             patch('llm_campaign_common.time.sleep'), \
             patch('llm_campaign_common.default_session_start_timeout', return_value=5):
            result = start_session(
                'http://127.0.0.1:5889', 42, api_key='key',
                timeout=5, poll_interval=0.01,
            )

        self.assertEqual(
            result, session,
            'Worker must retry session creation when world generation completes '
            'but no active session has appeared',
        )


    def test_start_session_reraises_unexpected_error_from_retry_post(self):
        from llm_campaign_common import start_session, ApiError

        call_plan = [
            ('get', '/campaigns/42/sessions', {'sessions': []}),
            ('post', '/campaigns/42/sessions', ApiError('HTTP 409: generation_in_progress')),
            ('get', '/campaigns/42/sessions', {'sessions': []}),
            ('get', '/campaigns/42/world', {'generation_in_progress': False}),
            ('post', '/campaigns/42/sessions', ApiError('HTTP 500: Internal Server Error')),
        ]
        step = [0]

        def mock_get(base, path, **kwargs):
            while step[0] < len(call_plan):
                kind, url_fragment, result = call_plan[step[0]]
                if kind == 'get' and url_fragment in path:
                    step[0] += 1
                    return result
                break
            return {}

        def mock_post(base, path, payload=None, **kwargs):
            while step[0] < len(call_plan):
                kind, url_fragment, result = call_plan[step[0]]
                if kind == 'post' and url_fragment in path:
                    step[0] += 1
                    if isinstance(result, ApiError):
                        raise result
                    return result
                break
            raise ApiError('POST /campaigns/42/sessions -> unexpected call')

        with patch('llm_campaign_common.api_get', side_effect=mock_get), \
             patch('llm_campaign_common.api_post', side_effect=mock_post), \
             patch('llm_campaign_common.time.sleep'), \
             patch('llm_campaign_common.default_session_start_timeout', return_value=5):
            with self.assertRaises(ApiError):
                start_session(
                    'http://127.0.0.1:5889', 42, api_key='key',
                    timeout=5, poll_interval=0.01,
                )

    def test_world_generation_is_in_progress_passes_owner_token(self):
        from llm_campaign_common import world_generation_is_in_progress

        with patch('llm_campaign_common.api_get', return_value={'generation_in_progress': True}) as mock_get:
            result = world_generation_is_in_progress(
                'http://127.0.0.1:5889', 42, owner_token='owner-tok', api_key='key',
            )

        self.assertTrue(result)
        mock_get.assert_called_once_with(
            'http://127.0.0.1:5889', '/api/campaigns/42/world',
            owner_token='owner-tok', api_key='key',
        )


class DmTurnTimeoutReconciliationTest(unittest.TestCase):
    def _args(self, reconciliation_seconds=30.0):
        return SimpleNamespace(
            api_base='http://127.0.0.1:5889',
            owner_api_key='owner-key',
            worker_id='worker-1',
            poll_interval=3.0,
            dm_late_completion_reconciliation_seconds=reconciliation_seconds,
            dm_visible_response_timeout=None,
            dm_post_turn_timeout=None,
            dm_response_timeout=None,
        )

    def _run_reconcile(self, args, timeout_phase='post_turn', timeout_error='dm_post_turn_timeout'):
        return worker._reconcile_dm_turn_timeout(
            args,
            {'session': {'id': 4}},
            {'run': {'attempt_count': 2}},
            5,
            'lease-1',
            411,
            'stage-0',
            timeout_phase,
            timeout_error,
            {'status': 'speak', 'post_turn_status': 'pending'},
            lambda: None,
        )

    def test_reconciliation_recovers_when_post_turn_completes(self):
        args = self._args()
        statuses = [
            {'status': 'speak', 'post_turn_complete': False, 'post_turn_status': 'pending', 'dm_message_id': 20},
            {'status': 'speak', 'post_turn_complete': False, 'post_turn_status': 'pending', 'dm_message_id': 20},
            {
                'status': 'speak',
                'post_turn_complete': True,
                'post_turn_status': 'complete',
                'dm_message_id': 20,
                'memory_status': 'complete',
                'clock_status': 'complete',
                'finished_at': '2026-07-14T02:26:03Z',
            },
        ]

        with patch.object(worker.autonomous, 'fetch_dm_turn_status', side_effect=statuses) as fetch, \
                patch.object(worker, 'fetch_run', return_value={'run': {'status': 'reconciling'}}), \
                patch.object(worker.time, 'monotonic', return_value=0.0), \
                patch.object(worker.time, 'sleep') as sleep, \
                patch.object(worker, 'append_event') as append:
            recovered, stop_state, terminal_error = self._run_reconcile(args)

        self.assertEqual(recovered, statuses[-1])
        self.assertIsNone(stop_state)
        self.assertIsNone(terminal_error)
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        started = append.call_args_list[0]
        self.assertEqual(started[0][5], 'dm_turn_reconciliation_started')
        self.assertEqual(started[1]['status'], 'reconciling')
        self.assertEqual(started[0][6]['reconciliation_window_seconds'], 30.0)
        self.assertEqual(started[0][6]['timeout_phase'], 'post_turn')
        self.assertEqual(started[0][6]['attempt_count'], 2)
        recovered_call = append.call_args_list[1]
        self.assertEqual(recovered_call[0][5], 'dm_turn_reconciliation_recovered')
        self.assertEqual(recovered_call[1]['status'], 'running')
        self.assertEqual(recovered_call[0][6]['final_post_turn_status'], 'complete')
        self.assertEqual(recovered_call[0][6]['reconciliation_outcome'], 'recovered')
        self.assertEqual(append.call_count, 2)

    def test_reconciliation_recovers_visible_phase_timeout(self):
        args = self._args()
        statuses = [
            {
                'status': 'speak',
                'post_turn_status': 'complete',
                'dm_message_id': 20,
                'memory_status': 'complete',
                'clock_status': 'complete',
            },
        ]

        with patch.object(worker.autonomous, 'fetch_dm_turn_status', side_effect=statuses), \
                patch.object(worker, 'fetch_run', return_value={'run': {'status': 'reconciling'}}), \
                patch.object(worker.time, 'monotonic', return_value=0.0), \
                patch.object(worker.time, 'sleep'), \
                patch.object(worker, 'append_event') as append:
            recovered, stop_state, terminal_error = self._run_reconcile(
                args,
                timeout_phase='visible',
                timeout_error='dm_visible_response_timeout',
            )

        self.assertEqual(recovered, statuses[-1])
        self.assertIsNone(stop_state)
        self.assertIsNone(terminal_error)
        self.assertEqual(append.call_args_list[0][0][6]['timeout_phase'], 'visible')
        self.assertEqual(append.call_args_list[1][0][5], 'dm_turn_reconciliation_recovered')

    def test_reconciliation_exhausts_window_and_returns_terminal_error(self):
        args = self._args(reconciliation_seconds=0.0)
        statuses = [
            {'status': 'speak', 'post_turn_complete': False, 'post_turn_status': 'pending', 'dm_message_id': 20},
        ]

        with patch.object(worker.autonomous, 'fetch_dm_turn_status', side_effect=statuses), \
                patch.object(worker, 'fetch_run', return_value={'run': {'status': 'reconciling'}}), \
                patch.object(worker.time, 'monotonic', return_value=0.0), \
                patch.object(worker.time, 'sleep'), \
                patch.object(worker, 'append_event') as append:
            recovered, stop_state, terminal_error = self._run_reconcile(args)

        self.assertIsNone(recovered)
        self.assertIsNone(stop_state)
        self.assertEqual(terminal_error, 'dm_post_turn_timeout')
        exhausted = append.call_args_list[1]
        self.assertEqual(exhausted[0][5], 'dm_turn_reconciliation_exhausted')
        self.assertEqual(exhausted[0][6]['terminal_reason'], 'window_exhausted')
        self.assertEqual(exhausted[0][6]['timeout_classification'], 'dm_post_turn_timeout')

    def test_reconciliation_reports_turn_error_during_window(self):
        args = self._args()
        statuses = [
            {'status': 'speak', 'post_turn_status': 'error', 'dm_message_id': 20, 'error_text': 'memory failed'},
        ]

        with patch.object(worker.autonomous, 'fetch_dm_turn_status', side_effect=statuses), \
                patch.object(worker, 'fetch_run', return_value={'run': {'status': 'reconciling'}}), \
                patch.object(worker.time, 'monotonic', return_value=0.0), \
                patch.object(worker.time, 'sleep'), \
                patch.object(worker, 'append_event') as append:
            recovered, stop_state, terminal_error = self._run_reconcile(args)

        self.assertIsNone(recovered)
        self.assertIsNone(stop_state)
        self.assertEqual(terminal_error, 'memory failed')
        exhausted = append.call_args_list[1]
        self.assertEqual(exhausted[0][5], 'dm_turn_reconciliation_exhausted')
        self.assertEqual(exhausted[0][6]['terminal_reason'], 'dm_turn_error')

    def test_reconciliation_stops_on_external_stop_request(self):
        args = self._args()
        stop_run = {'status': 'stop_requested', 'error_text': None}

        with patch.object(worker.autonomous, 'fetch_dm_turn_status') as fetch, \
                patch.object(worker, 'fetch_run', return_value={'run': stop_run}), \
                patch.object(worker.time, 'monotonic', return_value=0.0), \
                patch.object(worker.time, 'sleep'), \
                patch.object(worker, 'append_event') as append:
            recovered, stop_state, terminal_error = self._run_reconcile(args)

        self.assertIsNone(recovered)
        self.assertEqual(stop_state, stop_run)
        self.assertIsNone(terminal_error)
        fetch.assert_not_called()
        self.assertEqual(append.call_count, 1)
        self.assertEqual(append.call_args_list[0][0][5], 'dm_turn_reconciliation_started')


if __name__ == '__main__':
    unittest.main()

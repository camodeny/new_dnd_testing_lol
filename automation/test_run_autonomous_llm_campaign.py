import json
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import run_autonomous_llm_campaign as autonomous


class RunAutonomousLlmCampaignTests(unittest.TestCase):
    def test_update_same_fingerprint_retry_state_retries_until_budget_exhausted(self):
        force_retry, retry_count = autonomous.update_same_fingerprint_retry_state('no_action', 0, 3)
        self.assertTrue(force_retry)
        self.assertEqual(retry_count, 1)

        force_retry, retry_count = autonomous.update_same_fingerprint_retry_state('no_action', retry_count, 3)
        self.assertTrue(force_retry)
        self.assertEqual(retry_count, 2)

        force_retry, retry_count = autonomous.update_same_fingerprint_retry_state('no_action', retry_count, 3)
        self.assertFalse(force_retry)
        self.assertEqual(retry_count, 3)

    def test_update_same_fingerprint_retry_state_resets_after_real_action(self):
        force_retry, retry_count = autonomous.update_same_fingerprint_retry_state('speak', 2, 3)
        self.assertFalse(force_retry)
        self.assertEqual(retry_count, 0)

    def test_acquire_manifest_lock_rejects_live_pid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = pathlib.Path(tmpdir) / 'campaign.json'
            lock_path = autonomous.lock_path_for_manifest(manifest_path)
            lock_path.write_text('424242\n', encoding='utf-8')

            with patch.object(autonomous, 'process_is_running', return_value=True):
                with self.assertRaisesRegex(RuntimeError, 'Another autonomous runner is already active'):
                    autonomous.acquire_manifest_lock(manifest_path)

    def test_acquire_manifest_lock_reclaims_stale_lock(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = pathlib.Path(tmpdir) / 'campaign.json'
            lock_path = autonomous.lock_path_for_manifest(manifest_path)
            lock_path.write_text('424242\n', encoding='utf-8')

            with patch.object(autonomous, 'process_is_running', return_value=False):
                acquired_lock_path = autonomous.acquire_manifest_lock(manifest_path)

            self.assertEqual(acquired_lock_path, lock_path)
            self.assertEqual(lock_path.read_text(encoding='utf-8').strip(), str(autonomous.os.getpid()))
            autonomous.release_manifest_lock(acquired_lock_path)

    def test_normalize_overseer_decision_accepts_valid_player_choice(self):
        manifest = {
            'llm_players': [
                {'llm_player': {'id': 13}},
                {'llm_player': {'id': 14}},
            ],
        }

        normalized = autonomous.normalize_overseer_decision(
            manifest,
            {'action': 'choose_player', 'llm_player_id': 14, 'reason': 'Elara is in the active sub-scene.'},
        )

        self.assertEqual(normalized['action'], 'choose_player')
        self.assertEqual(normalized['llm_player_id'], 14)

    def test_normalize_overseer_decision_rejects_unknown_player(self):
        manifest = {
            'llm_players': [
                {'llm_player': {'id': 13}},
            ],
        }

        with self.assertRaisesRegex(RuntimeError, 'unknown llm_player_id'):
            autonomous.normalize_overseer_decision(
                manifest,
                {'action': 'choose_player', 'llm_player_id': 99},
            )

    def test_run_orchestrator_passes_player_id(self):
        args = SimpleNamespace(
            opencode_server='http://127.0.0.1:4040',
            model='opencode-go/deepseek-v4-flash',
            message_window=12,
            opencode_password=None,
            dry_run=False,
        )

        with patch.object(autonomous.subprocess, 'run') as subprocess_run:
            subprocess_run.return_value = SimpleNamespace(
                returncode=0,
                stdout=json.dumps({'decision': {'action': 'speak'}}),
                stderr='',
            )

            autonomous.run_orchestrator(args, pathlib.Path('automation/state/test.json'), player_id=15)

        command = subprocess_run.call_args.args[0]
        self.assertIn('--player-id', command)
        self.assertIn('15', command)

    def test_choose_player_with_overseer_retries_invalid_player_id(self):
        args = SimpleNamespace(
            message_window=12,
            model='opencode-go/deepseek-v4-flash',
            opencode_server='http://127.0.0.1:4040',
            opencode_password=None,
        )
        manifest = {
            'llm_players': [
                {'llm_player': {'id': 13, 'label': 'Audit Player 1'}, 'character': {'name': 'Thorin Ironbeard'}},
                {'llm_player': {'id': 14, 'label': 'Audit Player 2'}, 'character': {'name': 'Seraphina Duskweaver'}},
            ],
        }
        session = {'id': 6, 'messages': []}

        with patch.object(autonomous, 'build_overseer_context', return_value={'roster': []}), \
                patch.object(autonomous, 'request_overseer_decision', side_effect=[
                    ({'action': 'choose_player', 'llm_player_id': 17}, '{"action":"choose_player","llm_player_id":17}', 0),
                    ({'action': 'choose_player', 'llm_player_id': 14}, '{"action":"choose_player","llm_player_id":14}', 0),
                ]) as request_overseer_decision:
            selection = autonomous.choose_player_with_overseer(args, manifest, session)

        self.assertEqual(selection['llm_player_id'], 14)
        self.assertEqual(selection['validation_retry_count'], 1)
        self.assertEqual(request_overseer_decision.call_count, 2)

    def test_choose_player_with_overseer_threads_last_dm_turn_into_context(self):
        args = SimpleNamespace(
            message_window=12,
            model='opencode-go/deepseek-v4-flash',
            opencode_server='http://127.0.0.1:4040',
            opencode_password=None,
        )
        manifest = {'llm_players': [{'llm_player': {'id': 7, 'label': 'Auto Player 1'}, 'character': {'name': 'Aria'}}]}
        session = {'id': 99, 'messages': []}
        last_dm_turn = {'status': 'silent', 'player_message_id': 4321, 'reason': 'letting players drive'}

        with patch.object(autonomous, 'build_overseer_context', return_value={'roster': []}) as build_context, \
                patch.object(autonomous, 'request_overseer_decision', return_value=({'action': 'no_action'}, '{}', 0)):
            autonomous.choose_player_with_overseer(args, manifest, session, last_dm_turn=last_dm_turn)

        build_context.assert_called_once()
        kwargs = build_context.call_args.kwargs
        self.assertEqual(kwargs.get('last_dm_turn'), last_dm_turn)

    def test_find_latest_player_message_id_returns_latest_player_message(self):
        posted = [
            {'id': 101, 'role': 'dm', 'content': 'A gust rattles the shutters.'},
            {'id': 102, 'role': 'player', 'content': 'I steady the door.'},
        ]
        self.assertEqual(autonomous.find_latest_player_message_id(posted), 102)

    def test_find_latest_player_message_id_skips_non_player_messages(self):
        posted = [{'id': 9001, 'role': 'system', 'content': 'ping'}]
        self.assertIsNone(autonomous.find_latest_player_message_id(posted))

    def test_find_latest_player_message_id_returns_none_for_empty_input(self):
        self.assertIsNone(autonomous.find_latest_player_message_id(None))
        self.assertIsNone(autonomous.find_latest_player_message_id([]))

    def test_wait_for_dm_response_returns_status_when_dm_responds(self):
        args = SimpleNamespace(poll_interval=0.01, dm_response_timeout=5)
        manifest = {
            'api_base': 'http://127.0.0.1:5889',
            'owner': {'token': 'tok', 'api_key': None},
            'session': {'id': 7},
        }
        dm_status = {'status': 'silent', 'player_message_id': 555, 'reason': 'let the scene breathe'}

        with patch.object(autonomous, 'fetch_dm_turn_status', return_value=dm_status) as fetch_status:
            result, timed_out = autonomous.wait_for_dm_response(args, manifest, 555)

        self.assertFalse(timed_out)
        self.assertEqual(result, dm_status)
        fetch_status.assert_called_once_with(manifest, 555)

    def test_wait_for_dm_response_waits_for_post_turn_completion(self):
        args = SimpleNamespace(poll_interval=0.01, dm_response_timeout=5)
        manifest = {
            'api_base': 'http://127.0.0.1:5889',
            'owner': {'token': 'tok', 'api_key': None},
            'session': {'id': 7},
        }
        statuses = [
            {
                'status': 'speak',
                'player_message_id': 555,
                'dm_message_id': 556,
                'post_turn_complete': False,
                'post_turn_status': 'pending',
            },
            {
                'status': 'speak',
                'player_message_id': 555,
                'dm_message_id': 556,
                'post_turn_complete': True,
                'post_turn_status': 'complete',
            },
        ]

        with patch.object(autonomous, 'fetch_dm_turn_status', side_effect=statuses) as fetch_status, \
                patch.object(autonomous.time, 'sleep') as sleep:
            result, timed_out = autonomous.wait_for_dm_response(args, manifest, 555)

        self.assertFalse(timed_out)
        self.assertEqual(result, statuses[-1])
        self.assertEqual(fetch_status.call_count, 2)
        sleep.assert_called_once()

    def test_wait_for_dm_response_times_out_when_dm_never_responds(self):
        args = SimpleNamespace(poll_interval=0.05, dm_response_timeout=0.0)
        manifest = {
            'api_base': 'http://127.0.0.1:5889',
            'owner': {'token': 'tok', 'api_key': None},
            'session': {'id': 7},
        }
        pending = {'status': 'pending', 'player_message_id': 555}

        with patch.object(autonomous, 'fetch_dm_turn_status', return_value=pending):
            result, timed_out = autonomous.wait_for_dm_response(args, manifest, 555)

        self.assertTrue(timed_out)
        self.assertEqual(result, pending)

    def test_ensure_manifest_session_started_keeps_existing_session(self):
        args = SimpleNamespace(session_start_timeout=180)
        manifest = {
            'api_base': 'http://127.0.0.1:5889',
            'campaign': {'id': 12},
            'owner': {'token': 'tok', 'api_key': None},
            'session': {'id': 44},
        }

        with patch.object(autonomous, 'start_session') as start_session, \
                patch.object(autonomous, 'save_manifest') as save_manifest:
            result = autonomous.ensure_manifest_session_started(args, pathlib.Path('campaign.json'), manifest)

        self.assertEqual(result['session']['id'], 44)
        start_session.assert_not_called()
        save_manifest.assert_not_called()

    def test_ensure_manifest_session_started_starts_prestart_manifest(self):
        args = SimpleNamespace(session_start_timeout=180)
        manifest = {
            'api_base': 'http://127.0.0.1:5889',
            'campaign': {'id': 12},
            'owner': {'token': 'tok', 'api_key': None},
            'session': {'id': None},
            'bootstrap_state': 'prestart',
        }

        with patch.object(autonomous, 'start_session', return_value={'id': 77}) as start_session, \
                patch.object(autonomous, 'save_manifest') as save_manifest, \
                patch.object(autonomous, 'print_event') as print_event:
            result = autonomous.ensure_manifest_session_started(args, pathlib.Path('campaign.json'), manifest)

        self.assertEqual(result['session']['id'], 77)
        self.assertEqual(result['bootstrap_state'], 'started')
        start_session.assert_called_once_with(
            'http://127.0.0.1:5889',
            12,
            owner_token='tok',
            api_key=None,
            timeout=180,
        )
        save_manifest.assert_called_once()
        print_event.assert_called_once()


if __name__ == '__main__':
    unittest.main()

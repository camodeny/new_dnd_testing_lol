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
    def test_parse_args_defaults_to_no_wall_clock_cap(self):
        with patch.object(sys, 'argv', ['run_autonomous_llm_campaign.py']):
            args = autonomous.parse_args()

        self.assertIsNone(args.max_minutes)

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

    def test_field_proposal_priority_ranks_state_critical_fields(self):
        self.assertLess(
            autonomous.field_proposal_priority('current_hp'),
            autonomous.field_proposal_priority('death_save_failures'),
        )
        self.assertLess(
            autonomous.field_proposal_priority('death_save_failures'),
            autonomous.field_proposal_priority('condition:Poisoned'),
        )
        self.assertLess(
            autonomous.field_proposal_priority('condition:Unconscious'),
            autonomous.field_proposal_priority('spell_slots_used_3'),
        )
        self.assertLess(
            autonomous.field_proposal_priority('spell_slots_used_1'),
            autonomous.field_proposal_priority('equipment:Shortsword'),
        )
        self.assertIsNone(autonomous.field_proposal_priority('experience_points'))
        self.assertIsNone(autonomous.field_proposal_priority('gp'))
        self.assertIsNone(autonomous.field_proposal_priority(None))

    def test_proposal_state_priority_returns_most_urgent_change(self):
        proposal = {
            'id': 1,
            'changes': [
                {'field': 'equipment:Shortsword', 'after': {'count': 0}},
                {'field': 'current_hp', 'after': 0},
            ],
        }
        self.assertEqual(
            autonomous.proposal_state_priority(proposal),
            autonomous.field_proposal_priority('current_hp'),
        )

    def test_proposal_state_priority_none_for_non_critical(self):
        self.assertIsNone(autonomous.proposal_state_priority({'changes': [{'field': 'gp', 'after': 50}]}))
        self.assertIsNone(autonomous.proposal_state_priority({'changes': []}))
        self.assertIsNone(autonomous.proposal_state_priority({}))

    def test_select_priority_proposal_prefers_hp_over_resource_depletion(self):
        pending = [
            {'id': 11, 'created_at': '2026-07-01T10:00:00Z', 'changes': [{'field': 'spell_slots_used_1', 'after': 2}]},
            {'id': 10, 'created_at': '2026-07-01T10:00:00Z', 'changes': [{'field': 'current_hp', 'after': 0}]},
        ]
        self.assertEqual(autonomous.select_priority_proposal(pending)['id'], 10)

    def test_select_priority_proposal_breaks_ties_by_created_at_and_id(self):
        pending = [
            {'id': 21, 'created_at': '2026-07-01T10:01:00Z', 'changes': [{'field': 'condition:Poisoned', 'after': {'count': 1}}]},
            {'id': 20, 'created_at': '2026-07-01T10:00:00Z', 'changes': [{'field': 'condition:Poisoned', 'after': {'count': 1}}]},
        ]
        self.assertEqual(autonomous.select_priority_proposal(pending)['id'], 20)

    def test_select_priority_proposal_skips_non_state_critical(self):
        pending = [{'id': 30, 'changes': [{'field': 'gp', 'after': 5}]}]
        self.assertIsNone(autonomous.select_priority_proposal(pending))
        self.assertIsNone(autonomous.select_priority_proposal([]))
        self.assertIsNone(autonomous.select_priority_proposal(None))

    def test_find_priority_proposal_llm_player_matches_owning_character(self):
        manifest = {
            'llm_players': [
                {'llm_player': {'id': 4, 'label': 'Auto Player 1'}, 'character': {'id': 40, 'name': 'Mira Vell'}},
                {'llm_player': {'id': 5, 'label': 'Auto Player 2'}, 'character': {'id': 50, 'name': 'Toren Oakenshield'}},
            ],
        }
        session = {
            'pending_sheet_proposals': [
                {'id': 1, 'character_id': 40, 'changes': [{'field': 'current_hp', 'after': 0}]},
            ],
        }
        entry, proposal = autonomous.find_priority_proposal_llm_player(manifest, session)
        self.assertEqual(entry['llm_player']['id'], 4)
        self.assertEqual(proposal['id'], 1)

    def test_find_priority_proposal_llm_player_returns_none_without_owner(self):
        manifest = {
            'llm_players': [
                {'llm_player': {'id': 5, 'label': 'Auto Player 2'}, 'character': {'id': 50, 'name': 'Toren Oakenshield'}},
            ],
        }
        session = {
            'pending_sheet_proposals': [
                {'id': 1, 'character_id': 40, 'changes': [{'field': 'current_hp', 'after': 0}]},
            ],
        }
        entry, proposal = autonomous.find_priority_proposal_llm_player(manifest, session)
        self.assertIsNone(entry)
        self.assertIsNone(proposal)

    def test_run_orchestrator_passes_proposal_only_flag(self):
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
                stdout=json.dumps({'decision': {'action': 'apply_proposal', 'proposal_id': 1}}),
                stderr='',
            )

            autonomous.run_orchestrator(args, pathlib.Path('automation/state/test.json'), player_id=4, proposal_only=True)

        command = subprocess_run.call_args.args[0]
        self.assertIn('--proposal-only', command)

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
        args = SimpleNamespace(poll_interval=0.01, dm_response_timeout=5,
                               dm_visible_response_timeout=None,
                               dm_post_turn_timeout=None)
        manifest = {
            'api_base': 'http://127.0.0.1:5889',
            'owner': {'token': 'tok', 'api_key': None},
            'session': {'id': 7},
        }
        dm_status = {'status': 'silent', 'player_message_id': 555, 'reason': 'let the scene breathe'}

        with patch.object(autonomous, 'fetch_dm_turn_status', return_value=dm_status) as fetch_status:
            result, timed_out, timeout_phase = autonomous.wait_for_dm_response(args, manifest, 555)

        self.assertFalse(timed_out)
        self.assertIsNone(timeout_phase)
        self.assertEqual(result, dm_status)
        self.assertEqual(fetch_status.call_count, 2)

    def test_wait_for_dm_response_waits_for_post_turn_completion(self):
        args = SimpleNamespace(poll_interval=0.01, dm_response_timeout=5,
                               dm_visible_response_timeout=None,
                               dm_post_turn_timeout=None)
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
            result, timed_out, timeout_phase = autonomous.wait_for_dm_response(args, manifest, 555)

        self.assertFalse(timed_out)
        self.assertIsNone(timeout_phase)
        self.assertEqual(result, statuses[-1])
        self.assertEqual(fetch_status.call_count, 2)
        sleep.assert_not_called()

    def test_wait_for_dm_response_times_out_when_dm_never_responds(self):
        args = SimpleNamespace(poll_interval=0.05, dm_response_timeout=0.0,
                               dm_visible_response_timeout=None,
                               dm_post_turn_timeout=None)
        manifest = {
            'api_base': 'http://127.0.0.1:5889',
            'owner': {'token': 'tok', 'api_key': None},
            'session': {'id': 7},
        }
        pending = {'status': 'pending', 'player_message_id': 555}

        with patch.object(autonomous, 'fetch_dm_turn_status', return_value=pending):
            result, timed_out, timeout_phase = autonomous.wait_for_dm_response(args, manifest, 555)

        self.assertTrue(timed_out)
        self.assertEqual(timeout_phase, 'visible')
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

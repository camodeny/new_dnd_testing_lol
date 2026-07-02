import contextlib
import io
import itertools
import json
import os
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import automationctl


def run_event_payload(run_id, run_status, event_id, event_type, event_payload=None, delta=None):
    return {
        'type': 'run_event',
        'run_id': run_id,
        'run': {
            'id': run_id,
            'status': run_status,
            'derived_campaign_id': 100005,
            'last_event_sequence': event_id,
        },
        'event': {
            'id': event_id,
            'event_type': event_type,
            'payload': event_payload or {},
        },
        'delta': delta or {},
    }


class AutomationCtlTests(unittest.TestCase):
    def invoke(self, argv):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = automationctl.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_campaign_create_bootstraps_manifest_and_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = pathlib.Path(tmpdir) / 'campaign.json'
            campaign = {'id': 21, 'name': 'Fresh Campaign', 'description': 'A new run'}
            brief = {'seed': 'abc123'}
            owner = {'id': 9, 'username': 'owner-user'}
            llm_player_responses = [
                {
                    'llm_player': {'id': 301, 'label': 'Auto Player 1'},
                    'character': {'id': 401, 'name': 'Aria'},
                    'api_key': 'llm-key-1',
                },
                {
                    'llm_player': {'id': 302, 'label': 'Auto Player 2'},
                    'character': {'id': 402, 'name': 'Bryn'},
                    'api_key': 'llm-key-2',
                },
            ]
            with patch.object(automationctl, 'api_get', return_value={'user': owner}) as api_get, \
                    patch.object(automationctl, 'api_post', side_effect=[
                        {'campaign': campaign, 'brief': brief},
                        llm_player_responses[0],
                        llm_player_responses[1],
                    ]) as api_post, \
                    patch.object(automationctl, 'api_put_with_key', return_value={'campaign': campaign}) as api_put_with_key, \
                    patch.object(automationctl, 'start_session', return_value={'id': 77}) as start_session:
                code, stdout, stderr = self.invoke([
                    'campaign', 'create',
                    '--campaign-name', 'CLI Seed Campaign',
                    '--llm-count', '2',
                    '--manifest', str(manifest_path),
                    '--owner-api-key', 'owner-key',
                ])
            manifest_payload = json.loads(manifest_path.read_text(encoding='utf-8'))

        self.assertEqual(code, 0)
        self.assertEqual(stderr, '')
        payload = json.loads(stdout)
        self.assertEqual(payload['campaign']['id'], 21)
        self.assertEqual(payload['session']['id'], 77)
        self.assertEqual(payload['bootstrap_state'], 'started')
        self.assertEqual(payload['manifest_path'], str(manifest_path))
        self.assertEqual([entry['llm_player']['id'] for entry in payload['llm_players']], [301, 302])

        api_get.assert_called_once_with(
            automationctl.default_api_base(),
            '/api/me',
            owner_token=None,
            api_key='owner-key',
        )
        self.assertEqual(api_post.call_args_list[0].args[1], '/api/campaigns/quick-create')
        self.assertEqual(api_post.call_args_list[0].args[2], {
            'seed': None,
            'difficulty': None,
            'required_players': 2,
            'loot_mode': 'rare_quality',
        })
        self.assertEqual(api_post.call_args_list[1].args[1], '/api/campaigns/21/llm-players')
        self.assertEqual(api_post.call_args_list[2].args[1], '/api/campaigns/21/llm-players')
        api_put_with_key.assert_any_call(
            automationctl.default_api_base(),
            '/api/campaigns/21',
            {'name': 'CLI Seed Campaign', 'description': 'A new run'},
            api_key='owner-key',
        )
        api_put_with_key.assert_any_call(
            automationctl.default_api_base(),
            '/api/campaigns/21/members/9',
            {'role': 'spectator'},
            api_key='owner-key',
        )
        start_session.assert_called_once_with(
            automationctl.default_api_base(),
            21,
            owner_token=None,
            api_key='owner-key',
            timeout=automationctl.default_session_start_timeout(),
        )
        self.assertEqual(manifest_payload['campaign']['id'], 21)
        self.assertEqual(manifest_payload['session']['id'], 77)
        self.assertEqual(manifest_payload['turn_order'], [301, 302])

    def test_campaign_create_no_start_writes_prestart_manifest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = pathlib.Path(tmpdir) / 'campaign.json'
            campaign = {'id': 22, 'name': 'Fresh Campaign', 'description': 'Later'}
            brief = {'seed': 'def456'}
            owner = {'id': 10, 'username': 'owner-user'}
            llm_player = {
                'llm_player': {'id': 303, 'label': 'Auto Player 1'},
                'character': {'id': 403, 'name': 'Cato'},
                'api_key': 'llm-key-3',
            }
            with patch.object(automationctl, 'api_get', return_value={'user': owner}), \
                    patch.object(automationctl, 'api_post', side_effect=[
                        {'campaign': campaign, 'brief': brief},
                        llm_player,
                    ]), \
                    patch.object(automationctl, 'api_put_with_key', return_value={'campaign': campaign}), \
                    patch.object(automationctl, 'start_session') as start_session:
                code, stdout, stderr = self.invoke([
                    'campaign', 'create',
                    '--llm-count', '1',
                    '--manifest', str(manifest_path),
                    '--no-start',
                    '--owner-api-key', 'owner-key',
                ])
            manifest_payload = json.loads(manifest_path.read_text(encoding='utf-8'))

        self.assertEqual(code, 0)
        self.assertEqual(stderr, '')
        payload = json.loads(stdout)
        self.assertIsNone(payload['session'])
        self.assertEqual(payload['bootstrap_state'], 'prestart')
        start_session.assert_not_called()
        self.assertIsNone(manifest_payload['session']['id'])
        self.assertEqual(manifest_payload['bootstrap_state'], 'prestart')

    def test_run_status_prints_json_object(self):
        run_payload = {'run': {'id': 7, 'status': 'running', 'derived_campaign_id': 100003}}
        with patch.object(automationctl, 'api_get', return_value=run_payload):
            code, stdout, stderr = self.invoke(['run', 'status', '--run-id', '7', '--owner-api-key', 'owner'])

        self.assertEqual(code, 0)
        self.assertEqual(stderr, '')
        self.assertEqual(json.loads(stdout), run_payload)

    def test_run_wait_returns_immediately_for_existing_after_dm_pause(self):
        run_payload = {
            'run': {
                'id': 4,
                'status': 'awaiting_audit',
                'derived_campaign_id': 100005,
                'last_event_sequence': 8,
            },
            'current_audit_cycle': {
                'id': 2,
                'phase': 'after_dm',
                'player_message_id': 417,
                'dm_message_id': 418,
            },
        }
        with patch.object(automationctl, 'api_get', return_value=run_payload), \
                patch.object(automationctl, 'iter_sse_messages') as iter_sse_messages:
            code, stdout, _ = self.invoke(['run', 'wait', '--run-id', '4', '--owner-api-key', 'owner'])

        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload['result'], 'matched')
        self.assertEqual(payload['matched_condition'], 'after_dm')
        self.assertEqual(payload['audit_cycle_id'], 2)
        iter_sse_messages.assert_not_called()

    def test_run_wait_unblocks_on_player_message_posted(self):
        initial = {'run': {'id': 9, 'status': 'running', 'last_event_id': 21, 'derived_campaign_id': 100004}}
        messages = iter([{
            'event_id': 22,
            'payload': run_event_payload(
                9,
                'running',
                22,
                'player_message_posted',
                event_payload={'message': {'id': 501}},
            ),
        }])
        with patch.object(automationctl, 'api_get', return_value=initial), \
                patch.object(automationctl, 'iter_sse_messages', return_value=messages):
            code, stdout, _ = self.invoke([
                'run', 'wait', '--run-id', '9', '--wait-for', 'after_player', '--owner-api-key', 'owner',
            ])

        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload['matched_condition'], 'after_player')
        self.assertEqual(payload['player_message_id'], 501)
        self.assertEqual(payload['event_type'], 'player_message_posted')

    def test_run_wait_ignores_dm_turn_status_until_audit_pause(self):
        initial = {'run': {'id': 10, 'status': 'running', 'last_event_id': 30, 'derived_campaign_id': 100005}}
        messages = iter([
            {
                'event_id': 31,
                'payload': run_event_payload(
                    10,
                    'running',
                    31,
                    'dm_turn_status',
                    event_payload={'status': 'speak', 'player_message_id': 601, 'dm_message_id': 602},
                ),
            },
            {
                'event_id': 32,
                'payload': run_event_payload(
                    10,
                    'awaiting_audit',
                    32,
                    'audit_cycle_paused',
                    event_payload={
                        'phase': 'after_dm',
                        'audit_cycle': {
                            'id': 7,
                            'phase': 'after_dm',
                            'player_message_id': 601,
                            'dm_message_id': 602,
                        },
                    },
                    delta={
                        'current_audit_cycle': {
                            'id': 7,
                            'phase': 'after_dm',
                            'player_message_id': 601,
                            'dm_message_id': 602,
                        },
                    },
                ),
            },
        ])
        with patch.object(automationctl, 'api_get', return_value=initial), \
                patch.object(automationctl, 'iter_sse_messages', return_value=messages):
            code, stdout, _ = self.invoke([
                'run', 'wait', '--run-id', '10', '--wait-for', 'after_dm', '--owner-api-key', 'owner',
            ])

        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload['matched_condition'], 'after_dm')
        self.assertEqual(payload['audit_cycle_id'], 7)
        self.assertEqual(payload['dm_message_id'], 602)
        self.assertEqual(payload['event_type'], 'audit_cycle_paused')

    def test_run_wait_unblocks_on_awaiting_audit_transition(self):
        initial = {'run': {'id': 11, 'status': 'running', 'last_event_id': 40, 'derived_campaign_id': 100005}}
        messages = iter([{
            'event_id': 41,
            'payload': run_event_payload(
                11,
                'awaiting_audit',
                41,
                'audit_cycle_paused',
                event_payload={
                    'phase': 'after_dm',
                    'audit_cycle': {
                        'id': 6,
                        'phase': 'after_dm',
                        'player_message_id': 700,
                        'dm_message_id': 701,
                    },
                },
                delta={
                    'current_audit_cycle': {
                        'id': 6,
                        'phase': 'after_dm',
                        'player_message_id': 700,
                        'dm_message_id': 701,
                    },
                },
            ),
        }])
        with patch.object(automationctl, 'api_get', return_value=initial), \
                patch.object(automationctl, 'iter_sse_messages', return_value=messages):
            code, stdout, _ = self.invoke([
                'run', 'wait', '--run-id', '11', '--wait-for', 'after_dm', '--owner-api-key', 'owner',
            ])

        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload['audit_cycle_id'], 6)
        self.assertEqual(payload['matched_condition'], 'after_dm')

    def test_run_wait_ignores_pings_and_unrelated_events(self):
        initial = {'run': {'id': 12, 'status': 'running', 'last_event_id': 50, 'derived_campaign_id': 100005}}
        messages = iter([
            None,
            {
                'event_id': 51,
                'payload': run_event_payload(12, 'running', 51, 'run_scorecard_updated'),
            },
            {
                'event_id': 52,
                'payload': run_event_payload(
                    12,
                    'running',
                    52,
                    'player_message_posted',
                    event_payload={'message': {'id': 800}},
                ),
            },
        ])
        with patch.object(automationctl, 'api_get', return_value=initial), \
                patch.object(automationctl, 'iter_sse_messages', return_value=messages):
            code, stdout, _ = self.invoke([
                'run', 'wait', '--run-id', '12', '--wait-for', 'after_player', '--owner-api-key', 'owner',
            ])

        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload['player_message_id'], 800)
        self.assertEqual(payload['matched_condition'], 'after_player')

    def test_run_wait_times_out(self):
        initial = {'run': {'id': 13, 'status': 'running', 'last_event_id': 60, 'derived_campaign_id': 100005}}
        with patch.object(automationctl, 'api_get', return_value=initial), \
                patch.object(automationctl, 'iter_sse_messages', return_value=itertools.repeat(None)), \
                patch.object(automationctl.time, 'monotonic', side_effect=[0, 1, 3]):
            code, stdout, _ = self.invoke([
                'run', 'wait', '--run-id', '13', '--wait-for', 'after_dm', '--timeout-seconds', '2', '--owner-api-key', 'owner',
            ])

        self.assertEqual(code, automationctl.EXIT_TIMEOUT)
        payload = json.loads(stdout)
        self.assertEqual(payload['result'], 'timeout')

    def test_run_wait_returns_terminal_before_target(self):
        initial = {'run': {'id': 14, 'status': 'running', 'last_event_id': 70, 'derived_campaign_id': 100005}}
        messages = iter([{
            'event_id': 71,
            'payload': run_event_payload(14, 'failed', 71, 'run_completed', event_payload={'status': 'failed'}),
        }])
        with patch.object(automationctl, 'api_get', return_value=initial), \
                patch.object(automationctl, 'iter_sse_messages', return_value=messages):
            code, stdout, _ = self.invoke([
                'run', 'wait', '--run-id', '14', '--wait-for', 'after_dm', '--owner-api-key', 'owner',
            ])

        self.assertEqual(code, automationctl.EXIT_TERMINAL_BEFORE_TARGET)
        payload = json.loads(stdout)
        self.assertEqual(payload['result'], 'terminal_before_target')
        self.assertEqual(payload['run_status'], 'failed')

    def test_scorecard_create_posts_input_payload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            payload_path = pathlib.Path(tmpdir) / 'scorecard.json'
            payload = {
                'name': 'Audit v1',
                'description': 'Runtime-truth grading',
                'instructions': 'Use runtime truth',
                'criteria': [{'id': 'memory_quality', 'label': 'Memory Quality'}],
                'defaults': {'pause_phases': ['after_dm']},
            }
            payload_path.write_text(json.dumps(payload), encoding='utf-8')
            with patch.object(automationctl, 'api_post', return_value={'scorecard': {'id': 2}}) as api_post:
                code, stdout, _ = self.invoke([
                    'scorecard', 'create', '--input-file', str(payload_path), '--owner-api-key', 'owner',
                ])

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout), {'scorecard': {'id': 2}})
        api_post.assert_called_once_with(
            automationctl.default_api_base(),
            '/api/automation/scorecards',
            payload,
            api_key='owner',
        )

    def test_scenario_snapshot_and_run_commands_use_expected_routes(self):
        with patch.object(automationctl, 'api_post', side_effect=[
            {'scenario': {'id': 2}},
            {'snapshot': {'id': 3}},
            {'run': {'id': 4}},
        ]) as api_post:
            scenario_code, _, _ = self.invoke([
                'scenario', 'create',
                '--source-campaign-id', '100002',
                '--name', 'Fresh Campaign Runtime Truth Audit',
                '--owner-api-key', 'owner',
            ])
            snapshot_code, _, _ = self.invoke([
                'snapshot', 'create',
                '--scenario-id', '2',
                '--label', 'Pre-audit snapshot',
                '--owner-api-key', 'owner',
            ])
            run_code, _, _ = self.invoke([
                'run', 'start',
                '--scenario-id', '2',
                '--snapshot-id', '3',
                '--owner-api-key', 'owner',
            ])

        self.assertEqual((scenario_code, snapshot_code, run_code), (0, 0, 0))
        self.assertEqual(api_post.call_args_list[0].args[1], '/api/automation/scenarios')
        self.assertEqual(api_post.call_args_list[1].args[1], '/api/automation/scenarios/2/snapshots')
        self.assertEqual(api_post.call_args_list[2].args[1], '/api/automation/scenarios/2/runs')

    def test_run_audit_and_continue_use_expected_payloads(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            audit_path = pathlib.Path(tmpdir) / 'audit.json'
            audit_payload = {
                'summary': 'Scene state drifted.',
                'notes': 'World state did not move.',
                'scorecard': {'overall_status': 'fail'},
            }
            audit_path.write_text(json.dumps(audit_payload), encoding='utf-8')

            with patch.object(automationctl, 'api_post', side_effect=[
                {'audit_cycle': {'id': 2}},
                {'run': {'id': 4, 'status': 'running'}},
            ]) as api_post:
                audit_code, _, _ = self.invoke([
                    'run', 'audit',
                    '--run-id', '4',
                    '--cycle-id', '2',
                    '--input-file', str(audit_path),
                    '--owner-api-key', 'owner',
                ])
                continue_code, _, _ = self.invoke([
                    'run', 'continue',
                    '--run-id', '4',
                    '--force',
                    '--owner-api-key', 'owner',
                ])

        self.assertEqual((audit_code, continue_code), (0, 0))
        self.assertEqual(api_post.call_args_list[0].args[1], '/api/automation/runs/4/audit-cycles/2/audit')
        self.assertEqual(api_post.call_args_list[0].args[2], audit_payload)
        self.assertEqual(api_post.call_args_list[1].args[1], '/api/automation/runs/4/continue')
        self.assertEqual(api_post.call_args_list[1].args[2], {'force': True})

    def test_run_scorecard_and_compare_commands(self):
        with patch.object(automationctl, 'api_get', return_value={'scorecard': []}) as api_get, \
                patch.object(automationctl, 'api_post', return_value={'comparisons': []}) as api_post:
            scorecard_code, _, _ = self.invoke([
                'run', 'scorecard', '--run-id', '4', '--owner-api-key', 'owner',
            ])
            compare_code, _, _ = self.invoke([
                'run', 'compare', '--left-run-id', '4', '--right-run-id', '5', '--owner-api-key', 'owner',
            ])

        self.assertEqual((scorecard_code, compare_code), (0, 0))
        api_get.assert_called_once_with(
            automationctl.default_api_base(),
            '/api/automation/runs/4/scorecard',
            api_key='owner',
        )
        api_post.assert_called_once_with(
            automationctl.default_api_base(),
            '/api/automation/compare',
            {'left_run_id': 4, 'right_run_id': 5},
            api_key='owner',
        )

    def test_worker_start_delegates_to_existing_worker_entrypoint(self):
        completed = SimpleNamespace(returncode=0)
        with patch.object(automationctl.subprocess, 'run', return_value=completed) as run_process:
            code, stdout, _ = self.invoke([
                'worker', 'start',
                '--run-id', '4',
                '--poll-interval', '2',
                '--dm-response-timeout', '180',
                '--owner-api-key', 'owner',
            ])

        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload['result'], 'worker_exited')
        command = run_process.call_args.args[0]
        self.assertIn(str(automationctl.WORKER_ENTRYPOINT), command)
        self.assertIn('--run-id', command)
        self.assertIn('4', command)
        self.assertIn('--dm-response-timeout', command)
        self.assertIn('180.0', command)


if __name__ == '__main__':
    unittest.main()

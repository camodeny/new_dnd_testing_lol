import pathlib
import sys
import unittest
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import run_llm_campaign_orchestrator as orchestrator


class RunLlmCampaignOrchestratorTests(unittest.TestCase):
    def test_execute_player_roll_formats_advantage_roll(self):
        with patch.object(orchestrator.random, 'randint', side_effect=[4, 17]):
            roll = orchestrator.execute_player_roll(
                'Arcana check (Advantage)',
                '2d20kh1+5',
            )

        self.assertEqual(roll['total'], 22)
        self.assertEqual(roll['modifier'], 5)
        self.assertEqual(roll['sides'], 20)
        self.assertEqual(roll['rolls'], [4, 17])
        self.assertEqual(roll['kept'], [17])
        self.assertEqual(
            roll['message'],
            '[Roll: Arcana check (Advantage)] total: 22 | rolls: 4, 17 | mod: 5 | sides: 20',
        )

    def test_choose_next_player_stays_on_same_llm_player_for_pending_roll(self):
        manifest = {
            'turn_order': [4, 5],
            'llm_players': [
                {'llm_player': {'id': 4, 'user_id': 6, 'label': 'Auto Player 1'}},
                {'llm_player': {'id': 5, 'user_id': 7, 'label': 'Auto Player 2'}},
            ],
        }
        session = {
            'messages': [
                {'role': 'player', 'user_id': 6, 'content': 'I inspect the runes.'},
                {'role': 'dm', 'content': 'Elara, the lens pulses with a faint inner light. Make an **Arcana check** to read what is wrong.'},
            ],
        }

        chosen = orchestrator.choose_next_player(manifest, session)

        self.assertEqual(chosen['llm_player']['id'], 4)

    def test_choose_next_player_rotates_without_pending_roll_prompt(self):
        manifest = {
            'turn_order': [4, 5],
            'llm_players': [
                {'llm_player': {'id': 4, 'user_id': 6, 'label': 'Auto Player 1'}},
                {'llm_player': {'id': 5, 'user_id': 7, 'label': 'Auto Player 2'}},
            ],
        }
        session = {
            'messages': [
                {'role': 'player', 'user_id': 6, 'content': 'I inspect the runes.'},
                {'role': 'dm', 'content': 'The foreman kicks open the panel and waits for your answer.'},
            ],
        }

        chosen = orchestrator.choose_next_player(manifest, session)

        self.assertEqual(chosen['llm_player']['id'], 5)

    def test_find_pending_proposal_player_uses_turn_order(self):
        manifest = {
            'turn_order': [4, 5],
            'llm_players': [
                {'llm_player': {'id': 4, 'user_id': 6, 'label': 'Auto Player 1'}, 'api_key': 'key-1'},
                {'llm_player': {'id': 5, 'user_id': 7, 'label': 'Auto Player 2'}, 'api_key': 'key-2'},
            ],
        }

        with patch.object(orchestrator, 'get_pending_sheet_proposals', side_effect=[
            [],
            [{'id': 19, 'character_id': 10, 'changes': [{'field': 'current_hp', 'after': 21}]}],
        ]):
            chosen, proposals = orchestrator.find_pending_proposal_player(manifest, 'http://example.test', 7)

        self.assertEqual(chosen['llm_player']['id'], 5)
        self.assertEqual(proposals[0]['id'], 19)

    def test_build_prompt_includes_pending_sheet_proposals(self):
        manifest = {
            'llm_players': [
                {
                    'llm_player': {'id': 4, 'label': 'Auto Player 1'},
                    'character': {'name': 'Seraphina Duskweaver'},
                },
            ],
        }
        campaign = {'id': 6, 'name': 'Copperhollow Under Mirror'}
        world_payload = {'world': {'current_scene': {'location_name': 'The Hanging Switchyard'}}}
        session = {'id': 5, 'started_at': '2026-06-19T23:00:00Z', 'messages': []}
        chosen_player = manifest['llm_players'][0]
        pending_proposals = [{'id': 44, 'reason': 'Gain 3 HP', 'changes': [{'field': 'current_hp', 'after': 21}]}]

        prompt = orchestrator.build_prompt(
            manifest,
            campaign,
            world_payload,
            session,
            chosen_player,
            pending_proposals,
            16,
        )

        self.assertIn('"pending_sheet_proposals"', prompt)
        self.assertIn('"id": 44', prompt)

    def test_execute_player_decision_applies_pending_proposal(self):
        chosen_player = {
            'llm_player': {'id': 5, 'user_id': 7, 'label': 'Auto Player 2'},
            'character': {'name': 'Seraphina Duskweaver'},
            'api_key': 'key-2',
        }
        decision = {'action': 'apply_proposal', 'proposal_id': 44}
        pending_proposals = [{'id': 44, 'character_id': 10}]

        with patch.object(orchestrator, 'api_post', return_value={'proposal': {'id': 44, 'status': 'applied'}}) as api_post:
            result = orchestrator.execute_player_decision(
                'http://example.test',
                6,
                5,
                chosen_player,
                decision,
                pending_proposals,
                dry_run=False,
            )

        self.assertEqual(result['proposal_action']['action'], 'apply_proposal')
        self.assertEqual(result['proposal_result']['proposal']['status'], 'applied')
        self.assertEqual(
            api_post.call_args.args[1],
            '/api/sessions/5/proposals/44/apply',
        )

    def test_execute_player_decision_rejects_non_pending_proposal(self):
        chosen_player = {
            'llm_player': {'id': 5, 'user_id': 7, 'label': 'Auto Player 2'},
            'character': {'name': 'Seraphina Duskweaver'},
            'api_key': 'key-2',
        }

        with self.assertRaisesRegex(RuntimeError, 'is not pending'):
            orchestrator.execute_player_decision(
                'http://example.test',
                6,
                5,
                chosen_player,
                {'action': 'dismiss_proposal', 'proposal_id': 99},
                [{'id': 44, 'character_id': 10}],
                dry_run=False,
            )

    def test_build_proposal_only_prompt_requires_proposal_action(self):
        manifest = {
            'llm_players': [
                {
                    'llm_player': {'id': 4, 'label': 'Auto Player 1'},
                    'character': {'name': 'Mira Vell'},
                },
            ],
        }
        campaign = {'id': 6, 'name': 'Copperhollow Under Mirror'}
        world_payload = {'world': {'world_state': {}}}
        session = {'id': 5, 'started_at': '2026-06-19T23:00:00Z', 'messages': []}
        chosen_player = manifest['llm_players'][0]
        pending_proposals = [{'id': 1, 'changes': [{'field': 'current_hp', 'after': 0}]}]

        prompt = orchestrator.build_proposal_only_prompt(
            manifest,
            campaign,
            world_payload,
            session,
            chosen_player,
            pending_proposals,
            16,
        )

        self.assertIn('apply_proposal', prompt)
        self.assertIn('dismiss_proposal', prompt)
        self.assertIn('Do not speak, roll, or narrate', prompt)

    def test_request_proposal_only_decision_returns_first_proposal_action(self):
        with patch.object(orchestrator, 'request_opencode_decision', return_value=(
            {'action': 'apply_proposal', 'proposal_id': 1},
            '{"action":"apply_proposal","proposal_id":1}',
            0,
        )) as request_opencode_decision:
            decision, response_text, json_retry_count, attempts = orchestrator.request_proposal_only_decision(
                'http://server.test', None, 'model', 'prompt',
            )

        self.assertEqual(decision, {'action': 'apply_proposal', 'proposal_id': 1})
        self.assertEqual(attempts, 1)
        request_opencode_decision.assert_called_once()

    def test_request_proposal_only_decision_retries_non_proposal_action(self):
        with patch.object(orchestrator, 'request_opencode_decision', side_effect=[
            ({'action': 'speak', 'content': 'Mira stirs.'}, '{"action":"speak"}', 0),
            ({'action': 'dismiss_proposal', 'proposal_id': 1}, '{"action":"dismiss_proposal","proposal_id":1}', 0),
        ]) as request_opencode_decision:
            decision, _response, _json_retry, attempts = orchestrator.request_proposal_only_decision(
                'http://server.test', None, 'model', 'prompt',
            )

        self.assertEqual(decision['action'], 'dismiss_proposal')
        self.assertEqual(attempts, 2)
        self.assertEqual(request_opencode_decision.call_count, 2)
        second_prompt = request_opencode_decision.call_args_list[1].args[3]
        self.assertIn('did not resolve', second_prompt)

    def test_request_proposal_only_decision_raises_when_never_resolves(self):
        with patch.object(orchestrator, 'request_opencode_decision', return_value=(
            {'action': 'no_action'},
            '{"action":"no_action"}',
            0,
        )):
            with self.assertRaisesRegex(RuntimeError, 'failed to resolve'):
                orchestrator.request_proposal_only_decision(
                    'http://server.test', None, 'model', 'prompt',
                    max_attempts=2,
                )


if __name__ == '__main__':
    unittest.main()

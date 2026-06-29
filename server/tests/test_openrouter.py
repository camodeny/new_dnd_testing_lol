import json
import os
import sys
import unittest
from unittest.mock import patch

import requests

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from openrouter import (
    _assistant_tool_message,
    _provider_request_payload_options,
    _json_error_excerpt,
    _json_loads_with_error,
    _json_loads_with_repair,
    _post_chat_response,
    SESSION_MEMORY_MAX_ATTEMPTS,
    SESSION_MEMORY_MAX_TOKENS,
    SESSION_MEMORY_TIMEOUT_SECONDS,
    SESSION_PREFLIGHT_MAX_TOKENS,
    SESSION_PREFLIGHT_TIMEOUT_SECONDS,
    build_session_clock_adjudication_messages,
    build_session_dm_tool_messages,
    build_session_memory_clocks_messages,
    build_session_memory_facts_messages,
    build_session_memory_summary_scene_messages,
    build_session_preflight_messages,
    check_session_spoilers_with_llm,
    get_character_sheet_answer,
    get_session_clock_updates,
    get_session_memory_patch,
    get_opening_scene_response,
    get_planning_dm_response,
    get_planning_dm_response_streaming,
    get_session_preflight_decision,
    get_world_genesis_package,
    normalize_session_preflight_decision,
    normalize_session_spoiler_check,
)


class OpenRouterJsonRepairTest(unittest.TestCase):
    def test_session_memory_patch_uses_bounded_best_effort_request(self):
        with patch('openrouter.get_llm_provider', return_value='openrouter'), patch(
            'openrouter._post_chat',
            return_value='{"running_summary":"Mortimer saw a dark figure near the vault.","scene_patch":{}}',
        ) as post_chat:
            patch_data = get_session_memory_patch({
                'latest_player_message': 'What did Mortimer see?',
                'latest_dm_response': 'He saw a dark figure near the vault.',
            })

        self.assertIn('_telemetry', patch_data)
        self.assertFalse(post_chat.call_args.kwargs['allow_thinking'])
        self.assertEqual(post_chat.call_args.kwargs['timeout_seconds'], SESSION_MEMORY_TIMEOUT_SECONDS)
        self.assertEqual(post_chat.call_args.kwargs['max_attempts'], SESSION_MEMORY_MAX_ATTEMPTS)
        self.assertEqual(post_chat.call_args.kwargs['max_tokens'], SESSION_MEMORY_MAX_TOKENS)

    def test_session_memory_patch_retries_blank_response_before_fallback(self):
        with patch('openrouter.get_llm_provider', return_value='openrouter'), patch('openrouter._post_chat', side_effect=[
            '',
            '{"running_summary":"The dockhand pointed toward the south jetty.","scene_patch":{"location_id":"south_jetty"}}',
        ]) as post_chat:
            patch_data = get_session_memory_patch({
                'latest_player_message': 'What did the dockhand say?',
                'latest_dm_message': 'He pointed toward the south jetty.',
            })

        self.assertNotIn('_fallback', patch_data)
        self.assertEqual(patch_data['running_summary'], 'The dockhand pointed toward the south jetty.')
        self.assertEqual(patch_data['scene_patch']['location_id'], 'south_jetty')
        self.assertEqual(post_chat.call_count, 2)
        self.assertEqual(
            post_chat.call_args_list[1].kwargs['audit_context']['operation'],
            'session_memory_update_blank_retry',
        )

    def test_session_memory_patch_returns_fallback_for_empty_response(self):
        with patch('openrouter.get_llm_provider', return_value='openrouter'), patch('openrouter._post_chat', return_value=''):
            patch_data = get_session_memory_patch({
                'latest_player_message': 'What did the dockhand say?',
                'latest_dm_message': 'He pointed toward the south jetty.',
                'hot_context': {
                    'current_scene': {
                        'location_id': 'south_jetty',
                        'location_name': 'South Jetty',
                    },
                },
            })

        self.assertEqual(patch_data['_fallback']['reason'], 'empty_memory_writer_response')
        self.assertIn('dockhand', patch_data['running_summary'])
        self.assertEqual(patch_data['scene_patch']['location_id'], 'south_jetty')

    def test_session_memory_patch_retries_empty_patch_before_fallback(self):
        with patch('openrouter.get_llm_provider', return_value='openrouter'), patch('openrouter._post_chat', side_effect=[
            '{}',
            '{"running_summary":"Renn described a limping figure with a scar.","scene_patch":{"active_npc_ids":["renn"]}}',
        ]) as post_chat:
            patch_data = get_session_memory_patch({
                'latest_player_message': 'Tell me about the limp.',
                'latest_dm_message': 'Renn describes a limping figure with a scar.',
            })

        self.assertNotIn('_fallback', patch_data)
        self.assertEqual(patch_data['running_summary'], 'Renn described a limping figure with a scar.')
        self.assertEqual(patch_data['scene_patch']['active_npc_ids'], ['renn'])
        self.assertEqual(post_chat.call_count, 2)
        self.assertEqual(
            post_chat.call_args_list[1].kwargs['audit_context']['operation'],
            'session_memory_update_empty_patch_retry',
        )

    def test_split_memory_builders_use_specialized_contexts(self):
        memory_context = {
            'latest_player_message': 'Aldric knocks on the door.',
            'latest_dm_message': 'A floorboard creaks inside and a shadow retreats.',
            'hot_context': {
                'current_scene': {
                    'location_id': 'lake_ward_hale_residence',
                    'location_name': 'Hale Residence, Lake Ward',
                    'active_npc_ids': ['renn'],
                },
                'active_clocks': [
                    {
                        'clock_id': 'lysander_confession_clock',
                        'name': "Lysander's Courage Wanes",
                        'filled': 0,
                        'segments': 4,
                        'status': 'active',
                        'summary': 'Lysander may confess if cornered.',
                    }
                ],
            },
            'active_clock_count': 1,
            'all_active_clocks_completed': False,
        }

        summary_payload = json.loads(build_session_memory_summary_scene_messages(memory_context)[1]['content'])
        facts_payload = json.loads(build_session_memory_facts_messages(memory_context)[1]['content'])
        clocks_payload = json.loads(build_session_memory_clocks_messages(memory_context)[1]['content'])

        self.assertIn('current_scene', summary_payload)
        self.assertNotIn('active_clocks', summary_payload)
        self.assertEqual(summary_payload['latest_player_message'], 'Aldric knocks on the door.')

        self.assertIn('current_scene', facts_payload)
        self.assertNotIn('active_clocks', facts_payload)
        self.assertEqual(
            facts_payload['latest_dm_message'],
            'A floorboard creaks inside and a shadow retreats.',
        )

        self.assertIn('active_clocks', clocks_payload)
        self.assertEqual(clocks_payload['active_clocks'][0]['clock_id'], 'lysander_confession_clock')

    def test_clock_adjudication_builder_includes_before_after_and_active_clocks(self):
        clock_context = {
            'current_scene_before': {
                'location_id': 'docks',
                'location_name': 'Ferry Docks Market',
            },
            'current_scene_after': {
                'location_id': 'crypt_road',
                'location_name': 'Road to the Crypts',
            },
            'latest_player_message': 'We run after the grave robbers toward the crypt road.',
            'latest_dm_message': 'The chase spills out of the market and onto the crypt road.',
            'active_clocks': [
                {
                    'clock_id': 'race_to_crypts',
                    'name': 'Race to the Crypts',
                    'filled': 0,
                    'segments': 4,
                    'status': 'active',
                }
            ],
        }

        payload = json.loads(build_session_clock_adjudication_messages(clock_context)[1]['content'])
        self.assertEqual(payload['current_scene_before']['location_id'], 'docks')
        self.assertEqual(payload['current_scene_after']['location_id'], 'crypt_road')
        self.assertEqual(payload['active_clocks'][0]['clock_id'], 'race_to_crypts')
        self.assertEqual(payload['latest_player_message'], clock_context['latest_player_message'])

    def test_clock_adjudicator_returns_advance_clocks_payload(self):
        with patch('openrouter.get_llm_provider', return_value='openrouter'), patch(
            'openrouter._post_chat',
            return_value=json.dumps({
                'create_clocks': [],
                'advance_clocks': [
                    {
                        'clock_id': 'race_to_crypts',
                        'delta': 1,
                        'reason': 'The pursuit visibly moved onto the crypt road.',
                        'evidence': [
                            'The player pursued the robbers toward the crypts.',
                            'The DM confirmed the chase left the market for the crypt road.',
                        ],
                    }
                ],
                'retire_clocks': [],
                'no_change_explanations': [],
            }),
        ) as post_chat:
            updates = get_session_clock_updates({
                'current_scene_before': {'location_id': 'docks'},
                'current_scene_after': {'location_id': 'crypt_road'},
                'latest_player_message': 'We chase them toward the crypts.',
                'latest_dm_message': 'You break into a run toward the crypt road.',
                'active_clocks': [{'clock_id': 'race_to_crypts', 'filled': 0, 'segments': 4, 'status': 'active'}],
            })

        self.assertEqual(updates['create_clocks'], [])
        self.assertEqual(updates['advance_clocks'][0]['clock_id'], 'race_to_crypts')
        self.assertEqual(updates['advance_clocks'][0]['delta'], 1)
        self.assertEqual(
            post_chat.call_args.kwargs['audit_context']['operation'],
            'session_clock_adjudication',
        )

    def test_session_memory_patch_opencode_go_uses_split_memory_passes(self):
        with patch('openrouter.get_llm_provider', return_value='opencode_go'), patch(
            'openrouter._post_chat',
            side_effect=[
                '{"turn_summary":"Thorin pays Renn and the party leans harder toward noble involvement.","scene_patch":{"location_id":"ferry_guild_depot","active_npc_ids":["renn"]}}',
                '{"upsert_graph_facts":[]}',
                '{"create_clocks":[],"retire_clocks":[]}',
            ],
        ) as post_chat:
            patch_data = get_session_memory_patch({
                'prior_running_summary': 'The party questioned Renn about the suspect.',
                'latest_player_message': 'Thorin pays Renn a copper.',
                'latest_dm_message': 'Renn pockets the coin and stays nearby.',
                'hot_context': {
                    'current_scene': {
                        'location_id': 'ferry_guild_depot',
                        'location_name': 'Ferry Guild depot',
                        'active_npc_ids': ['renn'],
                    },
                    'active_clocks': [
                        {
                            'clock_id': 'glassmere_truce',
                            'name': 'Glassmere truce frays',
                            'filled': 2,
                            'segments': 4,
                            'status': 'active',
                            'summary': 'The accusation threatens the city truce.',
                        }
                    ],
                },
                'active_clock_count': 1,
                'all_active_clocks_completed': False,
            })

        self.assertEqual(post_chat.call_count, 3)
        self.assertIn('Thorin pays Renn', patch_data['running_summary'])
        self.assertEqual(patch_data['scene_patch']['location_id'], 'ferry_guild_depot')
        self.assertEqual(patch_data['upsert_graph_facts'], [])
        self.assertEqual(patch_data['create_clocks'], [])
        self.assertEqual(patch_data['retire_clocks'], [])
        self.assertEqual(patch_data['_telemetry']['mode'], 'split_opencode_go_memory_writer')
        self.assertEqual(
            [call.kwargs['audit_context']['operation'] for call in post_chat.call_args_list],
            [
                'session_memory_update_summary_scene',
                'session_memory_update_facts',
                'session_memory_update_clocks',
            ],
        )
        self.assertTrue(
            all(call.kwargs['timeout_seconds'] == SESSION_MEMORY_TIMEOUT_SECONDS for call in post_chat.call_args_list)
        )
        self.assertTrue(
            all(call.kwargs['max_tokens'] == SESSION_MEMORY_MAX_TOKENS for call in post_chat.call_args_list)
        )

    def test_session_memory_patch_opencode_go_falls_back_when_summary_scene_is_blank(self):
        with patch('openrouter.get_llm_provider', return_value='opencode_go'), patch(
            'openrouter._post_chat',
            return_value='',
        ):
            patch_data = get_session_memory_patch({
                'latest_player_message': 'What did the dockhand say?',
                'latest_dm_message': 'He pointed toward the south jetty.',
                'hot_context': {
                    'current_scene': {
                        'location_id': 'south_jetty',
                        'location_name': 'South Jetty',
                    },
                },
            })

        self.assertEqual(patch_data['_fallback']['reason'], 'empty_memory_writer_response')
        self.assertEqual(
            patch_data['_telemetry']['summary_scene_error'],
            'blank_or_invalid_summary_scene',
        )

    def test_session_memory_patch_filters_player_characters_from_active_npc_ids(self):
        with patch('openrouter.get_llm_provider', return_value='opencode_go'), patch(
            'openrouter._post_chat',
            side_effect=[
                '{"turn_summary":"The party reaches the villa.","scene_patch":{"location_id":"lake_ward_hale_residence","active_npc_ids":["renn","elara","thorin_ironbeard"]}}',
                '{"upsert_graph_facts":[]}',
                '{"create_clocks":[],"retire_clocks":[]}',
            ],
        ):
            patch_data = get_session_memory_patch({
                'latest_player_message': 'We follow the trail south.',
                'latest_dm_message': 'The trail leads to the Hale Residence.',
                'hot_context': {
                    'current_scene': {
                        'location_id': 'lake_ward_street',
                        'location_name': 'Lake Ward Street',
                        'active_npc_ids': ['renn', 'elara'],
                    },
                    'protected_player_characters': [
                        {'name': 'Elara'},
                        {'name': 'Thorin Ironbeard'},
                    ],
                },
            })

        self.assertEqual(
            patch_data['scene_patch']['active_npc_ids'],
            ['renn'],
        )

    def test_session_memory_patch_fallback_infers_updated_scene_from_dm_reply(self):
        with patch('openrouter.get_llm_provider', return_value='openrouter'), patch('openrouter._post_chat', return_value=''):
            patch_data = get_session_memory_patch({
                'latest_player_message': 'We head to the harbor.',
                'latest_dm_response': (
                    'You reach Blackwater Harbor by dusk. '
                    '<npc target="Maren Ashworth">"Keep your voices down."</npc> '
                    '<npc target="Lysander Hale">"The crate is under the tarpaulin."</npc>'
                ),
                'hot_context': {
                    'current_scene': {
                        'location_id': 'crossroads',
                        'location_name': 'Crossroads',
                        'time_of_day': 'dawn',
                        'active_npc_ids': [],
                        'immediate_tension': 'Travelers gather.',
                    },
                },
            })

        self.assertEqual(patch_data['_fallback']['reason'], 'empty_memory_writer_response')
        self.assertEqual(patch_data['scene_patch']['location_id'], 'blackwater_harbor')
        self.assertEqual(patch_data['scene_patch']['location_name'], 'Blackwater Harbor')
        self.assertEqual(patch_data['scene_patch']['time_of_day'], 'dusk')
        self.assertEqual(
            patch_data['scene_patch']['active_npc_ids'],
            ['maren_ashworth', 'lysander_hale'],
        )
        self.assertIn('Blackwater Harbor', patch_data['scene_patch']['immediate_tension'])

    def test_session_memory_patch_still_calls_llm_for_large_prompt(self):
        large_text = 'x' * 30000
        with patch('openrouter.get_llm_provider', return_value='openrouter'), patch(
            'openrouter._post_chat',
            return_value='{"running_summary":"The dockhand pointed toward the south jetty.","scene_patch":{"location_id":"south_jetty"}}',
        ) as post_chat:
            patch_data = get_session_memory_patch({
                'latest_player_message': 'What did the dockhand say?',
                'latest_dm_response': 'He pointed toward the south jetty.',
                'hot_context': {'current_scene': {'location_id': 'south_jetty'}},
                'relevant_memory': large_text,
            })

        self.assertEqual(patch_data['running_summary'], 'The dockhand pointed toward the south jetty.')
        self.assertGreater(patch_data['_telemetry']['prompt_tokens_estimate'], 6000)
        post_chat.assert_called_once()

    def test_json_error_excerpt_marks_the_bad_line(self):
        malformed = '{\n  "items": [\n    {"id": "one"},\n      "id": "two"\n  ]\n}'

        _data, error, candidate = _json_loads_with_error(malformed)
        excerpt = _json_error_excerpt(candidate, error)

        self.assertIn('Parser error', f'Parser error: {error.msg}')
        self.assertIn('>> 4:       "id": "two"', excerpt)
        self.assertIn('^', excerpt)

    def test_json_repair_uses_parse_error_and_returns_fixed_document(self):
        malformed = (
            '```json\n'
            '{\n'
            '  "entities": [\n'
            '    {"id": "lower_city"},\n'
            '      "id": "the_elfsong_tavern"\n'
            '  ]\n'
            '}\n'
            '```'
        )
        repaired = '{"entities":[{"id":"lower_city"},{"id":"the_elfsong_tavern"}]}'

        with patch('openrouter._post_chat', return_value=repaired) as post_chat:
            data = _json_loads_with_repair(
                malformed,
                audit_context={'operation': 'world_genesis', 'actor': 'world_architect'},
            )

        self.assertEqual(
            data,
            {'entities': [{'id': 'lower_city'}, {'id': 'the_elfsong_tavern'}]},
        )
        messages = post_chat.call_args.args[0]
        self.assertIn('Expecting', messages[1]['content'])
        self.assertIn('"id": "the_elfsong_tavern"', messages[1]['content'])
        self.assertEqual(
            post_chat.call_args.kwargs['audit_context']['operation'],
            'world_genesis_json_repair',
        )

    def test_world_genesis_builds_sections_with_retained_history_and_repair(self):
        malformed_intro = '{"public_intro": {"title": "Test"}'
        repaired_intro = '{"public_intro": {"title": "Test"}}'
        section_responses = [
            malformed_intro,
            repaired_intro,
            '{"knowledge_graph": {"entities": [{"id": "stonehaven", "type": "location"}], "relations": [], "facts": []}}',
            '{"world_state": {"current_arc": "Opening", "current_scene": {"location_id": "stonehaven"}}}',
            '{"dm_private": {"true_inciting_incident": "A hidden forge curse."}}',
            '{"npc_actors": [{"id": "elder_mara", "name": "Elder Mara"}]}',
            '{"clocks": [{"id": "curse_wakes", "name": "The Curse Wakes"}]}',
        ]

        with patch('openrouter._post_chat', side_effect=section_responses) as post_chat:
            data = get_world_genesis_package(
                {'campaign': {'name': 'Test'}},
                audit_context={'operation': 'world_genesis'},
            )

        self.assertEqual(data['public_intro']['title'], 'Test')
        self.assertEqual(data['knowledge_graph']['entities'][0]['id'], 'stonehaven')
        self.assertEqual(data['world_state']['current_arc'], 'Opening')
        self.assertEqual(data['dm_private']['true_inciting_incident'], 'A hidden forge curse.')
        self.assertEqual(data['npc_actors'][0]['id'], 'elder_mara')
        self.assertEqual(data['clocks'][0]['id'], 'curse_wakes')
        self.assertEqual(post_chat.call_count, 7)

        operations = [
            call.kwargs['audit_context']['operation']
            for call in post_chat.call_args_list
        ]
        self.assertEqual(
            operations,
            [
                'world_genesis_public_intro',
                'world_genesis_public_intro_json_repair',
                'world_genesis_knowledge_graph',
                'world_genesis_world_state',
                'world_genesis_dm_private',
                'world_genesis_npc_actors',
                'world_genesis_clocks',
            ],
        )

        knowledge_graph_messages = post_chat.call_args_list[2].args[0]
        self.assertIn('"public_intro": {"title": "Test"}', knowledge_graph_messages[-2]['content'])
        self.assertIn('"section": "knowledge_graph"', knowledge_graph_messages[-1]['content'])


class PlanningDmResponseTest(unittest.TestCase):
    def test_planning_response_retries_blank_json_output(self):
        class Message:
            role = 'player'
            content = 'What age fits my character?'

        with patch('openrouter._post_chat', side_effect=[
            '      ',
            '{"message":"Barrow could plausibly be in his late 30s.","active_page":"story","form_patch":{}}',
        ]) as post_chat:
            result = get_planning_dm_response(
                {'campaign': {'name': 'Test'}},
                [Message()],
                audit_context={'operation': 'planning_dm_response', 'actor': 'planning_dm'},
            )

        self.assertEqual(result['message'], 'Barrow could plausibly be in his late 30s.')
        self.assertEqual(result['active_page'], 'story')
        self.assertEqual(post_chat.call_count, 2)
        self.assertEqual(
            post_chat.call_args_list[1].kwargs['audit_context']['operation'],
            'planning_dm_response_blank_retry',
        )

    def test_streaming_planning_response_does_not_return_blank_message(self):
        class Message:
            role = 'player'
            content = 'What height fits my character?'

        with patch('openrouter._post_chat_stream', return_value='     '), \
                patch('openrouter._post_chat', return_value='{}'):
            result = get_planning_dm_response_streaming(
                {'campaign': {'name': 'Test'}},
                [Message()],
                audit_context={'operation': 'planning_dm_response', 'actor': 'planning_dm'},
            )

        self.assertIsNone(result)


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(
                f'{self.status_code} error',
                response=self,
            )

    def json(self):
        return self._payload


class OpenRouterRetryTest(unittest.TestCase):
    def test_post_chat_response_retries_transient_404_then_succeeds(self):
        success = {
            'choices': [{'message': {'content': 'ok'}}],
        }

        with patch('openrouter.OPENROUTER_API_KEY', 'test-key'), \
                patch('openrouter.get_llm_provider', return_value='openrouter'), \
                patch('openrouter.get_llm_model', return_value='test-model'), \
                patch('openrouter.requests.post', side_effect=[
                    FakeResponse(404),
                    FakeResponse(200, success),
                ]) as post, patch('openrouter.time.sleep') as sleep:
            result = _post_chat_response([{'role': 'user', 'content': 'hello'}])

        self.assertEqual(result, success)
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_post_chat_response_does_not_retry_permanent_400(self):
        with patch('openrouter.OPENROUTER_API_KEY', 'test-key'), \
                patch('openrouter.get_llm_provider', return_value='openrouter'), \
                patch('openrouter.get_llm_model', return_value='test-model'), \
                patch('openrouter.requests.post', return_value=FakeResponse(400)) as post, \
                patch('openrouter.time.sleep') as sleep:
            with self.assertRaises(requests.HTTPError):
                _post_chat_response([{'role': 'user', 'content': 'hello'}])

        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()


class ProviderCompatibilityTest(unittest.TestCase):
    def test_deepseek_thinking_mode_omits_unsupported_tool_controls(self):
        with patch('openrouter.OPENCODE_GO_THINKING', 'enabled'), \
                patch('openrouter.OPENCODE_GO_REASONING_EFFORT', 'max'):
            options = _provider_request_payload_options(
                'opencode_go',
                'deepseek-v4-flash',
                [{'type': 'function', 'function': {'name': 'get_clock'}}],
                'auto',
                False,
            )

        self.assertTrue(options['thinking_enabled'])
        self.assertEqual(options['thinking'], {'type': 'enabled'})
        self.assertEqual(options['reasoning_effort'], 'max')
        self.assertIsNone(options['tool_choice'])
        self.assertIsNone(options['parallel_tool_calls'])

    def test_deepseek_required_tool_choice_is_omitted_even_when_thinking_disabled(self):
        options = _provider_request_payload_options(
            'opencode_go',
            'deepseek-v4-flash',
            [{'type': 'function', 'function': {'name': 'talk_to_player'}}],
            'required',
            False,
            allow_thinking=False,
        )

        self.assertFalse(options['thinking_enabled'])
        self.assertIsNone(options['tool_choice'])
        self.assertFalse(options['parallel_tool_calls'])

    def test_assistant_tool_message_preserves_deepseek_reasoning_content(self):
        self.assertEqual(
            _assistant_tool_message({
                'content': None,
                'reasoning_content': 'Check the clock before answering.',
                'tool_calls': [{'id': 'call_1'}],
            }),
            {
                'role': 'assistant',
                'content': '',
                'tool_calls': [{'id': 'call_1'}],
                'reasoning_content': 'Check the clock before answering.',
            },
        )

    def test_opencode_go_deepseek_thinking_request_uses_provider_specific_payload(self):
        success = {
            'choices': [{'message': {'content': 'ok'}}],
        }

        with patch('openrouter.OPENCODE_GO_API_KEY', 'go-key'), \
                patch('openrouter.OPENCODE_GO_THINKING', 'enabled'), \
                patch('openrouter.OPENCODE_GO_REASONING_EFFORT', 'high'), \
                patch('openrouter.get_llm_provider', return_value='opencode_go'), \
                patch('openrouter.get_llm_model', return_value='deepseek-v4-flash'), \
                patch('openrouter.requests.post', return_value=FakeResponse(200, success)) as post:
            result = _post_chat_response(
                [{'role': 'user', 'content': 'hello'}],
                tools=[{'type': 'function', 'function': {'name': 'get_clock'}}],
                tool_choice='auto',
                parallel_tool_calls=False,
            )

        self.assertEqual(result, success)
        request = post.call_args
        self.assertEqual(request.args[0], 'https://opencode.ai/zen/go/v1/chat/completions')
        self.assertEqual(request.kwargs['headers']['Authorization'], 'Bearer go-key')
        self.assertEqual(request.kwargs['json']['thinking'], {'type': 'enabled'})
        self.assertEqual(request.kwargs['json']['reasoning_effort'], 'high')
        self.assertNotIn('tool_choice', request.kwargs['json'])
        self.assertNotIn('parallel_tool_calls', request.kwargs['json'])

    def test_post_chat_response_can_disable_thinking_for_lightweight_calls(self):
        success = {
            'choices': [{'message': {'content': 'ok'}}],
        }

        with patch('openrouter.OPENCODE_GO_API_KEY', 'go-key'), \
                patch('openrouter.OPENCODE_GO_THINKING', 'enabled'), \
                patch('openrouter.get_llm_provider', return_value='opencode_go'), \
                patch('openrouter.get_llm_model', return_value='deepseek-v4-flash'), \
                patch('openrouter.requests.post', return_value=FakeResponse(200, success)) as post:
            result = _post_chat_response(
                [{'role': 'user', 'content': 'hello'}],
                allow_thinking=False,
            )

        self.assertEqual(result, success)
        self.assertNotIn('thinking', post.call_args.kwargs['json'])
        self.assertNotIn('reasoning_effort', post.call_args.kwargs['json'])

    def test_opening_scene_reprompts_when_visible_content_is_empty(self):
        empty_response = {
            'choices': [{
                'message': {
                    'content': '',
                    'reasoning_content': 'The town square waits in uneasy silence.\n\nWhat do you do?',
                },
                'finish_reason': 'stop',
            }],
        }
        retry_response = {
            'choices': [{
                'message': {
                    'content': 'The town square waits in uneasy silence.\n\nWhat do you do?',
                },
                'finish_reason': 'stop',
            }],
        }

        with patch('openrouter._post_chat_response', side_effect=[empty_response, retry_response]) as post_chat:
            result = get_opening_scene_response({}, {})

        self.assertEqual(result, 'The town square waits in uneasy silence.\n\nWhat do you do?')
        self.assertEqual(post_chat.call_count, 2)
        retry_messages = post_chat.call_args.args[0]
        self.assertIn('no visible assistant content', retry_messages[-1]['content'])
        self.assertEqual(
            post_chat.call_args.kwargs['audit_context']['operation'],
            'opening_scene_format_retry',
        )

    def test_opening_scene_returns_none_when_retry_has_no_visible_content(self):
        response = {
            'choices': [{
                'message': {
                    'content': '',
                },
                'finish_reason': 'stop',
            }],
        }

        with patch('openrouter._post_chat_response', return_value=response) as post_chat:
            result = get_opening_scene_response({}, {})

        self.assertIsNone(result)
        self.assertEqual(post_chat.call_count, 2)


class SessionSpoilerCheckTest(unittest.TestCase):
    def test_normalize_session_spoiler_check_marks_leaked_ids_unsafe(self):
        self.assertEqual(
            normalize_session_spoiler_check({
                'safe': True,
                'leaked_item_ids': ['fact_trap'],
                'evidence': ['trap'],
                'reason': 'Leaked hidden truth.',
            }),
            {
                'safe': False,
                'leaked_item_ids': ['fact_trap'],
                'evidence': ['trap'],
                'reason': 'Leaked hidden truth.',
            },
        )

    def test_session_dm_prompt_omits_guard_only_private_corpus(self):
        messages = build_session_dm_tool_messages({
            'campaign': {'name': 'Test'},
            'private_output_terms': ['Crimson Veil'],
            'private_spoiler_items': [
                {'id': 'villain', 'text': 'Lord Ember secretly funds the crimson cult.'},
            ],
        })

        prompt_context = messages[1]['content']
        self.assertIn('"campaign"', prompt_context)
        self.assertNotIn('private_output_terms', prompt_context)
        self.assertNotIn('private_spoiler_items', prompt_context)
        self.assertNotIn('Crimson Veil', prompt_context)
        self.assertNotIn('crimson cult', prompt_context)

    def test_preflight_normalizer_only_allows_high_confidence_safe_modes_to_skip(self):
        self.assertEqual(
            normalize_session_preflight_decision({
                'dm_reply_mode': 'mechanics_only',
                'skip_spoiler_check': True,
                'main_call_thinking': False,
                'confidence': 'high',
                'reason': 'Only reports a roll total.',
            }),
            {
                'dm_reply_mode': 'mechanics_only',
                'skip_spoiler_check': True,
                'main_call_thinking': False,
                'latest_player_intent_requires_mechanics': False,
                'required_mechanic': '',
                'confidence': 'high',
                'reason': 'Only reports a roll total.',
            },
        )
        self.assertFalse(normalize_session_preflight_decision({
            'dm_reply_mode': 'narrative',
            'skip_spoiler_check': True,
            'confidence': 'high',
        })['skip_spoiler_check'])
        self.assertFalse(normalize_session_preflight_decision({
            'dm_reply_mode': 'ooc_only',
            'skip_spoiler_check': True,
            'confidence': 'medium',
        })['skip_spoiler_check'])

    def test_preflight_normalizer_allows_thinking_off_for_simple_narrative_but_not_spoiler_skip(self):
        result = normalize_session_preflight_decision({
            'dm_reply_mode': 'simple_narrative',
            'skip_spoiler_check': True,
            'main_call_thinking': False,
            'confidence': 'high',
            'reason': 'Simple public scene color.',
        })

        self.assertEqual(result['dm_reply_mode'], 'simple_narrative')
        self.assertFalse(result['skip_spoiler_check'])
        self.assertFalse(result['main_call_thinking'])

    def test_preflight_normalizer_keeps_thinking_on_when_uncertain_or_complex(self):
        self.assertTrue(normalize_session_preflight_decision({
            'dm_reply_mode': 'narrative',
            'main_call_thinking': False,
            'confidence': 'high',
        })['main_call_thinking'])
        self.assertTrue(normalize_session_preflight_decision({
            'dm_reply_mode': 'ooc_only',
            'main_call_thinking': False,
            'confidence': 'medium',
        })['main_call_thinking'])

    def test_session_preflight_prompt_uses_policy_and_not_private_spoiler_corpus(self):
        messages = build_session_preflight_messages(
            {
                'current_player_character': {'id': 1, 'name': 'Aria'},
                'private_spoiler_items': [
                    {'id': 'villain', 'text': 'Lord Ember secretly funds the crimson cult.'},
                ],
            },
            [{'role': 'player', 'content': '<ooc>What is my AC?</ooc>'}],
            [{'type': 'function', 'function': {'name': 'ask_character_sheet'}}],
        )

        payload = messages[1]['content']
        self.assertIn('excruciatingly obvious', messages[0]['content'])
        self.assertIn('has_unrevealed_private_items', payload)
        self.assertIn('main_call_thinking', payload)
        self.assertIn('ask_character_sheet', payload)
        self.assertNotIn('Lord Ember', payload)
        self.assertNotIn('crimson cult', payload)

    def test_session_preflight_uses_single_fast_non_thinking_call(self):
        with patch('openrouter._post_chat', return_value='{"dm_reply_mode":"ooc_only","skip_spoiler_check":true,"main_call_thinking":false,"confidence":"high","reason":"OOC help."}') as post_chat:
            result = get_session_preflight_decision(
                {},
                [{'role': 'player', 'content': '<ooc>How do I roll?</ooc>'}],
                [],
            )

        self.assertTrue(result['skip_spoiler_check'])
        self.assertFalse(result['main_call_thinking'])
        self.assertFalse(post_chat.call_args.kwargs['allow_thinking'])
        self.assertEqual(post_chat.call_args.kwargs['max_attempts'], 1)
        self.assertEqual(post_chat.call_args.kwargs['timeout_seconds'], SESSION_PREFLIGHT_TIMEOUT_SECONDS)
        self.assertEqual(post_chat.call_args.kwargs['max_tokens'], SESSION_PREFLIGHT_MAX_TOKENS)

    def test_spoiler_checker_skips_llm_when_preflight_explicitly_allows_skip(self):
        hot_context = {
            'private_spoiler_items': [
                {'id': 'villain', 'text': 'Lord Ember secretly funds the crimson cult.'},
            ],
        }

        with patch('openrouter._post_chat') as post_chat:
            result = check_session_spoilers_with_llm(
                'Lord Ember watches the crimson cult from a balcony.',
                hot_context,
                skip_spoiler_check=True,
            )

        self.assertTrue(result['safe'])
        post_chat.assert_not_called()

    def test_spoiler_checker_disables_thinking_when_preflight_does_not_skip(self):
        hot_context = {
            'private_spoiler_items': [
                {'id': 'villain', 'text': 'Lord Ember secretly funds the crimson cult.'},
            ],
        }

        with patch('openrouter._post_chat', return_value='{"safe": true, "leaked_item_ids": [], "evidence": [], "reason": ""}') as post_chat:
            result = check_session_spoilers_with_llm(
                'Lord Ember watches the crimson cult from a balcony.',
                hot_context,
                skip_spoiler_check=False,
            )

        self.assertTrue(result['safe'])
        self.assertFalse(post_chat.call_args.kwargs['allow_thinking'])


class CharacterSheetAgentTest(unittest.TestCase):
    def test_character_sheet_agent_returns_compact_answer(self):
        with patch('openrouter._post_chat', return_value='{"answer":"AC 15.","character_ids":[7],"missing":false}'):
            result = get_character_sheet_answer(
                'What is the AC?',
                'current_player',
                [{'id': 7, 'name': 'Aria', 'combat': {'armor_class': 15}}],
                audit_context={'trace_id': 'session_dm:test'},
            )

        self.assertEqual(result, {
            'answer': 'AC 15.',
            'character_ids': [7],
            'missing': False,
        })


if __name__ == '__main__':
    unittest.main()

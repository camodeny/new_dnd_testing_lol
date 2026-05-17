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
    get_character_sheet_answer,
    get_world_genesis_package,
    normalize_session_spoiler_check,
)


class OpenRouterJsonRepairTest(unittest.TestCase):
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

    def test_world_genesis_uses_json_repair_once_after_parse_failure(self):
        malformed = '{"public_intro": {"title": "Test"}, "knowledge_graph": [}'
        repaired = '{"public_intro": {"title": "Test"}, "knowledge_graph": []}'

        with patch('openrouter._post_chat', side_effect=[malformed, repaired]) as post_chat:
            data = get_world_genesis_package({}, audit_context={'operation': 'world_genesis'})

        self.assertEqual(data['public_intro']['title'], 'Test')
        self.assertEqual(data['knowledge_graph'], [])
        self.assertEqual(post_chat.call_count, 2)


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

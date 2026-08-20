import os
import sys
import unittest
from unittest.mock import patch


sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from llm_providers import OpenCodeGoAdapter, ProviderRequest  # noqa: E402


class OpenCodeGoResponsesTests(unittest.TestCase):
    def test_muse_spark_uses_responses_endpoint_and_payload(self):
        adapter = OpenCodeGoAdapter()
        request = ProviderRequest(
            model='muse-spark-1.2-contributor',
            messages=[
                {'role': 'system', 'content': 'Keep the session coherent.'},
                {'role': 'user', 'content': 'What do I see?'},
                {
                    'role': 'assistant',
                    'content': '',
                    'tool_calls': [{
                        'id': 'call_123',
                        'function': {'name': 'inspect_scene', 'arguments': '{"area":"gate"}'},
                    }],
                },
                {'role': 'tool', 'tool_call_id': 'call_123', 'content': '{"result":"quiet"}'},
            ],
            tools=[{
                'type': 'function',
                'function': {
                    'name': 'inspect_scene',
                    'description': 'Inspect an area.',
                    'parameters': {'type': 'object', 'properties': {'area': {'type': 'string'}}},
                },
            }],
            tool_choice='required',
        )
        with patch.dict(os.environ, {
            'OPENCODE_GO_THINKING': 'enabled',
            'OPENCODE_GO_REASONING_EFFORT': 'xhigh',
        }, clear=False):
            payload = adapter.build_payload(request)

        self.assertEqual(adapter.base_url(request), 'https://opencode.ai/zen/go/v1/responses')
        self.assertEqual(payload['model'], 'muse-spark-1.2-contributor')
        self.assertEqual(payload['reasoning'], {'effort': 'xhigh'})
        self.assertEqual(payload['tools'][0]['type'], 'function')
        self.assertEqual(payload['input'][0]['role'], 'developer')
        self.assertEqual(payload['input'][2]['type'], 'function_call')
        self.assertEqual(payload['input'][3]['type'], 'function_call_output')

    def test_muse_spark_normalizes_responses_tool_calls(self):
        response = OpenCodeGoAdapter().parse_response({
            'model': 'muse-spark-1.2-contributor',
            'status': 'completed',
            'output': [
                {'type': 'reasoning', 'summary': [{'type': 'summary_text', 'text': 'Checking clues.'}]},
                {
                    'type': 'message',
                    'content': [{'type': 'output_text', 'text': 'The gate stands open.'}],
                },
                {
                    'type': 'function_call',
                    'call_id': 'call_456',
                    'name': 'inspect_scene',
                    'arguments': '{"area":"gate"}',
                },
            ],
        })

        self.assertEqual(response.content, 'The gate stands open.')
        self.assertEqual(response.reasoning, 'Checking clues.')
        self.assertEqual(response.tool_calls[0].id, 'call_456')
        self.assertEqual(response.tool_calls[0].name, 'inspect_scene')


if __name__ == '__main__':
    unittest.main()

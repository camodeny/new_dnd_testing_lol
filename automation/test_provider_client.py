import unittest
from unittest.mock import MagicMock, patch
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
import provider_client


class ProviderClientTests(unittest.TestCase):
    def test_resolve_provider_and_model_from_prefixed_model(self):
        provider, model = provider_client.resolve_provider_and_model('opencode-go/deepseek-v4-flash')
        self.assertEqual(provider, 'opencode_go')
        self.assertEqual(model, 'deepseek-v4-flash')

    @patch.object(provider_client.requests, 'post')
    def test_request_json_decision_retries_invalid_json(self, mock_post):
        invalid_response = MagicMock()
        invalid_response.raise_for_status.return_value = None
        invalid_response.json.return_value = {
            'choices': [{'message': {'content': 'not json'}}],
            'usage': {'total_tokens': 10},
        }
        valid_response = MagicMock()
        valid_response.raise_for_status.return_value = None
        valid_response.json.return_value = {
            'choices': [{'message': {'content': '{"action":"no_action"}'}}],
            'usage': {'total_tokens': 12},
        }
        mock_post.side_effect = [invalid_response, valid_response]

        with patch.object(provider_client, 'OPENROUTER_API_KEY', 'key'), \
                patch.object(provider_client, 'OPENROUTER_MODEL', 'gpt-test'), \
                patch.object(provider_client, 'LLM_PROVIDER', 'openrouter'):
            result = provider_client.request_json_decision(
                'system',
                'prompt',
                model='openrouter/gpt-test',
                timeout_seconds=5,
                max_attempts=1,
            )

        self.assertEqual(result['decision']['action'], 'no_action')
        self.assertEqual(result['json_retry_count'], 1)
        self.assertEqual(mock_post.call_count, 2)


if __name__ == '__main__':
    unittest.main()

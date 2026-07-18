import unittest
from unittest.mock import MagicMock, patch
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
import provider_client
import llm_providers


class ProviderClientTests(unittest.TestCase):
    def test_resolve_provider_and_model_from_prefixed_model(self):
        provider, model = provider_client.resolve_provider_and_model('opencode-go/deepseek-v4-flash')
        self.assertEqual(provider, 'opencode_go')
        self.assertEqual(model, 'deepseek-v4-flash')

    def test_resolve_provider_and_model_unknown_vendor_prefix_falls_back_to_env_model(self):
        with patch.dict(os.environ, {
                'LLM_PROVIDER': 'opencode_go',
                'OPENCODE_GO_MODEL': 'deepseek-v4-flash',
                'OPENROUTER_MODEL': 'nvidia/nemotron-3-super-120b-a12b:free',
        }):
            provider, model = provider_client.resolve_provider_and_model('nvidia/nemotron-3-super-120b-a12b:free')
        self.assertEqual(provider, 'opencode_go')
        self.assertEqual(model, 'deepseek-v4-flash')

    @patch.object(llm_providers.requests, 'post')
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

        with patch.dict(os.environ, {
                'OPENROUTER_API_KEY': 'key',
                'OPENROUTER_MODEL': 'gpt-test',
                'LLM_PROVIDER': 'openrouter',
        }):
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

    def test_shared_registry_supplies_provider_config(self):
        self.assertIs(provider_client.provider_registry, llm_providers.provider_registry)
        with patch.dict(os.environ, {
                'OPENCODE_GO_API_KEY': 'go-key',
                'OPENCODE_GO_THINKING': 'enabled',
                'OPENCODE_GO_REASONING_EFFORT': 'max',
        }):
            adapter = llm_providers.provider_registry.get('opencode_go')
            capabilities = adapter.capabilities_for('deepseek-v4-flash')
        self.assertTrue(capabilities.supports_thinking)
        self.assertEqual(capabilities.reasoning_effort, 'max')


if __name__ == '__main__':
    unittest.main()

import json
import os
import sys
import unittest
from unittest.mock import patch

import requests

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import llm_providers
from llm_providers import (
    LLMProviderAdapter,
    NormalizedChatResponse,
    OpenCodeGoAdapter,
    OpenRouterAdapter,
    ProviderError,
    ProviderRegistry,
    ProviderRequest,
    execute_chat,
    provider_registry,
    stream_chat,
)


class FakeResponse:
    def __init__(self, status_code, payload=None, lines=None):
        self.status_code = status_code
        self._payload = payload or {}
        self._lines = lines or []

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f'{self.status_code} error', response=self)

    def json(self):
        return self._payload

    def iter_lines(self, decode_unicode=True):
        return iter(self._lines)


class ProviderRegistryTest(unittest.TestCase):
    def test_builtin_providers_registered(self):
        self.assertEqual(provider_registry.names(), {'openrouter', 'opencode_go'})
        self.assertIsInstance(provider_registry.get('openrouter'), OpenRouterAdapter)
        self.assertIsInstance(provider_registry.get('opencode_go'), OpenCodeGoAdapter)

    def test_get_normalizes_provider_names(self):
        self.assertIs(provider_registry.get('opencode-go'), provider_registry.get('opencode_go'))
        self.assertIs(provider_registry.get(' OpenRouter '), provider_registry.get('openrouter'))

    def test_unknown_provider_raises(self):
        with self.assertRaises(RuntimeError):
            provider_registry.get('not_a_provider')

    def test_current_reads_llm_provider_env(self):
        with patch.dict(os.environ, {'LLM_PROVIDER': 'opencode_go'}):
            self.assertEqual(provider_registry.current().name, 'opencode_go')
        with patch.dict(os.environ, {'LLM_PROVIDER': 'openrouter'}):
            self.assertEqual(provider_registry.current().name, 'openrouter')

    def test_current_rejects_unknown_provider(self):
        with patch.dict(os.environ, {'LLM_PROVIDER': 'bogus'}):
            with self.assertRaises(RuntimeError):
                provider_registry.current()

    def test_register_fake_provider_without_workflow_changes(self):
        registry = ProviderRegistry()
        registry.register(OpenRouterAdapter())

        class FakeAdapter(LLMProviderAdapter):
            name = 'fake'
            env_prefix = 'FAKE'
            default_base_url = 'https://fake.example/chat'

        registry.register(FakeAdapter())
        with patch.dict(os.environ, {'LLM_PROVIDER': 'fake'}):
            # Swap the process-wide env the shared registry reads.
            llm_providers.provider_registry = registry
            try:
                self.assertEqual(registry.current().name, 'fake')
                provider, model = registry.resolve_provider_and_model('fake/some-model')
                self.assertEqual((provider, model), ('fake', 'some-model'))
            finally:
                llm_providers.provider_registry = provider_registry

    def test_resolve_provider_and_model_hint_parsing(self):
        self.assertEqual(
            provider_registry.resolve_provider_and_model('opencode-go/deepseek-v4-flash'),
            ('opencode_go', 'deepseek-v4-flash'),
        )
        with patch.dict(os.environ, {'LLM_PROVIDER': 'opencode_go', 'OPENCODE_GO_MODEL': 'env-model'}):
            self.assertEqual(
                provider_registry.resolve_provider_and_model(''),
                ('opencode_go', 'env-model'),
            )
            self.assertEqual(
                provider_registry.resolve_provider_and_model('explicit-model'),
                ('opencode_go', 'explicit-model'),
            )


class ProviderCapabilitiesTest(unittest.TestCase):
    def test_openrouter_supports_tool_controls(self):
        adapter = OpenRouterAdapter()
        capabilities = adapter.capabilities_for('any-model')
        self.assertTrue(capabilities.supports_tool_choice_required)
        self.assertTrue(capabilities.supports_parallel_tool_calls)
        self.assertFalse(capabilities.supports_thinking)

    def test_opencode_go_deepseek_thinking_capability(self):
        adapter = OpenCodeGoAdapter()
        with patch.dict(os.environ, {'OPENCODE_GO_THINKING': 'enabled', 'OPENCODE_GO_REASONING_EFFORT': 'max'}):
            capabilities = adapter.capabilities_for('deepseek-v4-flash')
        self.assertTrue(capabilities.supports_thinking)
        self.assertEqual(capabilities.reasoning_effort, 'max')
        self.assertFalse(capabilities.supports_tool_choice_required)

    def test_opencode_go_thinking_disabled_by_default(self):
        adapter = OpenCodeGoAdapter()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('OPENCODE_GO_THINKING', None)
            capabilities = adapter.capabilities_for('deepseek-v4-flash')
        self.assertFalse(capabilities.supports_thinking)

    def test_opencode_go_non_deepseek_has_no_thinking(self):
        adapter = OpenCodeGoAdapter()
        with patch.dict(os.environ, {'OPENCODE_GO_THINKING': 'enabled'}):
            capabilities = adapter.capabilities_for('some-other-model')
        self.assertFalse(capabilities.supports_thinking)
        self.assertTrue(capabilities.supports_tool_choice_required)

    def test_thinking_payload_strips_tool_controls(self):
        adapter = OpenCodeGoAdapter()
        request = ProviderRequest(
            messages=[{'role': 'user', 'content': 'hi'}],
            model='deepseek-v4-flash',
            tools=[{'type': 'function', 'function': {'name': 'get_clock'}}],
            tool_choice='auto',
            parallel_tool_calls=False,
        )
        with patch.dict(os.environ, {'OPENCODE_GO_THINKING': 'enabled', 'OPENCODE_GO_REASONING_EFFORT': 'high'}):
            payload = adapter.build_payload(request)
        self.assertEqual(payload['thinking'], {'type': 'enabled'})
        self.assertEqual(payload['reasoning_effort'], 'high')
        self.assertNotIn('tool_choice', payload)
        self.assertNotIn('parallel_tool_calls', payload)
        self.assertIn('tools', payload)

    def test_required_tool_choice_dropped_for_deepseek_without_thinking(self):
        adapter = OpenCodeGoAdapter()
        request = ProviderRequest(
            messages=[{'role': 'user', 'content': 'hi'}],
            model='deepseek-v4-flash',
            tools=[{'type': 'function', 'function': {'name': 'talk'}}],
            tool_choice='required',
            parallel_tool_calls=False,
            allow_thinking=False,
        )
        with patch.dict(os.environ, {'OPENCODE_GO_THINKING': 'enabled'}):
            payload = adapter.build_payload(request)
        self.assertNotIn('thinking', payload)
        self.assertNotIn('tool_choice', payload)
        self.assertEqual(payload['parallel_tool_calls'], False)

    def test_openrouter_passes_tool_controls_through(self):
        adapter = OpenRouterAdapter()
        request = ProviderRequest(
            messages=[{'role': 'user', 'content': 'hi'}],
            model='model-x',
            tools=[{'type': 'function', 'function': {'name': 'talk'}}],
            tool_choice='required',
            parallel_tool_calls=False,
        )
        payload = adapter.build_payload(request)
        self.assertEqual(payload['tool_choice'], 'required')
        self.assertEqual(payload['parallel_tool_calls'], False)
        self.assertNotIn('thinking', payload)

    def test_json_mode_and_max_tokens_payload(self):
        adapter = OpenRouterAdapter()
        request = ProviderRequest(
            messages=[], model='m', json_mode=True, max_tokens=128,
        )
        payload = adapter.build_payload(request)
        self.assertEqual(payload['response_format'], {'type': 'json_object'})
        self.assertEqual(payload['max_tokens'], 128)


class ResponseNormalizationTest(unittest.TestCase):
    def test_parse_response_normalizes_tool_calls_reasoning_usage(self):
        adapter = OpenRouterAdapter()
        data = {
            'model': 'model-x',
            'choices': [{
                'message': {
                    'content': None,
                    'reasoning_content': 'thinking aloud',
                    'tool_calls': [{
                        'id': 'call_1',
                        'type': 'function',
                        'function': {'name': 'get_clock', 'arguments': '{"a": 1}'},
                    }],
                },
                'finish_reason': 'tool_calls',
            }],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 4},
        }
        normalized = adapter.parse_response(data)
        self.assertIsInstance(normalized, NormalizedChatResponse)
        self.assertEqual(normalized.provider, 'openrouter')
        self.assertEqual(normalized.model, 'model-x')
        self.assertEqual(normalized.content, '')
        self.assertEqual(normalized.finish_reason, 'tool_calls')
        self.assertEqual(normalized.usage['prompt_tokens'], 10)
        self.assertEqual(normalized.reasoning, 'thinking aloud')
        self.assertEqual(len(normalized.tool_calls), 1)
        self.assertEqual(normalized.tool_calls[0].name, 'get_clock')
        self.assertEqual(normalized.tool_calls[0].arguments, '{"a": 1}')
        self.assertIs(normalized.raw, data)

    def test_parse_response_joins_list_content(self):
        adapter = OpenRouterAdapter()
        data = {'choices': [{'message': {'content': [{'text': 'foo'}, {'text': 'bar'}]}}]}
        self.assertEqual(adapter.parse_response(data).content, 'foobar')

    def test_malformed_response_raises_provider_error(self):
        adapter = OpenCodeGoAdapter()
        with self.assertRaises(ProviderError) as ctx:
            adapter.parse_response({'unexpected': True})
        self.assertEqual(ctx.exception.kind, 'malformed')
        self.assertFalse(ctx.exception.retryable)


class StreamNormalizationTest(unittest.TestCase):
    def _stream_lines(self):
        return [
            'data: {"choices": [{"delta": {"content": "Hello"}}]}',
            '',
            'data: {"choices": [{"delta": {"content": " world"}}]}',
            'data: not-json',
            'data: [DONE]',
            'data: {"choices": [{"delta": {"content": "ignored"}}]}',
        ]

    def test_stream_events_normalized_for_both_providers(self):
        for adapter in (OpenRouterAdapter(), OpenCodeGoAdapter()):
            response = FakeResponse(200, lines=self._stream_lines())
            events = list(adapter.iter_stream_events(response))
            self.assertEqual([e.kind for e in events], ['token', 'token', 'done'])
            self.assertEqual(''.join(e.text for e in events if e.kind == 'token'), 'Hello world')

    def test_stream_chat_uses_adapter_transport(self):
        adapter = OpenRouterAdapter()
        with patch.dict(os.environ, {'OPENROUTER_API_KEY': 'k', 'OPENROUTER_MODEL': 'm'}), \
                patch('llm_providers.requests.post', return_value=FakeResponse(200, lines=self._stream_lines())) as post:
            request = ProviderRequest(messages=[{'role': 'user', 'content': 'hi'}], model='m')
            events = list(stream_chat(adapter, request))
        self.assertEqual(post.call_args.kwargs['json']['stream'], True)
        self.assertEqual(events[-1].kind, 'done')


class ErrorClassificationTest(unittest.TestCase):
    def _http_error(self, status):
        return requests.HTTPError(f'{status} error', response=FakeResponse(status))

    def test_transient_vs_permanent_classification_consistent_across_adapters(self):
        for adapter in (OpenRouterAdapter(), OpenCodeGoAdapter()):
            for status in (404, 408, 409, 425, 429, 500, 503):
                self.assertTrue(adapter.classify_error(self._http_error(status)).retryable, status)
            for status in (400, 401, 403, 422):
                self.assertFalse(adapter.classify_error(self._http_error(status)).retryable, status)
            self.assertTrue(adapter.classify_error(requests.ConnectionError('x')).retryable)
            self.assertTrue(adapter.classify_error(requests.Timeout('x')).retryable)
            self.assertFalse(adapter.classify_error(ValueError('x')).retryable)

    def test_execute_chat_retries_transient_then_succeeds(self):
        adapter = OpenRouterAdapter()
        success = {'choices': [{'message': {'content': 'ok'}}]}
        with patch.dict(os.environ, {'OPENROUTER_API_KEY': 'k'}), \
                patch('llm_providers.requests.post', side_effect=[FakeResponse(404), FakeResponse(200, success)]) as post, \
                patch('llm_providers.time.sleep') as sleep:
            result = execute_chat(adapter, ProviderRequest(messages=[], model='m'))
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(1)
        self.assertEqual(result.content, 'ok')

    def test_execute_chat_does_not_retry_permanent_errors(self):
        adapter = OpenRouterAdapter()
        with patch.dict(os.environ, {'OPENROUTER_API_KEY': 'k'}), \
                patch('llm_providers.requests.post', return_value=FakeResponse(400)) as post, \
                patch('llm_providers.time.sleep') as sleep:
            with self.assertRaises(requests.HTTPError):
                execute_chat(adapter, ProviderRequest(messages=[], model='m'))
        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()

    def test_execute_chat_requires_config(self):
        adapter = OpenCodeGoAdapter()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('OPENCODE_GO_API_KEY', None)
            with self.assertRaises(RuntimeError) as ctx:
                execute_chat(adapter, ProviderRequest(messages=[], model='m'))
        self.assertIn('OPENCODE_GO_API_KEY', str(ctx.exception))

    def test_retry_hook_invoked(self):
        adapter = OpenRouterAdapter()
        calls = []

        class Hooks:
            def on_retry(self, attempt, max_attempts, delay_seconds, error):
                calls.append((attempt, max_attempts, delay_seconds))

            def on_error(self, error):
                calls.append(('error', repr(error)))

        with patch.dict(os.environ, {'OPENROUTER_API_KEY': 'k'}), \
                patch('llm_providers.requests.post', side_effect=[FakeResponse(429), FakeResponse(200, {'choices': [{'message': {'content': 'ok'}}]})]), \
                patch('llm_providers.time.sleep'):
            execute_chat(adapter, ProviderRequest(messages=[], model='m'), hooks=Hooks())
        self.assertEqual(calls, [(1, 4, 1)])


class FakeAdapter(LLMProviderAdapter):
    name = 'fake'
    env_prefix = 'FAKE'
    default_base_url = 'https://fake.example/chat'


def _register_fake_adapter():
    provider_registry.register(FakeAdapter())


def _unregister_fake_adapter():
    provider_registry.unregister('fake')


def _finalizer_speak_response(content='The DM nods.'):
    return {
        'choices': [{
            'message': {
                'content': None,
                'tool_calls': [{
                    'id': 'call_1',
                    'type': 'function',
                    'function': {
                        'name': 'talk_to_player',
                        'arguments': json.dumps({'content': content, 'commit_action_ids': []}),
                    },
                }],
            },
            'finish_reason': 'tool_calls',
        }],
    }


class CrossProviderWorkflowParityTest(unittest.TestCase):
    """The same workflow code must run for every registered provider.

    Adding a provider (e.g. the fake one here) requires only an adapter plus a
    registry entry — no workflow branches.
    """

    def setUp(self):
        _register_fake_adapter()
        self.addCleanup(_unregister_fake_adapter)
        import openrouter  # noqa: F401 -- sys.path set above

    def _run_dm_loop_with_recorder(self, provider):
        import openrouter

        calls = []

        def fake_post_chat_response(messages, **kwargs):
            calls.append({
                'operation': (kwargs.get('audit_context') or {}).get('operation'),
                'json_mode': kwargs.get('json_mode'),
                'has_tools': bool(kwargs.get('tools')),
            })
            adapter = provider_registry.get(provider)
            if kwargs.get('tools'):
                return adapter.parse_response(_finalizer_speak_response())
            return adapter.parse_response({'choices': [{'message': {'content': '{"safe": true}'}}]})

        preflight = {
            'dm_reply_mode': 'unknown',
            'skip_spoiler_check': False,
            'main_call_thinking': True,
            'confidence': 'low',
            'reason': 'test',
        }
        with patch('openrouter.get_llm_provider', return_value=provider), \
                patch('openrouter.get_llm_model', return_value='test-model'), \
                patch('openrouter.get_session_preflight_decision', return_value=preflight), \
                patch('openrouter._post_chat_normalized', side_effect=fake_post_chat_response):
            decision = openrouter.DMExecutionLoop().run(
                {},
                [],
                [],
                lambda *args, **kwargs: {},
                audit_context={'operation': 'session_dm_response'},
            )
        return calls, decision

    def test_dm_loop_call_order_identical_across_providers(self):
        baselines = {}
        for provider in ('openrouter', 'opencode_go', 'fake'):
            calls, decision = self._run_dm_loop_with_recorder(provider)
            self.assertEqual(decision['mode'], 'speak')
            baselines[provider] = calls
        self.assertEqual(baselines['openrouter'], baselines['opencode_go'])
        self.assertEqual(baselines['openrouter'], baselines['fake'])
        # The loop must have run preflight routing then the main tool call.
        operations = [c['operation'] for c in baselines['openrouter']]
        self.assertIn('session_dm_response', operations)
        self.assertTrue(any(c['has_tools'] for c in baselines['openrouter']))

    def test_dm_loop_binds_explicit_adapter_for_all_subcalls(self):
        """DMExecutionLoop(adapter) must use that adapter for preflight,
        guards, and the main generation — not just the main call."""
        import openrouter

        calls = []

        def fake_execute_chat(adapter, request, hooks=None):
            calls.append((adapter.name, request.model, bool(request.tools)))
            if request.tools:
                return adapter.parse_response(_finalizer_speak_response())
            return adapter.parse_response({'choices': [{'message': {'content': (
                '{"dm_reply_mode": "unknown", "skip_spoiler_check": true,'
                ' "main_call_thinking": false, "confidence": "low", "reason": "test"}'
            )}}]})

        with patch.dict(os.environ, {'FAKE_API_KEY': 'fake-key', 'FAKE_MODEL': 'fake-model'}), \
                patch('openrouter.get_llm_provider', return_value='openrouter'), \
                patch('openrouter.get_llm_model', return_value='global-model'), \
                patch('openrouter.execute_chat', side_effect=fake_execute_chat):
            decision = openrouter.DMExecutionLoop(provider_registry.get('fake')).run(
                {},
                [],
                [],
                lambda *args, **kwargs: {},
                audit_context={'operation': 'session_dm_response'},
            )

        self.assertEqual(decision['mode'], 'speak')
        # Preflight (json subcall), the main tool call, and any guard subcalls
        # must all have gone through the fake adapter — never the global one.
        self.assertTrue(any(has_tools for _, _, has_tools in calls))
        self.assertTrue(any(not has_tools for _, _, has_tools in calls))
        self.assertEqual({(name, model) for name, model, _ in calls}, {('fake', 'fake-model')})

    def _run_staged_memory_with_recorder(self, provider):
        import openrouter

        stages = []

        def fake_request_json(messages, audit_context, operation, **kwargs):
            stages.append(operation)
            if operation == 'session_memory_extract':
                return {'running_summary': 'summary', 'candidate_facts': []}, 120
            if operation == 'session_memory_update_clocks':
                return {'create_clocks': [], 'retire_clocks': []}, 40
            return None, 0

        def fake_resolve(messages, **kwargs):
            stages.append('resolution')
            adapter = provider_registry.get(provider)
            payload = json.dumps({'running_summary': 'summary', 'resolved_facts': []})
            return adapter.parse_response({'choices': [{'message': {'content': payload}}]})

        def fake_compile(memory_context, extracted, final_payload):
            stages.append('compilation')
            return {'running_summary': 'summary', 'compile_summary': {}}

        with patch('openrouter.get_llm_provider', return_value=provider), \
                patch('openrouter.get_llm_model', return_value='test-model'), \
                patch('openrouter._request_session_memory_json', side_effect=fake_request_json), \
                patch('openrouter._post_chat_normalized', side_effect=fake_resolve), \
                patch('services.session_memory_agent.compile_staged_memory_patch', side_effect=fake_compile):
            compiled = openrouter._get_session_memory_patch_staged(
                {'campaign_id': 1, 'session_id': 1},
                {'trace_id': 't', 'trace_label': 'l'},
                {},
            )
        return stages, compiled

    def test_staged_memory_stage_order_identical_across_providers(self):
        expected = [
            'session_memory_extract',
            'resolution',
            'compilation',
            'session_memory_update_clocks',
        ]
        baselines = {}
        for provider in ('openrouter', 'opencode_go', 'fake'):
            stages, compiled = self._run_staged_memory_with_recorder(provider)
            self.assertEqual(stages, expected)
            self.assertEqual(compiled['create_clocks'], [])
            baselines[provider] = stages
        self.assertEqual(baselines['openrouter'], baselines['opencode_go'])
        self.assertEqual(baselines['openrouter'], baselines['fake'])


class CapabilityGatingTest(unittest.TestCase):
    """Declared capabilities must be authoritative: an adapter that declares a
    feature unsupported must not receive its payload fields."""

    class LimitedAdapter(LLMProviderAdapter):
        name = 'limited'
        env_prefix = 'LIMITED'
        default_base_url = 'https://limited.example/chat'

        def capabilities_for(self, model):
            from llm_providers import ProviderCapabilities
            return ProviderCapabilities(
                supports_json_mode=False,
                supports_parallel_tool_calls=False,
            )

    def test_parallel_tool_calls_omitted_when_unsupported(self):
        adapter = self.LimitedAdapter()
        request = ProviderRequest(
            messages=[{'role': 'user', 'content': 'hi'}],
            model='m',
            tools=[{'type': 'function', 'function': {'name': 'talk'}}],
            tool_choice='auto',
            parallel_tool_calls=False,
        )
        payload = adapter.build_payload(request)
        self.assertNotIn('parallel_tool_calls', payload)
        self.assertEqual(payload['tool_choice'], 'auto')

    def test_json_mode_rejected_when_unsupported(self):
        adapter = self.LimitedAdapter()
        request = ProviderRequest(messages=[], model='m', json_mode=True)
        with self.assertRaises(ProviderError) as ctx:
            adapter.build_payload(request)
        self.assertEqual(ctx.exception.kind, 'unsupported_feature')
        self.assertFalse(ctx.exception.retryable)

    def test_openrouter_declares_and_sends_both(self):
        adapter = OpenRouterAdapter()
        request = ProviderRequest(
            messages=[], model='m', json_mode=True,
            tools=[{'type': 'function', 'function': {'name': 'talk'}}],
            parallel_tool_calls=False,
        )
        payload = adapter.build_payload(request)
        self.assertEqual(payload['response_format'], {'type': 'json_object'})
        self.assertEqual(payload['parallel_tool_calls'], False)


class MessageViewTest(unittest.TestCase):
    def test_message_view_built_from_normalized_fields(self):
        adapter = OpenRouterAdapter()
        data = {
            'choices': [{
                'message': {
                    'content': None,
                    'reasoning_content': 'thinking',
                    'tool_calls': [{
                        'id': 'call_1',
                        'type': 'function',
                        'function': {'name': 'get_clock', 'arguments': '{"a": 1}'},
                    }],
                },
                'finish_reason': 'tool_calls',
            }],
        }
        view = adapter.parse_response(data).message_view()
        self.assertEqual(view['content'], '')
        self.assertEqual(view['reasoning_content'], 'thinking')
        self.assertEqual(view['tool_calls'], [{
            'id': 'call_1',
            'type': 'function',
            'function': {'name': 'get_clock', 'arguments': '{"a": 1}'},
        }])
        # The view must not alias the provider's native structures.
        self.assertIsNot(view['tool_calls'][0], data['choices'][0]['message']['tool_calls'][0])


class StreamRetryTest(unittest.TestCase):
    def test_stream_retries_pre_emission_transient_failure(self):
        adapter = OpenRouterAdapter()
        retries = []

        class Hooks:
            def on_retry(self, attempt, max_attempts, delay_seconds, error):
                retries.append(attempt)

            def on_error(self, error):
                raise AssertionError('on_error should not fire')

        lines = ['data: {"choices": [{"delta": {"content": "ok"}}]}', 'data: [DONE]']
        with patch.dict(os.environ, {'OPENROUTER_API_KEY': 'k'}), \
                patch('llm_providers.requests.post', side_effect=[FakeResponse(429), FakeResponse(200, lines=lines)]) as post, \
                patch('llm_providers.time.sleep'):
            events = list(stream_chat(adapter, ProviderRequest(messages=[], model='m'), hooks=Hooks()))
        self.assertEqual(post.call_count, 2)
        self.assertEqual(retries, [1])
        self.assertEqual([e.text for e in events if e.kind == 'token'], ['ok'])

    def test_stream_does_not_retry_permanent_failure(self):
        adapter = OpenRouterAdapter()
        errors = []

        class Hooks:
            def on_retry(self, *args):
                raise AssertionError('on_retry should not fire')

            def on_error(self, error):
                errors.append(repr(error))

        with patch.dict(os.environ, {'OPENROUTER_API_KEY': 'k'}), \
                patch('llm_providers.requests.post', return_value=FakeResponse(400)) as post, \
                patch('llm_providers.time.sleep'):
            with self.assertRaises(requests.HTTPError):
                list(stream_chat(adapter, ProviderRequest(messages=[], model='m'), hooks=Hooks()))
        self.assertEqual(post.call_count, 1)
        self.assertEqual(len(errors), 1)

    def test_stream_mid_stream_failure_not_retried(self):
        adapter = OpenRouterAdapter()
        errors = []

        class Hooks:
            def on_retry(self, *args):
                raise AssertionError('mid-stream failures must not retry')

            def on_error(self, error):
                errors.append(repr(error))

        class BrokenStream(FakeResponse):
            def iter_lines(self, decode_unicode=True):
                yield 'data: {"choices": [{"delta": {"content": "partial"}}]}'
                raise requests.ConnectionError('stream dropped')

        with patch.dict(os.environ, {'OPENROUTER_API_KEY': 'k'}), \
                patch('llm_providers.requests.post', return_value=BrokenStream(200)) as post, \
                patch('llm_providers.time.sleep'):
            with self.assertRaises(requests.ConnectionError):
                list(stream_chat(adapter, ProviderRequest(messages=[], model='m'), hooks=Hooks()))
        self.assertEqual(post.call_count, 1)
        self.assertEqual(len(errors), 1)


if __name__ == '__main__':
    unittest.main()

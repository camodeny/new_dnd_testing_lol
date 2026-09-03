"""Base chat-completions provider adapter.

Owns request/headers/payload construction, capability declarations,
response/stream normalization, and transient-vs-permanent error classification.
It depends only on the standard library, ``requests``, and the local
``contracts``/``config`` modules.
"""
import json
import os
from typing import Iterable, Optional

import requests

from app.providers.config import RETRIABLE_STATUS_CODES
from app.providers.contracts import (
    NormalizedChatResponse,
    NormalizedStreamEvent,
    NormalizedToolCall,
    ProviderCapabilities,
    ProviderError,
)


class LLMProviderAdapter:
    """Base chat-completions provider adapter.

    Subclasses declare ``name``/``env_prefix``/``default_base_url`` and may
    override ``capabilities_for`` to declare model-specific quirks. All
    payload/headers/response handling flows through these methods, so adding a
    provider means subclassing and registering — never branching in a workflow.
    """

    name = 'base'
    env_prefix = ''
    default_base_url = ''

    # -- configuration -------------------------------------------------
    def api_key(self):
        return os.environ.get(f'{self.env_prefix}_API_KEY', '')

    def env_model(self):
        return os.environ.get(f'{self.env_prefix}_MODEL', '')

    def base_url(self, request=None):
        return os.environ.get(f'{self.env_prefix}_BASE_URL', self.default_base_url)

    def require_config(self, model=None):
        if not self.api_key():
            raise RuntimeError(f'{self.env_prefix}_API_KEY is not set')
        if not (model if model is not None else self.env_model()):
            raise RuntimeError(f'{self.env_prefix}_MODEL is not set')

    def capabilities_for(self, model):
        return ProviderCapabilities()

    def configured_reasoning_effort(self):
        """Reasoning-effort knob value to report in provider settings, if any."""
        return None

    # -- request construction ------------------------------------------
    def build_headers(self):
        return {
            'Authorization': f'Bearer {self.api_key()}',
            'Content-Type': 'application/json',
        }

    def supports_thinking_for_model(self, model):
        return self.capabilities_for(model).supports_thinking

    def payload_options(
        self,
        model,
        tools,
        tool_choice,
        parallel_tool_calls,
        allow_thinking=True,
        force_thinking=False,
        force_tool_choice=False,
    ):
        """Effective request options after applying capability constraints."""
        capabilities = self.capabilities_for(model)
        thinking_enabled = bool(allow_thinking) and (
            capabilities.supports_thinking or (force_thinking and self.supports_thinking_for_model(model))
        )
        options = {
            'thinking_enabled': thinking_enabled,
            'tool_choice': tool_choice,
            'parallel_tool_calls': parallel_tool_calls,
        }
        if thinking_enabled:
            options['thinking'] = {'type': 'enabled'}
            options['reasoning_effort'] = capabilities.reasoning_effort or 'high'
            if tools:
                # Thinking mode rejects tool_choice and does not document parallel_tool_calls.
                options['tool_choice'] = None
                options['parallel_tool_calls'] = None
        elif (
            tools
            and tool_choice == 'required'
            and not capabilities.supports_tool_choice_required
            and not force_tool_choice
        ):
            options['tool_choice'] = None
        return options

    def build_payload(self, request):
        capabilities = self.capabilities_for(request.model)
        if request.json_mode and not capabilities.supports_json_mode:
            raise ProviderError(
                f'Provider {self.name} does not support JSON mode for model {request.model!r}',
                provider=self.name, kind='unsupported_feature',
            )
        options = self.payload_options(
            request.model,
            request.tools,
            request.tool_choice,
            request.parallel_tool_calls,
            allow_thinking=request.allow_thinking,
            force_thinking=request.force_thinking,
            force_tool_choice=request.force_tool_choice,
        )
        payload = {
            'model': request.model,
            'messages': request.messages,
        }
        if request.max_tokens is not None:
            payload['max_tokens'] = request.max_tokens
        if request.tools:
            payload['tools'] = request.tools
        if options.get('tool_choice') is not None:
            payload['tool_choice'] = options['tool_choice']
        if options.get('parallel_tool_calls') is not None and capabilities.supports_parallel_tool_calls:
            payload['parallel_tool_calls'] = options['parallel_tool_calls']
        if options.get('thinking_enabled'):
            payload['thinking'] = options['thinking']
            payload['reasoning_effort'] = options['reasoning_effort']
        if request.json_schema:
            payload['response_format'] = {
                'type': 'json_schema',
                'json_schema': {
                    'name': request.json_schema_name or 'structured_response',
                    'strict': True,
                    'schema': request.json_schema,
                },
            }
        elif request.json_mode:
            payload['response_format'] = {'type': 'json_object'}
        if request.stream:
            payload['stream'] = True
        return payload

    # -- response normalization ----------------------------------------
    def parse_response(self, data):
        if not isinstance(data, dict):
            raise ProviderError(
                f'Provider {self.name} returned a non-object response',
                provider=self.name, kind='malformed',
            )
        choices = data.get('choices') or []
        if not choices or not isinstance(choices[0], dict):
            raise ProviderError(
                f'Provider {self.name} returned a response without choices',
                provider=self.name, kind='malformed',
            )
        choice = choices[0]
        message = choice.get('message') or {}
        tool_calls = [
            NormalizedToolCall(
                id=call.get('id'),
                name=(call.get('function') or {}).get('name'),
                arguments=(call.get('function') or {}).get('arguments'),
                raw=call,
            )
            for call in (message.get('tool_calls') or [])
            if isinstance(call, dict)
        ]
        return NormalizedChatResponse(
            provider=self.name,
            model=data.get('model'),
            content=self.extract_text(data),
            tool_calls=tool_calls,
            finish_reason=choice.get('finish_reason'),
            usage=data.get('usage') or {},
            reasoning=message.get('reasoning_content') or message.get('reasoning'),
            reasoning_details=message.get('reasoning_details'),
            raw=data,
        )

    @staticmethod
    def extract_text(response_json):
        choices = response_json.get('choices') or [] if isinstance(response_json, dict) else []
        if not choices:
            return ''
        message = choices[0].get('message') or {}
        content = message.get('content')
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get('text'), str):
                    parts.append(item['text'])
            return ''.join(parts)
        return ''

    def iter_stream_events(self, response) -> Iterable[NormalizedStreamEvent]:
        tool_calls: dict[int, dict] = {}
        for line in response.iter_lines(decode_unicode=True):
            if not line:
                continue
            if not line.startswith('data: '):
                continue
            data_str = line[6:]
            if data_str.strip() == '[DONE]':
                break
            try:
                chunk = json.loads(data_str)
                choice = (chunk.get('choices') or [{}])[0]
                delta = choice.get('delta') or {}
                content = delta.get('content') or ''
                # tool call deltas (chat completions)
                for tc in delta.get('tool_calls') or []:
                    idx = tc.get('index', 0)
                    cur = tool_calls.get(idx, {"id": None, "name": None, "arguments": ""})
                    if tc.get('id'):
                        cur["id"] = tc.get('id')
                    fn = tc.get('function') or {}
                    if fn.get('name'):
                        cur["name"] = fn.get('name')
                    if fn.get('arguments'):
                        cur["arguments"] += fn.get('arguments')
                    tool_calls[idx] = cur
                # also handle finish with tool_calls in choice.message (rare)
                msg = choice.get('message') or {}
                for tc in msg.get('tool_calls') or []:
                    idx = len(tool_calls)
                    fn = tc.get('function') or {}
                    tool_calls[idx] = {"id": tc.get('id'), "name": fn.get('name'), "arguments": fn.get('arguments') or ""}
            except (json.JSONDecodeError, IndexError, KeyError):
                continue
            if content:
                yield NormalizedStreamEvent(kind='token', text=content)
        for tc in tool_calls.values():
            yield NormalizedStreamEvent(
                kind='tool_call',
                tool_call=NormalizedToolCall(id=tc.get('id'), name=tc.get('name'), arguments=tc.get('arguments') or "", raw=tc),
            )
        yield NormalizedStreamEvent(kind='done')

    # -- error classification -------------------------------------------
    def classify_error(self, error):
        if isinstance(error, ProviderError):
            return error
        if isinstance(error, requests.HTTPError):
            response = getattr(error, 'response', None)
            status_code = getattr(response, 'status_code', None)
            retryable = status_code in RETRIABLE_STATUS_CODES or (status_code is not None and status_code >= 500)
            return ProviderError(
                repr(error), provider=self.name, status_code=status_code,
                retryable=retryable, kind='http', original=error,
            )
        if isinstance(error, requests.Timeout):
            return ProviderError(repr(error), provider=self.name, retryable=True, kind='timeout', original=error)
        if isinstance(error, requests.ConnectionError):
            return ProviderError(repr(error), provider=self.name, retryable=True, kind='connection', original=error)
        return ProviderError(repr(error), provider=self.name, retryable=False, kind='http', original=error)

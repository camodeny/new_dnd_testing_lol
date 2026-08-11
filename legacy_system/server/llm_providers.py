"""Provider adapters and registry for LLM transport.

This module owns everything provider-specific: credential/URL/model resolution,
request headers, payload quirks, capability declarations, response/stream
normalization, and transient-vs-permanent error classification. It depends only
on the standard library and ``requests`` so both the Flask server and the
standalone automation workers can import it.

Application workflows (session DM loop, staged memory pipeline, automation
decisions) consume providers exclusively through ``provider_registry`` and the
normalized types below; they must not branch on provider names themselves.
"""
import json
import os
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

import requests


RETRIABLE_STATUS_CODES = {404, 408, 409, 425, 429}


def _enabled(value):
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'on', 'enabled'}


def default_max_attempts():
    return max(1, int(os.environ.get('LLM_MAX_ATTEMPTS', os.environ.get('OPENROUTER_MAX_ATTEMPTS', '4'))))


def retry_base_delay_seconds():
    return max(
        0.0,
        float(os.environ.get('LLM_RETRY_BASE_DELAY_SECONDS', os.environ.get('OPENROUTER_RETRY_BASE_DELAY_SECONDS', '1'))),
    )


def retry_max_delay_seconds():
    return max(
        retry_base_delay_seconds(),
        float(os.environ.get('LLM_RETRY_MAX_DELAY_SECONDS', os.environ.get('OPENROUTER_RETRY_MAX_DELAY_SECONDS', '8'))),
    )


def retry_delay_seconds(failed_attempt):
    return min(
        retry_base_delay_seconds() * (2 ** max(failed_attempt - 1, 0)),
        retry_max_delay_seconds(),
    )


@dataclass(frozen=True)
class ProviderCapabilities:
    """Declared feature support for one (provider, model) pair.

    Workflows must gate optional behavior on these declarations instead of
    checking provider or model names.
    """
    supports_json_mode: bool = True
    supports_tool_choice_required: bool = True
    supports_parallel_tool_calls: bool = True
    supports_thinking: bool = False
    reasoning_effort: Optional[str] = None


@dataclass
class ProviderRequest:
    messages: list
    model: str
    json_mode: bool = False
    tools: Optional[list] = None
    tool_choice: Optional[object] = None
    parallel_tool_calls: Optional[bool] = None
    allow_thinking: bool = True
    force_thinking: bool = False
    timeout_seconds: float = 60
    max_attempts: Optional[int] = None
    max_tokens: Optional[int] = None
    stream: bool = False


@dataclass
class NormalizedToolCall:
    id: Optional[str]
    name: Optional[str]
    arguments: object
    raw: dict = field(default_factory=dict)


@dataclass
class NormalizedChatResponse:
    provider: str
    model: Optional[str]
    content: str
    tool_calls: list
    finish_reason: Optional[str]
    usage: dict
    reasoning: Optional[str]
    reasoning_details: object
    raw: dict

    def message_view(self):
        """Canonical assistant-message payload for workflow consumption.

        Built solely from normalized fields, so workflows never need to index
        the provider's native response shape. ``raw`` remains available for
        audit/debugging only.
        """
        message = {'content': self.content}
        if self.tool_calls:
            message['tool_calls'] = [
                {
                    'id': tool_call.id,
                    'type': 'function',
                    'function': {'name': tool_call.name, 'arguments': tool_call.arguments},
                }
                for tool_call in self.tool_calls
            ]
        if self.reasoning:
            message['reasoning_content'] = self.reasoning
        if self.reasoning_details is not None:
            message['reasoning_details'] = self.reasoning_details
        return message


@dataclass
class NormalizedStreamEvent:
    kind: str  # 'token' | 'done'
    text: str = ''


class ProviderError(Exception):
    """Normalized provider failure.

    ``retryable`` distinguishes transient transport failures from permanent
    ones; ``kind`` is one of 'http', 'connection', 'timeout', 'malformed',
    'config', 'unsupported_feature'.
    """

    def __init__(self, message, *, provider=None, status_code=None, retryable=False, kind='http', original=None):
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.retryable = retryable
        self.kind = kind
        self.original = original


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

    def base_url(self):
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

    def payload_options(self, model, tools, tool_choice, parallel_tool_calls, allow_thinking=True, force_thinking=False):
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
        elif tools and tool_choice == 'required' and not capabilities.supports_tool_choice_required:
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
        if request.json_mode:
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
                delta = (chunk.get('choices') or [{}])[0].get('delta') or {}
                content = delta.get('content') or ''
            except (json.JSONDecodeError, IndexError, KeyError):
                continue
            if content:
                yield NormalizedStreamEvent(kind='token', text=content)
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


class OpenRouterAdapter(LLMProviderAdapter):
    name = 'openrouter'
    env_prefix = 'OPENROUTER'
    default_base_url = 'https://openrouter.ai/api/v1/chat/completions'


class OpenCodeGoAdapter(LLMProviderAdapter):
    name = 'opencode_go'
    env_prefix = 'OPENCODE_GO'
    default_base_url = 'https://opencode.ai/zen/go/v1/chat/completions'

    def configured_reasoning_effort(self):
        return os.environ.get('OPENCODE_GO_REASONING_EFFORT', 'high')

    def capabilities_for(self, model):
        is_deepseek_v4 = str(model or '').strip().lower().startswith('deepseek-v4-')
        thinking = is_deepseek_v4 and _enabled(os.environ.get('OPENCODE_GO_THINKING', 'disabled'))
        effort = (os.environ.get('OPENCODE_GO_REASONING_EFFORT', 'high') or 'high').strip().lower()
        if effort not in {'high', 'max'}:
            effort = 'high'
        return ProviderCapabilities(
            supports_tool_choice_required=not is_deepseek_v4,
            supports_parallel_tool_calls=True,
            supports_thinking=thinking,
            reasoning_effort=effort if thinking else None,
        )

    def supports_thinking_for_model(self, model):
        return str(model or '').strip().lower().startswith('deepseek-v4-')


class ProviderRegistry:
    def __init__(self):
        self._adapters = {}

    @staticmethod
    def normalize_name(value):
        return (value or '').strip().lower().replace('-', '_')

    def register(self, adapter):
        self._adapters[adapter.name] = adapter
        return adapter

    def unregister(self, name):
        self._adapters.pop(self.normalize_name(name), None)

    def names(self):
        return set(self._adapters)

    def get(self, name):
        normalized = self.normalize_name(name)
        adapter = self._adapters.get(normalized)
        if adapter is None:
            registered = ', '.join(sorted(self._adapters))
            raise RuntimeError(f"Unknown LLM provider {name!r} (registered: {registered})")
        return adapter

    def current(self):
        return self.get(os.environ.get('LLM_PROVIDER', 'openrouter') or 'openrouter')

    def _lenient_current_name(self):
        normalized = self.normalize_name(os.environ.get('LLM_PROVIDER', 'openrouter') or 'openrouter')
        return normalized if normalized in self._adapters else 'openrouter'

    def resolve_provider_and_model(self, model=None):
        """Resolve an optional 'provider/model' hint to (provider_name, model)."""
        raw_model = (model or '').strip()
        if '/' in raw_model:
            provider_hint, model_id = raw_model.split('/', 1)
            normalized = self.normalize_name(provider_hint)
            if normalized in self._adapters:
                return normalized, model_id
            provider = self._lenient_current_name()
            return provider, self.get(provider).env_model()
        provider = self._lenient_current_name()
        return provider, raw_model or self.get(provider).env_model()


provider_registry = ProviderRegistry()
provider_registry.register(OpenRouterAdapter())
provider_registry.register(OpenCodeGoAdapter())


class TransportHooks:
    """Optional observability hooks for ``execute_chat``/``stream_chat``."""

    def on_retry(self, attempt, max_attempts, delay_seconds, error):
        pass

    def on_error(self, error):
        pass


def execute_chat(adapter, request, *, hooks=None):
    """POST a chat completion through an adapter with transient retry.

    Returns a NormalizedChatResponse. The original exception is re-raised when
    attempts are exhausted or the failure is permanent.
    """
    adapter.require_config(request.model)
    payload = adapter.build_payload(request)
    attempt_limit = max(1, int(request.max_attempts or default_max_attempts()))
    for attempt in range(1, attempt_limit + 1):
        try:
            response = requests.post(
                adapter.base_url(),
                headers=adapter.build_headers(),
                json=payload,
                timeout=request.timeout_seconds,
            )
            response.raise_for_status()
            return adapter.parse_response(response.json())
        except Exception as error:
            classified = adapter.classify_error(error)
            if attempt < attempt_limit and classified.retryable:
                delay_seconds = retry_delay_seconds(attempt)
                if hooks is not None:
                    hooks.on_retry(attempt, attempt_limit, delay_seconds, error)
                time.sleep(delay_seconds)
                continue
            if hooks is not None:
                hooks.on_error(error)
            raise


def stream_chat(adapter, request, *, hooks=None):
    """POST a streaming chat completion and yield NormalizedStreamEvents.

    Connection/HTTP failures before any token is emitted are classified and
    retried like ``execute_chat``. Failures after emission has begun are not
    retried (the stream is not resumable); they are reported through the hooks
    and re-raised.
    """
    adapter.require_config(request.model)
    request.stream = True
    payload = adapter.build_payload(request)
    attempt_limit = max(1, int(request.max_attempts or default_max_attempts()))
    for attempt in range(1, attempt_limit + 1):
        try:
            response = requests.post(
                adapter.base_url(),
                headers=adapter.build_headers(),
                json=payload,
                timeout=request.timeout_seconds,
                stream=True,
            )
            response.raise_for_status()
        except Exception as error:
            classified = adapter.classify_error(error)
            if attempt < attempt_limit and classified.retryable:
                delay_seconds = retry_delay_seconds(attempt)
                if hooks is not None:
                    hooks.on_retry(attempt, attempt_limit, delay_seconds, error)
                time.sleep(delay_seconds)
                continue
            if hooks is not None:
                hooks.on_error(error)
            raise
        emitted = False
        try:
            for event in adapter.iter_stream_events(response):
                if event.kind == 'token':
                    emitted = True
                yield event
            return
        except Exception as error:
            classified = adapter.classify_error(error)
            if attempt < attempt_limit and classified.retryable and not emitted:
                delay_seconds = retry_delay_seconds(attempt)
                if hooks is not None:
                    hooks.on_retry(attempt, attempt_limit, delay_seconds, error)
                time.sleep(delay_seconds)
                continue
            if hooks is not None:
                hooks.on_error(error)
            raise

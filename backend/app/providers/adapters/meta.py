"""Meta (Llama API) chat-completions adapter (direct).

Native endpoint ``POST https://api.llama.com/v1/chat/completions`` with
``Authorization: Bearer <key>``. Unlike OpenAI, successful responses use a
top-level ``completion_message`` envelope (``content`` / ``tool_calls`` /
``stop_reason`` plus a ``metrics`` list), and streaming emits SSE
``event`` objects (``progress`` deltas / ``complete``).

Accepts ``META_*`` env vars, with ``LLAMA_*`` as fallback aliases since
Meta's own docs/SDK default to ``LLAMA_API_KEY``.
"""

import json
import os

from app.providers.adapters.base import LLMProviderAdapter
from app.providers.contracts import (
    NormalizedChatResponse,
    NormalizedStreamEvent,
    NormalizedToolCall,
    ProviderError,
)


class MetaAdapter(LLMProviderAdapter):
    name = 'meta'
    env_prefix = 'META'
    default_base_url = 'https://api.llama.com/v1/chat/completions'
    default_model = 'Llama-4-Maverick-17B-128E-Instruct-FP8'

    def api_key(self):
        return os.environ.get('META_API_KEY', '') or os.environ.get('LLAMA_API_KEY', '')

    def env_model(self):
        return os.environ.get('META_MODEL', '') or os.environ.get('LLAMA_MODEL', '')

    def base_url(self, request=None):
        return (
            os.environ.get('META_BASE_URL')
            or os.environ.get('LLAMA_BASE_URL')
            or self.default_base_url
        )

    def require_config(self, model=None):
        if not self.api_key():
            raise RuntimeError('META_API_KEY (or LLAMA_API_KEY) is not set')
        if not (model if model is not None else self.env_model()):
            raise RuntimeError('META_MODEL (or LLAMA_MODEL) is not set')

    def build_payload(self, request):
        """Translate the OpenAI-style base payload to native Llama params."""
        base = super().build_payload(request)
        payload = {
            'model': base['model'],
            'messages': base['messages'],
        }
        # Native token-limit param (base uses OpenAI's max_tokens).
        if 'max_tokens' in base:
            payload['max_completion_tokens'] = base['max_tokens']
        # Native tools/response_format/stream share OpenAI names/shapes.
        for key in ('tools', 'tool_choice', 'response_format', 'stream'):
            if key in base:
                payload[key] = base[key]
        # parallel_tool_calls / thinking / reasoning_effort are
        # OpenAI-specific and intentionally dropped.
        return payload

    @staticmethod
    def _message_text(content):
        if isinstance(content, str):
            return content
        if isinstance(content, dict):
            text = content.get('text')
            return text if isinstance(text, str) else ''
        return ''

    def parse_response(self, data):
        if not isinstance(data, dict) or not isinstance(data.get('completion_message'), dict):
            raise ProviderError(
                f'Provider {self.name} returned a response without completion_message',
                provider=self.name, kind='malformed',
            )
        message = data['completion_message']
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
        metrics = data.get('metrics')
        return NormalizedChatResponse(
            provider=self.name,
            model=data.get('model'),
            content=self._message_text(message.get('content')),
            tool_calls=tool_calls,
            finish_reason=message.get('stop_reason'),
            usage={'metrics': metrics} if isinstance(metrics, list) else {},
            reasoning=None,
            reasoning_details=None,
            raw=data,
        )

    def iter_stream_events(self, response):
        for line in response.iter_lines(decode_unicode=True):
            if not line or not line.startswith('data: '):
                continue
            data_str = line[6:]
            if data_str.strip() == '[DONE]':
                break
            try:
                chunk = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            event = chunk.get('event') if isinstance(chunk, dict) else None
            if not isinstance(event, dict):
                continue
            event_type = event.get('event_type')
            if event_type == 'complete':
                break
            if event_type == 'progress':
                delta = event.get('delta') or {}
                text = delta.get('text') if isinstance(delta, dict) else None
                if text:
                    yield NormalizedStreamEvent(kind='token', text=str(text))
        yield NormalizedStreamEvent(kind='done')

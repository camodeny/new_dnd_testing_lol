#!/usr/bin/env python3
"""Automation-side LLM access.

This module is a thin workflow layer (JSON-decision retry loop) over the shared
provider adapters in ``server/llm_providers.py``. All provider configuration,
payload quirks, retry classification, and response parsing live in the shared
provider registry — this file must not re-implement them.
"""
import json
import os
import sys

_SERVER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'server'))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from llm_providers import ProviderRequest, execute_chat, provider_registry  # noqa: E402


def resolve_provider_and_model(model=None):
    return provider_registry.resolve_provider_and_model(model)


def _json_object_from_text(text):
    stripped = (text or '').strip()
    if not stripped:
        raise RuntimeError('Provider returned an empty response')
    start = stripped.find('{')
    end = stripped.rfind('}')
    if start == -1 or end == -1 or end < start:
        raise RuntimeError(f'Provider response did not contain JSON: {text}')
    return json.loads(stripped[start:end + 1])


def post_chat_completion(messages, *, model=None, json_mode=False, timeout_seconds=120, max_attempts=None, allow_thinking=True):
    provider, resolved_model = resolve_provider_and_model(model=model)
    adapter = provider_registry.get(provider)
    normalized = execute_chat(
        adapter,
        ProviderRequest(
            messages=messages,
            model=resolved_model,
            json_mode=json_mode,
            allow_thinking=allow_thinking,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
        ),
    )
    return {
        'provider': provider,
        'model': resolved_model,
        'content': normalized.content,
        # Raw provider payload is kept only for persistence/audit records.
        'response': normalized.raw,
    }


def request_json_decision(system_prompt, prompt, *, model=None, timeout_seconds=120, max_attempts=None, allow_thinking=True):
    base_messages = [
        {'role': 'system', 'content': system_prompt},
        {'role': 'user', 'content': prompt},
    ]
    last_text = ''
    last_error = None

    for json_retry_count in range(2):
        messages = list(base_messages)
        if json_retry_count:
            messages.append({
                'role': 'user',
                'content': (
                    'Your previous reply was invalid because it was not a bare JSON object. '
                    'Reply again with exactly one JSON object and no explanation, markdown, or surrounding text.'
                ),
            })
        result = post_chat_completion(
            messages,
            model=model,
            json_mode=True,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            allow_thinking=allow_thinking,
        )
        response_json = result['response']
        response_text = result['content']
        last_text = response_text
        try:
            decision = _json_object_from_text(response_text)
            return {
                'decision': decision,
                'raw_response_text': response_text,
                'raw_response': response_json,
                'json_retry_count': json_retry_count,
                'provider': result['provider'],
                'model': result['model'],
                'usage': response_json.get('usage') if isinstance(response_json, dict) else {},
            }
        except Exception as error:
            last_error = error

    raise RuntimeError(f'{last_error}; last response: {last_text}')

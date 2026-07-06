#!/usr/bin/env python3
import json
import os
import time

import requests


OPENROUTER_API_KEY = os.environ.get('OPENROUTER_API_KEY', '')
OPENROUTER_MODEL = os.environ.get('OPENROUTER_MODEL', '')
OPENROUTER_BASE_URL = os.environ.get('OPENROUTER_BASE_URL', 'https://openrouter.ai/api/v1/chat/completions')

OPENCODE_GO_API_KEY = os.environ.get('OPENCODE_GO_API_KEY', '')
OPENCODE_GO_MODEL = os.environ.get('OPENCODE_GO_MODEL', '')
OPENCODE_GO_BASE_URL = os.environ.get('OPENCODE_GO_BASE_URL', 'https://opencode.ai/zen/go/v1/chat/completions')
OPENCODE_GO_THINKING = os.environ.get('OPENCODE_GO_THINKING', 'disabled')
OPENCODE_GO_REASONING_EFFORT = os.environ.get('OPENCODE_GO_REASONING_EFFORT', 'high')

LLM_PROVIDER = (os.environ.get('LLM_PROVIDER', 'openrouter') or 'openrouter').strip().lower()
LLM_MAX_ATTEMPTS = max(1, int(os.environ.get('LLM_MAX_ATTEMPTS', '4')))
LLM_RETRY_BASE_DELAY_SECONDS = max(0.0, float(os.environ.get('LLM_RETRY_BASE_DELAY_SECONDS', '1')))
LLM_RETRY_MAX_DELAY_SECONDS = max(LLM_RETRY_BASE_DELAY_SECONDS, float(os.environ.get('LLM_RETRY_MAX_DELAY_SECONDS', '8')))


def _env_model(provider):
    return OPENCODE_GO_MODEL if provider == 'opencode_go' else OPENROUTER_MODEL


def _api_key(provider):
    return OPENCODE_GO_API_KEY if provider == 'opencode_go' else OPENROUTER_API_KEY


def _base_url(provider):
    return OPENCODE_GO_BASE_URL if provider == 'opencode_go' else OPENROUTER_BASE_URL


def _normalize_provider(value):
    clean = (value or '').strip().lower().replace('-', '_')
    if clean in {'opencode_go', 'openrouter'}:
        return clean
    return None


def resolve_provider_and_model(model=None):
    raw_model = (model or '').strip()
    if '/' in raw_model:
        provider_hint, model_id = raw_model.split('/', 1)
        provider = _normalize_provider(provider_hint)
        if provider:
            return provider, model_id
        provider = _normalize_provider(LLM_PROVIDER) or 'openrouter'
        return provider, _env_model(provider)
    provider = _normalize_provider(LLM_PROVIDER) or 'openrouter'
    return provider, raw_model or _env_model(provider)


def _thinking_enabled(provider, model):
    return (
        provider == 'opencode_go'
        and str(model or '').strip().lower().startswith('deepseek-v4-')
        and str(OPENCODE_GO_THINKING or '').strip().lower() in {'1', 'true', 'yes', 'on', 'enabled'}
    )


def _retry_delay(attempt):
    return min(LLM_RETRY_BASE_DELAY_SECONDS * (2 ** max(attempt - 1, 0)), LLM_RETRY_MAX_DELAY_SECONDS)


def _is_retriable(error):
    if isinstance(error, requests.HTTPError):
        status_code = getattr(error.response, 'status_code', None)
        return status_code in {404, 408, 409, 425, 429} or (status_code is not None and status_code >= 500)
    return isinstance(error, (requests.ConnectionError, requests.Timeout))


def _extract_text(response_json):
    choices = response_json.get('choices') or []
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
    if not _api_key(provider):
        env_prefix = 'OPENCODE_GO' if provider == 'opencode_go' else 'OPENROUTER'
        raise RuntimeError(f'{env_prefix}_API_KEY is not set')
    if not resolved_model:
        env_prefix = 'OPENCODE_GO' if provider == 'opencode_go' else 'OPENROUTER'
        raise RuntimeError(f'{env_prefix}_MODEL is not set')

    payload = {
        'model': resolved_model,
        'messages': messages,
    }
    if json_mode:
        payload['response_format'] = {'type': 'json_object'}
    if _thinking_enabled(provider, resolved_model) and allow_thinking:
        payload['thinking'] = {'type': 'enabled'}
        effort = (OPENCODE_GO_REASONING_EFFORT or 'high').strip().lower()
        payload['reasoning_effort'] = effort if effort in {'high', 'max'} else 'high'

    attempt_limit = max(1, int(max_attempts or LLM_MAX_ATTEMPTS))
    last_error = None
    for attempt in range(1, attempt_limit + 1):
        try:
            response = requests.post(
                _base_url(provider),
                headers={
                    'Authorization': f'Bearer {_api_key(provider)}',
                    'Content-Type': 'application/json',
                },
                json=payload,
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            return {
                'provider': provider,
                'model': resolved_model,
                'response': response.json(),
            }
        except Exception as error:
            last_error = error
            if attempt >= attempt_limit or not _is_retriable(error):
                raise
            time.sleep(_retry_delay(attempt))
    raise last_error


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
        response_text = _extract_text(response_json)
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

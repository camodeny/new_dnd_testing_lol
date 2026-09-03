"""Transport: chat/stream execution with transient retry and hooks."""

import time

import requests

from app.providers.config import default_max_attempts, retry_delay_seconds


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
                adapter.base_url(request),
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
                adapter.base_url(request),
                headers=adapter.build_headers(),
                json=payload,
                timeout=request.timeout_seconds,
                stream=True,
            )
            response.raise_for_status()
            response.encoding = 'utf-8'
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

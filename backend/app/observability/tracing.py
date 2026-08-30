"""Correlation context and structured, content-safe logging helpers."""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import re
import uuid

from starlette.middleware.base import BaseHTTPMiddleware

_trace_id = contextvars.ContextVar("trace_id", default=None)
_operation_id = contextvars.ContextVar("operation_id", default=None)
_VALID_TRACE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_VALID_OPERATION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _valid(value: str | None, *, max_length: int = 64) -> str | None:
    value = (value or "").strip()
    pattern = _VALID_TRACE_ID if max_length == 64 else _VALID_OPERATION_ID
    return value if pattern.fullmatch(value) else None


def current_trace_id() -> str | None:
    return _trace_id.get()


def current_operation_id() -> str | None:
    return _operation_id.get()


@contextlib.contextmanager
def trace_context(trace_id: str | None = None, operation_id: str | None = None):
    trace_token = _trace_id.set(_valid(trace_id) or uuid.uuid4().hex)
    operation_token = _operation_id.set(_valid(operation_id, max_length=128))
    try:
        yield _trace_id.get()
    finally:
        _operation_id.reset(operation_token)
        _trace_id.reset(trace_token)


class TraceMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        supplied_trace = _valid(request.headers.get("X-Trace-ID"))
        supplied_operation = _valid(request.headers.get("X-Operation-ID"), max_length=128)
        with trace_context(supplied_trace, supplied_operation) as trace_id:
            request.state.trace_id = trace_id
            request.state.operation_id = supplied_operation
            response = await call_next(request)
            response.headers["X-Trace-ID"] = trace_id
            if supplied_operation:
                response.headers["X-Operation-ID"] = supplied_operation
            return response


def structured_log(logger: logging.Logger, level: int, event: str, **fields) -> None:
    """Emit correlation metadata without accepting prompt/content fields."""
    forbidden = {"prompt", "content", "messages", "response", "campaign_state"}
    safe = {key: value for key, value in fields.items() if key not in forbidden}
    safe.update(event=event, trace_id=current_trace_id(), operation_id=current_operation_id())
    logger.log(level, json.dumps(safe, default=str, sort_keys=True))


def otel_attributes(**fields) -> dict:
    """OpenTelemetry-compatible attributes; callers may attach them to any span."""
    return {
        "dnd.trace_id": current_trace_id(),
        "dnd.operation_id": current_operation_id(),
        **{f"dnd.{key}": value for key, value in fields.items()},
    }

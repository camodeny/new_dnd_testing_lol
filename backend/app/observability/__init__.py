"""Observability — tracing, AI run ledger, TTFT helpers.

Stubbed for #192. Telemetry must not corrupt gameplay; failures are soft.

Contract (from #192):
- One trace ID propagates API -> outbox -> queue -> worker.
- AI runs are ledgered independently from domain state.
- TTFT measures submission-to-first-visible-output with explicit timestamps.
"""
import logging
import time
import uuid

logger = logging.getLogger(__name__)


def new_trace_id() -> str:
    return uuid.uuid4().hex


def now_ms() -> int:
    return int(time.time() * 1000)


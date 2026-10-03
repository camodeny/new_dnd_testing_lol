"""In-memory realtime publisher for tests (install via ``set_realtime_publisher``)."""

from __future__ import annotations

import logging
from typing import Any

from app.realtime.service import RealtimePublisher

logger = logging.getLogger(__name__)


class InMemoryRealtimePublisher(RealtimePublisher):
    """Records publishes for assertions and can inject failures."""

    def __init__(self, *, fail_next: bool = False):
        self.published: list[dict[str, Any]] = []
        self.fail_next = fail_next
        self.fail_all = False

    def publish(self, channel: str, event: str, payload: dict[str, Any]) -> bool:
        if self.fail_all or self.fail_next:
            self.fail_next = False
            logger.warning("realtime publish injected failure channel=%s event=%s", channel, event)
            raise RuntimeError("injected realtime publish failure")
        rec = {"channel": channel, "event": event, "payload": dict(payload)}
        self.published.append(rec)
        logger.info("realtime publish (memory) channel=%s event=%s event_id=%s", channel, event, payload.get("event_id"))
        return True

    def clear(self) -> None:
        self.published.clear()

    def events_for_channel(self, channel: str) -> list[dict[str, Any]]:
        return [p for p in self.published if p["channel"] == channel]

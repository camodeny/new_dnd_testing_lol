"""Worker message envelope — issue #191.

Uses identifiers / expected revision rather than authoritative snapshots.
Worker must re-authorize and validate campaign scope on read; payload must
not be treated as truth. Sensitive data stays in Postgres, broker carries
only locators.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

# Keys that would indicate an embedded snapshot — forbidden in payload
FORBIDDEN_PAYLOAD_KEYS = {
    "snapshot",
    "campaign_snapshot",
    "campaign_state",
    "campaign_data",
    "state_snapshot",
    "full_campaign",
    "campaign",
    "entity_snapshot",
}


@dataclass
class WorkerEnvelope:
    """Stable logical envelope for queue delivery.

    job_id is the logical dedupe key (also WorkerExecution.id). At-least-once
    delivery may duplicate this envelope; consumers must be idempotent on job_id.
    """

    job_id: uuid.UUID
    job_type: str
    campaign_id: uuid.UUID | None = None
    aggregate_type: str = "campaign"
    aggregate_id: uuid.UUID | None = None
    expected_revision: int | None = None
    operation_id: str | None = None
    idempotency_key: str | None = None
    trace_id: str | None = None
    payload: dict | None = None
    attempt: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not self.job_type or not self.job_type.strip():
            raise ValueError("job_type is required")
        self.job_type = self.job_type.strip()
        if self.expected_revision is not None and self.expected_revision < 0:
            raise ValueError("expected_revision must be >= 0")
        # normalize UUIDs from strings
        if isinstance(self.job_id, str):
            self.job_id = uuid.UUID(self.job_id)
        if isinstance(self.campaign_id, str):
            self.campaign_id = uuid.UUID(self.campaign_id)
        if isinstance(self.aggregate_id, str):
            try:
                self.aggregate_id = uuid.UUID(self.aggregate_id)
            except ValueError:
                pass
        # validate payload does not carry snapshot truth
        if self.payload is not None:
            if not isinstance(self.payload, dict):
                raise ValueError("payload must be a dict of identifiers")
            lowered = {k.lower() for k in self.payload.keys()}
            offending = lowered & FORBIDDEN_PAYLOAD_KEYS
            if offending:
                raise ValueError(
                    f"payload must not contain snapshot keys {offending}; "
                    "use identifiers + expected_revision and re-read from Postgres"
                )
            # also forbid nested snapshot via values that look like full campaign dumps
            # (heuristic: if payload has >20 keys or contains large nested dict with name+revision)
            if "name" in lowered and "revision" in lowered and len(self.payload) > 5:
                raise ValueError(
                    "payload appears to embed campaign state; use identifiers only"
                )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["job_id"] = str(self.job_id)
        d["campaign_id"] = str(self.campaign_id) if self.campaign_id else None
        d["aggregate_id"] = str(self.aggregate_id) if self.aggregate_id else None
        d["created_at"] = self.created_at.isoformat() if self.created_at else None
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "WorkerEnvelope":
        return cls(
            job_id=uuid.UUID(data["job_id"]) if isinstance(data["job_id"], str) else data["job_id"],
            job_type=data["job_type"],
            campaign_id=uuid.UUID(data["campaign_id"]) if data.get("campaign_id") else None,
            aggregate_type=data.get("aggregate_type", "campaign"),
            aggregate_id=uuid.UUID(data["aggregate_id"]) if data.get("aggregate_id") else None,
            expected_revision=data.get("expected_revision"),
            operation_id=data.get("operation_id"),
            idempotency_key=data.get("idempotency_key"),
            trace_id=data.get("trace_id"),
            payload=data.get("payload"),
            attempt=data.get("attempt", 0),
            created_at=datetime.fromisoformat(data["created_at"]) if data.get("created_at") else datetime.now(timezone.utc),
        )

    def queue_lag_seconds(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        ca = self.created_at
        if ca.tzinfo is None:
            ca = ca.replace(tzinfo=timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return (now - ca).total_seconds()

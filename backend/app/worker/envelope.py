"""Worker job envelope — issue #191.

Uses identifiers / expected revision rather than authoritative snapshots.
Workers must re-authorize and validate campaign scope on read; payload must
not be treated as truth. Sensitive data stays in Postgres; the envelope
carries only locators.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

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
    """Stable logical envelope for one worker job.

    job_id is the logical dedupe key (also WorkerExecution.id). Sweeps may
    re-run the same envelope; handlers must be idempotent on job_id.
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


def new_envelope(
    *,
    job_type: str,
    campaign_id: uuid.UUID | str | None = None,
    aggregate_id: uuid.UUID | str | None = None,
    expected_revision: int | None = None,
    operation_id: str | None = None,
    idempotency_key: str | None = None,
    trace_id: str | None = None,
    payload: dict | None = None,
    job_id: uuid.UUID | None = None,
) -> WorkerEnvelope:
    """Create envelope with identifiers only; validates no snapshot."""
    if trace_id is None:
        from app.observability.tracing import current_trace_id
        trace_id = current_trace_id()
    return WorkerEnvelope(
        job_id=job_id or uuid.uuid4(),
        job_type=job_type,
        campaign_id=campaign_id,
        aggregate_id=aggregate_id,
        expected_revision=expected_revision,
        operation_id=operation_id,
        idempotency_key=idempotency_key,
        trace_id=trace_id,
        payload=payload,
    )

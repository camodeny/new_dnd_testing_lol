"""Durable, actor-scoped idempotent command execution — issue #189."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models.reliability import IdempotentCommand

logger = logging.getLogger(__name__)


class IdempotencyConflictError(Exception):
    """The same command identity was reused with materially different input."""


class IdempotencyInProgressError(Exception):
    """A durable record exists without a committed result; retry deterministically."""


def canonical_payload_hash(payload: object) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_safe_result(result: dict | list) -> dict | list:
    """Normalize UUID/datetime values before persisting and returning a result."""
    return json.loads(json.dumps(result, ensure_ascii=False, default=str))


def execute_idempotent_command(
    db: Session,
    *,
    actor_id: uuid.UUID,
    idempotency_key: str,
    command_type: str,
    scope_type: str,
    scope_id: str | uuid.UUID,
    payload: object,
    execute: Callable[[], dict | list],
) -> tuple[dict | list, bool]:
    """Execute and record a mutation atomically, or replay its committed result.

    The unique identity is actor + key + command type + scope. The initial insert
    serializes concurrent callers at the database unique constraint. The command
    record, mutation, and result commit together; exceptions roll all three back.
    """
    key = idempotency_key.strip()
    if not key or len(key) > 255:
        raise ValueError("Idempotency key must be between 1 and 255 characters")
    if not command_type.strip() or not scope_type.strip():
        raise ValueError("command_type and scope_type are required")

    identity = {
        "actor_id": actor_id,
        "idempotency_key": key,
        "command_type": command_type.strip(),
        "scope_type": scope_type.strip(),
        "scope_id": str(scope_id),
    }
    digest = canonical_payload_hash(payload)

    def find_existing() -> IdempotentCommand | None:
        return db.execute(
            select(IdempotentCommand).filter_by(**identity)
        ).scalars().first()

    existing = find_existing()
    if existing is not None:
        return _replay(existing, digest)

    record = IdempotentCommand(**identity, payload_hash=digest, status="in_progress")
    db.add(record)
    try:
        db.flush()
    except IntegrityError:
        # A concurrent transaction won. PostgreSQL waits for it to finish before
        # raising the uniqueness error, so the following read sees its outcome.
        db.rollback()
        existing = find_existing()
        if existing is None:
            logger.warning("idempotency ambiguous identity=%s", identity)
            raise IdempotencyInProgressError("Command outcome is not yet available; retry")
        return _replay(existing, digest)

    logger.info("idempotency first_execution identity=%s", identity)
    try:
        result = execute()
        if not isinstance(result, (dict, list)):
            raise TypeError("Idempotent command result must be a JSON object or array")
        result = _json_safe_result(result)
        record.result = result
        record.status = "completed"
        record.completed_at = datetime.now(timezone.utc)
        db.flush()
        db.commit()
    except Exception:
        db.rollback()
        raise
    return result, False


def _replay(record: IdempotentCommand, digest: str) -> tuple[dict | list, bool]:
    if record.payload_hash != digest:
        logger.warning("idempotency payload_mismatch command_id=%s", record.id)
        raise IdempotencyConflictError(
            "Idempotency key was already used with a different payload"
        )
    if record.status != "completed" or record.result is None:
        logger.warning("idempotency in_progress command_id=%s", record.id)
        raise IdempotencyInProgressError("Command outcome is not yet available; retry")
    logger.info("idempotency duplicate_hit command_id=%s", record.id)
    return record.result, True

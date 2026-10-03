"""Small validation/persistence helpers shared by the world writers and readers."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session


def normalize_idempotency_key(value: Any) -> str | None:
    key = str(value or "").strip() or None
    if key and len(key) > 128:
        raise ValueError("idempotency_key must be 128 characters or fewer")
    return key


def clamp_limit(limit: Any, *, default: int, maximum: int) -> int:
    try:
        value = int(limit if limit is not None else default)
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, maximum))


def require_provenance(value: Any, *, subject: str) -> dict:
    if not isinstance(value, dict) or not str(value.get("source") or "").strip():
        raise ValueError(f"provenance.source is required for {subject} changes")
    return dict(value)


def dialect_upsert_insert(db: Session):
    """``INSERT ... ON CONFLICT DO NOTHING`` construct for the bound dialect.

    Postgres and SQLite both support it. Returns None on dialects without
    upsert support (callers fall back to a savepoint-isolated insert).
    """
    try:
        dialect_name = db.get_bind().dialect.name
    except Exception:
        return None
    if dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        return pg_insert
    if dialect_name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        return sqlite_insert
    return None

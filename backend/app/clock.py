"""Shared UTC clock helpers."""

from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; treat them as UTC for arithmetic."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def ms_between(start: datetime | None, end: datetime | None) -> int:
    start, end = as_utc(start), as_utc(end)
    if start is None or end is None:
        return 0
    return max(0, int((end - start).total_seconds() * 1000))

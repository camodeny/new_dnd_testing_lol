"""Shared value-object primitives: the strict pydantic base and id coercion."""

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def coerce_uuid(value: Any, *, field: str) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"Invalid {field} {value!r}") from exc


def coerce_optional_uuid(value: Any, *, field: str = "id") -> uuid.UUID | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return coerce_uuid(value, field=field)

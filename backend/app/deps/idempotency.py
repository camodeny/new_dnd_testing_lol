"""FastAPI adapter for durable idempotent application commands."""

from collections.abc import Callable
import uuid

from fastapi import HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.idempotency import (
    IdempotencyConflictError,
    IdempotencyInProgressError,
    execute_idempotent_command,
)


def require_idempotency_key(request: Request, fallback: str | None = None) -> str:
    key = (request.headers.get("Idempotency-Key") or fallback or "").strip()
    if not key:
        raise HTTPException(
            status_code=400,
            detail="Idempotency-Key header (or operation_id) is required",
        )
    return key


def execute_http_idempotent(
    db: Session,
    response: Response,
    *,
    actor_id: uuid.UUID,
    idempotency_key: str,
    command_type: str,
    scope_type: str,
    scope_id: str | uuid.UUID,
    payload: object,
    execute: Callable[[], dict | list],
) -> dict | list:
    try:
        result, replayed = execute_idempotent_command(
            db,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            command_type=command_type,
            scope_type=scope_type,
            scope_id=scope_id,
            payload=payload,
            execute=execute,
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IdempotencyInProgressError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
            headers={"Retry-After": "1"},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    response.headers["X-Idempotent-Replay"] = "true" if replayed else "false"
    return result

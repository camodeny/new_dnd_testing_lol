"""Transport adapter for auth — extracts ``Request`` headers and maps errors to HTTP.

This module is intentionally transport-bound (imports ``fastapi.Request`` /
``HTTPException``). It is the **only** FastAPI adapter for auth; pure
profile/JWT resolution lives in ``app.auth.service`` and ``app.auth.jwt``
(application layer), which have no FastAPI dependency and raise
``app.auth.errors.AuthError``. Routers should import ``resolve_profile`` from
here; domain/application code should accept a resolved ``Profile`` or
``owner_id`` instead of importing this module.
"""
from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from app.auth.errors import AuthError
from app.auth.service import resolve_profile_pure


def resolve_profile(request: Request, db: Session):
    """FastAPI dependency / helper that resolves the caller's Profile.

    Thin wrapper over ``app.auth.service.resolve_profile_pure`` that handles
    HTTP header extraction and ``AuthError`` → ``HTTPException`` mapping so
    the service layer stays transport-agnostic.
    """
    auth_header = request.headers.get("authorization") or request.headers.get("Authorization")
    try:
        return resolve_profile_pure(db, auth_header)
    except AuthError as e:
        # All auth failures are 401 in the previous implementation; preserve detail text.
        raise HTTPException(status_code=401, detail=str(e)) from e

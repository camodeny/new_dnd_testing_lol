"""Auth service — pure application logic, no FastAPI transport imports.

This module resolves a Profile from a raw Authorization header value (or None)
plus a DB session. It does not import `fastapi.Request` or `HTTPException`;
callers in the transport layer (e.g. `app.deps.auth`) convert `ValueError`
into HTTP responses. This satisfies the dependency rule: domain/application
must not import transport concerns.

The Supabase JWT verification itself lives in `backend/auth.py` (infrastructure)
and may raise `fastapi.HTTPException`; this module catches that and re-raises
as `ValueError` so the application layer stays transport-agnostic.
"""
import os
import uuid as uuid_lib

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Profile

MOCK_USER_ID = uuid_lib.UUID("23f3b2d1-efb6-4785-9a67-fa7ca57d72a3")


def _get_or_create_mock_profile(db: Session) -> Profile:
    profile = db.get(Profile, MOCK_USER_ID)
    if not profile:
        try:
            # Vanilla Postgres (CI disposable DB) has a stub auth.users that must
            # contain the profile id for the FK. Supabase already has it; for
            # CI we ensure it exists (idempotent).
            try:
                from sqlalchemy import text

                db.execute(text("INSERT INTO auth.users (id) VALUES (:id) ON CONFLICT (id) DO NOTHING"), {"id": MOCK_USER_ID})
                db.flush()
            except Exception:
                # auth schema may not exist on older DBs or SQLite (which ignores FK)
                pass
            profile = Profile(id=MOCK_USER_ID, email="camdenpendergrass@gmail.com", username="dev")
            db.add(profile)
            db.commit()
            db.refresh(profile)
        except Exception:
            db.rollback()
            existing = db.execute(select(Profile)).scalars().first()
            if existing:
                return existing
            raise
    return profile


def is_mock_auth_allowed() -> bool:
    if os.getenv("ALLOW_MOCK_AUTH", "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    if os.getenv("NEXT_PUBLIC_MOCK_USER", "").strip().lower() in ("1", "true"):
        return True
    if os.getenv("VERCEL_ENV") == "production":
        return False
    return os.getenv("VERCEL_ENV") is None and os.getenv("NODE_ENV") != "production"


def _resolve_with_token(db: Session, token: str) -> Profile:
    # verify_supabase_jwt lives in infrastructure and raises HTTPException on failure;
    # convert to ValueError so this module has no FastAPI dependency.
    try:
        from auth import verify_supabase_jwt

        payload = verify_supabase_jwt(token)
    except Exception as e:  # HTTPException or other
        # Preserve detail if it's an HTTPException
        detail = getattr(e, "detail", None)
        msg = str(detail) if detail is not None else str(e)
        # Normalise to the messages callers expect
        if "Invalid token" in msg or "JWKS" in msg:
            raise ValueError(msg) from e
        raise ValueError(msg) from e

    sub = payload.get("sub")
    if not sub:
        raise ValueError("Token missing sub")
    try:
        uid = uuid_lib.UUID(str(sub))
    except ValueError:
        raise ValueError("Invalid sub format")
    prof = db.get(Profile, uid)
    if prof:
        return prof
    email = payload.get("email")
    md = payload.get("user_metadata") or {}
    username = md.get("username") or md.get("full_name") or md.get("name")
    # Ensure auth.users row exists for FK (CI vanilla Postgres)
    try:
        from sqlalchemy import text as _text

        db.execute(_text("INSERT INTO auth.users (id) VALUES (:id) ON CONFLICT (id) DO NOTHING"), {"id": uid})
        db.flush()
    except Exception:
        pass
    prof = Profile(id=uid, email=email, username=username)
    db.add(prof)
    db.commit()
    db.refresh(prof)
    return prof


def resolve_profile_pure(db: Session, auth_header: str | None) -> Profile:
    """Pure profile resolution from a raw ``Authorization`` header value.

    Args:
        db: SQLAlchemy session
        auth_header: value of ``Authorization`` header or ``None`` when absent

    Returns:
        Profile ORM object

    Raises:
        ValueError: with a human-readable message that the transport layer
            should map to HTTP 401 (or 403 where appropriate). Messages are
            kept compatible with the previous ``HTTPException(detail=...)``
            strings so API behavior is preserved.
    """
    has_credentials = bool(auth_header and auth_header.strip())
    if has_credentials:
        header = auth_header.strip()
        if not header.lower().startswith("bearer "):
            raise ValueError("Invalid Authorization header")
        token = header[7:].strip()
        if not token:
            raise ValueError("Missing token")
        return _resolve_with_token(db, token)

    if is_mock_auth_allowed():
        return _get_or_create_mock_profile(db)
    raise ValueError("Missing Authorization header")

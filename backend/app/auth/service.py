"""Auth service — pure application logic, no FastAPI transport imports.

This module resolves a Profile from a raw Authorization header value (or None)
plus a DB session. It does not import `fastapi.Request` or `HTTPException`;
callers in the transport layer (e.g. `app.deps.auth`) convert
``app.auth.errors.AuthError`` into HTTP responses. This satisfies the
dependency rule: domain/application must not import transport concerns.

JWT/JWKS verification lives in ``app.auth.jwt`` (also transport-agnostic) and
raises ``AuthError`` directly; there is a single profile upsert path
(``upsert_profile``) used by both the token and mock resolution flows.
"""

import os
import uuid as uuid_lib

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.errors import AuthError
from app.auth.jwt import verify_supabase_jwt
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
    # A production marker always wins over the development/test escape hatch.
    # This prevents a copied local environment file from disabling auth in a
    # production deployment. Comparisons are whitespace/case-insensitive so
    # " Production ", "PRODUCTION", etc. are treated as production.
    vercel_env = os.getenv("VERCEL_ENV", "").strip().lower()
    node_env = os.getenv("NODE_ENV", "").strip().lower()
    if vercel_env == "production":
        return False
    if node_env == "production":
        return False

    # Fail closed: backend-only flag is required; frontend flag never authorizes.
    # NEXT_PUBLIC_MOCK_USER controls frontend UI mocking and is not authorization.
    allow = os.getenv("ALLOW_MOCK_AUTH", "").strip().lower() in ("1", "true", "yes", "on")
    if not allow:
        return False

    # Missing/unknown deployment metadata fails closed even if ALLOW is present.
    # This prevents a self-hosted public deployment with unset metadata from
    # becoming fail-open if a local .env with ALLOW_MOCK_AUTH=true is copied.
    # Supported local/test environments must explicitly opt in via:
    #   - NODE_ENV=development|test  (generic / CI / local)
    #   - VERCEL_ENV=development|preview  (Vercel non-prod)
    if node_env in ("development", "test") or vercel_env in ("development", "preview", "test"):
        return True
    return False


def upsert_profile(
    db: Session,
    uid: uuid_lib.UUID,
    *,
    email: str | None = None,
    username: str | None = None,
) -> Profile:
    """Single upsert path for ``public.profiles``.

    Creates the profile if missing and refreshes email/username when new
    identity claims arrive. This is the only place that writes ``Profile``
    rows on auth resolution.
    """
    profile = db.get(Profile, uid)
    if profile:
        dirty = False
        if email and profile.email != email:
            profile.email = email
            dirty = True
        if username and not profile.username:
            profile.username = username
            dirty = True
        if dirty:
            db.commit()
            db.refresh(profile)
        return profile

    profile = Profile(id=uid, email=email, username=username)
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def _resolve_with_token(db: Session, token: str) -> Profile:
    payload = verify_supabase_jwt(token)

    sub = payload.get("sub")
    if not sub:
        raise AuthError("Token missing sub")
    try:
        uid = uuid_lib.UUID(str(sub))
    except ValueError:
        raise AuthError("Invalid sub format")

    email = payload.get("email")
    user_metadata = payload.get("user_metadata") or {}
    username = user_metadata.get("username") or user_metadata.get("full_name") or user_metadata.get("name")

    return upsert_profile(db, uid, email=email, username=username)


def resolve_profile_pure(db: Session, auth_header: str | None) -> Profile:
    """Pure profile resolution from a raw ``Authorization`` header value.

    Args:
        db: SQLAlchemy session
        auth_header: value of ``Authorization`` header or ``None`` when absent

    Returns:
        Profile ORM object

    Raises:
        AuthError: with a human-readable message that the transport layer
            should map to HTTP 401 (or 403 where appropriate). Messages are
            kept compatible with the previous ``HTTPException(detail=...)``
            strings so API behavior is preserved.
    """
    has_credentials = bool(auth_header and auth_header.strip())
    if has_credentials:
        header = auth_header.strip()
        if not header.lower().startswith("bearer "):
            raise AuthError("Invalid Authorization header")
        token = header[7:].strip()
        if not token:
            raise AuthError("Missing token")
        return _resolve_with_token(db, token)

    if is_mock_auth_allowed():
        return _get_or_create_mock_profile(db)
    raise AuthError("Missing Authorization header")

"""Auth service — pure application logic, no FastAPI transport imports.

This module resolves a Profile from a raw Authorization header value (or None)
plus a DB session. It does not import `fastapi.Request` or `HTTPException`;
callers in the transport layer (e.g. `app.deps.auth`) convert
``app.auth.errors.AuthError`` into HTTP responses. This satisfies the
dependency rule: domain/application must not import transport concerns.

JWT/JWKS verification lives in ``app.auth.jwt`` (also transport-agnostic) and
raises ``AuthError`` directly; there is a single profile upsert path
(``upsert_profile``) used by the token resolution flow.

There is no mock/dev auth bypass: every request must bear a real Supabase
JWT, including local development (see docs/local-dev-auth.md).
"""

import uuid as uuid_lib

from sqlalchemy.orm import Session

from app.auth.errors import AuthError
from app.auth.jwt import verify_supabase_jwt
from models.profiles import Profile

# Fixed UUID used by backend tests as fixture data only (profile/character
# ownership in test DBs). It grants nothing — tests monkeypatch
# resolve_profile rather than going through any auth bypass.
TEST_USER_ID = uuid_lib.UUID("23f3b2d1-efb6-4785-9a67-fa7ca57d72a3")


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
    if not has_credentials:
        raise AuthError("Missing Authorization header")
    header = auth_header.strip()
    if not header.lower().startswith("bearer "):
        raise AuthError("Invalid Authorization header")
    token = header[7:].strip()
    if not token:
        raise AuthError("Missing token")
    return _resolve_with_token(db, token)

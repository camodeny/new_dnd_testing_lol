"""Auth dependency helpers — application layer.

Domain logic should not import this module; routers use it to resolve the
caller's Profile. Pure service functions accept `profile`/`owner_id` instead.
"""
import logging
import os
import uuid as uuid_lib

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Profile

logger = logging.getLogger(__name__)

MOCK_USER_ID = uuid_lib.UUID("23f3b2d1-efb6-4785-9a67-fa7ca57d72a3")


def _get_or_create_mock_profile(db: Session) -> Profile:
    profile = db.get(Profile, MOCK_USER_ID)
    if not profile:
        try:
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


def resolve_profile(request: Request, db: Session) -> Profile:
    """Resolve profile via verified JWT; mock only when no credentials and mock is explicitly allowed."""
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    has_credentials = bool(auth and auth.strip())
    if has_credentials:
        if not auth.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="Invalid Authorization header")
        token = auth[7:].strip()
        if not token:
            raise HTTPException(status_code=401, detail="Missing token")
        from auth import verify_supabase_jwt

        payload = verify_supabase_jwt(token)  # raises 401 on failure
        sub = payload.get("sub")
        if not sub:
            raise HTTPException(status_code=401, detail="Token missing sub")
        try:
            uid = uuid_lib.UUID(str(sub))
        except ValueError:
            raise HTTPException(status_code=401, detail="Invalid sub format")
        prof = db.get(Profile, uid)
        if prof:
            return prof
        email = payload.get("email")
        md = payload.get("user_metadata") or {}
        username = md.get("username") or md.get("full_name") or md.get("name")
        prof = Profile(id=uid, email=email, username=username)
        db.add(prof)
        db.commit()
        db.refresh(prof)
        return prof

    if is_mock_auth_allowed():
        return _get_or_create_mock_profile(db)
    raise HTTPException(status_code=401, detail="Missing Authorization header")


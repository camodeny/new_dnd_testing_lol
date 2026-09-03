"""Auth transport — /api/me and /api/auth/config."""
import os

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.deps.auth import resolve_profile
from database import get_db

router = APIRouter()


@router.get("/api/me")
def me(request: Request, db: Session = Depends(get_db)):
    """Return the authenticated user's profile. Verifies Supabase JWT via Authorization header."""
    profile = resolve_profile(request, db)
    return {"user": profile.to_dict()}


@router.get("/api/auth/config")
def auth_config():
    supabase_url = os.getenv("SUPABASE_URL") or os.getenv("NEXT_PUBLIC_SUPABASE_URL") or ""
    has_key = bool(
        os.getenv("SUPABASE_PUBLISHABLE_KEY")
        or os.getenv("NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY")
        or os.getenv("SUPABASE_SECRET_KEY")
    )
    return {
        "sso_enabled": False,
        "supabase_url": supabase_url or None,
        "supabase_configured": bool(supabase_url and has_key),
    }

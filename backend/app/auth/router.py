"""Auth transport — /api/me and /api/auth/config."""
import os

from fastapi import APIRouter, Depends

from auth import get_current_profile
from models.profiles import Profile

router = APIRouter()


@router.get("/api/me")
def me(profile: Profile = Depends(get_current_profile)):
    """Return the authenticated user's profile. Verifies Supabase JWT via Authorization header."""
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


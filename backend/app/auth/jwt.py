"""Supabase JWT/JWKS verification — application/infrastructure, no FastAPI.

Flow:
  frontend (supabase-js) -> login -> Supabase returns JWT (access_token)
  frontend -> `Authorization: Bearer <jwt>` -> backend verifies via Supabase JWKS

Env (new Supabase integration only — no legacy fallbacks):
  SUPABASE_URL / NEXT_PUBLIC_SUPABASE_URL
  SUPABASE_PUBLISHABLE_KEY / NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY (sb_publishable_...)
  SUPABASE_SECRET_KEY (sb_secret_..., for health checks only)
  SUPABASE_JWKS_URL (optional override)

Supabase signs JWTs with RS256/ES256; we fetch JWKS from
`{SUPABASE_URL}/auth/v1/.well-known/jwks.json`.

This module raises ``app.auth.errors.AuthError`` (never FastAPI's
``HTTPException``); the transport adapter in ``app.deps.auth`` maps that to an
HTTP response.
"""

import os
import time

import httpx
from jose import jwt, JWTError

from app.auth.errors import AuthError

def _env_first(*names: str) -> str:
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return ""


SUPABASE_URL = _env_first("SUPABASE_URL", "NEXT_PUBLIC_SUPABASE_URL").strip().rstrip("/")
SUPABASE_PUBLISHABLE_KEY = _env_first(
    "SUPABASE_PUBLISHABLE_KEY",
    "NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY",
)
SUPABASE_SECRET_KEY = os.getenv("SUPABASE_SECRET_KEY", "")
SUPABASE_JWKS_URL = os.getenv("SUPABASE_JWKS_URL", "")

# Simple in-memory JWKS cache
_jwks_cache: dict | None = None
_jwks_fetched_at: float = 0
JWKS_TTL = 600  # 10m
EXPECTED_AUDIENCE = "authenticated"


def _get_jwks_url() -> str | None:
    if SUPABASE_JWKS_URL:
        return SUPABASE_JWKS_URL
    if SUPABASE_URL:
        return f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json"
    return None


def _get_expected_issuer() -> str | None:
    if not SUPABASE_URL.strip():
        return None
    return f"{SUPABASE_URL}/auth/v1"


def _fetch_jwks() -> dict | None:
    global _jwks_cache, _jwks_fetched_at
    now = time.time()
    if _jwks_cache and (now - _jwks_fetched_at) < JWKS_TTL:
        return _jwks_cache

    url = _get_jwks_url()
    if not url:
        return None
    try:
        headers = {}
        if SUPABASE_PUBLISHABLE_KEY:
            headers["apikey"] = SUPABASE_PUBLISHABLE_KEY
        resp = httpx.get(url, timeout=5.0, headers=headers)
        resp.raise_for_status()
        _jwks_cache = resp.json()
        _jwks_fetched_at = now
        return _jwks_cache
    except Exception:
        return _jwks_cache  # stale if available, else None


def verify_supabase_jwt(token: str) -> dict:
    """Verify token via JWKS (RS256/ES256).

    Returns decoded payload. Raises ``AuthError`` on failure.
    """
    if not token:
        raise AuthError("Missing token")

    expected_issuer = _get_expected_issuer()
    if not expected_issuer:
        # A JWKS override identifies where keys are fetched, not which Supabase
        # project is trusted. Without the project URL, issuer validation cannot
        # be performed safely.
        raise AuthError("Invalid token")

    jwks = _fetch_jwks()
    if not jwks or not isinstance(jwks.get("keys"), list) or not jwks["keys"]:
        raise AuthError("Invalid token")

    try:
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header.get("kid")
        key = next((k for k in jwks["keys"] if k.get("kid") == kid), None)
        candidates = [key] if key else jwks["keys"]
        for k in candidates:
            try:
                payload = jwt.decode(
                    token,
                    k,
                    algorithms=["RS256", "RS512", "ES256", "ES512"],
                    audience=EXPECTED_AUDIENCE,
                    issuer=expected_issuer,
                    options={"require_aud": True, "require_iss": True},
                )
                return payload
            except JWTError:
                continue
        raise AuthError("Invalid token")
    except AuthError:
        raise
    except Exception:
        raise AuthError("Invalid token")

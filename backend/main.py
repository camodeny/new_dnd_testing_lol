import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from auth import get_current_profile
from database import Base, db_healthcheck, engine, get_database_url
from models import Profile

logger = logging.getLogger(__name__)


def _run_migrations():
    """Run `alembic upgrade head` on startup when DATABASE_URL is set.

    This makes deploys self-migrating — no manual `alembic upgrade head` needed.
    Uses Alembic's Python API (same as CLI) and is safe to run concurrently;
    Alembic acquires a row lock on alembic_version.
    """
    db_url = get_database_url()
    if not db_url:
        logger.info("Skipping migrations — DATABASE_URL not set")
        return
    try:
        from alembic.config import Config
        from alembic import command

        alembic_ini = os.path.join(os.path.dirname(__file__), "alembic.ini")
        if not os.path.exists(alembic_ini):
            logger.warning("alembic.ini not found at %s — falling back to create_all", alembic_ini)
            Base.metadata.create_all(bind=engine)
            return

        cfg = Config(alembic_ini)
        cfg.set_main_option("sqlalchemy.url", db_url)
        # Ensure backend/ is on path for env.py imports when running from Vercel's wd
        cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "alembic"))
        command.upgrade(cfg, "head")
        logger.info("Alembic migrations applied (head)")
    except Exception as e:
        # Don't crash boot — log and fallback to create_all so health still works
        logger.warning("Alembic upgrade failed (%s) — falling back to create_all: %s", type(e).__name__, e)
        try:
            if engine is not None:
                Base.metadata.create_all(bind=engine)
        except Exception as ce:
            logger.warning("create_all fallback also failed: %s", ce)

APP_NAME = "dnd-backend"
APP_VERSION = "0.1.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Auto-migrate on boot so deploys don't require manual `alembic upgrade head`.
    # Checks DATABASE_URL/POSTGRES_URL inside _run_migrations so it works even if
    # engine was None at import time (e.g. env injected after module load).
    _run_migrations()
    yield


app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health():
    db_ok = db_healthcheck()
    has_db_env = any(
        os.getenv(k) for k in ("POSTGRES_URL", "POSTGRES_PRISMA_URL", "POSTGRES_URL_NON_POOLING", "DATABASE_URL")
    )
    return {
        "status": "ok",
        "service": APP_NAME,
        "version": APP_VERSION,
        "db": "ok" if db_ok else "unconfigured" if not has_db_env else "unreachable",
    }


@app.get("/api/hello")
def hello(name: str = "world"):
    return {"message": f"Hello, {name}! From the Python (FastAPI) backend."}


# ── Auth (Supabase JWT -> profiles) ──────────────────────────────────────


@app.get("/api/me")
def me(profile: Profile = Depends(get_current_profile)):
    """Return the authenticated user's profile. Verifies Supabase JWT via Authorization header."""
    return {"user": profile.to_dict()}


@app.get("/api/auth/config")
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


@app.get("/api/db/ping")
def db_ping():
    """Quick SQLAlchemy sanity check — remove after wiring."""
    if engine is None:
        return {"db": "unconfigured", "hint": "Set POSTGRES_URL in Vercel env"}
    try:
        with engine.connect() as conn:
            val = conn.execute(text("SELECT 1")).scalar()
        return {"db": "ok", "result": val}
    except Exception as e:
        return {"db": "error", "error": str(e)}

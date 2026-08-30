"""Health transport."""
import os

from fastapi import APIRouter
from sqlalchemy import text

from database import db_healthcheck, engine

router = APIRouter()

APP_NAME = "dnd-backend"
APP_VERSION = "0.1.0"


@router.get("/api/health")
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


@router.get("/api/hello")
def hello(name: str = "world"):
    return {"message": f"Hello, {name}! From the Python (FastAPI) backend."}


@router.get("/api/db/ping")
def db_ping():
    if engine is None:
        return {"db": "unconfigured", "hint": "Set POSTGRES_URL in Vercel env"}
    try:
        with engine.connect() as conn:
            val = conn.execute(text("SELECT 1")).scalar()
        return {"db": "ok", "result": val}
    except Exception as e:
        return {"db": "error", "error": str(e)}


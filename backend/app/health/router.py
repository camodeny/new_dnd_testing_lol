"""Health transport."""
import os

from fastapi import APIRouter

from app.factory import APP_NAME, APP_VERSION
from database import db_healthcheck

router = APIRouter()


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

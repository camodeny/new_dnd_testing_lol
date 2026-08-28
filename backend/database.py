"""SQLAlchemy engine/session for Supabase Postgres on Vercel.

Vercel's Supabase integration injects one of:
- DATABASE_URL
- POSTGRES_URL (pooled, port 6543 via Supavisor)
- POSTGRES_URL_NON_POOLING (direct, port 5432)

Pooled is preferred in serverless (1 connection per invocation).
We keep pool small; Supavisor handles multiplexing.
"""

import os
from typing import Generator

from dotenv import load_dotenv

load_dotenv()

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker, Session

def get_database_url() -> str:
    """Resolve DB URL from Vercel Supabase integration (new keys only)."""
    url = (
        os.getenv("POSTGRES_URL")
        or os.getenv("POSTGRES_PRISMA_URL")
        or os.getenv("POSTGRES_URL_NON_POOLING")
        or os.getenv("DATABASE_URL")
        or ""
    )
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url and "sslmode" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}sslmode=require"
    return url


DATABASE_URL = get_database_url()

# Fallback for local dev without DB (health still works, get_db raises)
if DATABASE_URL:
    engine = create_engine(
        DATABASE_URL,
        pool_size=5,          # small; Vercel funcs are short-lived
        max_overflow=5,       # allow burst; Supavisor multiplexes
        pool_pre_ping=True,   # drop stale connections after cold start
        pool_recycle=300,     # recycle every 5m
        echo=False,
    )
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
else:
    engine = None  # type: ignore
    SessionLocal = None  # type: ignore


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    """FastAPI Depends — yields a Session and ensures close."""
    if SessionLocal is None:
        raise RuntimeError(
            "DATABASE_URL / POSTGRES_URL not set — "
            "add Supabase Postgres URL to Vercel env (or .env.local for dev)"
        )
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def db_healthcheck() -> bool:
    """Lightweight check used by /api/health."""
    if engine is None:
        return False
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False

"""Explicit migration runner for production deploys.

Replaces the former FastAPI cold-start auto-migration.

Usage:
  Local:      alembic upgrade head
              # or
              python -m scripts.migrate
              DATABASE_URL=... python scripts/migrate.py

  Production (Vercel/Supabase):
              POSTGRES_URL_NON_POOLING="..." python -m scripts.migrate
              # exit code !=0 means migration failed — do not promote the release

This script:
- prefers a direct/non-pooling migration URL over pooled runtime URLs
- logs alembic_version before and after
- runs `alembic upgrade head` via the Alembic Python API
- exits non-zero and logs failure details on error (no silent fallback to create_all)
"""

from __future__ import annotations

import logging
import os
import sys
import traceback

# Ensure backend/ is importable when run as `python scripts/migrate.py`
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

logger = logging.getLogger("migrate")
# Use a dedicated handler so Alembic's fileConfig (which resets root logging
# to WARNING) does not suppress our INFO logs after `command.upgrade`.
_handler = logging.StreamHandler(sys.stderr)
_handler.setLevel(logging.INFO)
_handler.setFormatter(logging.Formatter("%(levelname)s [%(name)s] %(message)s"))
logger.addHandler(_handler)
logger.setLevel(logging.INFO)
logger.propagate = False
# Also configure root for any early logs before alembic reconfigures it
logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")


def get_migration_database_url() -> str:
    """Resolve a migration connection, preferring direct/session endpoints.

    Runtime traffic can use Supavisor's pooled URL, but Alembic needs a stable
    session and should use the non-pooling connection whenever one is supplied.
    """
    for key in (
        "POSTGRES_URL_NON_POOLING",
        "SUPABASE_DB_URL",
        "DATABASE_URL",
        "POSTGRES_PRISMA_URL",
        "POSTGRES_URL",
    ):
        value = os.environ.get(key)
        if value:
            if value.startswith("postgres://"):
                value = value.replace("postgres://", "postgresql://", 1)
            return value
    return ""


def _get_current_revision(db_url: str) -> str | None:
    """Best-effort: read alembic_version.version_num if the table exists."""
    try:
        from sqlalchemy import create_engine, text
        from sqlalchemy.pool import NullPool

        # Use NullPool — this is a short-lived CLI, not a server.
        engine = create_engine(db_url, poolclass=NullPool)
        with engine.connect() as conn:
            try:
                row = conn.execute(text("SELECT version_num FROM alembic_version")).fetchone()
                return row[0] if row else None
            except Exception:
                # table may not exist yet on a fresh DB
                return None
    except Exception as e:
        logger.debug("Could not read current revision: %s", e)
        return None


def main() -> int:
    db_url = get_migration_database_url()
    if not db_url:
        logger.error(
            "No database URL found. Set POSTGRES_URL_NON_POOLING, SUPABASE_DB_URL, "
            "DATABASE_URL, POSTGRES_PRISMA_URL, or POSTGRES_URL."
        )
        return 1

    # Avoid logging the full URL with password at INFO; log host only.
    safe_host = db_url.split("@")[-1].split("/")[0] if "@" in db_url else "(unknown host)"
    logger.info("Migration starting — host: %s", safe_host)

    before = _get_current_revision(db_url)
    logger.info("Current revision before: %s", before or "(none)")

    alembic_ini = os.path.join(os.path.dirname(os.path.dirname(__file__)), "alembic.ini")
    if not os.path.exists(alembic_ini):
        logger.error("alembic.ini not found at %s", alembic_ini)
        return 1

    try:
        from alembic.config import Config
        from alembic import command

        cfg = Config(alembic_ini)
        # ConfigParser interpolates % — escape for URL
        cfg.set_main_option("sqlalchemy.url", db_url.replace("%", "%%"))
        cfg.set_main_option(
            "script_location", os.path.join(os.path.dirname(os.path.dirname(__file__)), "alembic")
        )
        command.upgrade(cfg, "head")
        # fileConfig in alembic/env.py historically disabled existing loggers;
        # ensure our migrate logger stays enabled even if env.py is called earlier.
        logger.disabled = False
        logger.setLevel(logging.INFO)
    except SystemExit:
        # alembic command may call sys.exit on error
        raise
    except Exception:
        logger.disabled = False
        logger.setLevel(logging.INFO)
        logger.error("Migration failed — deploy must not be considered healthy")
        logger.error(traceback.format_exc())
        # Re-log the before revision to aid operator triage
        try:
            after_fail = _get_current_revision(db_url)
            logger.error("Revision after failure: %s (before was %s)", after_fail or "(none)", before or "(none)")
        except Exception:
            pass
        return 1

    after = _get_current_revision(db_url)
    logger.info("Migration complete — revision before=%s after=%s", before or "(none)", after or "(none)")

    if after is None and before is None:
        logger.warning("Could not verify revision after migration — check alembic_version table")

    # alembic_version should have advanced or been created. If we started with no
    # revision and still have none, something is wrong.
    if after is None:
        logger.error("Migration appeared to succeed but alembic_version is still empty")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

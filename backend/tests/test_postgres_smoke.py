"""Disposable Postgres smoke — proves CI DB is actually used, not just migrated.

Issue #286 acceptance: database-backed tests run against an isolated disposable
Postgres database. The existing DB-heavy suites use SQLite overrides, so this
small integration test exercises the real app Postgres path via DATABASE_URL.

- Skips locally when no CI Postgres URL is present (so ./scripts/ci/backend.sh
  remains runnable without a DB).
- In CI, connects via the disposable URL (with ?sslmode=disable), verifies
  Alembic migrated the schema, and does a minimal ORM round-trip.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

def _get_db_url() -> str:
    return (
        os.getenv("POSTGRES_URL_NON_POOLING")
        or os.getenv("POSTGRES_URL")
        or os.getenv("DATABASE_URL")
        or ""
    )


def _should_run() -> bool:
    url = _get_db_url()
    return bool(url and "ci_test" in url)


def _skip_if_no_postgres():
    if not _should_run():
        pytest.skip("No disposable Postgres DB URL — skipping Postgres smoke (set DATABASE_URL with ci_test for CI)")


def test_ci_database_url_respects_sslmode_disable(monkeypatch):
    """database.get_database_url() must preserve ?sslmode=disable for CI.

    database.py appends sslmode=require when no sslmode is present, which
    would break the non-SSL disposable service. CI URLs are required to carry
    ?sslmode=disable so the real app stack can connect.
    """
    _skip_if_no_postgres()
    # Ensure the app's normal DB config sees the disposable URL. database.py
    # loads backend/.env at import (which contains a Supabase POSTGRES_URL),
    # so we must override that env var to point at the disposable service.
    db_url = _get_db_url()
    monkeypatch.setenv("POSTGRES_URL", db_url)
    monkeypatch.setenv("POSTGRES_URL_NON_POOLING", db_url)
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("POSTGRES_PRISMA_URL", "")
    monkeypatch.setenv("SUPABASE_DB_URL", "")

    import database

    # get_database_url reads env each call, so it should see the CI URL as-is
    url = database.get_database_url()
    assert "sslmode=disable" in url, f"CI URL must contain sslmode=disable, got {url!r}"
    assert "sslmode=require" not in url, "CI URL must not be rewritten to sslmode=require"


def test_disposable_postgres_migrated_schema_usable():
    """Connect to the migrated disposable Postgres and do a minimal ORM write/read.

    Proves the database-backed path is not just SQLite overrides.
    """
    _skip_if_no_postgres()
    db_url = _get_db_url()
    engine = create_engine(db_url, poolclass=NullPool)

    # 1) Basic connectivity
    with engine.connect() as conn:
        assert conn.execute(text("SELECT 1")).scalar() == 1

        # 2) Alembic actually migrated this DB (alembic_version populated)
        row = conn.execute(text("SELECT version_num FROM alembic_version")).fetchone()
        assert row is not None, "alembic_version should have a row after scripts.migrate"
        assert isinstance(row[0], str) and len(row[0]) > 0

    # 3) ORM round-trip via real models (not SQLite). Insert a throwaway profile+campaign,
    #    verify read, then clean up. This exercises the real Postgres type mapping (UUID, JSONB, etc.).
    from sqlalchemy.orm import sessionmaker
    import models  # noqa: F401 — ensure models are imported so Base.metadata is populated

    Session = sessionmaker(bind=engine)
    pid = uuid.uuid4()
    cid = uuid.uuid4()
    with Session() as db:
        # Use raw Profile/Campaign to avoid triggering app business logic that might
        # depend on SQLite-specific behavior.
        profile = models.Profile(id=pid, email=f"ci-smoke-{pid.hex[:8]}@example.com")
        db.add(profile)
        db.commit()

        # Verify we can read it back
        found = db.get(models.Profile, pid)
        assert found is not None
        assert found.email == profile.email

        # Campaign with FK to profile
        campaign = models.Campaign(id=cid, owner_id=pid, name="ci-smoke-campaign")
        db.add(campaign)
        db.commit()

        found_c = db.get(models.Campaign, cid)
        assert found_c is not None
        assert found_c.owner_id == pid
        assert found_c.name == "ci-smoke-campaign"

        # Clean up (campaign first due to FK)
        db.delete(found_c)
        db.delete(found)
        db.commit()

        # Verify cleanup
        assert db.get(models.Campaign, cid) is None
        assert db.get(models.Profile, pid) is None

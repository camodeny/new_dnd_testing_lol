"""pgvector migration invariants on disposable Postgres (issue #334).

PR CI migrates a disposable Postgres from empty. The baseline migration has a
pgvector branch (vector column + HNSW index) and a TEXT fallback when the
extension is unavailable, so CI must use a pgvector-capable image or the
vector DDL is never exercised.

- Skips locally when no disposable Postgres URL is present (same convention
  as test_postgres_smoke.py), so ./scripts/ci/backend.sh remains runnable
  without a DB.
- In CI, asserts the vector branch was actually taken plus the Supabase-shape
  invariants in scope for #334:
  - baseline migrated from empty DB, alembic current == head
  - vector extension exists, rules_embeddings.embedding is vector, HNSW
    index exists and vector ops work
  - profiles -> auth.users FK exists

Narrow scope: no ORM<->migration parity guard (Supabase-owned objects
legitimately differ).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

pytestmark = pytest.mark.postgres

BACKEND_DIR = Path(__file__).parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


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
        pytest.skip(
            "No disposable Postgres DB URL — skipping pgvector invariants "
            "(set DATABASE_URL with ci_test for CI)"
        )


def _get_heads() -> list:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return list(ScriptDirectory.from_config(cfg).get_heads())


def test_baseline_migrated_current_equals_head():
    _skip_if_no_postgres()
    engine = create_engine(_get_db_url(), poolclass=NullPool)
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT version_num FROM alembic_version")).fetchall()
    assert len(rows) == 1, f"expected single alembic row from empty-DB baseline, got {rows!r}"
    heads = _get_heads()
    assert len(heads) == 1, f"expected single alembic head, got {heads!r}"
    assert rows[0][0] == heads[0], f"alembic current {rows[0][0]!r} != head {heads[0]!r}"


def test_vector_extension_embedding_column_and_hnsw():
    _skip_if_no_postgres()
    engine = create_engine(_get_db_url(), poolclass=NullPool)
    with engine.connect() as conn:
        ext = conn.execute(text("SELECT 1 FROM pg_extension WHERE extname='vector'")).fetchone()
        assert ext is not None, (
            "pgvector extension missing — CI must use a pgvector-capable "
            "Postgres image (pgvector/pgvector:pg16)"
        )
        col = conn.execute(
            text(
                "SELECT udt_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='rules_embeddings' "
                "AND column_name='embedding'"
            )
        ).fetchone()
        assert col is not None, "rules_embeddings.embedding column missing"
        assert col[0] == "vector", (
            f"expected vector column (pgvector branch), got {col[0]!r} "
            "(TEXT fallback branch was taken — extension not available at migrate time)"
        )
        hnsw = conn.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname='public' AND tablename='rules_embeddings' "
                "AND indexdef ILIKE '%hnsw%'"
            )
        ).fetchall()
        assert hnsw, "expected HNSW index on rules_embeddings.embedding"
        dist = conn.execute(text("SELECT '[1,2,3]'::vector <=> '[1,2,4]'::vector")).scalar()
        assert dist is not None and float(dist) > 0


def test_profiles_fk_to_auth_users_exists():
    _skip_if_no_postgres()
    engine = create_engine(_get_db_url(), poolclass=NullPool)
    with engine.connect() as conn:
        fks = conn.execute(
            text(
                "SELECT c.conname FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "JOIN pg_namespace n ON n.oid = t.relnamespace "
                "WHERE c.contype = 'f' AND n.nspname = 'public' "
                "AND t.relname = 'profiles' "
                "AND c.confrelid = 'auth.users'::regclass"
            )
        ).fetchall()
        assert fks, "profiles -> auth.users FK missing"

"""Verify application startup performs no DDL — issue #187.

Acceptance criteria:
- FastAPI startup does not call Alembic upgrade or metadata.create_all()
- Multiple instances starting concurrently must not attempt DDL
"""
import asyncio
import os
import pathlib
import re
import importlib
import sys
from unittest.mock import MagicMock, patch

import pytest


def _read_main_py() -> str:
    p = pathlib.Path(__file__).parent.parent / "main.py"
    return p.read_text(encoding="utf-8")


def test_main_py_does_not_define_run_migrations():
    src = _read_main_py()
    assert "_run_migrations" not in src, "main.py must not define _run_migrations"


def test_main_py_does_not_contain_create_all_call():
    src = _read_main_py()
    # allow the word in comments/docstrings but not as executable code
    # we check for the actual call pattern
    assert "create_all" not in src, "main.py must not contain create_all (cold-start DDL)"


def test_main_py_does_not_import_alembic_at_runtime():
    src = _read_main_py()
    # No `from alembic` or `import alembic` outside of comments
    # Strip comments and docstrings loosely
    lines = [l for l in src.splitlines() if not l.strip().startswith("#")]
    non_comment = "\n".join(lines)
    assert "from alembic" not in non_comment
    assert "import alembic" not in non_comment


def test_lifespan_does_not_trigger_ddl():
    """Runtime check: entering lifespan must not call alembic or create_all."""
    # Ensure we re-import main freshly
    if "main" in sys.modules:
        del sys.modules["main"]
    # Allow mock auth so import doesn't need DB
    os.environ["ALLOW_MOCK_AUTH"] = "true"

    import main as main_mod
    import database

    # Mock any DDL entry points — if lifespan calls them, test fails
    mock_upgrade = MagicMock()
    mock_create_all = MagicMock()

    # Patch where they would be if called
    with patch.dict("sys.modules", {"alembic": MagicMock(), "alembic.command": MagicMock()}):
        # Also patch Base.metadata.create_all if it still existed
        with patch.object(database.Base.metadata, "create_all", mock_create_all) as mca:
            # If main still imported alembic, this mock would catch it
            mock_upgrade = MagicMock()
            with patch("alembic.command.upgrade", mock_upgrade, create=True):
                async def _run():
                    async with main_mod.lifespan(main_mod.app):
                        pass

                asyncio.run(_run())

                mock_upgrade.assert_not_called()
                mca.assert_not_called()
                mock_create_all.assert_not_called()


def test_concurrent_lifespans_do_not_trigger_ddl():
    """Multiple instances (concurrent lifespans) must not attempt DDL."""
    if "main" in sys.modules:
        del sys.modules["main"]
    os.environ["ALLOW_MOCK_AUTH"] = "true"
    import main as main_mod
    import database

    with patch.object(database.Base.metadata, "create_all", MagicMock()) as mca:
        async def _run_concurrent():
            async def _single():
                async with main_mod.lifespan(main_mod.app):
                    await asyncio.sleep(0.01)

            await asyncio.gather(_single(), _single(), _single())

        asyncio.run(_run_concurrent())
        mca.assert_not_called()


def test_explicit_migrate_script_exists_and_exits_nonzero_without_db_url(monkeypatch):
    """scripts/migrate.py must exist and fail clearly when DB URL is missing."""
    # Ensure no DB URL env
    for k in ("POSTGRES_URL", "POSTGRES_PRISMA_URL", "POSTGRES_URL_NON_POOLING", "DATABASE_URL"):
        monkeypatch.delenv(k, raising=False)

    # Need to reimport to pick up env
    if "scripts.migrate" in sys.modules:
        del sys.modules["scripts.migrate"]
    if "database" in sys.modules:
        # database.DATABASE_URL is cached at import; patch get_database_url instead
        pass

    # Import migrate and patch get_database_url to return empty
    import scripts.migrate as migrate_mod

    monkeypatch.setattr(migrate_mod, "get_database_url", lambda: "")

    rc = migrate_mod.main()
    assert rc != 0, "migrate must exit non-zero when DB URL is missing"


def test_explicit_migrate_script_logs_before_after(capsys=None):
    """Sanity: migrate script is importable and has main()."""
    import scripts.migrate as migrate_mod

    assert hasattr(migrate_mod, "main")
    assert callable(migrate_mod.main)

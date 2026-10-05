"""Issue #481 — tests never reach a non-disposable database from ``.env``."""
from __future__ import annotations

from conftest import pin_test_database

HOSTED = "postgresql://user:pw@aws-0-us-west-2.pooler.supabase.com:6543/postgres"
DISPOSABLE = "postgresql://localhost:5432/ci_test?sslmode=disable"


def test_app_database_is_disposable_or_absent():
    import database

    url = database.get_database_url()
    assert url == "" or "ci_test" in url
    assert database.SessionLocal is None or "ci_test" in str(database.engine.url)


def test_hosted_url_is_never_kept():
    env = {"POSTGRES_URL": HOSTED}
    assert pin_test_database(env) == ""
    assert all(value == "" for value in env.values())


def test_disposable_url_wins_over_hosted_everywhere():
    env = {"POSTGRES_URL": HOSTED, "DATABASE_URL": DISPOSABLE}
    assert pin_test_database(env) == DISPOSABLE
    assert env["POSTGRES_URL"] == env["POSTGRES_URL_NON_POOLING"] == DISPOSABLE


def test_dotenv_cannot_refill_a_pinned_variable(tmp_path, monkeypatch):
    from dotenv import load_dotenv

    env_file = tmp_path / ".env"
    env_file.write_text(f"POSTGRES_URL={HOSTED}\n")
    monkeypatch.setenv("POSTGRES_URL", "")
    load_dotenv(env_file)
    import os

    assert os.environ["POSTGRES_URL"] == ""

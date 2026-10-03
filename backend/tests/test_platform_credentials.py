"""Platform credentials are the only AI execution path."""

import importlib.util
import os
from pathlib import Path
import uuid

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from main import app


def test_user_credential_endpoints_are_removed():
    with TestClient(app) as client:
        assert client.post(
            "/api/byok/credentials", json={"provider": "openai", "secret": "test-key"},
        ).status_code == 404
        assert client.get("/api/byok/credentials").status_code == 404
        assert client.get("/api/byok/routes").status_code == 404
        assert client.put(
            f"/api/campaigns/{uuid.uuid4()}/byok-policy", json={"credential_id": str(uuid.uuid4())},
        ).status_code == 404


@pytest.mark.postgres
def test_credential_cleanup_preserves_platform_funding_and_ai_runs():
    """Exercise the removal against the prior schema, then roll back all DDL."""
    url = (os.getenv("POSTGRES_URL_NON_POOLING") or os.getenv("POSTGRES_URL")
           or os.getenv("DATABASE_URL") or "")
    if "ci_test" not in url:
        pytest.skip("Requires a disposable migrated Postgres database")

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = Path(__file__).parents[1] / "alembic/versions/rmb257byok01_remove_byok.py"
    spec = importlib.util.spec_from_file_location("credential_cleanup", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(url)
    owner, campaign, credential, run, funding, marker = [uuid.uuid4() for _ in range(6)]
    try:
        with engine.connect() as conn:
            transaction = conn.begin()
            try:
                with Operations.context(MigrationContext.configure(conn)):
                    migration.downgrade()
                    conn.execute(text("INSERT INTO auth.users (id) VALUES (:id)"), {"id": owner})
                    conn.execute(text("INSERT INTO profiles (id, email) VALUES (:id, :email)"),
                                 {"id": owner, "email": f"{owner}@example.com"})
                    conn.execute(text("INSERT INTO campaigns (id, owner_id, name) VALUES (:id, :owner, 'cleanup test')"),
                                 {"id": campaign, "owner": owner})
                    conn.execute(text(
                        "INSERT INTO provider_credentials (id, owner_user_id, provider, encrypted_secret, key_fingerprint) "
                        "VALUES (:id, :owner, 'openai', 'encrypted test credential', 'test fingerprint')"
                    ), {"id": credential, "owner": owner})
                    conn.execute(text(
                        "INSERT INTO campaign_byok_policies (campaign_id, credential_id) VALUES (:campaign, :credential)"
                    ), {"campaign": campaign, "credential": credential})
                    conn.execute(text(
                        "INSERT INTO ai_runs (id, trace_id, operation_id, logical_operation, role, provider, model, "
                        "attempt, classification, billable, status, started_at, credential_id) "
                        "VALUES (:id, 'cleanup', 'cleanup', 'cleanup', 'forward_dm', 'openai', 'test', "
                        "1, 'primary', false, 'succeeded', now(), :credential)"
                    ), {"id": run, "credential": credential})
                    conn.execute(text(
                        "INSERT INTO campaign_usage_entries (id, campaign_id, entry_type, amount_cents, idempotency_key) "
                        "VALUES (:id, :campaign, :type, :amount, :key)"
                    ), [
                        {"id": funding, "campaign": campaign, "type": "allocation", "amount": 500, "key": "funding"},
                        {"id": marker, "campaign": campaign, "type": "byok_marker", "amount": 0, "key": "marker"},
                    ])
                    migration.upgrade()

                tables = inspect(conn).get_table_names()
                assert "provider_credentials" not in tables
                assert "campaign_byok_policies" not in tables
                assert "credential_id" not in {c["name"] for c in inspect(conn).get_columns("ai_runs")}
                assert conn.execute(text("SELECT count(*) FROM ai_runs WHERE id = :id"), {"id": run}).scalar() == 1
                entries = conn.execute(text(
                    "SELECT id, amount_cents FROM campaign_usage_entries WHERE campaign_id = :id"
                ), {"id": campaign}).all()
                assert entries == [(funding, 500)]
                with pytest.raises(IntegrityError), conn.begin_nested():
                    conn.execute(text(
                        "INSERT INTO campaign_usage_entries (id, campaign_id, entry_type, amount_cents, idempotency_key) "
                        "VALUES (:id, :campaign, 'byok_marker', 0, 'rejected-marker')"
                    ), {"id": uuid.uuid4(), "campaign": campaign})
            finally:
                transaction.rollback()
    finally:
        engine.dispose()

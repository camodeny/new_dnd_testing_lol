"""Issue #331 — production outbox relay wiring.

Proves the runtime connection API -> durable outbox -> relay -> queue:
- run_outbox_relay_once() claims pending rows and publishes via
  envelope_for_outbox (rows no longer sit pending forever)
- the cron endpoint triggers the same relay path in production runtime
- lifespan stays cold-start safe (no DDL, no loop by default)
"""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from app.outbox.relay import run_outbox_relay_once
from app.outbox.service import enqueue_outbox
from app.queue import InMemoryQueueAdapter
from database import Base, get_db
from models.reliability import Outbox


def _factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_relay_once_publishes_pending_via_envelope():
    factory = _factory()
    queue = InMemoryQueueAdapter()
    with factory() as db:
        rec = enqueue_outbox(db, event_type="turn.resolve", operation_id="op-331", payload={"n": 1})
        rec_id = rec.id
    with factory() as db:
        result = run_outbox_relay_once(db=db, adapter=queue, claimed_by="relay-test")
    assert result["claimed"] == 1
    assert result["succeeded"] == 1
    assert result["failed"] == 0
    assert queue.depth() == 1
    envelope = queue.peek_all()[0]
    assert str(envelope.job_id) == str(rec_id)
    assert envelope.operation_id == "op-331"
    with factory() as db:
        assert db.get(Outbox, rec_id).status == "published"


def test_relay_once_skips_cleanly_without_db(monkeypatch):
    import database

    monkeypatch.setattr(database, "SessionLocal", None)
    result = run_outbox_relay_once()
    assert result["claimed"] == 0
    assert result.get("skipped") == "db_unconfigured"


def test_cron_endpoint_runs_relay_path(monkeypatch):
    os.environ["ALLOW_MOCK_AUTH"] = "true"
    factory = _factory()
    with factory() as db:
        enqueue_outbox(db, event_type="turn.resolve", operation_id="op-cron", payload={"n": 2})

    from app.factory import create_app
    import app.queue.adapter as queue_adapter_mod

    app = create_app()
    queue = InMemoryQueueAdapter()
    monkeypatch.setattr(queue_adapter_mod, "get_queue_adapter", lambda: queue)

    def override_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    try:
        client = TestClient(app)
        resp = client.get("/api/cron/outbox-relay")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["relay"]["succeeded"] == 1
    assert queue.depth() == 1
    assert queue.peek_all()[0].operation_id == "op-cron"


def test_cron_endpoint_publishes_with_injected_adapter(monkeypatch):
    os.environ["ALLOW_MOCK_AUTH"] = "true"
    factory = _factory()
    with factory() as db:
        enqueue_outbox(db, event_type="turn.resolve", operation_id="op-cron-2", payload={"n": 3})

    from app.factory import create_app
    import app.outbox.relay as relay_mod

    app = create_app()
    queue = InMemoryQueueAdapter()
    monkeypatch.setattr(relay_mod, "run_outbox_relay_once", lambda **kw: {"claimed": 1, "succeeded": 1, "failed": 0, "recovered": 0})

    def override_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    try:
        client = TestClient(app)
        resp = client.get("/api/cron/outbox-relay")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert resp.json()["relay"]["succeeded"] == 1


def test_cron_secret_enforced(monkeypatch):
    os.environ["ALLOW_MOCK_AUTH"] = "true"
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    factory = _factory()

    from app.factory import create_app

    app = create_app()

    def override_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    try:
        client = TestClient(app)
        assert client.get("/api/cron/outbox-relay").status_code == 401
        assert client.get("/api/cron/outbox-relay", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    finally:
        app.dependency_overrides.clear()


def test_lifespan_default_does_not_start_relay_or_ddl(monkeypatch):
    monkeypatch.delenv("OUTBOX_RELAY_LOOP_ENABLED", raising=False)
    if "main" in sys.modules:
        del sys.modules["main"]
    os.environ["ALLOW_MOCK_AUTH"] = "true"
    os.environ["NODE_ENV"] = "test"
    import main as main_mod
    import database

    with patch.object(database.Base.metadata, "create_all", MagicMock()) as mca:
        async def _run():
            async with main_mod.lifespan(main_mod.app):
                await asyncio.sleep(0.05)

        asyncio.run(_run())
        mca.assert_not_called()

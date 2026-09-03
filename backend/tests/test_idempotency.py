"""Issue #189 — durable idempotent command acceptance."""

from __future__ import annotations

import threading
import time
import uuid

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from database import Base, get_db  # noqa: E402
from app.idempotency import (  # noqa: E402
    IdempotencyConflictError,
    execute_idempotent_command,
)
from models import Campaign, CampaignMember, IdempotentCommand, Profile  # noqa: E402


def _setup(url: str = "sqlite://"):
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 5})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    actor = uuid.uuid4()
    with factory() as db:
        db.add(Profile(id=actor, email="actor@example.com"))
        db.commit()
    return engine, factory, actor


def _run(db: Session, actor: uuid.UUID, key: str, payload: dict, execute, **scope):
    return execute_idempotent_command(
        db, actor_id=actor, idempotency_key=key,
        command_type=scope.pop("command_type", "test.command"),
        scope_type=scope.pop("scope_type", "user"),
        scope_id=scope.pop("scope_id", actor), payload=payload, execute=execute,
    )


def test_duplicate_returns_original_result_without_executing_again():
    _, factory, actor = _setup()
    calls = []
    with factory() as db:
        first, replayed = _run(db, actor, "key-1", {"value": 1}, lambda: calls.append(1) or {"id": "result-1"})
        second, replayed_second = _run(db, actor, "key-1", {"value": 1}, lambda: calls.append(2) or {"id": "wrong"})
    assert first == second == {"id": "result-1"}
    assert replayed is False and replayed_second is True
    assert calls == [1]


def test_result_survives_new_session_process_boundary():
    _, factory, actor = _setup()
    with factory() as db:
        _run(db, actor, "restart-key", {"x": 1}, lambda: {"event_id": "durable"})
    with factory() as restarted_db:
        result, replayed = _run(
            restarted_db, actor, "restart-key", {"x": 1},
            lambda: pytest.fail("must not execute after restart"),
        )
    assert result == {"event_id": "durable"}
    assert replayed is True


def test_same_key_different_payload_is_rejected():
    _, factory, actor = _setup()
    with factory() as db:
        _run(db, actor, "same", {"amount": 1}, lambda: {"ok": True})
        with pytest.raises(IdempotencyConflictError):
            _run(db, actor, "same", {"amount": 2}, lambda: {"ok": False})


def test_failed_execution_does_not_permanently_record_key():
    _, factory, actor = _setup()
    with factory() as db:
        with pytest.raises(RuntimeError):
            _run(db, actor, "retry", {"x": 1}, lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        assert db.scalar(select(func.count()).select_from(IdempotentCommand)) == 0
        result, replayed = _run(db, actor, "retry", {"x": 1}, lambda: {"ok": True})
    assert result == {"ok": True} and replayed is False


def test_keys_are_isolated_by_actor_and_campaign_or_user_scope():
    _, factory, actor = _setup()
    other = uuid.uuid4()
    with factory() as db:
        db.add(Profile(id=other, email="other@example.com"))
        db.commit()
        a, _ = _run(db, actor, "shared", {}, lambda: {"n": 1}, scope_type="campaign", scope_id="campaign-a")
        b, _ = _run(db, actor, "shared", {}, lambda: {"n": 2}, scope_type="user", scope_id=actor)
        c, _ = _run(db, other, "shared", {}, lambda: {"n": 3}, scope_type="campaign", scope_id="campaign-a")
    assert [a["n"], b["n"], c["n"]] == [1, 2, 3]


def test_concurrent_duplicates_only_execute_once(tmp_path):
    db_file = tmp_path / "idempotency.sqlite"
    _, factory, actor = _setup(f"sqlite:///{db_file}")
    started = threading.Event()
    calls = []
    results = []
    errors = []

    def worker(first: bool):
        try:
            with factory() as db:
                def execute():
                    calls.append(first)
                    if first:
                        started.set()
                        time.sleep(0.2)
                    return {"winner": "one"}
                if not first:
                    started.wait(2)
                results.append(_run(db, actor, "race", {"x": 1}, execute))
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    one = threading.Thread(target=worker, args=(True,))
    two = threading.Thread(target=worker, args=(False,))
    one.start(); two.start(); one.join(); two.join()
    assert errors == []
    assert calls == [True]
    assert sorted(replayed for _, replayed in results) == [False, True]


def test_duplicate_http_retry_returns_same_campaign_event(monkeypatch):
    from app.auth.service import MOCK_USER_ID
    from main import app

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    campaign_id = uuid.uuid4()
    with factory() as db:
        db.add(Profile(id=MOCK_USER_ID, email="mock@example.com"))
        db.add(Campaign(id=campaign_id, owner_id=MOCK_USER_ID, name="Before"))
        db.add(CampaignMember(campaign_id=campaign_id, user_id=MOCK_USER_ID, role="owner"))
        db.commit()

    def override_db():
        with factory() as db:
            yield db

    monkeypatch.setenv("ALLOW_MOCK_AUTH", "true")
    monkeypatch.setenv("NODE_ENV", "test")
    app.dependency_overrides[get_db] = override_db
    try:
        client = TestClient(app)
        body = {
            "expected_revision": 0,
            "event_type": "campaign.renamed",
            "mutate": {"name": "After"},
        }
        headers = {"Idempotency-Key": "http-retry-1"}
        first = client.post(f"/api/campaigns/{campaign_id}/mutations", json=body, headers=headers)
        second = client.post(f"/api/campaigns/{campaign_id}/mutations", json=body, headers=headers)
        assert first.status_code == second.status_code == 200
        assert first.json() == second.json()
        assert first.headers["X-Idempotent-Replay"] == "false"
        assert second.headers["X-Idempotent-Replay"] == "true"
        assert first.json()["campaign"]["revision"] == 1

        mismatch = client.post(
            f"/api/campaigns/{campaign_id}/mutations",
            json={**body, "mutate": {"name": "Different"}}, headers=headers,
        )
        assert mismatch.status_code == 409
    finally:
        app.dependency_overrides.clear()


def test_campaign_put_retry_replays_after_revision_advanced(monkeypatch):
    from app.auth.service import MOCK_USER_ID
    from main import app

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    campaign_id = uuid.uuid4()
    with factory() as db:
        db.add(Profile(id=MOCK_USER_ID, email="mock@example.com"))
        db.add(Campaign(id=campaign_id, owner_id=MOCK_USER_ID, name="Before"))
        db.add(CampaignMember(campaign_id=campaign_id, user_id=MOCK_USER_ID, role="owner"))
        db.commit()

    def override_db():
        with factory() as db:
            yield db

    monkeypatch.setenv("ALLOW_MOCK_AUTH", "true")
    monkeypatch.setenv("NODE_ENV", "test")
    app.dependency_overrides[get_db] = override_db
    try:
        client = TestClient(app)
        body = {"expected_revision": 0, "name": "After"}
        headers = {"Idempotency-Key": "campaign-put-retry"}
        first = client.put(f"/api/campaigns/{campaign_id}", json=body, headers=headers)
        second = client.put(f"/api/campaigns/{campaign_id}", json=body, headers=headers)

        assert first.status_code == second.status_code == 200
        assert first.json() == second.json()
        assert first.json()["campaign"]["revision"] == 1
        assert first.headers["X-Idempotent-Replay"] == "false"
        assert second.headers["X-Idempotent-Replay"] == "true"
    finally:
        app.dependency_overrides.clear()


def test_character_create_is_a_real_user_scoped_idempotent_command(monkeypatch):
    from app.auth.service import MOCK_USER_ID
    from main import app
    from models import Character

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        db.add(Profile(id=MOCK_USER_ID, email="mock@example.com"))
        db.commit()

    def override_db():
        with factory() as db:
            yield db

    monkeypatch.setenv("ALLOW_MOCK_AUTH", "true")
    monkeypatch.setenv("NODE_ENV", "test")
    app.dependency_overrides[get_db] = override_db
    try:
        client = TestClient(app)
        body = {"name": "Idempotent Hero", "system": "dnd5e"}
        headers = {"Idempotency-Key": "character-create-retry"}
        first = client.post("/api/characters", json=body, headers=headers)
        second = client.post("/api/characters", json=body, headers=headers)

        assert first.status_code == second.status_code == 200
        assert first.json() == second.json()
        assert first.headers["X-Idempotent-Replay"] == "false"
        assert second.headers["X-Idempotent-Replay"] == "true"
        with factory() as db:
            assert db.scalar(select(func.count()).select_from(Character)) == 1
            command = db.execute(
                select(IdempotentCommand).where(
                    IdempotentCommand.command_type == "character.create"
                )
            ).scalars().one()
            assert command.scope_type == "user"
            assert command.scope_id == str(MOCK_USER_ID)
    finally:
        app.dependency_overrides.clear()

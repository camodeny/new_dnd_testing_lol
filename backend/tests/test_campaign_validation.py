"""Regression: campaign name is limited to 128 chars at API boundary."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

os.environ["ALLOW_MOCK_AUTH"] = "true"
os.environ["NODE_ENV"] = "test"

from app.auth.service import MOCK_USER_ID  # noqa: E402
from database import Base, get_db  # noqa: E402
from main import app  # noqa: E402
from models.profiles import Profile  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add(Profile(id=MOCK_USER_ID, email="owner@example.com"))
        db.commit()

    def override_db():
        with factory() as db:
            yield db

    monkeypatch.setattr(
        "app.campaigns.router.resolve_profile",
        lambda request, db: db.get(Profile, MOCK_USER_ID),
    )
    app.dependency_overrides[get_db] = override_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _cleanup(client: TestClient, campaign_id: str):
    try:
        client.delete(f"/api/campaigns/{campaign_id}")
    except Exception:
        pass


def test_create_name_128_ok(client: TestClient):
    name = "a" * 128
    resp = client.post("/api/campaigns", json={"name": name})
    assert resp.status_code == 200, resp.text
    cid = resp.json()["campaign"]["id"]
    _cleanup(client, cid)


def test_create_name_129_rejected(client: TestClient):
    name = "a" * 129
    resp = client.post("/api/campaigns", json={"name": name})
    assert resp.status_code == 400
    assert "128" in resp.json().get("detail", "")


def test_create_empty_rejected(client: TestClient):
    resp = client.post("/api/campaigns", json={"name": "   "})
    assert resp.status_code == 400


def test_update_name_129_rejected(client: TestClient):
    name = "a" * 10
    resp = client.post("/api/campaigns", json={"name": name})
    assert resp.status_code == 200
    cid = resp.json()["campaign"]["id"]
    try:
        upd = client.put(f"/api/campaigns/{cid}", json={"name": "b" * 129})
        assert upd.status_code == 400
        assert "128" in upd.json().get("detail", "")
        got = client.get(f"/api/campaigns/{cid}")
        assert got.json()["campaign"]["name"] == name
    finally:
        _cleanup(client, cid)


def test_update_seed_129_rejected(client: TestClient):
    resp = client.post("/api/campaigns", json={"name": "seed-test"})
    assert resp.status_code == 200
    cid = resp.json()["campaign"]["id"]
    try:
        upd = client.put(f"/api/campaigns/{cid}", json={"random_seed": "x" * 129})
        assert upd.status_code == 400
    finally:
        _cleanup(client, cid)

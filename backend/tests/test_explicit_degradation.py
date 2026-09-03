"""Issue #320 — explicit degradation instead of silent broad exception swallowing.

Focused coverage for the four audited surfaces:
- realtime channel naming now validates UUIDs (fail-closed) instead of a no-op check
- rules search embedding degrades to lexical search with a logged reason
- runtime thread UUID parsing fails closed with 422 (narrow catch)
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from app.deps.auth import MOCK_USER_ID  # noqa: E402
from app.realtime.channels import live_table_channel, parse_live_table_channel  # noqa: E402
from database import Base, get_db  # noqa: E402
from main import app  # noqa: E402
import models  # noqa: E402, F401
from models import Campaign, CampaignMember, Profile  # noqa: E402

TEST_OFFICIAL_HASH = "8974902d109d6e63672d7c490bde9ccf052410503d9cfa768237154fbc5e3d87"


# ── realtime channel UUID validation ────────────────────────────────────────


def test_live_table_channel_normalizes_to_lowercase_uuid():
    cid = uuid.uuid4()
    tid = uuid.uuid4()
    ch = live_table_channel(str(cid).upper(), str(tid).upper())
    assert ch == f"live-table:campaign:{cid}:thread:{tid}"
    assert ch == ch.lower()


def test_live_table_channel_fails_closed_on_invalid_uuid():
    with pytest.raises(ValueError):
        live_table_channel("not-a-uuid", uuid.uuid4())
    with pytest.raises(ValueError):
        live_table_channel(uuid.uuid4(), "not-a-uuid")


def test_parse_live_table_channel_round_trips():
    cid = uuid.uuid4()
    tid = uuid.uuid4()
    ch = live_table_channel(cid, tid)
    assert parse_live_table_channel(ch) == (cid, tid)


def test_parse_live_table_channel_rejects_malformed():
    assert parse_live_table_channel("live-table:campaign:not-a-uuid:thread:") is None
    assert parse_live_table_channel("other:namespace:x:y:z") is None
    assert parse_live_table_channel("live-table:campaign:x:thread:y:extra") is None
    assert parse_live_table_channel("live-table:campaign:x:thread:not-a-uuid") is None
    assert parse_live_table_channel("live-table:campaign:x:thread") is None


# ── rules search embedding degradation ──────────────────────────────────────


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    s = Session()
    yield s
    s.close()
    engine.dispose()


def test_rules_search_degrades_to_lexical_when_embedding_fails(db, monkeypatch):
    from app.rules import router as rules_router
    from app.rules.ingest import import_fixture_sections

    sections = [
        {
            "document": "playing-the-game",
            "heading_path": ["Combat", "Attack Rolls"],
            "title": "Attack Rolls",
            "body": "When you make an attack, roll a d20 and add modifiers.",
            "structured_tables": None,
        }
    ]
    _, recs = import_fixture_sections(
        db, sections, source_artifact_hash=TEST_OFFICIAL_HASH, validate_canaries=False
    )
    # Seed a gemini embedding row so the search path attempts a live embedding
    db.add(
        models.RulesEmbedding(
            rule_id=recs[0].rule_id,
            corpus_id="dnd-srd",
            corpus_version="5.2.1",
            embedding_model="gemini-embedding-2",
            embedding_version="1",
            build_id="test",
        )
    )
    db.commit()

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    def boom(*_a, **_k):
        raise RuntimeError("simulated provider failure")

    monkeypatch.setattr("app.rules.gemini.gemini_embed_query", boom)

    # Must degrade to lexical search without raising, and still return hits
    result = rules_router.search(q="attack", limit=5, corpus_id=None, db=db)
    assert result["count"] >= 1
    assert any("Attack Rolls" in h["title"] for h in result["hits"])


# ── runtime thread UUID parsing fails closed ────────────────────────────────


@pytest.fixture
def api(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    campaign_id = uuid.uuid4()
    with factory() as db:
        db.add_all(
            [
                Profile(id=MOCK_USER_ID, email="owner@example.com"),
                Campaign(id=campaign_id, owner_id=MOCK_USER_ID, name="Table"),
                CampaignMember(
                    campaign_id=campaign_id, user_id=MOCK_USER_ID, role="owner"
                ),
            ]
        )
        db.commit()

    def override_db():
        with factory() as db:
            yield db

    monkeypatch.setenv("ALLOW_MOCK_AUTH", "true")
    monkeypatch.setenv("NODE_ENV", "test")
    app.dependency_overrides[get_db] = override_db
    try:
        yield TestClient(app), campaign_id
    finally:
        app.dependency_overrides.clear()


def test_direct_thread_rejects_invalid_participant_id(api):
    client, campaign_id = api
    r = client.post(
        f"/api/campaigns/{campaign_id}/threads/direct",
        json={"participant_id": "not-a-uuid"},
    )
    assert r.status_code == 422
    assert "valid user id" in r.text


def test_create_thread_rejects_invalid_member_id(api):
    client, campaign_id = api
    r = client.post(
        f"/api/campaigns/{campaign_id}/threads",
        json={"thread_type": "private", "member_ids": ["not-a-uuid"]},
    )
    assert r.status_code == 422
    assert "Invalid member id" in r.text

"""Issue #251 — knowledge-visibility lane reader for #202 context assembly."""

from __future__ import annotations

import uuid

from sqlalchemy import create_engine
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from database import Base  # noqa: E402
import models  # noqa: E402, F401
from app.dm.context import (  # noqa: E402
    ContextRecord,
    _scope,
    _source,
)
from app.world.epistemics import (  # noqa: E402
    assert_knowledge_inline,
    build_knowledge_visibility_values,
)
from app.world.knowledge import create_fact_authoritative  # noqa: E402
from app.world.service import create_entity_authoritative  # noqa: E402
from models.campaigns import Campaign, CampaignMember  # noqa: E402
from models.profiles import Profile  # noqa: E402


def _setup():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=eng)
    Fac = sessionmaker(bind=eng, expire_on_commit=False)
    db = Fac()
    owner = uuid.uuid4()
    db.add(Profile(id=owner, email="owner@example.com"))
    camp = Campaign(id=uuid.uuid4(), owner_id=owner, name="Lane", revision=0)
    db.add(camp)
    db.flush()
    db.add(CampaignMember(campaign_id=camp.id, user_id=owner, role="owner"))
    db.commit()
    return Fac, db.get(Campaign, camp.id)


def test_no_pc_yields_single_empty_value():
    Fac, camp = _setup()
    db = Fac()
    values = build_knowledge_visibility_values(db, camp, set())
    assert values == [{"perspectives": [], "note": "no_pc_in_attempt"}]


def test_unresolved_character_is_explicit_empty():
    Fac, camp = _setup()
    db = Fac()
    values = build_knowledge_visibility_values(db, camp, {uuid.uuid4()})
    assert len(values) == 1
    assert values[0]["subject_resolved"] is False
    assert values[0]["subject_entity_id"] is None
    assert values[0]["entries"] == []


def test_resolved_subject_returns_bounded_stance_refs():
    Fac, camp = _setup()
    db = Fac()
    aria, _ = create_entity_authoritative(
        db, camp.id, 0, entity_type="character", name="Aria",
        operation_id="op-aria-251",
        details={"character_id": "00000000-0000-0000-0000-000000000000"},
    )
    fact, _ = create_fact_authoritative(
        db, camp.id, 1, content="The bridge is trapped.",
        entity_refs=[aria.id], epistemic_state="confirmed",
        visibility="dm_only",
        provenance={"source": "dm_adjudication"},
        operation_id="op-truth-251",
    )
    assert_knowledge_inline(
        db, camp, subject_kind="character", subject_entity_id=aria.id,
        target_kind="fact", target_fact_id=fact.id,
        knowledge_state="knows", acquisition_source="direct_observation",
        operation_id="op-know-251",
    )
    # Point the fake character link at the real subject entity.
    aria.details = {"character_id": str(aria.id)}
    db.flush()
    values = build_knowledge_visibility_values(db, camp, {aria.id})
    assert len(values) == 1
    value = values[0]
    assert value["subject_resolved"] is True
    assert value["subject_entity_id"] == str(aria.id)
    assert len(value["entries"]) == 1
    entry = value["entries"][0]
    assert entry["target_kind"] == "fact"
    assert entry["target_id"] == str(fact.id)
    assert entry["knowledge_state"] == "knows"
    # Stance refs only — no truth text in the lane value.
    assert "content" not in entry
    assert "The bridge" not in str(value)


def test_entries_truncate_at_limit():
    Fac, camp = _setup()
    db = Fac()
    aria, _ = create_entity_authoritative(
        db, camp.id, 0, entity_type="character", name="Aria",
        operation_id="op-aria-251t",
    )
    for i in range(4):
        fact, _ = create_fact_authoritative(
            db, camp.id, i + 1, content=f"Secret fact number {i}.",
            entity_refs=[aria.id], epistemic_state="confirmed",
            visibility="dm_only",
            provenance={"source": "dm_adjudication"},
            operation_id=f"op-truth-251t-{i}",
        )
        assert_knowledge_inline(
            db, camp, subject_kind="character", subject_entity_id=aria.id,
            target_kind="fact", target_fact_id=fact.id,
            knowledge_state="knows", acquisition_source="direct_observation",
            operation_id=f"op-know-251t-{i}",
        )
    values = build_knowledge_visibility_values(db, camp, {aria.id}, max_entries_per_subject=2)
    assert values[0]["truncated"] is True
    assert len(values[0]["entries"]) == 2


def test_lane_records_are_dm_only_adjudication_only():
    _, camp = _setup()
    scope = _scope(camp.id)
    record = ContextRecord(
        record_id="knowledge:no-pc",
        required=False,
        priority=90,
        value={"perspectives": [], "note": "no_pc_in_attempt"},
        sources=[_source("dm_turn_attempt", uuid.uuid4(), 0, 0, lane="knowledge_visibility")],
        authorization=scope,
        visibility="dm_only",
        use="adjudication_only",
    )
    assert record.visibility == "dm_only"
    assert record.use == "adjudication_only"

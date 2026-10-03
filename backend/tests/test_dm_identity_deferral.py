"""Identity-deferral retry advisory on re-adjudication."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from database import Base  # noqa: E402
from models.campaigns import Campaign  # noqa: E402
from models.dm import DmTurnAttempt  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.threads import CampaignThread  # noqa: E402

from app.dm.context import (  # noqa: E402
    IDENTITY_DEFERRAL_RECORD_ID,
    LaneName,
    build_retry_deferral_advisory,
)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'deferral.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    owner = uuid.uuid4()
    with factory() as s:
        camp_id = uuid.uuid4()
        thread_id = uuid.uuid4()
        s.add(Profile(id=owner, email="owner@example.com"))
        s.add(Campaign(id=camp_id, owner_id=owner, name="Table", revision=0))
        s.add(
            CampaignThread(
                id=thread_id,
                campaign_id=camp_id,
                thread_type="campaign",
                created_by=owner,
            )
        )
        s.commit()
        yield s, camp_id, thread_id


def _submit(s, camp_id, thread_id, text="..."):
    from app.dm.turns import coordinate_turn
    from app.submissions.service import accept_submission

    accept_submission(
        s,
        campaign_id=camp_id,
        user_id=s.get(Campaign, camp_id).owner_id,
        raw_content=text,
        segments=[{"type": "ic", "text": text}],
        thread_id=str(thread_id),
    )
    s.commit()
    coord = coordinate_turn(s, camp_id, str(thread_id), commit=False)
    s.commit()
    assert coord is not None
    return coord


def _silent(packet, feedback=None):
    from app.dm.contract import CONTRACT_VERSION, normalize_contract

    return normalize_contract(
        {
            "contract_version": CONTRACT_VERSION,
            "mode": "silent",
            "reason": "retry disambiguates after deferral advisory",
        }
    )


def test_no_deferral_memo_no_advisory(db):
    s, camp_id, thread_id = db
    _turn, attempt = _submit(s, camp_id, thread_id)
    assert build_retry_deferral_advisory(s, attempt) is None


def test_identity_deferral_memo_reaches_retry_adjudication(db):
    from app.dm.execution import execute_dm_attempt

    s, camp_id, thread_id = db
    turn, old = _submit(s, camp_id, thread_id)
    old.identity_resolutions = [
        {
            "temp_id": "tmp_npc_1", "outcome": "DEFER", "via": "deferred",
            "jit_key": "jit",
            "proposal": {
                "kind": "npc", "public_name": "Watchful Figure",
                "role": None, "public_summary": "A silent watcher",
            },
            "candidate_labels": ["Ember Compact (group)"],
        }
    ]
    old.status = "abandoned"
    old.abandonment_reason = "explicit_retry"
    child = DmTurnAttempt(
        id=uuid.uuid4(), turn_id=turn.id, campaign_id=camp_id,
        thread_id=str(thread_id), audience="campaign",
        attempt_number=old.attempt_number + 1, parent_attempt_id=old.id,
        status="prepared", source_revision=0,
        input_set_revision=turn.input_set_revision,
        submission_ids=list(old.submission_ids or []),
        roll_evidence=[], staged_effects=[],
    )
    s.add(child)
    turn.current_attempt_id = child.id
    turn.status = "pending"
    s.commit()

    seen = {}

    def _generative(packet, feedback=None):
        seen["packet"] = packet
        return _silent(packet, feedback)

    result = execute_dm_attempt(
        s, child.id, adjudicate=_generative, narrator="deterministic",
    )
    assert result.mode == "silent"
    lane = next(ln for ln in seen["packet"].lanes if ln.name == LaneName.PLAYER_INPUTS)
    advisories = [r for r in lane.records if r.record_id == IDENTITY_DEFERRAL_RECORD_ID]
    assert len(advisories) == 1
    assert advisories[0].use == "adjudication_only"
    assert "Watchful Figure" in advisories[0].value["note"]
    assert "Ember Compact" in advisories[0].value["note"]

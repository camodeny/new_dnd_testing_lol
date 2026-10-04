"""Issue #458 — the DM context packet carries the active adventure so the
model can stage ``complete_adventure`` when the fiction resolves the arc."""
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from database import Base  # noqa: E402
from models.campaigns import Adventure, Campaign  # noqa: E402
from models.dm import DmTurn  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.threads import CampaignThread  # noqa: E402

from app.adventures.service import start_adventure  # noqa: E402
from app.dm.context import LaneName, assemble_attempt_context  # noqa: E402
from app.dm.contract import CONTRACT_VERSION, normalize_contract  # noqa: E402
from app.dm.execution import execute_dm_attempt  # noqa: E402


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'adventure_lane.sqlite'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    owner = uuid.uuid4()
    with factory() as s:
        camp_id = uuid.uuid4()
        thread_id = uuid.uuid4()
        s.add(Profile(id=owner, email="owner@example.com"))
        s.add(Campaign(id=camp_id, owner_id=owner, name="Table", revision=0))
        s.add(CampaignThread(id=thread_id, campaign_id=camp_id, thread_type="campaign", created_by=owner))
        s.commit()
        yield s, camp_id, thread_id


def _submit(s, camp_id, thread_id, text="I smash the last taint vessel."):
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
    turn, attempt = coordinate_turn(s, camp_id, str(thread_id), commit=False)
    s.commit()
    return turn, attempt


def _na_status():
    return {
        LaneName.CURRENT_SCENE: "not_applicable",
        LaneName.KNOWLEDGE_VISIBILITY: "not_applicable",
    }


def _completing_adjudicator(seen):
    """Stub adjudicator for a resolved arc: reads the adventure lane, stages completion."""

    def _adj(packet, feedback=None):
        seen.append(next(lane for lane in packet.lanes if lane.name == LaneName.ACTIVE_ADVENTURE))
        return normalize_contract(
            {
                "contract_version": CONTRACT_VERSION,
                "mode": "respond",
                "reason": "the arc is resolved",
                "beats": [
                    {
                        "id": "beat_1",
                        "type": "narration",
                        "claims": [
                            {
                                "text": "The last vessel shatters and the taint drains from the wells.",
                                "claim_kind": "observation",
                                "origin": "dm_adjudication",
                                "visibility": "public",
                            }
                        ],
                    }
                ],
                "staged_effects": [
                    {
                        "id": "eff_adv_done",
                        "effect_type": "complete_adventure",
                        "arguments": {"outcome": "victory", "reason": "The scheme is ended."},
                    }
                ],
            }
        )

    return _adj


def test_active_adventure_in_packet_and_staged_completion_commits(db):
    s, camp_id, thread_id = db
    adv_id = start_adventure(
        s, camp_id, "The Failing Wells", adventure_metadata={"premise": "Wells are being tainted."}
    ).id
    turn, attempt = _submit(s, camp_id, thread_id)
    seen = []
    execute_dm_attempt(s, attempt.id, adjudicate=_completing_adjudicator(seen), narrator="deterministic")

    lane = seen[0]
    assert lane.required is False
    assert [record.value for record in lane.records] == [
        {
            "adventure_id": str(adv_id),
            "title": "The Failing Wells",
            "premise": "Wells are being tainted.",
            "status": "active",
        }
    ]
    assert lane.records[0].use == "adjudication_only"
    assert s.get(DmTurn, turn.id).status == "succeeded"
    s.expire_all()
    done = s.get(Adventure, adv_id)
    assert done.status == "completed"
    assert done.outcome == "victory"
    assert done.source_turn_id == turn.id


def test_lane_empty_without_active_adventure_and_hidden_from_narration(db):
    s, camp_id, thread_id = db
    _, attempt = _submit(s, camp_id, thread_id)
    empty = assemble_attempt_context(s, attempt.id, supplemental_status=_na_status())
    assert next(l for l in empty.lanes if l.name == LaneName.ACTIVE_ADVENTURE).records == []

    start_adventure(s, camp_id, "Arc", adventure_metadata={"premise": "Secret premise."})
    packet = assemble_attempt_context(s, attempt.id, supplemental_status=_na_status())
    assert "Secret premise." in packet.serialize_for_adjudication()
    assert "Secret premise." not in packet.serialize_for_narration()

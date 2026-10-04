"""Issue #456 — accepted-but-uncoordinated submissions converge via the sweep."""
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from database import Base  # noqa: E402
from models.campaigns import Campaign  # noqa: E402
from models.dm import DmTurn  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.threads import CampaignThread, PlayerSubmission  # noqa: E402

from app.clock import utcnow  # noqa: E402
from app.dm.execution import run_dm_execute_sweep  # noqa: E402
from app.dm.turns import StreamBoundaryError, TurnConflictError, coordinate_turn  # noqa: E402
from app.submissions.service import accept_submission  # noqa: E402
from tests.test_dm_execution_spine import _fake_adjudicate  # noqa: E402


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'stranded.sqlite'}", connect_args={"check_same_thread": False}
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


def _accept(s, camp_id, thread_id, text, *, age_seconds=60, audience="campaign"):
    sub = accept_submission(
        s,
        campaign_id=camp_id,
        user_id=s.get(Campaign, camp_id).owner_id,
        raw_content=text,
        segments=[{"type": "ic", "text": text}],
        thread_id=str(thread_id),
        audience=audience,
    )
    sub.accepted_at = utcnow() - timedelta(seconds=age_seconds)
    s.commit()
    return sub


def _sweep(s):
    return run_dm_execute_sweep(s, adjudicate=_fake_adjudicate(), narrator="deterministic")


def test_submission_during_streaming_turn_converges_in_one_sweep(db):
    s, camp_id, thread_id = db
    first = _accept(s, camp_id, thread_id, "I step into the hall.")
    turn, _attempt = coordinate_turn(s, camp_id, str(thread_id))
    turn.status = "streaming"
    s.commit()

    late = _accept(s, camp_id, thread_id, "I also draw my sword.")
    with pytest.raises((StreamBoundaryError, TurnConflictError)):
        coordinate_turn(s, camp_id, str(thread_id), commit=False)
    s.rollback()

    # While the earlier turn is still active the sweep leaves the input alone.
    assert _sweep(s)["coordinated"] == []

    # The earlier turn commits; only its own input is resolved.
    turn = s.get(DmTurn, turn.id)
    turn.status = "succeeded"
    s.get(PlayerSubmission, first.id).resolution_status = "resolved"
    s.commit()

    sweep = _sweep(s)
    assert len(sweep["coordinated"]) == 1, sweep
    assert len(sweep["executed"]) == 1, sweep
    new_turn = s.get(DmTurn, uuid.UUID(sweep["coordinated"][0]["turn_id"]))
    assert new_turn.submission_ids == [str(late.id)]
    assert new_turn.status == "succeeded"


def test_sweep_waits_for_stranded_delay(db):
    s, camp_id, thread_id = db
    _accept(s, camp_id, thread_id, "Too fresh to sweep.", age_seconds=0)
    assert _sweep(s)["coordinated"] == []
    assert s.execute(select(DmTurn)).scalars().all() == []


def test_sweep_does_not_coordinate_direct_lobby_or_archived(db):
    s, camp_id, thread_id = db
    owner = s.get(Campaign, camp_id).owner_id
    direct = CampaignThread(
        id=uuid.uuid4(), campaign_id=camp_id, thread_type="private",
        private_kind="direct", private_key=f"direct:{uuid.uuid4()}", created_by=owner,
    )
    lobby = CampaignThread(id=uuid.uuid4(), campaign_id=camp_id, thread_type="lobby", created_by=owner)
    s.add_all([direct, lobby])
    s.commit()
    _accept(s, camp_id, direct.id, "Psst, player to player.")
    _accept(s, camp_id, lobby.id, "OOC table talk.", audience="lobby")

    assert _sweep(s)["coordinated"] == []
    assert s.execute(select(DmTurn)).scalars().all() == []

    # Archived campaigns are not coordinated; a real campaign thread is.
    _accept(s, camp_id, thread_id, "I knock on the door.")
    camp = s.get(Campaign, camp_id)
    camp.status = "archived"
    s.commit()
    assert _sweep(s)["coordinated"] == []
    camp.status = "active"
    s.commit()
    sweep = _sweep(s)
    assert [c["thread_id"] for c in sweep["coordinated"]] == [str(thread_id)]

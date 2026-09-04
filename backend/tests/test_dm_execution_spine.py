"""Issue #354 — submission → autonomous execution → persisted DM reply.

Starts from normal submission acceptance (the same calls the
POST /submissions endpoint makes) and observes the final persisted DM
output WITHOUT manually advancing turn lifecycle endpoints (no
mark_streaming_started / commit calls in the test body — the orchestrator
owns them).
"""
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from database import Base  # noqa: E402
from models.campaigns import Campaign  # noqa: E402
from models.dm import DMStreamChunk, DmTurn, DmTurnAttempt  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.threads import CampaignThread  # noqa: E402

from app.dm.contract import CONTRACT_VERSION, normalize_contract  # noqa: E402
from app.dm.execution import (  # noqa: E402
    DM_TURN_EXECUTE_JOB,
    execute_dm_attempt,
    run_dm_execute_sweep,
)
from app.dm.narration import materialize_final_narration  # noqa: E402
from app.dm_streams.service import reconstruct_text  # noqa: E402


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
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
        yield s, camp_id, thread_id, factory


def _submit(s, camp_id, thread_id, text="I step into the torchlit hall."):
    """Normal player submission acceptance (mirrors POST /submissions)."""
    from app.runtime.submissions import accept_submission
    from app.dm.turns import coordinate_turn

    accept_submission(
        s,
        campaign_id=camp_id,
        user_id=uuid.uuid4(),
        raw_content=text,
        segments=[{"type": "ic", "text": text}],
        thread_id=str(thread_id),
    )
    s.commit()
    coord = coordinate_turn(s, camp_id, str(thread_id), commit=False)
    s.commit()
    assert coord is not None
    return coord


def _fake_adjudicate(text="Torchlight gutters as something stirs beyond the arch."):
    def _adj(packet, feedback=None):
        return normalize_contract(
            {
                "contract_version": CONTRACT_VERSION,
                "mode": "respond",
                "reason": "story continuation",
                "beats": [
                    {
                        "id": "beat_1",
                        "type": "narration",
                        "claims": [
                            {
                                "text": text,
                                "claim_kind": "observation",
                                "origin": "dm_adjudication",
                                "visibility": "public",
                            }
                        ],
                    }
                ],
                "open_player_choice": "What do you do?",
            }
        )

    return _adj


def test_submission_autonomously_executes_to_persisted_dm_reply(db):
    s, camp_id, thread_id, factory = db
    turn, attempt = _submit(s, camp_id, thread_id)
    assert turn.status == "pending"

    # No manual lifecycle endpoints — the sweeper claims and executes.
    sweep = run_dm_execute_sweep(
        s, limit=5, adjudicate=_fake_adjudicate(), narrator="deterministic"
    )
    assert sweep["executed"] == [str(attempt.id)], sweep
    assert sweep["failed"] == []

    fresh_turn = s.get(DmTurn, turn.id)
    fresh_attempt = s.get(DmTurnAttempt, attempt.id)
    assert fresh_turn.status == "succeeded"
    assert fresh_attempt.status == "succeeded"
    assert fresh_attempt.stream_id is not None

    # Durably persisted narration, reconstructable after refresh/reconnect
    # via a brand-new session (authoritative state, not request memory).
    stream_id = fresh_attempt.stream_id
    s.close()
    with factory() as s2:
        chunks = s2.execute(
            select(DMStreamChunk)
            .where(DMStreamChunk.stream_id == stream_id)
            .order_by(DMStreamChunk.sequence)
        ).scalars().all()
        assert len(chunks) >= 1
        text = reconstruct_text(s2, stream_id)
        assert "Torchlight gutters" in text
        mat = materialize_final_narration(s2, stream_id)
        assert mat["final_text"] == text
        assert s2.get(DmTurn, turn.id).status == "succeeded"


def test_failed_model_execution_leaves_visible_failure_not_stuck_thinking(db):
    s, camp_id, thread_id, _ = db
    turn, attempt = _submit(s, camp_id, thread_id)

    def _boom(packet, feedback=None):
        raise RuntimeError("simulated provider outage")

    with pytest.raises(RuntimeError):
        execute_dm_attempt(s, attempt.id, adjudicate=_boom, narrator="deterministic")

    fresh_turn = s.get(DmTurn, turn.id)
    fresh_attempt = s.get(DmTurnAttempt, attempt.id)
    # Observable terminal failure — never permanently pending/running.
    assert fresh_attempt.status in ("failed", "failed_visible")
    assert fresh_attempt.last_error and "provider outage" in fresh_attempt.last_error
    assert fresh_turn.status != "streaming"


def test_missing_provider_config_fails_clearly(db):
    s, camp_id, thread_id, _ = db
    turn, attempt = _submit(s, camp_id, thread_id)
    from app.dm import adjudication as adj_mod

    real_resolve = adj_mod.resolve_dm_provider

    def _missing():
        raise RuntimeError("OPENCODE_GO_API_KEY is not set")

    adj_mod.resolve_dm_provider = _missing
    try:
        with pytest.raises(RuntimeError, match="API_KEY is not set"):
            execute_dm_attempt(s, attempt.id)
    finally:
        adj_mod.resolve_dm_provider = real_resolve
    # Visible failure marker, not stuck thinking.
    assert s.get(DmTurnAttempt, attempt.id).status in ("failed", "failed_visible")


def test_worker_handler_registered_for_queue_path():
    from app.queue.consumer import WORKER_HANDLERS, resolve_worker_handler

    assert DM_TURN_EXECUTE_JOB in WORKER_HANDLERS

    class _Env:
        job_type = DM_TURN_EXECUTE_JOB

    assert resolve_worker_handler(_Env()) is WORKER_HANDLERS[DM_TURN_EXECUTE_JOB]

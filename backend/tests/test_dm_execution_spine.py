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
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'execution.sqlite'}", connect_args={"check_same_thread": False}
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


@pytest.mark.parametrize("regenerate", [False, True])
def test_evidence_survives_validation_and_regeneration(db, regenerate):
    from models.campaigns import CampaignMember
    from models.characters import Character, Dnd5eCharacterSheet
    s, camp_id, thread_id, _ = db
    owner = s.get(Campaign, camp_id).owner_id
    s.add(CampaignMember(campaign_id=camp_id, user_id=owner, role="owner"))
    char = Character(owner_id=owner, name="Hero", system="dnd5e")
    s.add(char)
    s.flush()
    sheet = Dnd5eCharacterSheet.from_frontend({"name": "Hero", "total_level": 1, "armor_class": 15}, owner)
    sheet.character_id = char.id
    s.add(sheet)
    s.commit()
    sheet_id = str(sheet.id)
    _, attempt = _submit(s, camp_id, thread_id)
    calls = []

    def adjudicate(packet, feedback=None):
        calls.append(packet)
        if len(calls) == 1:
            return normalize_contract({
                "contract_version": CONTRACT_VERSION, "mode": "need_evidence",
                "reason": "check sheet", "beats": [], "safe_prelude": "Checking the sheet.",
                "evidence_requests": [{"id": "evidence_1", "tool": "ask_character_sheet",
                                       "question": "What is AC?", "scope": "current_player"}],
            })
        evidence = next(r for lane in packet.lanes for r in lane.records
                        if r.record_id == "evidence:evidence_1")
        assert evidence.value["status"] == "ok", evidence.value
        assert any(source.source_id == sheet_id for source in evidence.sources)
        assert evidence.value["result"]["combat"]["armor_class"]["value"] == 15
        contract = _fake_adjudicate("AC is 15.")(packet).model_dump(mode="json")
        claim = contract["beats"][0]["claims"][0]
        claim.update(origin="resolver_evidence", evidence_refs=[
            "unknown-source" if regenerate and len(calls) == 2 else "evidence:evidence_1",
        ])
        return normalize_contract(contract)

    result = execute_dm_attempt(s, attempt.id, adjudicate=adjudicate, narrator="deterministic")
    assert result.attempt.status == "succeeded"
    assert len(calls) == (3 if regenerate else 2)


def test_two_sessions_only_one_executor_reaches_adjudication(db):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    s, camp_id, thread_id, factory = db
    _, attempt = _submit(s, camp_id, thread_id)
    aid = attempt.id
    entered, release = Event(), Event()
    calls = []

    def adjudicate(packet, feedback=None):
        calls.append(1)
        entered.set()
        assert release.wait(10)
        return _fake_adjudicate()(packet)

    def winner():
        with factory() as worker:
            return execute_dm_attempt(worker, aid, adjudicate=adjudicate, narrator="deterministic")

    # s retains its stale prepared object while the winning session claims it.
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(winner)
        try:
            assert entered.wait(5)
            assert execute_dm_attempt(s, aid, adjudicate=adjudicate, narrator="deterministic") is None
        finally:
            release.set()
        assert future.result(timeout=10).attempt.status == "succeeded"
    assert len(calls) == 1


def test_scene_reader_failure_fails_closed_without_adjudication(db, monkeypatch):
    """Scene source failure must never downgrade to not_applicable."""
    from app.world import service as world_service

    s, camp_id, thread_id, _ = db
    _, attempt = _submit(s, camp_id, thread_id)

    def _boom(db, campaign, **kwargs):
        raise RuntimeError("simulated scene reader failure")

    monkeypatch.setattr(
        world_service, "build_current_scene_context_record", _boom
    )
    calls = []

    def adjudicate(packet, feedback=None):
        calls.append(1)
        return _fake_adjudicate()(packet)

    with pytest.raises(RuntimeError, match="scene reader failure"):
        execute_dm_attempt(
            s, attempt.id, adjudicate=adjudicate, narrator="deterministic"
        )
    assert calls == []
    fresh_attempt = s.get(DmTurnAttempt, attempt.id)
    assert fresh_attempt.status in ("failed", "failed_visible")
    assert fresh_attempt.last_error


def test_scene_db_error_mentioning_table_stays_fail_closed(db, monkeypatch):
    """A non-UndefinedTable DB error naming the scene table must not downgrade."""
    from sqlalchemy.exc import ProgrammingError

    from app.world import service as world_service

    s, camp_id, thread_id, _ = db
    _, attempt = _submit(s, camp_id, thread_id)

    def _denied(db, campaign, **kwargs):
        raise ProgrammingError(
            "SELECT * FROM campaign_current_scenes",
            {},
            Exception(
                'permission denied for table campaign_current_scenes'
            ),
        )

    monkeypatch.setattr(
        world_service, "build_current_scene_context_record", _denied
    )
    calls = []

    def adjudicate(packet, feedback=None):
        calls.append(1)
        return _fake_adjudicate()(packet)

    with pytest.raises(ProgrammingError, match="permission denied"):
        execute_dm_attempt(
            s, attempt.id, adjudicate=adjudicate, narrator="deterministic"
        )
    assert calls == []
    fresh_attempt = s.get(DmTurnAttempt, attempt.id)
    assert fresh_attempt.status in ("failed", "failed_visible")


def test_missing_scene_table_still_downgrades(db):
    """Not-yet-migrated rollout: absent scene relation stays compatible."""
    from sqlalchemy import text

    s, camp_id, thread_id, _ = db
    _, attempt = _submit(s, camp_id, thread_id)
    s.execute(text("DROP TABLE campaign_current_scenes"))
    result = execute_dm_attempt(
        s, attempt.id, adjudicate=_fake_adjudicate(), narrator="deterministic"
    )
    assert result.attempt.status == "succeeded"


def test_queue_delivery_executes_dm_attempt(db, monkeypatch):
    """Real consume_queue_delivery() path uses the single-arg worker contract."""
    import uuid as _uuid

    import database
    import app.dm.execution as exec_mod
    from app.queue.consumer import consume_queue_delivery
    from app.queue.envelope import WorkerEnvelope

    s, camp_id, thread_id, factory = db
    _, attempt = _submit(s, camp_id, thread_id)
    real_execute = exec_mod.execute_dm_attempt
    fake_adj = _fake_adjudicate()

    def _patched(session, attempt_id, **kw):
        kw.setdefault("adjudicate", fake_adj)
        kw.setdefault("narrator", "deterministic")
        return real_execute(session, attempt_id, **kw)

    monkeypatch.setattr(exec_mod, "execute_dm_attempt", _patched)
    monkeypatch.setattr(database, "SessionLocal", factory)
    env = WorkerEnvelope(
        job_id=_uuid.uuid4(),
        job_type=DM_TURN_EXECUTE_JOB,
        payload={"attempt_id": str(attempt.id)},
    )
    result, duplicate = consume_queue_delivery(s, env.to_dict())
    assert duplicate is False
    assert result["attempt_id"] == str(attempt.id)
    assert s.get(DmTurnAttempt, attempt.id).status == "succeeded"


def test_await_roll_creates_request_and_resumes_on_fulfill(db):
    """await_roll must persist a roll request, hold the turn open, and resume."""
    from models.campaigns import CampaignMember
    from models.characters import Character
    from models.dm import PlayerRollRequest
    from app.rolls.service import fulfill_roll, has_pending_rolls

    s, camp_id, thread_id, _ = db
    owner = s.get(Campaign, camp_id).owner_id
    s.add(CampaignMember(campaign_id=camp_id, user_id=owner, role="owner"))
    char = Character(owner_id=owner, name="Hero", system="dnd5e")
    s.add(char)
    s.commit()
    turn, attempt = _submit(s, camp_id, thread_id)
    char_id = str(char.id)

    def adjudicate(packet, feedback=None):
        return normalize_contract({
            "contract_version": CONTRACT_VERSION, "mode": "await_roll",
            "reason": "uncertain footing",
            "beats": [{
                "id": "beat_1", "type": "narration",
                "claims": [{
                    "text": "The ledge crumbles beneath your boots.",
                    "claim_kind": "observation",
                    "origin": "dm_adjudication", "visibility": "public",
                }],
            }],
            "roll_request": {
                "request_id": "check_1", "character_id": char_id,
                "roll_kind": "check",
                "ability_or_skill": "Acrobatics", "label": "Keep footing",
                "reason_public": "Roll to keep your footing.",
            },
        })

    result = execute_dm_attempt(
        s, attempt.id, adjudicate=adjudicate, narrator="deterministic"
    )
    assert result.mode == "await_roll"
    assert s.get(DmTurn, turn.id).status == "awaiting_roll"
    assert s.get(DmTurnAttempt, attempt.id).status == "awaiting_roll"
    assert has_pending_rolls(s, turn.id)
    rows = s.execute(
        select(PlayerRollRequest).where(PlayerRollRequest.turn_id == turn.id)
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == "pending"

    req, _fulfillment, resumed = fulfill_roll(
        s, request_id=rows[0].id, actor_id=owner,
        payload={"source": "app", "visibility": "public",
                 "raw_rolls": [14], "modifier": 2, "total": 16},
    )
    s.commit()
    assert req.status == "fulfilled"
    assert resumed is not None
    assert resumed.status == "prepared"
    assert s.get(DmTurn, turn.id).status == "pending"
    assert s.get(DmTurn, turn.id).current_attempt_id == resumed.id
    assert resumed.roll_evidence


def test_silent_completes_without_visible_stream(db):
    """silent must resolve terminally with no fabricated narration."""
    s, camp_id, thread_id, _ = db
    turn, attempt = _submit(s, camp_id, thread_id)

    def adjudicate(packet, feedback=None):
        return normalize_contract({
            "contract_version": CONTRACT_VERSION, "mode": "silent",
            "reason": "no output needed", "beats": [],
        })

    result = execute_dm_attempt(
        s, attempt.id, adjudicate=adjudicate, narrator="deterministic"
    )
    assert result.mode == "silent"
    assert s.get(DmTurn, turn.id).status == "succeeded"
    fresh_attempt = s.get(DmTurnAttempt, attempt.id)
    assert fresh_attempt.status == "succeeded"
    assert fresh_attempt.stream_id is None
    from models.dm import DMStream

    streams = s.execute(
        select(DMStream).where(
            DMStream.turn_id == str(turn.id)
        )
    ).scalars().all()
    assert streams == []


@pytest.mark.postgres
def test_live_postgres_executor_cannot_be_recovered_after_lease_age(tmp_path):
    import os
    from datetime import datetime, timedelta, timezone

    from app.dm.ownership import execution_ownership
    from app.dm.turns import mark_attempt_running, recover_stuck_attempts
    from tests.reliability.test_fault_injection import _safe_engine, _seed_campaign

    if not os.getenv("FAULT_TEST_DATABASE_URL"):
        pytest.skip("requires disposable Postgres")
    engine = _safe_engine(tmp_path)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    cid = _seed_campaign(factory)
    with factory() as db:
        owner = db.get(Campaign, cid).owner_id
        thread = CampaignThread(campaign_id=cid, thread_type="campaign", created_by=owner)
        db.add(thread)
        db.commit()
        _, attempt = _submit(db, cid, thread.id)
        aid = attempt.id
        with execution_ownership(db, aid) as acquired:
            assert acquired
            mark_attempt_running(db, aid)
            attempt.started_at = datetime.now(timezone.utc) - timedelta(hours=1)
            db.commit()
            with factory() as other:
                # Includes duplicate dispatch and an expired recovery timestamp.
                assert execute_dm_attempt(other, aid) is None
                assert recover_stuck_attempts(other, campaign_id=cid, lease_seconds=300) == 0
                assert other.get(DmTurnAttempt, aid).status == "running"
        with factory() as other:
            assert recover_stuck_attempts(other, campaign_id=cid, lease_seconds=300) == 1
            assert other.get(DmTurnAttempt, aid).status == "prepared"
    engine.dispose()

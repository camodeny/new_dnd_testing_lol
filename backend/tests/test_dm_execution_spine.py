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

from tests.support.world_writes import commit_world_write  # noqa: E402
from app.dm.contract import CONTRACT_VERSION, ContractValidationError, normalize_contract  # noqa: E402
from app.dm.execution import (  # noqa: E402
    execute_dm_attempt,
    run_dm_execute_sweep,
)
from app.dm.narration import materialize_final_narration  # noqa: E402
from app.dm.streams import reconstruct_text  # noqa: E402


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
    from app.submissions.service import accept_submission
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


def test_rules_guidance_precedes_adjudication_and_contradiction_is_advisory(db, monkeypatch):
    from app.dm import rules_guidance
    from app.dm.context import AuthorizationScope, ContextRecord, LaneName, SourceRef

    s, camp_id, thread_id, _ = db
    turn, attempt = _submit(s, camp_id, thread_id, "I attack with my sword.")
    order = []

    def enrich(db_session, packet, **kwargs):
        assert db_session is s
        order.append("retrieve")
        return packet.with_records(
            {LaneName.EVIDENCE_RESULTS: [ContextRecord(
                record_id="rules-guidance:test",
                value={"rules_guidance": True, "rule_id": "srd521.attack", "body": "Attack reference"},
                sources=[SourceRef(source_type="dnd_srd_rule", source_id="srd521.attack", source_version="5.2.1")],
                authorization=AuthorizationScope(campaign_id=str(camp_id)),
                visibility="public", use="adjudication_only",
            )]},
            dependency="rules_guidance",
        )

    fake = _fake_adjudicate()

    def adjudicate(packet, feedback=None):
        order.append("adjudicate")
        assert "Attack reference" in packet.serialize_for_adjudication()
        assert "Attack reference" not in packet.serialize_for_narration()
        return fake(packet, feedback)

    def check(packet, contract, **kwargs):
        order.append("check")
        assert contract.mode == "respond"
        return {"status": "evaluated", "outcome": "CONTRADICTED", "rule_ids": ["srd521.attack"]}

    monkeypatch.setattr(rules_guidance, "enrich_rules_context", enrich)
    monkeypatch.setattr(rules_guidance, "check_rules_advisory", check)
    execute_dm_attempt(s, attempt.id, adjudicate=adjudicate, narrator="deterministic")
    assert order == ["retrieve", "adjudicate", "check"]
    assert s.get(DmTurn, turn.id).status == "succeeded"


def test_rules_guidance_reused_after_validation_regeneration(db, monkeypatch):
    from app.dm import rules_guidance
    from app.dm.context import LaneName

    s, camp_id, thread_id, _ = db
    _, attempt = _submit(s, camp_id, thread_id)
    retrieved = []
    checked = []
    calls = []
    fake = _fake_adjudicate()

    def enrich(db_session, packet, **kwargs):
        retrieved.append(packet)
        return packet.with_records({LaneName.EVIDENCE_RESULTS: []}, dependency="rules_guidance")

    def adjudicate(packet, feedback=None):
        calls.append(packet)
        assert "rules_guidance" in packet.observability.retrieval_dependencies
        if len(calls) == 1:
            raise ContractValidationError("malformed", "retry this contract")
        return fake(packet, feedback)

    monkeypatch.setattr(rules_guidance, "enrich_rules_context", enrich)
    monkeypatch.setattr(rules_guidance, "check_rules_advisory", lambda packet, contract, **kwargs: checked.append(contract) or {"status": "skipped"})
    execute_dm_attempt(s, attempt.id, adjudicate=adjudicate, narrator="deterministic")
    assert len(retrieved) == 1
    assert len(calls) == 2
    assert len(checked) == 1


def test_optional_rules_database_failure_does_not_break_commit(db, monkeypatch):
    from sqlalchemy import text
    from app.dm import rules_guidance

    s, camp_id, thread_id, _ = db
    turn, attempt = _submit(s, camp_id, thread_id)

    def broken_search(req, audience, db=None, **kwargs):
        db.execute(text("SELECT * FROM nonexistent_rules_table"))

    def broken_judge(*args, **kwargs):
        raise RuntimeError("optional evaluator unavailable")

    monkeypatch.setattr(rules_guidance, "search_bm25_rules", broken_search)
    monkeypatch.setattr(rules_guidance, "check_rules_advisory", broken_judge)
    execute_dm_attempt(s, attempt.id, adjudicate=_fake_adjudicate(), narrator="deterministic")
    assert s.get(DmTurn, turn.id).status == "succeeded"


def test_imported_srd_passage_reaches_live_turn_adjudication(db):
    from app.rules_corpus.ingest import import_fixture_sections
    from app.dm.context import LaneName

    s, camp_id, thread_id, _ = db
    _, records = import_fixture_sections(s, [{
        "document": "playing-the-game", "heading_path": ["Combat", "Attack Rolls"],
        "title": "Attack Rolls", "body": "When you make an attack, roll a d20 and add modifiers.",
    }], source_artifact_hash="8974902d109d6e63672d7c490bde9ccf052410503d9cfa768237154fbc5e3d87", validate_canaries=False)
    s.commit()
    turn, attempt = _submit(s, camp_id, thread_id, "Attack Rolls")
    fake = _fake_adjudicate()
    seen = []

    def adjudicate(packet, feedback=None):
        evidence = next(l.records for l in packet.lanes if l.name == LaneName.EVIDENCE_RESULTS)
        passage = next(r for r in evidence if r.record_id.startswith("rules-guidance:") and r.value.get("rule_id"))
        assert passage.value["rule_id"] == records[0].rule_id
        assert passage.value["body"] == records[0].body
        assert passage.value["citation"]["license"] == "CC BY 4.0"
        assert passage.value["ranking"] == "unranked"
        seen.append(passage)
        return fake(packet, feedback)

    execute_dm_attempt(s, attempt.id, adjudicate=adjudicate, narrator="deterministic")
    assert len(seen) == 1
    assert s.get(DmTurn, turn.id).status == "succeeded"


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


def test_existing_npc_proposal_is_readjudicated_before_narration(db):
    from app.dm.context import LaneName
    from app.world.identity import add_alias
    from app.world.service import create_entity
    from models.world import WorldEntity

    s, camp_id, thread_id, _ = db
    campaign = s.get(Campaign, camp_id)
    npc, _ = create_entity(s, campaign, entity_type="npc", name="Mara Venn")
    add_alias(s, npc, "Mara")
    s.commit()
    turn, attempt = _submit(s, camp_id, thread_id, "I ask Mara what she saw.")
    calls = []

    def adjudicate(packet, feedback=None):
        calls.append(packet)
        if len(calls) == 1:
            return normalize_contract({
                "contract_version": CONTRACT_VERSION,
                "mode": "respond",
                "reason": "mistaken new NPC proposal",
                "beats": [{"id": "beat_1", "type": "narration", "claims": [{
                    "text": "A new traveler named Mara arrives at the door.",
                    "claim_kind": "observation", "origin": "dm_adjudication",
                }]}],
                "new_entities": [{
                    "temp_id": "tmp_npc_mara", "kind": "npc", "public_name": "Mara",
                }],
            })
        repairs = next(
            lane.records for lane in packet.lanes
            if lane.name == LaneName.REPAIR_DIRECTIVES
        )
        assert len(repairs) == 1
        assert repairs[0].value["canonical_entity"]["id"] == str(npc.id)
        return normalize_contract({
            "contract_version": CONTRACT_VERSION,
            "mode": "respond",
            "reason": "existing NPC answers",
            "beats": [{"id": "beat_1", "type": "narration", "claims": [{
                "text": "Mara Venn answers from beside the door.",
                "claim_kind": "observation", "origin": "dm_adjudication",
                "topic_refs": [{"type": "npc", "id": str(npc.id)}],
            }]}],
        })

    result = execute_dm_attempt(
        s, attempt.id, adjudicate=adjudicate, narrator="deterministic",
    )
    assert len(calls) == 2
    assert result.attempt.status == "succeeded"
    assert "Mara Venn answers" in result.narration.visible_text
    assert "new traveler" not in result.narration.visible_text
    assert (s.get(DmTurnAttempt, attempt.id).contract_snapshot or {})["new_entities"] == []
    assert [row.id for row in s.query(WorldEntity).all()] == [npc.id]


def test_repeated_existing_npc_proposal_never_streams(db):
    from app.world.identity import IdentityReuseRequiresReadjudication, add_alias
    from app.world.service import create_entity

    s, camp_id, thread_id, _ = db
    campaign = s.get(Campaign, camp_id)
    npc, _ = create_entity(s, campaign, entity_type="npc", name="Mara Venn")
    add_alias(s, npc, "Mara")
    s.commit()
    turn, attempt = _submit(s, camp_id, thread_id, "I ask Mara what she saw.")
    calls = []

    def adjudicate(packet, feedback=None):
        calls.append(packet)
        return normalize_contract({
            "contract_version": CONTRACT_VERSION,
            "mode": "respond",
            "reason": "repeated mistaken proposal",
            "beats": [{"id": "beat_1", "type": "narration", "claims": [{
                "text": "A new traveler named Mara arrives at the door.",
                "claim_kind": "observation", "origin": "dm_adjudication",
            }]}],
            "new_entities": [{
                "temp_id": "tmp_npc_mara", "kind": "npc", "public_name": "Mara",
            }],
        })

    with pytest.raises(IdentityReuseRequiresReadjudication):
        execute_dm_attempt(
            s, attempt.id, adjudicate=adjudicate, narrator="deterministic",
        )
    assert len(calls) == 2
    assert s.get(DmTurnAttempt, attempt.id).stream_id is None
    assert s.get(DmTurnAttempt, attempt.id).contract_snapshot is None
    assert s.get(DmTurn, turn.id).status != "succeeded"


def test_terminal_model_execution_leaves_visible_failure_not_stuck_thinking(db):
    s, camp_id, thread_id, _ = db
    turn, attempt = _submit(s, camp_id, thread_id)

    def _boom(packet, feedback=None):
        raise ValueError("simulated terminal adjudication poison")

    with pytest.raises(ValueError):
        execute_dm_attempt(s, attempt.id, adjudicate=_boom, narrator="deterministic")

    fresh_turn = s.get(DmTurn, turn.id)
    fresh_attempt = s.get(DmTurnAttempt, attempt.id)
    # Observable terminal failure — never permanently pending/running.
    assert fresh_attempt.status in ("failed", "failed_visible")
    assert fresh_attempt.last_error and "terminal adjudication" in fresh_attempt.last_error
    assert fresh_turn.status != "streaming"


def test_retriable_provider_error_stays_retryable_and_next_run_succeeds(db):
    s, camp_id, thread_id, _ = db
    turn, attempt = _submit(s, camp_id, thread_id)
    calls = []

    def _flaky(packet, feedback=None):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("simulated provider 503 unavailable")
        return _fake_adjudicate()(packet)

    with pytest.raises(TimeoutError, match="503"):
        execute_dm_attempt(s, attempt.id, adjudicate=_flaky, narrator="deterministic")
    # Pre-visibility transient: attempt back to prepared with the error kept,
    # requeued behind ready work via retry backoff.
    fresh_attempt = s.get(DmTurnAttempt, attempt.id)
    assert fresh_attempt.status == "prepared"
    assert fresh_attempt.last_error and "503" in fresh_attempt.last_error
    assert fresh_attempt.retry_count == 1
    assert fresh_attempt.next_retry_at is not None
    assert s.get(DmTurn, turn.id).status == "pending"
    # Direct execution is not gated by sweep eligibility: the same attempt
    # recovers with no manual repair once the provider heals.
    result = execute_dm_attempt(s, attempt.id, adjudicate=_flaky, narrator="deterministic")
    assert result.attempt.status == "succeeded"
    assert len(calls) == 2


def test_failing_attempt_does_not_starve_newer_prepared_attempt(db):
    """A backoff-delayed retry must not block the limit=1 cron sweep."""
    from datetime import datetime, timedelta, timezone

    s, camp_id, thread_id, factory = db
    owner = s.get(Campaign, camp_id).owner_id
    other_camp = uuid.uuid4()
    other_thread = uuid.uuid4()
    s.add(Profile(id=uuid.uuid4(), email="other@example.com"))
    s.add(Campaign(id=other_camp, owner_id=owner, name="Second", revision=0))
    s.add(
        CampaignThread(
            id=other_thread,
            campaign_id=other_camp,
            thread_type="campaign",
            created_by=owner,
        )
    )
    s.commit()
    old_turn, old_attempt = _submit(s, camp_id, thread_id, text="Old failing input.")
    new_turn, new_attempt = _submit(s, other_camp, other_thread, text="New healthy input.")
    calls = []

    def _flaky_then_healthy(packet, feedback=None):
        calls.append(str(packet.audience.campaign_id))
        if str(packet.audience.campaign_id) == str(camp_id):
            raise TimeoutError("simulated provider 503 unavailable")
        return _fake_adjudicate()(packet)

    first = run_dm_execute_sweep(
        s, limit=1, adjudicate=_flaky_then_healthy, narrator="deterministic"
    )
    assert first["executed"] == [] and len(first["failed"]) == 1
    assert s.get(DmTurnAttempt, old_attempt.id).status == "prepared"
    assert s.get(DmTurnAttempt, old_attempt.id).next_retry_at is not None
    # The newer attempt is still eligible and runs on the next sweep.
    second = run_dm_execute_sweep(
        s, limit=1, adjudicate=_flaky_then_healthy, narrator="deterministic"
    )
    assert second["executed"] == [str(new_attempt.id)], second
    assert s.get(DmTurnAttempt, new_attempt.id).status == "succeeded"
    # Once the backoff elapses the original attempt recovers on its own.
    stale = s.get(DmTurnAttempt, old_attempt.id)
    stale.next_retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    s.add(stale)
    s.commit()
    third = run_dm_execute_sweep(
        s, limit=1, adjudicate=_fake_adjudicate(), narrator="deterministic"
    )
    assert third["executed"] == [str(old_attempt.id)], third


def test_missing_provider_config_fails_clearly(db):
    s, camp_id, thread_id, _ = db
    turn, attempt = _submit(s, camp_id, thread_id)
    from app.dm import adjudication as adj_mod

    real_resolve = adj_mod.resolve_dm_provider

    def _missing():
        raise RuntimeError("OPENAI_API_KEY is not set")

    adj_mod.resolve_dm_provider = _missing
    try:
        with pytest.raises(RuntimeError, match="API_KEY is not set"):
            execute_dm_attempt(s, attempt.id)
    finally:
        adj_mod.resolve_dm_provider = real_resolve
    # Retriable config failure: back to prepared with the error kept, not stuck.
    fresh_attempt = s.get(DmTurnAttempt, attempt.id)
    assert fresh_attempt.status == "prepared"
    assert fresh_attempt.last_error and "API_KEY" in fresh_attempt.last_error


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

    s, camp_id, thread_id, _ = db
    _, attempt = _submit(s, camp_id, thread_id)

    def _boom(db, campaign, **kwargs):
        raise RuntimeError("simulated scene reader failure")

    monkeypatch.setattr(
        "app.dm.context.build_current_scene_context_record", _boom
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
    """A DB error naming the scene table must not downgrade to no-scene."""
    from sqlalchemy.exc import ProgrammingError


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
        "app.dm.context.build_current_scene_context_record", _denied
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

    req, _fulfillment, resumed, _encounter_ready = fulfill_roll(
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

    # The resumed autonomous execution must see the fulfilled die result.
    seen: dict = {}

    def resumed_adjudicate(packet, feedback=None):
        for lane in packet.lanes:
            if lane.name == "evidence_results":
                for record in lane.records:
                    if record.record_id == "roll_evidence:check_1":
                        seen["record"] = record
        return _fake_adjudicate("You hold your footing.")(packet)

    resumed_result = execute_dm_attempt(
        s, resumed.id, adjudicate=resumed_adjudicate, narrator="deterministic"
    )
    assert resumed_result.attempt.status == "succeeded"
    assert "record" in seen
    fulfillment = (seen["record"].value or {}).get("fulfillment") or {}
    assert fulfillment.get("total") == 16
    # Private DC data stays adjudication-only, never narration-eligible.
    assert seen["record"].use == "adjudication_only"


def test_silent_completes_without_visible_stream(db):
    """silent must resolve terminally with no fabricated narration."""
    from models.threads import PlayerSubmission

    s, camp_id, thread_id, _ = db
    turn, attempt = _submit(s, camp_id, thread_id)
    first_submission_ids = list(turn.submission_ids or [])
    assert len(first_submission_ids) == 1

    def adjudicate(packet, feedback=None):
        return normalize_contract({
            "contract_version": CONTRACT_VERSION, "mode": "silent",
            "reason": "no output needed", "beats": [],
        })

    result = execute_dm_attempt(
        s, attempt.id, adjudicate=adjudicate, narrator="deterministic"
    )
    assert result.mode == "silent"
    assert result.event is not None
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
    # Consumed input is resolved, so the next turn never re-adjudicates it.
    first = s.get(PlayerSubmission, uuid.UUID(str(first_submission_ids[0])))
    assert first.resolution_status == "resolved"
    next_turn, _ = _submit(s, camp_id, thread_id, text="I press on down the hall.")
    assert first_submission_ids[0] not in list(next_turn.submission_ids or [])
    assert len(next_turn.submission_ids or []) == 1


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


def test_structural_error_repairs_inside_round_and_mediates_evidence(db):
    """PR #360 review: a structurally invalid adjudication must heal inside
    the evidence round — need_evidence mediation and tools still run, and the
    turn commits normally."""
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
    _, attempt = _submit(s, camp_id, thread_id)
    calls = []

    def adjudicate(packet, feedback=None):
        calls.append(feedback)
        if len(calls) == 1:
            # Simulates a structurally invalid provider output (normalize fails).
            raise ContractValidationError("contract_validation_failed", "respond requires 1-8 beats")
        if len(calls) == 2:
            return normalize_contract({
                "contract_version": CONTRACT_VERSION, "mode": "need_evidence",
                "reason": "check sheet", "beats": [], "safe_prelude": "Checking the sheet.",
                "evidence_requests": [{"id": "evidence_1", "tool": "ask_character_sheet",
                                       "question": "What is AC?", "scope": "current_player"}],
            })
        contract = _fake_adjudicate("AC is 15.")(packet).model_dump(mode="json")
        claim = contract["beats"][0]["claims"][0]
        claim.update(origin="resolver_evidence", evidence_refs=["evidence:evidence_1"])
        return normalize_contract(contract)

    result = execute_dm_attempt(s, attempt.id, adjudicate=adjudicate, narrator="deterministic")
    assert result.attempt.status == "succeeded"
    # invalid first shot repaired in-round, need_evidence round mediated tools,
    # final respond committed: without in-round repair the first raise would
    # propagate and fail the attempt instead.
    assert len(calls) == 3


def test_structural_exhaustion_fails_visibly_not_running(db):
    """PR #360 review: when every regeneration is structurally invalid, the
    attempt must fail visibly (or requeue) — never stay stuck in running."""
    from app.dm.validators import ValidatorRejectionError
    s, camp_id, thread_id, _ = db
    _, attempt = _submit(s, camp_id, thread_id)

    def _always_broken(packet, feedback=None):
        raise ContractValidationError("contract_validation_failed", "respond requires 1-8 beats")

    with pytest.raises(ValidatorRejectionError):
        execute_dm_attempt(s, attempt.id, adjudicate=_always_broken, narrator="deterministic")
    fresh_attempt = s.get(DmTurnAttempt, attempt.id)
    assert fresh_attempt.status in ("failed", "failed_visible")
    assert fresh_attempt.last_error
    assert s.get(DmTurn, attempt.turn_id).status != "streaming"


def test_perspective_repair_packet_carries_through_validation(db):
    """Issue #455 review: a contract that passed against the perspective-repaired
    packet must not be re-validated against the unrepaired one (which would
    fail again and start a second full regeneration)."""
    from app.world.knowledge import assert_knowledge
    from app.world.service import create_entity

    s, camp_id, thread_id, _ = db
    camp = s.get(Campaign, camp_id)
    npc, _ = commit_world_write(
        s, camp_id, 0, create_entity, entity_type="npc", name="Hooded Traveler", operation_id="op-hood-exec",
    )
    well, _ = commit_world_write(
        s, camp_id, 1, create_entity, entity_type="location", name="Old Well", operation_id="op-well-exec",
    )
    assert_knowledge(
        s, camp, subject_kind="npc", subject_entity_id=npc.id,
        target_kind="entity", target_entity_id=well.id,
        knowledge_state="knows", acquisition_source="direct_observation",
        operation_id="op-know-exec",
    )
    s.commit()
    _, attempt = _submit(s, camp_id, thread_id)
    calls = []

    def adjudicate(packet, feedback=None):
        calls.append(feedback)
        if len(calls) == 1:
            raise ContractValidationError("contract_validation_failed", "respond requires 1-8 beats")
        return normalize_contract({
            "contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "npc speaks",
            "beats": [{
                "id": "b1", "type": "npc_dialogue",
                "speaker_ref": {"type": "npc", "id": str(npc.id)},
                "speaker_public_name": "Hooded traveler", "truth_status": "truthful",
                "claims": [{
                    "text": "The old well runs deep.", "claim_kind": "npc_utterance",
                    "actor_ref": {"type": "npc", "id": str(npc.id)},
                    "topic_refs": [{"type": "location", "id": str(well.id)}],
                    "origin": "dm_adjudication",
                }],
            }],
            "open_player_choice": "What do you do?",
        })

    result = execute_dm_attempt(s, attempt.id, adjudicate=adjudicate, narrator="deterministic")
    assert result.attempt.status == "succeeded"
    # structural failure, unrepaired dialogue, retry against the repaired packet
    assert len(calls) == 3

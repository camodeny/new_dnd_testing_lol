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


def _new_npc_contract():
    return normalize_contract({
        "contract_version": CONTRACT_VERSION,
        "mode": "respond",
        "reason": "introduces a new NPC",
        "beats": [{"id": "beat_1", "type": "narration", "claims": [{
            "text": "A stranger named Orsa Pell steps out of the fog.",
            "claim_kind": "observation", "origin": "dm_adjudication",
        }]}],
        "new_entities": [{
            "temp_id": "tmp_npc_1", "kind": "npc", "public_name": "Orsa Pell",
        }],
    })


def _stub_identity_decisions(monkeypatch, outcomes):
    """Stub the bounded identity decision; records prior_deferrals per call."""
    from types import SimpleNamespace

    from app.world import identity

    seen = []
    queue = list(outcomes)

    def fake_decide(db, campaign, frame, service, *, prior_deferrals=0, **kw):
        seen.append(prior_deferrals)
        selected = queue.pop(0)
        if selected == identity.DEFER and prior_deferrals > 0:
            return SimpleNamespace(selected_id=identity.NEW_ENTITY, runner_up_applied=True)
        return SimpleNamespace(selected_id=selected, runner_up_applied=False)

    monkeypatch.setattr(identity, "decide_identity", fake_decide)
    return seen


def _near_tie_npc(s, camp_id):
    from app.world.service import create_entity

    create_entity(s, s.get(Campaign, camp_id), entity_type="npc", name="Orsa Pelle")
    s.commit()


def test_identity_defer_then_new_entity_commits_in_one_attempt(db, monkeypatch):
    from app.world import identity

    s, camp_id, thread_id, _ = db
    _near_tie_npc(s, camp_id)
    turn, attempt = _submit(s, camp_id, thread_id, "I look into the fog.")
    seen = _stub_identity_decisions(monkeypatch, [identity.DEFER, identity.NEW_ENTITY])
    packets = []

    def adjudicate(packet, feedback=None):
        packets.append(packet)
        return _new_npc_contract()

    result = execute_dm_attempt(
        s, attempt.id, adjudicate=adjudicate, narrator="deterministic",
    )
    assert result.attempt.status == "succeeded"
    assert len(packets) == 2
    from app.dm.context import IDENTITY_DEFERRAL_RECORD_ID

    assert any(
        r.record_id == IDENTITY_DEFERRAL_RECORD_ID
        for lane in packets[1].lanes for r in lane.records
    )
    assert s.get(DmTurn, turn.id).status == "succeeded"
    assert seen == [0, 1]


def test_identity_repeat_defer_takes_runner_up_fallback(db, monkeypatch):
    from app.world import identity

    s, camp_id, thread_id, _ = db
    _near_tie_npc(s, camp_id)
    turn, attempt = _submit(s, camp_id, thread_id, "I look into the fog.")
    seen = _stub_identity_decisions(monkeypatch, [identity.DEFER, identity.DEFER])

    result = execute_dm_attempt(
        s, attempt.id, adjudicate=lambda p, feedback=None: _new_npc_contract(),
        narrator="deterministic",
    )
    assert result.attempt.status == "succeeded"
    assert seen == [0, 1]
    resolutions = s.get(DmTurnAttempt, attempt.id).identity_resolutions
    assert resolutions[0]["runner_up_fallback"] is True


def test_identity_defer_without_readjudication_is_retriable_not_visible_failure(db, monkeypatch):
    from types import SimpleNamespace

    from app.world import identity

    s, camp_id, thread_id, _ = db
    _near_tie_npc(s, camp_id)
    turn, attempt = _submit(s, camp_id, thread_id, "I look into the fog.")
    # A decision that keeps deferring even past the runner-up fallback.
    monkeypatch.setattr(
        identity, "decide_identity",
        lambda *a, **k: SimpleNamespace(selected_id=identity.DEFER, runner_up_applied=False),
    )
    with pytest.raises(Exception) as err:
        execute_dm_attempt(
            s, attempt.id, adjudicate=lambda p, feedback=None: _new_npc_contract(),
            narrator="deterministic",
        )
    assert "deferred" in str(err.value)
    row = s.get(DmTurnAttempt, attempt.id)
    assert row.status == "prepared" and row.error_class == "retriable"
    assert s.get(DmTurn, turn.id).status != "failed_visible"


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


def test_retry_asking_for_evidence_resumes_the_evidence_loop(db):
    """A regeneration retry may answer a rejection with need_evidence; the
    evidence must be fetched and the turn resolved, never committed as a
    prelude-only need_evidence turn."""
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
        calls.append(packet)
        if len(calls) == 1:
            # Valid structure, rejected by validation: cites a missing source.
            contract = _fake_adjudicate("AC is 15.")(packet).model_dump(mode="json")
            contract["beats"][0]["claims"][0].update(origin="resolver_evidence", evidence_refs=["unknown-source"])
            return normalize_contract(contract)
        if len(calls) == 2:
            return normalize_contract({
                "contract_version": CONTRACT_VERSION, "mode": "need_evidence",
                "reason": "check sheet", "beats": [], "safe_prelude": "Checking the sheet.",
                "evidence_requests": [{"id": "evidence_1", "tool": "ask_character_sheet",
                                       "question": "What is AC?", "scope": "current_player"}],
            })
        assert any(r.record_id == "evidence:evidence_1" for lane in packet.lanes for r in lane.records)
        contract = _fake_adjudicate("AC is 15.")(packet).model_dump(mode="json")
        contract["beats"][0]["claims"][0].update(origin="resolver_evidence", evidence_refs=["evidence:evidence_1"])
        return normalize_contract(contract)

    result = execute_dm_attempt(s, attempt.id, adjudicate=adjudicate, narrator="deterministic")
    assert result.attempt.status == "succeeded"
    assert result.attempt.contract_snapshot["mode"] == "respond"
    assert len(calls) == 3


def test_second_evidence_request_after_resume_is_feedback_not_failure(db):
    """After the one evidence resume, a retry that asks for evidence again is
    told to resolve from the packet; the turn resolves instead of failing."""
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

    def unsourced(packet):
        contract = _fake_adjudicate("AC is 15.")(packet).model_dump(mode="json")
        contract["beats"][0]["claims"][0].update(origin="resolver_evidence", evidence_refs=["unknown-source"])
        return normalize_contract(contract)

    def evidence_request(request_id):
        return normalize_contract({
            "contract_version": CONTRACT_VERSION, "mode": "need_evidence",
            "reason": "check sheet", "beats": [], "safe_prelude": "Checking the sheet.",
            "evidence_requests": [{"id": request_id, "tool": "ask_character_sheet",
                                   "question": "What is AC?", "scope": "current_player"}],
        })

    def adjudicate(packet, feedback=None):
        calls.append(feedback)
        n = len(calls)
        if n in (1, 3):
            return unsourced(packet)          # rejected: cites a missing source
        if n == 2:
            return evidence_request("evidence_1")  # retry resumes the loop once
        if n == 4:
            return evidence_request("evidence_2")  # asks again after the resume
        contract = _fake_adjudicate("AC is 15.")(packet).model_dump(mode="json")
        contract["beats"][0]["claims"][0].update(origin="resolver_evidence", evidence_refs=["evidence:evidence_1"])
        return normalize_contract(contract)

    result = execute_dm_attempt(s, attempt.id, adjudicate=adjudicate, narrator="deterministic")
    assert result.attempt.status == "succeeded"
    assert result.attempt.contract_snapshot["mode"] == "respond"
    assert len(calls) == 5
    assert "evidence_already_resolved" in (calls[4] or "")


def test_evidence_round_limit_resolves_from_gathered_evidence(db):
    """A DM that keeps asking for evidence past the round limit is told to
    resolve from what it gathered; the turn commits instead of failing."""
    from app.dm.evidence import MAX_EVIDENCE_ROUNDS
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
        if len(calls) <= MAX_EVIDENCE_ROUNDS + 1:
            return normalize_contract({
                "contract_version": CONTRACT_VERSION, "mode": "need_evidence",
                "reason": "check sheet", "beats": [], "safe_prelude": "Checking the sheet.",
                "evidence_requests": [{"id": f"evidence_{len(calls)}", "tool": "ask_character_sheet",
                                       "question": "What is AC?", "scope": "current_player"}],
            })
        assert any(r.record_id == "evidence:evidence_1" for lane in packet.lanes for r in lane.records)
        contract = _fake_adjudicate("AC is 15.")(packet).model_dump(mode="json")
        contract["beats"][0]["claims"][0].update(origin="resolver_evidence", evidence_refs=["evidence:evidence_1"])
        return normalize_contract(contract)

    result = execute_dm_attempt(s, attempt.id, adjudicate=adjudicate, narrator="deterministic")
    assert result.attempt.status == "succeeded"
    assert result.attempt.contract_snapshot["mode"] == "respond"


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


@pytest.mark.parametrize("repeat_key,repeat_skill,code", [
    ("check_2", " acrobatics ", "roll_already_resolved"),
    ("check_1", "Athletics", "roll_request_key_reused"),
])
def test_resumed_turn_cannot_reroll_its_resolved_intent(db, repeat_key, repeat_skill, code):
    """After a fulfilled roll, a repeat request for the same skill (or a reused
    request key) is regeneration feedback, never a new roll or a DB error."""
    from models.campaigns import CampaignMember
    from models.characters import Character
    from models.dm import PlayerRollRequest
    from app.rolls.service import fulfill_roll

    s, camp_id, thread_id, _ = db
    owner = s.get(Campaign, camp_id).owner_id
    s.add(CampaignMember(campaign_id=camp_id, user_id=owner, role="owner"))
    char = Character(owner_id=owner, name="Hero", system="dnd5e")
    s.add(char)
    s.commit()
    turn, attempt = _submit(s, camp_id, thread_id)
    char_id = str(char.id)

    def roll_contract(request_id, skill):
        return normalize_contract({
            "contract_version": CONTRACT_VERSION, "mode": "await_roll",
            "reason": "uncertain footing",
            "beats": [{"id": "beat_1", "type": "narration", "claims": [{
                "text": "The ledge crumbles beneath your boots.", "claim_kind": "observation",
                "origin": "dm_adjudication", "visibility": "public",
            }]}],
            "roll_request": {
                "request_id": request_id, "character_id": char_id, "roll_kind": "check",
                "ability_or_skill": skill, "label": "Keep footing",
                "reason_public": "Roll to keep your footing.",
            },
        })

    execute_dm_attempt(s, attempt.id, adjudicate=lambda p, f=None: roll_contract("check_1", "Acrobatics"),
                       narrator="deterministic")
    (pending,) = s.execute(select(PlayerRollRequest).where(PlayerRollRequest.turn_id == turn.id)).scalars().all()
    _req, _f, resumed, _e = fulfill_roll(
        s, request_id=pending.id, actor_id=owner,
        payload={"source": "app", "visibility": "public", "raw_rolls": [14], "modifier": 2, "total": 16},
    )
    s.commit()

    feedback_seen = []

    def resumed_adjudicate(packet, feedback=None):
        feedback_seen.append(feedback)
        if len(feedback_seen) == 1:
            return roll_contract(repeat_key, repeat_skill)
        return _fake_adjudicate("You hold your footing.")(packet)

    result = execute_dm_attempt(s, resumed.id, adjudicate=resumed_adjudicate, narrator="deterministic")
    assert result.attempt.status == "succeeded"
    assert len(feedback_seen) == 2 and code in (feedback_seen[1] or "")
    rows = s.execute(select(PlayerRollRequest).where(PlayerRollRequest.turn_id == turn.id)).scalars().all()
    assert [r.request_key for r in rows] == ["check_1"]


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


def test_npc_active_last_turn_has_perspective_on_next_first_call(db):
    """An NPC who spoke in a recent visible turn is in the next packet's
    knowledge lane before the first model call (no fail-repair-retry)."""
    from app.dm.context import LaneName
    from app.world.knowledge import assert_knowledge
    from app.world.service import create_entity

    s, camp_id, thread_id, _ = db
    camp = s.get(Campaign, camp_id)
    npc, _ = commit_world_write(
        s, camp_id, 0, create_entity, entity_type="npc", name="Hooded Traveler", operation_id="op-hood-active",
    )
    well, _ = commit_world_write(
        s, camp_id, 1, create_entity, entity_type="location", name="Old Well", operation_id="op-well-active",
    )
    assert_knowledge(
        s, camp, subject_kind="npc", subject_entity_id=npc.id,
        target_kind="entity", target_entity_id=well.id,
        knowledge_state="knows", acquisition_source="direct_observation",
        operation_id="op-know-active",
    )
    s.commit()
    first_call_subjects = []

    def adjudicate(packet, feedback=None):
        if feedback is None:
            lane = next(l for l in packet.lanes if l.name == LaneName.KNOWLEDGE_VISIBILITY)
            first_call_subjects.append({str((r.value or {}).get("subject_entity_id")) for r in lane.records})
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

    for text in ("I ask the traveler about the well.", "I ask what lies below."):
        _, attempt = _submit(s, camp_id, thread_id, text=text)
        result = execute_dm_attempt(s, attempt.id, adjudicate=adjudicate, narrator="deterministic")
        assert result.attempt.status == "succeeded"

    assert str(npc.id) not in first_call_subjects[0]
    assert str(npc.id) in first_call_subjects[1]


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
    # structural failure, then dialogue that passes once the NPC's stored
    # perspective is loaded into the packet — no further model call
    assert len(calls) == 2


@pytest.mark.parametrize("kind,dc,total,expected", [
    ("check", 13, 22, {"result": "success", "margin": 9}),
    ("save", 13, 13, {"result": "success", "margin": 0}),
    ("attack", 15, 11, None),  # #234: attacks resolve against AC, never a DC
    ("check", None, 18, None),
    ("initiative", 10, 18, None),
    ("check", 12, None, None),
])
def test_roll_evidence_states_code_computed_outcome(kind, dc, total, expected):
    from app.dm.context import _roll_outcome

    assert _roll_outcome(kind, dc, {"total": total}) == expected


@pytest.mark.parametrize("first_target", [None, "npc:pale-thing-never-registered"])
def test_attack_roll_must_target_a_registered_entity(db, first_target):
    """Combat against something only narrated can never land damage: the
    attack is refused as feedback until it targets a registered entity."""
    from app.world.service import create_entity
    from models.campaigns import CampaignMember
    from models.characters import Character, Dnd5eCharacterSheet
    from models.dm import PlayerRollRequest

    s, camp_id, thread_id, _ = db
    owner = s.get(Campaign, camp_id).owner_id
    char = Character(owner_id=owner, name="Hero", system="dnd5e")
    s.add(char)
    s.flush()
    s.add(CampaignMember(campaign_id=camp_id, user_id=owner, role="owner", selected_character_id=char.id))
    s.add(Dnd5eCharacterSheet(character_id=char.id, owner_id=owner, character_name="Hero", weapons=[
        {"name": "Longsword", "attack_bonus": 5, "damage": "1d8+3", "damage_type": "slashing"},
    ]))
    s.commit()
    beast, _ = commit_world_write(
        s, camp_id, 0, create_entity, entity_type="npc", name="Reef Horror", operation_id="op-reef-horror",
        details={"armor_class": 13, "hit_points": {"current": 20, "maximum": 20, "temporary": 0}},
    )
    s.commit()
    turn, attempt = _submit(s, camp_id, thread_id, text="I swing my sword at the reef horror.")
    feedback_seen = []

    def adjudicate(packet, feedback=None):
        feedback_seen.append(feedback)
        target = first_target if len(feedback_seen) == 1 else str(beast.id)
        roll = {"request_id": f"attack_{len(feedback_seen)}", "character_id": str(char.id),
                "roll_kind": "attack", "ability_or_skill": "Longsword", "label": "Sword strike",
                "reason_public": "Roll to hit the reef horror."}
        if target is not None:
            roll["target_ref"] = {"type": "npc", "id": target}
        return normalize_contract({
            "contract_version": CONTRACT_VERSION, "mode": "await_roll", "reason": "attack",
            "beats": [{"id": "beat_1", "type": "narration", "claims": [{
                "text": "The reef horror rears up from the surf.", "claim_kind": "observation",
                "origin": "dm_adjudication", "visibility": "public",
            }]}],
            "roll_request": roll,
        })

    result = execute_dm_attempt(s, attempt.id, adjudicate=adjudicate, narrator="deterministic")
    assert result.mode == "await_roll"
    assert len(feedback_seen) == 2 and "attack_target_unregistered" in (feedback_seen[1] or "")
    (row,) = s.execute(select(PlayerRollRequest).where(PlayerRollRequest.turn_id == turn.id)).scalars().all()
    assert row.request_key == "attack_2"
    assert (row.target_kind, row.target_id, row.attack_name) == ("npc", str(beast.id), "Longsword")


@pytest.mark.parametrize("post_turn_current", [False, True])
def test_context_over_budget_defers_while_post_turn_lags(db, monkeypatch, post_turn_current):
    """Required context overflowing because post-turn trails is backpressure:
    the attempt waits for catch-up instead of failing; with post-turn current,
    waiting cannot help and it fails visibly as before."""
    from app.dm import execution as ex
    from app.dm.context import ContextBudgetError
    from app.post_turn.service import get_checkpoint

    s, camp_id, thread_id, _ = db
    turn, attempt = _submit(s, camp_id, thread_id)
    # Played history the post-turn checkpoint has not consolidated yet.
    attempt.source_revision = 21
    cp = get_checkpoint(s, camp_id, commit=False)
    cp.processed_through_sequence = 21 if post_turn_current else 0
    s.commit()

    def overflow(_db, _attempt_id):
        raise ContextBudgetError("Required authoritative context is 64285 bytes, above 64000-byte budget")

    monkeypatch.setattr(ex, "_assemble_production_context", overflow)
    if post_turn_current:
        with pytest.raises(ContextBudgetError):
            execute_dm_attempt(s, attempt.id, adjudicate=_fake_adjudicate(), narrator="deterministic")
        assert s.get(DmTurnAttempt, attempt.id).status != "prepared"
        return
    assert execute_dm_attempt(s, attempt.id, adjudicate=_fake_adjudicate(), narrator="deterministic") is None
    s.expire_all()
    deferred = s.get(DmTurnAttempt, attempt.id)
    assert deferred.status == "prepared"
    assert deferred.retry_count == 0
    assert deferred.next_retry_at is not None

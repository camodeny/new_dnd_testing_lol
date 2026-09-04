"""Issue #207 — narration from audience-safe structured projection, low-TTFT durable streaming."""
import json
import uuid

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from database import Base  # noqa: E402
from models.campaigns import Campaign, CampaignDomainEvent  # noqa: E402
from models.dm import DMStream, DMStreamChunk  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.threads import CampaignThread  # noqa: E402

from app.dm.contract import CONTRACT_VERSION, normalize_contract  # noqa: E402
from app.dm.narration import (  # noqa: E402
    NARRATOR_CONTRACT,
    NarratorGenerationError,
    NarratorRequest,
    NarrationFidelityError,
    build_narration_projection,
    build_narrator_prompt,
    check_narration_fidelity_or_raise,
    chunk_narration_text,
    execute_validated_turn,
    get_narration_metrics,
    materialize_final_narration,
    render_deterministic_narration,
    reset_narration_metrics,
    resume_narration_stream,
    stream_narration,
    validate_narration_fidelity,
)


# ── fixtures ────────────────────────────────────────────────────────────────

def _claim(text, kind="observation", origin="established_state", **over):
    base = {"text": text, "claim_kind": kind, "origin": origin, "visibility": "public"}
    base.update(over)
    return base


def _respond(beats, **over):
    payload = {
        "contract_version": CONTRACT_VERSION, "mode": "respond",
        "reason": "story continuation", "beats": beats,
    }
    payload.update(over)
    return normalize_contract(payload)


def _narr_beat(text, beat_id="beat_1", **over):
    return {"id": beat_id, "type": "narration", "claims": [_claim(text, **over)]}


def _deceptive_npc():
    return _respond([
        {
            "id": "beat_1", "type": "npc_dialogue",
            "speaker_ref": {"type": "npc", "id": "npc:vera"},
            "speaker_public_name": "Vera",
            "truth_status": "deceptive",
            "dm_private_context": "Vera secretly serves the Red Mask cabal",
            "claims": [{
                "text": "The north road is perfectly safe, travelers.",
                "claim_kind": "npc_utterance", "origin": "dm_adjudication",
                "actor_ref": {"type": "npc", "id": "npc:vera"}, "visibility": "public",
            }],
        }
    ])


def _await_roll():
    return normalize_contract({
        "contract_version": CONTRACT_VERSION, "mode": "await_roll", "reason": "uncertain footing",
        "beats": [_narr_beat(
            "Loose stones cover the ledge ahead.",
            kind="roll_instruction", origin="dm_adjudication",
        )],
        "roll_request": {
            "request_id": "roll_1", "roll_kind": "check",
            "ability_or_skill": "Acrobatics", "label": "Acrobatics check",
            "advantage_state": "normal",
            "reason_public": "The ledge looks treacherous and demands care.",
            "dc_private": 14,
        },
    })


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
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
    reset_narration_metrics()


@pytest.fixture(autouse=True)
def _reset_metrics():
    reset_narration_metrics()
    yield
    reset_narration_metrics()


# ── projection: audience-safe ───────────────────────────────────────────────

def test_projection_strips_private_truth_hidden_dc_and_internal_ids():
    c = _deceptive_npc()
    proj = build_narration_projection(c)
    blob = json.dumps(proj, ensure_ascii=False)
    assert "Red Mask" not in blob
    assert "dm_private_context" not in blob.lower()
    assert "truth_status" not in blob.lower()
    # utterance itself survives
    assert "north road is perfectly safe" in blob


def test_projection_strips_hidden_dc_but_keeps_public_reason():
    c = _await_roll()
    proj = build_narration_projection(c)
    blob = json.dumps(proj, ensure_ascii=False)
    assert "dc_private" not in blob.lower()
    # hidden threshold value must not appear anywhere
    assert ": 14" not in blob and '"14"' not in blob
    assert proj["roll_request"]["reason_public"].startswith("The ledge looks treacherous")
    assert "request_id" in proj["roll_request"]


def test_projection_excludes_effects_evidence_provenance_and_temp_ids():
    c = _respond(
        [_narr_beat("Torchlight flickers on wet stone.")],
        staged_effects=[{
            "id": "eff_1", "effect_type": "record_world_event",
            "arguments": {"event_type": "torch_lit", "summary": "Torch lit", "visibility": "public"},
        }],
        new_entities=[{
            "temp_id": "tmp_npc_1", "kind": "npc", "public_name": "Hooded Stranger",
            "public_summary": "A watcher",
        }],
        open_player_choice="What do you do?",
    )
    proj = build_narration_projection(c)
    blob = json.dumps(proj, ensure_ascii=False)
    assert "eff_1" not in blob and "tmp_npc_1" not in blob
    assert "Hooded Stranger" not in blob
    assert "What do you do?" in blob


def test_narrator_prompt_carries_no_readjudication_contract():
    c = _respond([_narr_beat("Rain drums on the canvas.")])
    prompt = build_narrator_prompt(build_narration_projection(c))
    assert "NEVER re-adjudicate" in prompt or "re-adjudicate" in prompt
    assert "no game-state authority" in prompt.lower() or "change nothing" in prompt.lower()
    assert NARRATOR_CONTRACT.splitlines()[0] in prompt


# ── template narrator: truthful/deceptive/player-authored ───────────────────

def test_truthful_npc_renders_utterance_without_truth_commentary():
    c = normalize_contract({
        "contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "greeting",
        "beats": [{
            "id": "beat_1", "type": "npc_dialogue",
            "speaker_ref": {"type": "npc", "id": "npc:odin"},
            "speaker_public_name": "Odin",
            "truth_status": "truthful",
            "claims": [{
                "text": "Welcome to the hall.", "claim_kind": "npc_utterance",
                "origin": "dm_adjudication",
                "actor_ref": {"type": "npc", "id": "npc:odin"}, "visibility": "public",
            }],
        }],
    })
    text = render_deterministic_narration(build_narration_projection(c), c)
    assert 'Odin says: "Welcome to the hall."' in text
    assert "truthful" not in text.lower()
    assert validate_narration_fidelity(text, c) == []


def test_deceptive_npc_private_truth_never_narrated():
    c = _deceptive_npc()
    text = render_deterministic_narration(build_narration_projection(c), c)
    assert "Red Mask" not in text
    assert "deceptive" not in text.lower()
    assert 'Vera says: "The north road is perfectly safe, travelers."' in text
    assert validate_narration_fidelity(text, c) == []


def test_player_authored_declaration_preserved_with_attribution():
    c = _respond([{
        "id": "beat_1", "type": "narration",
        "claims": [{
            "text": 'Elara declares, "I will hold the bridge!"',
            "claim_kind": "player_declaration", "origin": "player_transcript",
            "actor_ref": {"type": "character", "id": "char:elara"},
            "evidence_refs": ["sub1"], "visibility": "public",
        }],
    }])
    text = render_deterministic_narration(build_narration_projection(c), c)
    assert "I will hold the bridge!" in text
    assert validate_narration_fidelity(
        text, c, pc_names={"char:elara": "Elara"}
    ) == []


def test_ordinary_narration_passes_fidelity():
    c = _respond([_narr_beat("Torchlight flickers on wet stone.")],
                 open_player_choice="What do you do?")
    text = render_deterministic_narration(build_narration_projection(c), c)
    assert "Torchlight flickers" in text and "What do you do?" in text
    assert validate_narration_fidelity(text, c) == []


# ── fidelity gates ──────────────────────────────────────────────────────────

def test_secret_fact_leak_rejected_pre_commit_with_no_persistence(db):
    s, camp_id, thread_id = db
    c = _respond([_narr_beat("The vault door stands shut.")])
    secret = "the vault code is moonfall"
    bad = "The vault door stands shut. You recall the vault code is moonfall."
    with pytest.raises(NarrationFidelityError) as ei:
        stream_narration(
            s, campaign_id=camp_id, thread_id=thread_id,
            turn_id=str(uuid.uuid4()), attempt_id=str(uuid.uuid4()),
            contract=c, narrator=lambda req: bad,
            publish_realtime=False, extra_secrets={secret},
        )
    assert any(v["category"] == "secret_leakage" for v in ei.value.violations)
    assert s.scalar(select(func.count()).select_from(DMStream)) == 0
    m = get_narration_metrics()
    assert m["secret_rejections"] >= 1 and m["fidelity_failures"] >= 1


def test_unsupported_narrator_addition_rejected():
    c = _respond([_narr_beat("The goblin snarls and circles.")])
    bad = "The goblin snarls and circles. It takes 7 damage and dies."
    with pytest.raises(NarrationFidelityError) as ei:
        check_narration_fidelity_or_raise(bad, c)
    assert any(v["category"] == "unsupported_addition" for v in ei.value.violations)


def test_pc_agency_violation_rejected():
    c = _respond([{
        "id": "beat_1", "type": "narration",
        "claims": [{
            "text": 'Elara declares, "I hold my ground."',
            "claim_kind": "player_declaration", "origin": "player_transcript",
            "actor_ref": {"type": "character", "id": "char:elara"},
            "evidence_refs": ["sub1"], "visibility": "public",
        }],
    }])
    bad = 'Elara declares, "I hold my ground." Elara charges the dragon and attacks.'
    with pytest.raises(NarrationFidelityError) as ei:
        check_narration_fidelity_or_raise(bad, c, pc_names={"char:elara": "Elara"})
    assert any(v["category"] == "agency_violation" for v in ei.value.violations)
    m = get_narration_metrics()
    assert m["agency_rejections"] >= 1


def test_contradiction_with_structured_result_rejected():
    c = _respond([_narr_beat("The old bridge stands intact over the gorge.")])
    bad = "The old bridge stands intact over the gorge. The bridge is broken and collapses."
    with pytest.raises(NarrationFidelityError) as ei:
        check_narration_fidelity_or_raise(bad, c)
    assert any(v["category"] == "contradiction" for v in ei.value.violations)


# ── streaming: durable-first, realtime-second, TTFT, resume ─────────────────

def test_stream_persists_before_delivery_and_ttft_measured(db, monkeypatch):
    from app.realtime.service import InMemoryRealtimePublisher, set_realtime_publisher
    s, camp_id, thread_id = db
    pub = InMemoryRealtimePublisher()
    set_realtime_publisher(pub)
    monkeypatch.setattr("app.realtime.service._publisher", pub, raising=False)
    try:
        c = _respond([_narr_beat(
            "Torchlight flickers on wet stone as the party descends the stair. "
            "Water drips somewhere in the dark below."
        )], open_player_choice="What do you do?")
        res = stream_narration(
            s, campaign_id=camp_id, thread_id=thread_id,
            turn_id=str(uuid.uuid4()), attempt_id=str(uuid.uuid4()),
            contract=c, chunk_size=64,
        )
        assert res.completed and res.chunk_count >= 2
        assert res.ttft_ms >= 0
        assert res.projection_bytes > 0
        assert res.final_text == res.visible_text
        # every delivered realtime chunk matches a persisted chunk in order
        chunk_events = [p for p in pub.published if p["event"] == "dm.chunk"]
        assert len(chunk_events) == res.chunk_count
        persisted = s.execute(
            select(DMStreamChunk).where(DMStreamChunk.stream_id == res.stream_id)
            .order_by(DMStreamChunk.sequence)
        ).scalars().all()
        assert [e["payload"]["text"] for e in chunk_events] == [ch.text for ch in persisted]
        assert "".join(ch.text for ch in persisted) == res.visible_text
        # stable dedupe identity
        assert len({e["payload"]["event_id"] for e in chunk_events}) == res.chunk_count
        m = get_narration_metrics()
        assert m["narrations_completed"] == 1
        assert m["ttft_ms_samples"] and m["total_duration_ms_samples"]
        assert m["projection_bytes_samples"] == [res.projection_bytes]
    finally:
        from app.realtime.service import SupabaseRealtimePublisher
        set_realtime_publisher(SupabaseRealtimePublisher())


def test_disconnect_mid_stream_reconstruct_and_resume(db):
    s, camp_id, thread_id = db
    c = _respond([_narr_beat(
        "Torchlight flickers on wet stone as the party descends the stair. "
        "Water drips somewhere in the dark below."
    )])
    full = render_deterministic_narration(build_narration_projection(c), c)
    turn_id, attempt_id = str(uuid.uuid4()), str(uuid.uuid4())
    partial = stream_narration(
        s, campaign_id=camp_id, thread_id=thread_id, turn_id=turn_id,
        attempt_id=attempt_id, contract=c, chunk_size=48,
        publish_realtime=False, max_chunks_to_persist=1,
    )
    assert not partial.completed and partial.chunk_count == 1
    # client reload reconstructs exactly what became visible
    from app.dm_streams.service import list_chunks, reconstruct_text
    assert reconstruct_text(s, partial.stream_id) == partial.visible_text
    assert len(list_chunks(s, partial.stream_id)) == 1
    # resume persists only the missing suffix — no duplication
    done = resume_narration_stream(s, partial.stream_id, full, chunk_size=48, publish_realtime=False)
    assert done.completed and done.final_text == full
    assert done.visible_text == full
    assert len(list_chunks(s, partial.stream_id)) == len(chunk_narration_text(full, chunk_size=48))
    mat = materialize_final_narration(s, partial.stream_id)
    assert mat["final_text"] == full and mat["status"] == "completed"
    assert len(mat["chunks"]) == done.chunk_count


def test_pre_first_chunk_failure_retryable_leaves_no_stream(db):
    s, camp_id, thread_id = db
    c = _respond([_narr_beat("Rain drums on the canvas.")])
    turn_id, attempt_id = str(uuid.uuid4()), str(uuid.uuid4())

    def _boom(req):
        raise RuntimeError("provider outage")

    with pytest.raises(NarratorGenerationError):
        stream_narration(
            s, campaign_id=camp_id, thread_id=thread_id, turn_id=turn_id,
            attempt_id=attempt_id, contract=c, narrator=_boom, publish_realtime=False,
        )
    assert s.scalar(select(func.count()).select_from(DMStream)) == 0
    # retry succeeds on the same turn/attempt identity
    res = stream_narration(
        s, campaign_id=camp_id, thread_id=thread_id, turn_id=turn_id,
        attempt_id=attempt_id, contract=c, publish_realtime=False,
    )
    assert res.completed and "Rain drums on the canvas." in res.final_text


def test_narrator_cannot_apply_game_state_effects(db):
    s, camp_id, thread_id = db
    c = _respond(
        [_narr_beat("The hall falls silent.")],
        staged_effects=[{
            "id": "eff_1", "effect_type": "record_world_event",
            "arguments": {"event_type": "silence", "summary": "Hall silent", "visibility": "public"},
        }],
    )
    before_rev = s.get(Campaign, camp_id).revision
    res = stream_narration(
        s, campaign_id=camp_id, thread_id=thread_id,
        turn_id=str(uuid.uuid4()), attempt_id=str(uuid.uuid4()),
        contract=c, publish_realtime=False,
    )
    assert res.completed
    s.refresh(s.get(Campaign, camp_id))
    assert s.get(Campaign, camp_id).revision == before_rev
    assert s.scalar(select(func.count()).select_from(CampaignDomainEvent)) == 0
    assert "silence" not in (res.final_text or "").lower()


def test_chunking_deterministic_and_rejoin_exact():
    text = "Alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu."
    assert chunk_narration_text(text, chunk_size=40) == chunk_narration_text(text, chunk_size=40)
    assert "".join(chunk_narration_text(text, chunk_size=40)) == text
    with pytest.raises(ValueError):
        chunk_narration_text(text, chunk_size=4)


def test_hidden_dc_value_in_narration_rejected_even_if_invented():
    c = _await_roll()
    good = render_deterministic_narration(build_narration_projection(c), c)
    assert "14" not in good
    assert validate_narration_fidelity(good, c) == []
    bad = good + " You need a 14 to succeed."
    with pytest.raises(NarrationFidelityError):
        check_narration_fidelity_or_raise(bad, c)


# ── provider contract binding (review finding 2) ──────────────────────────

def test_provider_receives_contract_bound_prompt(db):
    s, camp_id, thread_id = db
    c = _respond([_narr_beat("Rain drums on the canvas.")])
    seen: dict = {}

    def _provider(req: NarratorRequest) -> str:
        seen["prompt"] = req.prompt
        seen["projection"] = req.projection
        assert isinstance(req, NarratorRequest)
        return render_deterministic_narration(req.projection, c)

    res = stream_narration(
        s, campaign_id=camp_id, thread_id=thread_id,
        turn_id=str(uuid.uuid4()), attempt_id=str(uuid.uuid4()),
        contract=c, narrator=_provider, publish_realtime=False,
        provider="test-llm-v1",
    )
    assert res.completed and res.provider == "test-llm-v1"
    # The provider cannot get model-backed narration without the contract:
    # the service built the prompt with NARRATOR_CONTRACT prepended.
    assert NARRATOR_CONTRACT.splitlines()[0] in seen["prompt"]
    assert "re-adjudicate" in seen["prompt"]
    assert "Rain drums on the canvas." in seen["prompt"]
    assert seen["projection"]["beats"][0]["claims"][0]["text"] == "Rain drums on the canvas."


# ── number grounding across all public fields (review finding 3) ─────────

def _await_roll_with_numbers():
    return normalize_contract({
        "contract_version": CONTRACT_VERSION, "mode": "await_roll", "reason": "uncertain footing",
        "beats": [_narr_beat(
            "Loose stones cover the ledge ahead.",
            kind="roll_instruction", origin="dm_adjudication",
        )],
        "roll_request": {
            "request_id": "roll_1", "roll_kind": "check",
            "ability_or_skill": "Acrobatics", "label": "Acrobatics check 2",
            "advantage_state": "normal",
            "reason_public": "The 3 torches on the ledge gutter in the wind.",
            "dc_private": 14,
        },
        "open_player_choice": "What do you do with the 5 torches?",
    })


def test_numbers_in_roll_and_choice_public_fields_are_grounded():
    c = _await_roll_with_numbers()
    text = render_deterministic_narration(build_narration_projection(c), c)
    assert "3 torches" in text and "Acrobatics check 2" in text and "5 torches" in text
    assert validate_narration_fidelity(text, c) == []
    # The hidden DC is still ungrounded — never part of the public projection.
    with pytest.raises(NarrationFidelityError) as ei:
        check_narration_fidelity_or_raise(text + " You need a 14 to succeed.", c)
    assert any(v["code"] == "unsupported_number" for v in ei.value.violations)


def test_numbers_in_clarify_question_grounded():
    c = normalize_contract({
        "contract_version": CONTRACT_VERSION, "mode": "clarify", "reason": "ambiguous target",
        "beats": [_narr_beat("The corridor forks into darkness.")],
        "clarify_question": "Do you take the 2nd left or the 3rd right?",
    })
    text = render_deterministic_narration(build_narration_projection(c), c)
    assert "2nd left" in text and "3rd right" in text
    assert validate_narration_fidelity(text, c) == []


def test_numbers_in_table_chat_intent_grounded():
    c = normalize_contract({
        "contract_version": CONTRACT_VERSION, "mode": "table_chat", "reason": "logistics",
        "beats": [],
        "table_chat_intent": "Discuss the 4 missing supply packs before continuing.",
    })
    text = render_deterministic_narration(build_narration_projection(c), c)
    assert "4 missing supply packs" in text
    assert validate_narration_fidelity(text, c) == []


def test_numbers_in_safe_prelude_grounded():
    c = normalize_contract({
        "contract_version": CONTRACT_VERSION, "mode": "need_evidence", "reason": "memory check",
        "beats": [],
        "safe_prelude": "Checking the chronicle for the 6 sealed letters.",
        "evidence_requests": [{
            "id": "ev_1", "tool": "search_campaign_memory",
            "query": "sealed letters",
        }],
    })
    text = render_deterministic_narration(build_narration_projection(c), c)
    assert "6 sealed letters" in text
    assert validate_narration_fidelity(text, c) == []


# ── post-validation → narration → commit wiring (review finding 1) ───────

def test_execute_validated_turn_runs_narration_to_commit(db):
    from app.runtime.submissions import accept_submission
    from app.dm.turns import coordinate_turn

    s, camp_id, thread_id = db
    tid_str = str(thread_id)
    user_id = uuid.uuid4()
    accept_submission(
        s, campaign_id=camp_id, user_id=user_id,
        raw_content="I listen at the door.",
        segments=[{"type": "ic", "text": "I listen at the door."}],
        thread_id=tid_str,
    )
    s.commit()
    turn, attempt = coordinate_turn(s, camp_id, tid_str)
    assert turn.status == "pending"

    c = _respond(
        [_narr_beat("Silence presses against the oak door.")],
        staged_effects=[{
            "id": "eff_1", "effect_type": "record_world_event",
            "arguments": {"event_type": "listened", "summary": "Listened at door", "visibility": "public"},
        }],
        open_player_choice="What do you do?",
    )
    out = execute_validated_turn(
        s, turn_id=turn.id, attempt_id=attempt.id, contract=c,
        publish_realtime=False,
    )
    # Narration streamed durably to completion before the final commit.
    assert out.narration.completed and out.narration.chunk_count >= 1
    assert out.narration.final_text and "Silence presses" in out.narration.final_text
    # Stream-start boundary crossed, then atomic final commit.
    assert out.turn.status == "succeeded"
    assert out.attempt.status == "succeeded"
    assert str(out.attempt.stream_id) == str(out.narration.stream_id)
    assert out.event is not None
    s.refresh(s.get(Campaign, camp_id))
    assert s.get(Campaign, camp_id).revision == 1
    mat = materialize_final_narration(s, out.narration.stream_id)
    assert mat["final_text"] == out.narration.final_text
    assert mat["status"] == "completed"

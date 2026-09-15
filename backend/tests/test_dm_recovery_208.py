"""Issue #208 — provider/model execution policy, recovery, explicit Retry.

Fault-injection coverage on the #354 spine (no parallel stack):
timeout / 429 / 5xx / malformed classification, same-model failover,
unapproved-substitution block, narration-only survival + independent retry,
partial resume/continuation, exhaustion generic failure, explicit Retry
freshness, and non-billable recovery accounting.
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
from models.dm import DmTurn, DmTurnAttempt  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.threads import CampaignThread  # noqa: E402

from app.dm.contract import CONTRACT_VERSION, normalize_contract  # noqa: E402
from app.dm.execution import execute_dm_attempt  # noqa: E402
from app.providers import policy as role_policy  # noqa: E402
from app.providers.contracts import ProviderError  # noqa: E402


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'recovery208.sqlite'}",
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
        s.add(CampaignThread(id=thread_id, campaign_id=camp_id,
                             thread_type="campaign", created_by=owner))
        s.commit()
        yield s, camp_id, thread_id, factory


def _submit(s, camp_id, thread_id, text="I step into the torchlit hall."):
    from app.runtime.submissions import accept_submission
    from app.dm.turns import coordinate_turn

    accept_submission(s, campaign_id=camp_id, user_id=s.get(Campaign, camp_id).owner_id,
                      raw_content=text, segments=[{"type": "ic", "text": text}],
                      thread_id=str(thread_id))
    s.commit()
    coord = coordinate_turn(s, camp_id, str(thread_id), commit=False)
    s.commit()
    assert coord is not None
    return coord


def _contract(text="Torchlight gutters as something stirs."):
    return normalize_contract({
        "contract_version": CONTRACT_VERSION, "mode": "respond",
        "reason": "story continuation",
        "beats": [{"id": "beat_1", "type": "narration", "claims": [{
            "text": text, "claim_kind": "observation",
            "origin": "dm_adjudication", "visibility": "public"}]}],
        "open_player_choice": "What do you do?",
    })


# ── Classification ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("exc,expected", [
    (ProviderError("t", kind="timeout", retryable=True), "retriable"),
    (ProviderError("t", kind="http", status_code=429, retryable=True), "retriable"),
    (ProviderError("t", kind="http", status_code=503, retryable=True), "retriable"),
    (ProviderError("t", kind="malformed", retryable=False), "retriable"),
    (ProviderError("t", kind="unsupported_feature", retryable=False), "terminal"),
    (TimeoutError("provider timeout unavailable"), "retriable"),
    (ValueError("contract poison"), "terminal"),
])
def test_failure_classification_retryable_vs_terminal(exc, expected):
    cls, reason = role_policy.classify_execution_failure(exc)
    assert cls == expected
    assert reason


def test_unapproved_model_substitution_impossible():
    assert role_policy.is_model_approved(
        "forward_dm", *role_policy.get_role_policy("forward_dm").primary_provider
        and (role_policy.get_role_policy("forward_dm").primary_provider,
             role_policy.get_role_policy("forward_dm").primary_model)
    )
    assert not role_policy.is_model_approved("forward_dm", "evil", "unapproved-model-1")
    # Injected unapproved seam is blocked, never executed.
    from app.dm import adjudication as adj

    class _Bad:
        name = "evil"

    with pytest.raises(RuntimeError, match="Unapproved model substitution"):
        adj.adjudicate_with_failover(
            object(), adapter=_Bad(), model="unapproved-model-1")


# ── Same-model failover + non-billable recovery runs ──────────────────────────

class _FakeAdapter:
    def __init__(self, name):
        self.name = name

    def require_config(self, model=None):
        return None


def _ok_response(text):
    from app.providers.contracts import NormalizedChatResponse

    return NormalizedChatResponse(
        provider="fake", model="fake", content=text, tool_calls=[],
        finish_reason="stop", usage={}, reasoning=None,
        reasoning_details=None, raw={},
    )


def test_same_model_failover_recovers_and_records_non_billable(db, monkeypatch):
    import json

    import app.dm.adjudication as adj
    from models.reliability import AIRun

    s, camp_id, thread_id, _ = db
    turn, attempt = _submit(s, camp_id, thread_id)
    packet = _load_packet(s, attempt.id)

    contract_json = json.dumps(_contract().model_dump(mode="json"))
    calls = []

    def _fake_execute(adapter, request):
        calls.append(adapter.name)
        if len(calls) == 1:
            raise ProviderError("timeout", kind="timeout", retryable=True)
        return _ok_response(contract_json)

    monkeypatch.setattr("app.dm.adjudication.resolve_dm_provider",
                        lambda: (_FakeAdapter("primary"), "model-x", "primary"))
    monkeypatch.setattr(role_policy, "execution_path",
                        lambda role: [("primary", "model-x"), ("failover", "model-x")])
    monkeypatch.setattr(role_policy, "is_model_approved", lambda r, p, m: True)
    from app.providers import registry as reg

    monkeypatch.setattr(reg.provider_registry, "get", lambda name: _FakeAdapter(name))
    monkeypatch.setattr("app.providers.execute_chat", _fake_execute)
    # adjudication imports execute_chat from app.providers at call time;
    # patch the re-exported reference too.
    import app.providers as providers_pkg

    monkeypatch.setattr(providers_pkg, "execute_chat", _fake_execute)

    contract, info = adj.adjudicate_with_failover(packet, db=s, role="forward_dm")
    assert contract.mode == "respond"
    assert info["attempt_index"] == 1
    assert calls == ["primary", "failover"]
    runs = s.execute(select(AIRun)).scalars().all()
    primaries = [r for r in runs if r.classification == "primary"]
    recoveries = [r for r in runs if r.classification == "recovery"]
    assert primaries and primaries[0].billable is True
    assert recoveries and all(r.billable is False for r in recoveries)


def _load_packet(s, attempt_id):
    from app.dm.execution import _assemble_production_context

    return _assemble_production_context(s, attempt_id)


# ── Narration-only survival + independent retry ───────────────────────────────

def test_narration_only_failure_preserves_contract_and_retries_independently(db):
    from app.dm.recovery import retry_narration_only

    s, camp_id, thread_id, _ = db
    turn, attempt = _submit(s, camp_id, thread_id)

    def _good(packet, feedback=None):
        return _contract()

    def _boom_narrator(request):
        raise RuntimeError("narrator exploded pre-chunk")

    with pytest.raises(RuntimeError, match="narrator exploded"):
        execute_dm_attempt(s, attempt.id, adjudicate=_good, narrator=_boom_narrator)
    failed = s.get(DmTurnAttempt, attempt.id)
    # Pre-chunk narration failure stays retryable with the valid structured
    # packet preserved — narration can retry independently.
    assert failed.contract_snapshot is not None
    assert failed.status in ("prepared", "failed", "failed_visible")

    # Force a visible failure carrying the snapshot to exercise the
    # narration-only retry path end to end.
    from app.dm.turns import mark_attempt_failed

    if failed.status != "failed_visible":
        mark_attempt_failed(s, attempt.id, error="narrator exploded",
                            error_class="retriable", visible=True)
    failed = s.get(DmTurnAttempt, attempt.id)
    assert failed.contract_snapshot is not None
    _, fresh = retry_narration_only(s, camp_id, turn.id, attempt.id)
    s.commit()
    assert fresh.contract_snapshot == failed.contract_snapshot
    # Independent retry narrates without re-adjudication.
    adjudications = []

    def _counting(packet, feedback=None):
        adjudications.append(1)
        raise AssertionError("adjudication must not run on narration-only retry")

    result = execute_dm_attempt(s, fresh.id, adjudicate=_counting,
                                narrator="deterministic")
    assert result.attempt.status == "succeeded"
    assert adjudications == []


# ── Partial streams: resume vs semantic continuation ──────────────────────────

def test_partial_stream_resume_and_semantic_continuation(db):
    from app.dm.narration import (continue_partial_stream,
                                  resume_narration_stream)
    from app.dm_streams.service import reconstruct_text

    s, camp_id, thread_id, _ = db
    turn, attempt = _submit(s, camp_id, thread_id)
    long_text = ("Torchlight gutters as something stirs beyond the arch. " * 12
                 + "What do you do?")
    contract = _contract(long_text)
    from app.dm.narration import stream_narration

    partial = stream_narration(
        s, campaign_id=camp_id, thread_id=thread_id, turn_id=str(turn.id),
        attempt_id=str(attempt.id), contract=contract, narrator=None,
        chunk_size=120, provider="deterministic-template-v1",
        publish_realtime=False, max_chunks_to_persist=1,
    )
    assert partial.completed is False
    visible = reconstruct_text(s, partial.stream_id)
    assert visible

    # Same-text direct resume keeps every visible byte.
    full = long_text
    resumed = resume_narration_stream(s, partial.stream_id, full,
                                      publish_realtime=False)
    assert resumed.completed is True
    assert resumed.visible_text.startswith(visible)

    # Semantic continuation of a fresh partial must preserve the prefix.
    turn2, attempt2 = _submit(s, camp_id, thread_id, text="I listen closely.")
    partial2 = stream_narration(
        s, campaign_id=camp_id, thread_id=thread_id, turn_id=str(turn2.id),
        attempt_id=str(attempt2.id), contract=contract, narrator=None,
        chunk_size=120, provider="deterministic-template-v1",
        publish_realtime=False, max_chunks_to_persist=1,
    )
    visible2 = reconstruct_text(s, partial2.stream_id)
    continued = continue_partial_stream(
        s, partial2.stream_id, full, contract, publish_realtime=False)
    assert continued.visible_text.startswith(visible2)
    with pytest.raises(ValueError, match="contradicts the persisted visible prefix"):
        continue_partial_stream(s, partial2.stream_id,
                                "A completely different story.", contract,
                                publish_realtime=False)


# ── Partial recovery endpoint logic (recover_partial_stream) ────────────────

LONG_TEXT = ("Torchlight gutters as something stirs beyond the arch. " * 12
             + "What do you do?")


def _failed_partial(s, camp_id, thread_id, text=LONG_TEXT, effect=None):
    """Build a failed_visible turn/attempt with a failed partial stream."""
    from app.dm.narration import stream_narration
    from app.dm.turns import mark_attempt_failed, stage_validated_attempt
    from app.dm_streams.service import fail_stream

    turn, attempt = _submit(s, camp_id, thread_id, text="I step forward.")
    contract = _contract(text)
    if effect is not None:
        d = contract.model_dump(mode="json")
        d["staged_effects"] = [effect]
        contract = normalize_contract(d)
    stage_validated_attempt(s, attempt.id, contract)
    partial = stream_narration(
        s, campaign_id=camp_id, thread_id=thread_id, turn_id=str(turn.id),
        attempt_id=str(attempt.id), contract=contract, narrator=None,
        chunk_size=120, provider="deterministic-template-v1",
        publish_realtime=False, max_chunks_to_persist=1,
    )
    assert partial.completed is False
    fail_stream(s, partial.stream_id, reason="test_injected_failure")
    s.commit()
    attempt.stream_id = partial.stream_id
    s.add(attempt)
    s.commit()
    mark_attempt_failed(s, attempt.id, error="injected stream failure",
                        error_class="retriable", visible=True)
    return s.get(DmTurn, turn.id), s.get(DmTurnAttempt, attempt.id), partial.stream_id


def test_recover_partial_stream_completes_turn_and_promotes_effects(db):
    from app.dm.recovery import recover_partial_stream

    s, camp_id, thread_id, _ = db
    owner = s.get(Campaign, camp_id).owner_id
    turn, attempt, stream_id = _failed_partial(
        s, camp_id, thread_id,
        effect={"id": "eff-1", "effect_type": "record_world_event",
                "arguments": {"event_type": "test_event",
                              "summary": "recovery test event",
                              "visibility": "dm_private"}},
    )
    assert attempt.staged_effects, "setup must stage an effect to prove promotion"
    final_turn, final_attempt, event = recover_partial_stream(
        s, camp_id, turn.id, stream_id, LONG_TEXT, actor_id=owner)
    assert final_turn.status == "succeeded"
    assert final_attempt.status == "succeeded"
    assert event is not None
    from app.dm_streams.service import get_stream

    assert get_stream(s, stream_id).status == "completed"
    # Consumed input resolved so it is never re-adjudicated.
    from models.threads import PlayerSubmission

    for sid in (final_attempt.submission_ids or []):
        row = s.get(PlayerSubmission, uuid.UUID(str(sid)))
        assert row.resolution_status == "resolved"
    # Same-key retry after convergence returns the converged result.
    again_turn, again_attempt, again_event = recover_partial_stream(
        s, camp_id, turn.id, stream_id, LONG_TEXT, actor_id=owner)
    assert again_turn.status == "succeeded"
    assert again_attempt.status == "succeeded"
    assert getattr(again_event, "id", None) == getattr(event, "id", None)
    assert str(getattr(again_event, "id", None)) != str(stream_id)


# ── Atomicity: crash at any boundary leaves nothing durable ─────────────────


def _crash_after_chunks(s, stream_id, full_text, *, chunks_to_append="all"):
    """Simulate a crash mid-recovery: persist chunks + optionally complete,
    without finalizing the turn."""
    from app.dm.narration import chunk_narration_text
    from app.dm_streams.service import (
        append_chunk,
        complete_stream,
        list_chunks,
        reopen_failed_stream,
    )

    reopen_failed_stream(s, stream_id, reason="test_crash_sim")
    existing = list_chunks(s, stream_id)
    plan = chunk_narration_text(full_text, chunk_size=120)
    missing = list(range(len(existing), len(plan)))
    if chunks_to_append != "all":
        missing = missing[:chunks_to_append]
    for seq in missing:
        append_chunk(s, stream_id, seq, plan[seq])
    s.commit()
    if chunks_to_append == "all":
        complete_stream(s, stream_id)
        s.commit()


def test_recover_converges_after_crash_past_completion(db):
    """Crash after stream completion but before turn commit converges."""
    from app.dm.recovery import recover_partial_stream

    s, camp_id, thread_id, _ = db
    owner = s.get(Campaign, camp_id).owner_id
    turn, attempt, stream_id = _failed_partial(s, camp_id, thread_id)
    _crash_after_chunks(s, stream_id, LONG_TEXT, chunks_to_append="all")
    assert s.get(DmTurn, turn.id).status == "failed_visible"
    final_turn, final_attempt, event = recover_partial_stream(
        s, camp_id, turn.id, stream_id, LONG_TEXT, actor_id=owner)
    assert final_turn.status == "succeeded"
    assert final_attempt.status == "succeeded"
    assert event is not None


def test_recover_converges_after_crash_mid_suffix(db):
    """Crash after some recovered chunks converges without duplication."""
    from app.dm.recovery import recover_partial_stream
    from app.dm_streams.service import list_chunks

    s, camp_id, thread_id, _ = db
    owner = s.get(Campaign, camp_id).owner_id
    turn, attempt, stream_id = _failed_partial(s, camp_id, thread_id)
    before = len(list_chunks(s, stream_id))
    _crash_after_chunks(s, stream_id, LONG_TEXT, chunks_to_append=1)
    assert len(list_chunks(s, stream_id)) == before + 1
    final_turn, final_attempt, _ = recover_partial_stream(
        s, camp_id, turn.id, stream_id, LONG_TEXT, actor_id=owner)
    assert final_turn.status == "succeeded"
    assert final_attempt.status == "succeeded"
    from app.dm_streams.service import reconstruct_text

    assert reconstruct_text(s, stream_id) == LONG_TEXT


def test_explicit_retry_executes_as_non_billable_recovery(db, monkeypatch):
    """A fresh explicit-Retry attempt records recovery/non-billable AIRuns."""
    import json

    from app.dm.recovery import retry_failed_adjudication
    from models.reliability import AIRun

    s, camp_id, thread_id, _ = db
    turn, attempt = _submit(s, camp_id, thread_id)

    def _boom(packet, feedback=None):
        raise ValueError("terminal poison")

    with pytest.raises(ValueError):
        execute_dm_attempt(s, attempt.id, adjudicate=_boom,
                           narrator="deterministic")
    assert s.get(DmTurnAttempt, attempt.id).status in ("failed", "failed_visible")
    _, fresh = retry_failed_adjudication(s, camp_id, turn.id, attempt.id)
    s.commit()

    contract_json = json.dumps(_contract().model_dump(mode="json"))

    def _fake_execute(adapter, request):
        return _ok_response(contract_json)

    monkeypatch.setattr("app.dm.adjudication.resolve_dm_provider",
                        lambda: (_FakeAdapter("primary"), "model-x", "primary"))
    monkeypatch.setattr(role_policy, "execution_path",
                        lambda role: [("primary", "model-x")])
    monkeypatch.setattr(role_policy, "is_model_approved", lambda r, p, m: True)
    import app.providers as providers_pkg

    monkeypatch.setattr(providers_pkg, "execute_chat", _fake_execute)

    result = execute_dm_attempt(s, fresh.id, narrator="deterministic")
    assert result.attempt.status == "succeeded"
    runs = s.execute(select(AIRun)).scalars().all()
    assert runs, "retry execution must record AI runs"
    assert all(r.classification == "recovery" for r in runs)
    assert all(r.billable is False for r in runs)
    assert all(r.status == "succeeded" for r in runs)


def test_recover_partial_stream_rejects_cross_campaign_stream(db):
    from app.dm.recovery import recover_partial_stream

    s, camp_id, thread_id, _ = db
    owner = s.get(Campaign, camp_id).owner_id
    other_camp = uuid.uuid4()
    other_thread = uuid.uuid4()
    s.add(Profile(id=uuid.uuid4(), email="other@example.com"))
    s.add(Campaign(id=other_camp, owner_id=owner, name="Other", revision=0))
    s.add(CampaignThread(id=other_thread, campaign_id=other_camp,
                         thread_type="campaign", created_by=owner))
    s.commit()
    turn, _, _ = _failed_partial(s, camp_id, thread_id)
    _, _, foreign_stream = _failed_partial(s, other_camp, other_thread)
    with pytest.raises(LookupError):
        recover_partial_stream(s, camp_id, turn.id, foreign_stream, LONG_TEXT,
                               actor_id=owner)
    # Nothing mutated by the rejected recovery.
    assert s.get(DmTurn, turn.id).status == "failed_visible"


def test_recover_partial_stream_rejects_stale_attempt_stream(db):
    from app.dm.recovery import recover_partial_stream, retry_failed_adjudication

    s, camp_id, thread_id, _ = db
    owner = s.get(Campaign, camp_id).owner_id
    turn, _, _ = _failed_partial(s, camp_id, thread_id)
    # Supersede the failed attempt with an explicit Retry: the old stream is
    # now scoped to a non-current attempt and can never be continued here.
    _, fresh = retry_failed_adjudication(
        s, camp_id, turn.id, s.get(DmTurn, turn.id).current_attempt_id)
    s.commit()
    old_stream = s.get(DmTurnAttempt, fresh.parent_attempt_id).stream_id
    assert old_stream is not None
    with pytest.raises(LookupError):
        recover_partial_stream(s, camp_id, turn.id, old_stream, LONG_TEXT,
                               actor_id=owner)
    assert s.get(DmTurn, turn.id).status == "pending"


def test_recover_partial_stream_rejects_divergent_and_unfaithful_text(db):
    from app.dm.recovery import recover_partial_stream

    s, camp_id, thread_id, _ = db
    owner = s.get(Campaign, camp_id).owner_id
    turn, attempt, stream_id = _failed_partial(s, camp_id, thread_id)
    with pytest.raises(ValueError):
        recover_partial_stream(s, camp_id, turn.id, stream_id,
                               "A completely different story.", actor_id=owner)
    # Prefix-preserving but unfaithful: invented number + consequence.
    from app.dm_streams.service import reconstruct_text

    visible = reconstruct_text(s, stream_id)
    with pytest.raises(ValueError):
        recover_partial_stream(s, camp_id, turn.id, stream_id,
                               visible + " You take 10 damage.",
                               actor_id=owner)
    assert s.get(DmTurnAttempt, attempt.id).status == "failed_visible"


# ── Narration failover through the role policy ──────────────────────────────

def test_narration_failover_uses_next_candidate_pre_token(db, monkeypatch):
    import app.providers as providers_pkg
    from app.dm.adjudication import build_provider_narrator
    from app.dm.narration import NarratorRequest
    from app.providers import registry as reg
    from app.providers.contracts import NormalizedStreamEvent, ProviderError
    from models.reliability import AIRun

    s, _, _, _ = db
    calls = []

    def _fake_stream(adapter, request):
        calls.append(adapter.name)
        if len(calls) == 1:
            raise ProviderError("rate limited", kind="http", status_code=429,
                                retryable=True)
        yield NormalizedStreamEvent(kind="token", text="hello ")
        yield NormalizedStreamEvent(kind="token", text="world")

    monkeypatch.setattr(providers_pkg, "stream_chat", _fake_stream)
    monkeypatch.setattr(role_policy, "execution_path",
                        lambda role: [("p1", "m"), ("p2", "m")])
    monkeypatch.setattr(role_policy, "is_model_approved", lambda r, p, m: True)
    monkeypatch.setattr(reg.provider_registry, "get", lambda name: _FakeAdapter(name))
    monkeypatch.setattr("app.providers.areas.resolve_area",
                        lambda area: (_FakeAdapter("p1"), "m", "p1"))

    narrate = build_provider_narrator(db=s)
    text = "".join(narrate(NarratorRequest(prompt="p", projection={})))
    assert text == "hello world"
    assert calls == ["p1", "p2"]
    runs = {r.provider: r for r in s.execute(select(AIRun)).scalars().all()}
    assert runs["p1"].classification == "primary"
    assert runs["p1"].billable is True
    assert runs["p1"].status == "failed"
    assert runs["p2"].classification == "recovery"
    assert runs["p2"].billable is False
    assert runs["p2"].status == "succeeded"


def test_narration_unapproved_substitution_blocked():
    from app.dm.adjudication import build_provider_narrator

    with pytest.raises(RuntimeError, match="Unapproved model substitution"):
        build_provider_narrator(adapter=_FakeAdapter("evil"), model="bad-model")


def _provider_narrator_mocks(monkeypatch, *, path, fail_first=None):
    """Mock the narration policy path + streaming transport.

    fail_first: exception instance raised by the first provider after
    yielding ``fail_first_prefix`` (None = raise before any yield).
    """
    import app.providers as providers_pkg
    from app.providers import registry as reg
    from app.providers.contracts import NormalizedStreamEvent

    calls = []
    state = {"prefix": ""}

    def _fake_stream(adapter, request):
        calls.append(adapter.name)
        if fail_first is not None and adapter.name == path[0][0]:
            if state["prefix"]:
                yield NormalizedStreamEvent(kind="token", text=state["prefix"])
            raise fail_first
        for piece in state["pieces"]:
            yield NormalizedStreamEvent(kind="token", text=piece)

    monkeypatch.setattr(providers_pkg, "stream_chat", _fake_stream)
    monkeypatch.setattr(role_policy, "execution_path", lambda role: list(path))
    monkeypatch.setattr(role_policy, "is_model_approved", lambda r, p, m: True)
    monkeypatch.setattr(reg.provider_registry, "get", lambda name: _FakeAdapter(name))
    monkeypatch.setattr("app.providers.areas.resolve_area",
                        lambda area: (_FakeAdapter(path[0][0]), path[0][1], path[0][0]))
    return calls, state


def test_narration_failover_drops_unpersisted_prefix(db, monkeypatch):
    """Provider 1 yields a short prefix then 429s with zero durable chunks:
    failover drops the prefix; provider 2's text is the only visible output."""
    from app.dm.adjudication import build_provider_narrator
    from app.dm.narration import (
        NarratorRequest,
        render_deterministic_narration,
        stream_narration,
    )
    from app.dm_streams.service import reconstruct_text
    from app.providers.contracts import ProviderError

    s, camp_id, thread_id, _ = db
    turn, attempt = _submit(s, camp_id, thread_id)
    contract = _contract(LONG_TEXT)
    projection = contract.model_dump(mode="json")
    expected = render_deterministic_narration(
        __import__("app.dm.narration", fromlist=["build_narration_projection"])
        .build_narration_projection(contract), contract)
    half = len(expected) // 2
    calls, state = _provider_narrator_mocks(
        monkeypatch, path=[("p1", "m"), ("p2", "m")],
        fail_first=ProviderError("rate limited", kind="http", status_code=429,
                                 retryable=True),
    )
    state["prefix"] = "Hi. "
    state["pieces"] = [expected[:half], expected[half:]]

    narrate = build_provider_narrator(db=s)
    result = stream_narration(
        s, campaign_id=camp_id, thread_id=thread_id, turn_id=str(turn.id),
        attempt_id=str(attempt.id), contract=contract, narrator=narrate,
        chunk_size=120, provider="p1/p2", publish_realtime=False,
    )
    assert result.completed is True
    visible = reconstruct_text(s, result.stream_id)
    assert visible == expected
    assert "Hi." not in visible
    assert calls[0] == "p1" and "p2" in calls


def test_narration_skipped_primary_stays_recovery(db, monkeypatch):
    """An unavailable primary cannot promote the alternate to billable primary."""
    from app.dm.adjudication import build_provider_narrator
    from app.dm.narration import NarratorRequest
    from models.reliability import AIRun

    s, _, _, _ = db
    calls, state = _provider_narrator_mocks(
        monkeypatch, path=[("p1", "m"), ("p2", "m")])
    state["pieces"] = ["hello world"]

    def _no_primary(area):
        raise RuntimeError("META_API_KEY is not set")

    monkeypatch.setattr("app.providers.areas.resolve_area", _no_primary)
    narrate = build_provider_narrator(db=s)
    text = "".join(narrate(NarratorRequest(prompt="p", projection={})))
    assert text == "hello world"
    assert calls == ["p2"]
    runs = s.execute(select(AIRun)).scalars().all()
    assert len(runs) == 1
    assert runs[0].provider == "p2"
    assert runs[0].classification == "recovery"
    assert runs[0].billable is False
    assert runs[0].status == "succeeded"


def test_recover_partial_stream_maps_stale_revision_to_retryable(db):
    from app.dm.recovery import recover_partial_stream

    s, camp_id, thread_id, _ = db
    turn, attempt, stream_id = _failed_partial(s, camp_id, thread_id)
    camp = s.get(Campaign, camp_id)
    camp.revision = int(camp.revision or 0) + 1
    s.add(camp)
    s.commit()
    with pytest.raises(ValueError):
        recover_partial_stream(s, camp_id, turn.id, stream_id, LONG_TEXT,
                               actor_id=camp.owner_id)
    assert s.get(DmTurn, turn.id).status == "failed_visible"

def test_recovery_digest_distinguishes_text_past_char_4000(db):
    """Untruncated recovery payloads differing only after char 4,000 conflict."""
    from app.idempotency import (
        IdempotencyConflictError,
        execute_idempotent_command,
    )

    s, camp_id, _, _ = db
    owner = s.get(Campaign, camp_id).owner_id
    sid = str(uuid.uuid4())
    scope = str(uuid.uuid4())
    first = {"stream_id": sid, "continued_text": "a" * 4000 + "A"}
    second = {"stream_id": sid, "continued_text": "a" * 4000 + "B"}
    execute_idempotent_command(
        s, actor_id=owner, idempotency_key="k-4000", command_type="t",
        scope_type="dm_turn", scope_id=scope,
        payload=first, execute=lambda: {"ok": True},
    )
    with pytest.raises(IdempotencyConflictError):
        execute_idempotent_command(
            s, actor_id=owner, idempotency_key="k-4000", command_type="t",
            scope_type="dm_turn", scope_id=scope,
            payload=second, execute=lambda: {"ok": True},
        )

@pytest.mark.parametrize("fault", ["after_completion", "after_transition", "after_staging"])
def test_recovery_crash_boundary_leaves_nothing_durable(db, monkeypatch, fault):
    """With flush-only recovery, a crash after completion/transition/staging
    persists nothing: retry converges to exactly one commit."""
    from app.dm.recovery import recover_partial_stream
    from app.dm_streams.service import list_chunks, reconstruct_text

    s, camp_id, thread_id, _ = db
    owner = s.get(Campaign, camp_id).owner_id
    turn, attempt, stream_id = _failed_partial(s, camp_id, thread_id)
    chunks_before = len(list_chunks(s, stream_id))

    if fault == "after_completion":
        import app.dm.narration as narration_mod

        real_continue = narration_mod.continue_partial_stream

        def _fail(*a, **k):
            real_continue(*a, **k)
            raise RuntimeError("crash after stream completion")

        monkeypatch.setattr(narration_mod, "continue_partial_stream", _fail)
    elif fault == "after_transition":
        import app.dm.turns as turns_mod

        real_mark = turns_mod.mark_recovered_streaming

        def _fail(*a, **k):
            real_mark(*a, **k)
            raise RuntimeError("crash after recovered-streaming transition")

        monkeypatch.setattr(turns_mod, "mark_recovered_streaming", _fail)
    else:
        import app.dm.turns as turns_mod

        def _fail(*a, **k):
            raise RuntimeError("crash during effect/event staging")

        monkeypatch.setattr(turns_mod, "commit_turn_with_effects", _fail)

    with pytest.raises(RuntimeError, match="crash"):
        recover_partial_stream(s, camp_id, turn.id, stream_id, LONG_TEXT,
                               actor_id=owner, commit=False)
    s.rollback()
    # Nothing durable: stream still failed with original prefix, turn failed.
    from app.dm_streams.service import get_stream

    assert get_stream(s, stream_id).status == "failed"
    assert len(list_chunks(s, stream_id)) == chunks_before
    assert reconstruct_text(s, stream_id) != LONG_TEXT
    assert s.get(DmTurn, turn.id).status == "failed_visible"
    assert s.get(DmTurnAttempt, attempt.id).status == "failed_visible"
    # Retry converges to exactly one successful commit.
    monkeypatch.undo()
    final_turn, final_attempt, event = recover_partial_stream(
        s, camp_id, turn.id, stream_id, LONG_TEXT, actor_id=owner)
    assert final_turn.status == "succeeded"
    assert final_attempt.status == "succeeded"
    assert event is not None


def test_narration_only_retry_rejects_advanced_revision(db):
    """A snapshot computed against stale state must not narrate/commit."""
    from app.dm.recovery import retry_narration_only

    s, camp_id, thread_id, _ = db
    turn, attempt = _submit(s, camp_id, thread_id)

    def _good(packet, feedback=None):
        return _contract()

    def _boom_narrator(request):
        raise RuntimeError("narrator exploded pre-chunk")

    with pytest.raises(RuntimeError, match="narrator exploded"):
        execute_dm_attempt(s, attempt.id, adjudicate=_good, narrator=_boom_narrator)
    from app.dm.turns import mark_attempt_failed

    mark_attempt_failed(s, attempt.id, error="x", error_class="retriable", visible=True)
    camp = s.get(Campaign, camp_id)
    camp.revision = int(camp.revision or 0) + 1
    s.add(camp)
    s.commit()
    with pytest.raises(ValueError, match="state advanced"):
        retry_narration_only(s, camp_id, turn.id, attempt.id)


def test_automatic_retry_executes_as_non_billable_recovery(db, monkeypatch):
    """A retryable pre-visible failure requeues the same attempt; the next
    sweep's run is recovery/non-billable, not fresh primary work."""
    import json

    from app.providers.contracts import ProviderError
    from models.reliability import AIRun

    s, camp_id, thread_id, _ = db
    turn, attempt = _submit(s, camp_id, thread_id)
    contract_json = json.dumps(_contract().model_dump(mode="json"))
    calls = []

    def _flaky_execute(adapter, request):
        calls.append(1)
        if len(calls) == 1:
            raise ProviderError("timeout", kind="timeout", retryable=True)
        return _ok_response(contract_json)

    monkeypatch.setattr("app.dm.adjudication.resolve_dm_provider",
                        lambda: (_FakeAdapter("primary"), "model-x", "primary"))
    monkeypatch.setattr(role_policy, "execution_path",
                        lambda role: [("primary", "model-x")])
    monkeypatch.setattr(role_policy, "is_model_approved", lambda r, p, m: True)
    import app.providers as providers_pkg

    monkeypatch.setattr(providers_pkg, "execute_chat", _flaky_execute)

    with pytest.raises(ProviderError):
        execute_dm_attempt(s, attempt.id, narrator="deterministic")
    assert s.get(DmTurnAttempt, attempt.id).status == "prepared"
    assert s.get(DmTurnAttempt, attempt.id).retry_count == 1
    result = execute_dm_attempt(s, attempt.id, narrator="deterministic")
    assert result.attempt.status == "succeeded"
    runs = sorted(s.execute(select(AIRun)).scalars().all(), key=lambda r: r.attempt)
    assert len(runs) == 2
    assert runs[0].classification == "primary" and runs[0].billable is True
    assert runs[1].classification == "recovery" and runs[1].billable is False
    assert all(r.status == "succeeded" or r.status == "failed" for r in runs)


def test_failed_runs_survive_gameplay_rollback(db, monkeypatch):
    """Terminal provider failure rolls gameplay back, but the failed
    primary/failover AIRuns stay durably in the ledger — including when no
    trailing gameplay commit follows (direct adjudication call)."""
    from app.dm.adjudication import adjudicate_with_failover
    from app.providers.contracts import ProviderError
    from models.reliability import AIRun

    s, camp_id, thread_id, factory = db
    turn, attempt = _submit(s, camp_id, thread_id)
    packet = _load_packet(s, attempt.id)

    def _always_terminal(adapter, request):
        raise ProviderError("bad request", kind="http", status_code=400,
                            retryable=False)

    monkeypatch.setattr("app.dm.adjudication.resolve_dm_provider",
                        lambda: (_FakeAdapter("primary"), "model-x", "primary"))
    monkeypatch.setattr(role_policy, "execution_path",
                        lambda role: [("primary", "model-x")])
    monkeypatch.setattr(role_policy, "is_model_approved", lambda r, p, m: True)
    import app.providers as providers_pkg

    monkeypatch.setattr(providers_pkg, "execute_chat", _always_terminal)

    with pytest.raises(ProviderError):
        adjudicate_with_failover(packet, db=s)
    s.rollback()  # no trailing gameplay commit: rows must still be durable
    # A fresh session (post-crash view) sees only committed telemetry.
    s.close()
    with factory() as fresh:
        runs = fresh.execute(select(AIRun)).scalars().all()
    assert runs, "failed provider attempts must remain in the ledger"
    assert all(r.status == "failed" for r in runs)
    assert any(r.classification == "primary" and r.billable is True for r in runs)

    with factory() as s2:
        turn2, attempt2 = _submit(s2, camp_id, thread_id)
        with pytest.raises(ProviderError):
            execute_dm_attempt(s2, attempt2.id, narrator="deterministic")
        assert s2.get(DmTurnAttempt, attempt2.id).status in ("failed", "failed_visible")
    with factory() as fresh2:
        runs2 = fresh2.execute(select(AIRun)).scalars().all()
    assert len(runs2) > len(runs)


@pytest.mark.postgres
def test_failed_runs_durable_on_postgres(monkeypatch):
    """Production behavior: savepoint-flushed telemetry does NOT survive a
    gameplay rollback on Postgres, so accounting must use its own
    transaction. Uses a stub packet — no gameplay state needed."""
    import os

    url = os.getenv("FAULT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires disposable Postgres")
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.dm.adjudication import adjudicate_with_failover
    from app.providers.contracts import ProviderError
    from models.reliability import AIRun

    eng = create_engine(url)
    Fac = sessionmaker(bind=eng, expire_on_commit=False)
    tid = f"trace-{uuid.uuid4().hex}"

    class _StubPacket:
        def model_dump(self, mode="json"):
            return {"stub": True}

    def _always_terminal(adapter, request):
        raise ProviderError("bad request", kind="http", status_code=400,
                            retryable=False)

    monkeypatch.setattr("app.dm.adjudication.resolve_dm_provider",
                        lambda: (_FakeAdapter("primary"), "model-x", "primary"))
    monkeypatch.setattr(role_policy, "execution_path",
                        lambda role: [("primary", "model-x")])
    monkeypatch.setattr(role_policy, "is_model_approved", lambda r, p, m: True)
    import app.providers as providers_pkg

    monkeypatch.setattr(providers_pkg, "execute_chat", _always_terminal)

    with Fac() as s:
        with pytest.raises(ProviderError):
            adjudicate_with_failover(_StubPacket(), db=s, trace_id=tid)
        s.rollback()
    with Fac() as fresh:
        runs = fresh.execute(
            select(AIRun).where(AIRun.trace_id == tid)).scalars().all()
    assert runs, "failed runs must be durable independently of rollback"
    assert all(r.status == "failed" for r in runs)


def test_superseded_attempt_executes_as_primary_billable(db, monkeypatch):
    """Ordinary pre-stream supersession is first-try work: primary/billable."""
    import json

    from models.reliability import AIRun

    s, camp_id, thread_id, _ = db
    turn, old = _submit(s, camp_id, thread_id)
    child = DmTurnAttempt(
        id=uuid.uuid4(), turn_id=turn.id, campaign_id=camp_id,
        thread_id=turn.thread_id, audience=turn.audience,
        attempt_number=old.attempt_number + 1, parent_attempt_id=old.id,
        status="prepared", source_revision=turn.source_revision,
        input_set_revision=turn.input_set_revision,
        submission_ids=list(old.submission_ids or []),
    )
    s.add(child)
    old.status = "superseded"
    old.invalidation_reason = "new_eligible_submission_pre_stream"
    turn.current_attempt_id = child.id
    s.add(old)
    s.add(turn)
    s.commit()

    contract_json = json.dumps(_contract().model_dump(mode="json"))

    def _fake_execute(adapter, request):
        return _ok_response(contract_json)

    monkeypatch.setattr("app.dm.adjudication.resolve_dm_provider",
                        lambda: (_FakeAdapter("primary"), "model-x", "primary"))
    monkeypatch.setattr(role_policy, "execution_path",
                        lambda role: [("primary", "model-x")])
    monkeypatch.setattr(role_policy, "is_model_approved", lambda r, p, m: True)
    import app.providers as providers_pkg

    monkeypatch.setattr(providers_pkg, "execute_chat", _fake_execute)

    result = execute_dm_attempt(s, child.id, narrator="deterministic")
    assert result.attempt.status == "succeeded"
    runs = s.execute(select(AIRun)).scalars().all()
    assert runs
    assert all(r.classification == "primary" for r in runs)
    assert all(r.billable is True for r in runs)


# ── Exhaustion: generic retryable failure, no infra details ───────────────────

def test_exhausted_recovery_shows_generic_retry_without_infra_details(db):
    s, camp_id, thread_id, _ = db
    turn, attempt = _submit(s, camp_id, thread_id)

    def _always_terminal(packet, feedback=None):
        raise ValueError("provider status 500 model exploded infra-secret-123")

    with pytest.raises(ValueError):
        execute_dm_attempt(s, attempt.id, adjudicate=_always_terminal,
                           narrator="deterministic")
    fresh = s.get(DmTurnAttempt, attempt.id)
    assert fresh.status in ("failed", "failed_visible")
    public = (dict(fresh.result or {}).get("public_error") or "")
    assert "retry" in public.lower()
    for leaked in ("500", "infra-secret", "provider", "model"):
        assert leaked not in public


# ── Explicit Retry from partial: fresh, no duplicated effects ─────────────────

def test_explicit_retry_from_partial_starts_fresh_without_duplication(db):
    from app.dm.recovery import retry_failed_adjudication
    from app.dm.turns import mark_attempt_failed

    s, camp_id, thread_id, _ = db
    turn, attempt = _submit(s, camp_id, thread_id)
    attempt.staged_effects = [{"effect_type": "grant_xp", "id": "eff_1"}]
    attempt.contract_snapshot = _contract().model_dump(mode="json")
    s.commit()
    mark_attempt_failed(s, attempt.id, error="stream blew up",
                        error_class="retriable", visible=True)
    old = s.get(DmTurnAttempt, attempt.id)
    old_op = old.commit_operation_id or str(old.id)
    _, fresh = retry_failed_adjudication(s, camp_id, turn.id, attempt.id)
    s.commit()
    assert fresh.parent_attempt_id == old.id
    assert list(fresh.submission_ids or []) == list(old.submission_ids or [])
    assert (fresh.staged_effects or []) == []
    assert fresh.commit_operation_id != old_op
    assert s.get(DmTurnAttempt, old.id).status == "abandoned"
    # Fresh attempt executes cleanly to success.
    result = execute_dm_attempt(s, fresh.id,
                                adjudicate=lambda p, feedback=None: _contract(),
                                narrator="deterministic")
    assert result.attempt.status == "succeeded"

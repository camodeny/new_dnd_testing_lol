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

"""Issue #257 — BYOK credential routing through approved generative/decision runtime."""
from __future__ import annotations

import json
import types
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

import models  # noqa: E402 — register tables on Base.metadata
from database import Base  # noqa: E402
from models.byok import CampaignByokPolicy, ProviderCredential  # noqa: E402
from models.campaigns import Campaign, CampaignMember  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.reliability import AIRun, OperationTrace  # noqa: E402

from app.byok import crypto as byok_crypto  # noqa: E402
from app.byok import routing as byok_routing  # noqa: E402
from app.byok import service as byok_service  # noqa: E402
from app.byok.accounting import ByokExecution, is_auth_failure  # noqa: E402
from app.byok.errors import ByokError  # noqa: E402


def _factory():
    engine = create_engine("sqlite://", poolclass=None)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _key(monkeypatch):
    monkeypatch.setenv("BYOK_ENCRYPTION_KEY", byok_crypto.generate_key())


def _users(db, n=2):
    ids = []
    for i in range(n):
        uid = uuid.uuid4()
        db.add(Profile(id=uid, email=f"u{i}-{uid.hex[:6]}@example.com"))
        ids.append(uid)
    db.commit()
    return ids


def _campaign(db, owner_id):
    camp = uuid.uuid4()
    db.add(Campaign(id=camp, owner_id=owner_id, name="BYOK table"))
    db.add(CampaignMember(campaign_id=camp, user_id=owner_id, role="owner"))
    db.commit()
    return camp


def _add_member(db, camp, user_id):
    db.add(CampaignMember(campaign_id=camp, user_id=user_id, role="player"))
    db.commit()


def _credential(db, owner_id, provider="meta", secret="sk-test-credential-0123456789"):
    return byok_service.create_credential(
        db, owner_id=owner_id, provider=provider, secret=secret, label="test key"
    )


# ── 1. encrypted storage + masked reads ─────────────────────────────────────

def test_secret_encrypted_at_rest_and_masked_on_read(monkeypatch):
    _key(monkeypatch)
    factory = _factory()
    db = factory()
    (owner,) = _users(db, 1)
    secret = "sk-test-credential-0123456789"
    row = _credential(db, owner, secret=secret)
    db.commit()

    stored = db.get(ProviderCredential, row.id)
    assert stored.encrypted_secret != secret
    assert secret not in stored.encrypted_secret
    assert byok_service.decrypt_for_execution(stored) == secret

    masked = stored.to_masked_dict()
    assert "secret" not in masked and "encrypted_secret" not in masked
    assert masked["key_hint"].endswith(secret[-4:])
    assert secret not in json.dumps(masked)


def test_missing_server_key_fails_closed(monkeypatch):
    monkeypatch.delenv("BYOK_ENCRYPTION_KEY", raising=False)
    db = _factory()()
    (owner,) = _users(db, 1)
    with pytest.raises(ByokError, match="BYOK_ENCRYPTION_KEY"):
        _credential(db, owner)


def test_unsupported_provider_and_short_secret_rejected(monkeypatch):
    _key(monkeypatch)
    db = _factory()()
    (owner,) = _users(db, 1)
    with pytest.raises(ByokError, match="not supported"):
        _credential(db, owner, provider="evil-provider")
    with pytest.raises(ByokError, match="too short"):
        byok_service.create_credential(
            db, owner_id=owner, provider="meta", secret="short"
        )


# ── 2. ownership isolation ──────────────────────────────────────────────────

def test_unauthorized_read_returns_not_found(monkeypatch):
    _key(monkeypatch)
    db = _factory()()
    owner, stranger = _users(db, 2)
    row = _credential(db, owner)
    db.commit()
    with pytest.raises(ByokError, match="not found"):
        byok_service.get_owned_credential(db, credential_id=row.id, owner_id=stranger)
    rows = byok_service.list_credentials(db, owner_id=stranger)
    assert rows == []
    assert len(byok_service.list_credentials(db, owner_id=owner)) == 1


# ── 3. rotation + deletion ──────────────────────────────────────────────────

def test_rotation_changes_fingerprint_and_rearms(monkeypatch):
    _key(monkeypatch)
    db = _factory()()
    (owner,) = _users(db, 1)
    row = _credential(db, owner)
    db.commit()
    old_fp = row.key_fingerprint
    row.status = "invalid"
    updated = byok_service.update_credential(
        db, credential_id=row.id, owner_id=owner,
        secret="sk-rotated-credential-9988776655",
    )
    assert updated.key_fingerprint != old_fp
    assert updated.status == "active"
    assert byok_service.decrypt_for_execution(updated) == "sk-rotated-credential-9988776655"


def test_delete_clears_campaign_policy_and_falls_back(monkeypatch):
    _key(monkeypatch)
    db = _factory()()
    owner, member = _users(db, 2)
    camp = _campaign(db, owner)
    _add_member(db, camp, member)
    row = _credential(db, member)
    db.commit()
    byok_service.set_campaign_policy(
        db, campaign_id=camp, credential_id=row.id, authorized_by=owner
    )
    db.commit()
    assert byok_service.active_campaign_credential(db, camp) is not None
    byok_service.delete_credential(db, credential_id=row.id, owner_id=member)
    db.commit()
    assert db.get(ProviderCredential, row.id) is None
    # Routing returns to funded/provider policy: no credential resolves.
    assert byok_service.active_campaign_credential(db, camp) is None
    policy = db.get(CampaignByokPolicy, camp)
    assert policy.credential_id is None and policy.enabled is False


# ── 4. campaign policy authorization ────────────────────────────────────────

def test_campaign_policy_owner_only_and_member_key_only(monkeypatch):
    _key(monkeypatch)
    db = _factory()()
    owner, member, outsider = _users(db, 3)
    camp = _campaign(db, owner)
    _add_member(db, camp, member)
    member_key = _credential(db, member)
    outsider_key = _credential(db, outsider)
    db.commit()

    with pytest.raises(ByokError, match="only the campaign owner"):
        byok_service.set_campaign_policy(
            db, campaign_id=camp, credential_id=member_key.id, authorized_by=member
        )
    with pytest.raises(ByokError, match="not a member"):
        byok_service.set_campaign_policy(
            db, campaign_id=camp, credential_id=outsider_key.id, authorized_by=owner
        )
    member_key.status = "invalid"
    db.flush()
    with pytest.raises(ByokError, match="only active credentials"):
        byok_service.set_campaign_policy(
            db, campaign_id=camp, credential_id=member_key.id, authorized_by=owner
        )
    member_key.status = "active"
    policy = byok_service.set_campaign_policy(
        db, campaign_id=camp, credential_id=member_key.id, authorized_by=owner
    )
    db.commit()
    # Member-visible read carries IDs only, never secrets.
    read = byok_service.get_campaign_policy(db, campaign_id=camp, viewer_id=member)
    dumped = read.to_dict(credential=db.get(ProviderCredential, read.credential_id))
    assert dumped["credential_id"] == str(member_key.id)
    assert "secret" not in json.dumps(dumped) and "encrypted" not in json.dumps(dumped)
    with pytest.raises(ByokError):
        byok_service.get_campaign_policy(db, campaign_id=camp, viewer_id=outsider)
    assert policy.credential_id == member_key.id


# ── 5. test-connection ──────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code


def test_test_connection_ok_and_invalid(monkeypatch):
    _key(monkeypatch)
    db = _factory()()
    (owner,) = _users(db, 1)
    row = _credential(db, owner, provider="openai")
    db.commit()

    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(200))
    # No openai probe URL configured in this env? openai base URL contains
    # openai.com so the probe fires; either way result must be ok.
    result = byok_service.test_credential(db, credential_id=row.id, owner_id=owner)
    assert result["result"] == "ok"
    assert db.get(ProviderCredential, row.id).status == "active"

    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(401))
    with pytest.raises(ByokError, match="rejected"):
        byok_service.test_credential(db, credential_id=row.id, owner_id=owner)
    assert db.get(ProviderCredential, row.id).status == "invalid"


# ── 6. generative route approval ────────────────────────────────────────────

def test_generative_route_approval_and_rejection():
    from app.providers import policy as role_policy

    primary = role_policy.get_role_policy("forward_dm")
    route = byok_routing.resolve_generative_route(
        "forward_dm", provider=primary.primary_provider, model=primary.primary_model
    )
    assert route.provider == primary.primary_provider

    with pytest.raises(ByokError, match="not approved"):
        byok_routing.resolve_generative_route(
            "forward_dm", provider="evil", model=primary.primary_model
        )
    with pytest.raises(ByokError, match="not approved"):
        byok_routing.resolve_generative_route(
            "forward_dm", provider=primary.primary_provider, model="unapproved-model-9"
        )
    with pytest.raises(ByokError, match="unknown generative execution role"):
        byok_routing.resolve_generative_route(
            "forward_dm_evil", provider=primary.primary_provider,
            model=primary.primary_model,
        )


# ── 7. decision route approval ──────────────────────────────────────────────

def test_decision_route_approval_modes_and_versions():
    approval = byok_routing.get_decision_approval("skirmish_action")
    route = byok_routing.resolve_decision_route(
        "skirmish_action", provider="jev", model=approval.model, mode="primer"
    )
    assert route.adapter == "jev" and route.mode == "primer"

    # Direct execution is not approved by default: clear rejection.
    with pytest.raises(ByokError, match="not approved for mode"):
        byok_routing.resolve_decision_route(
            "skirmish_action", provider="jev", model=approval.model,
            mode="direct_execute",
        )
    # A Jev key approved for one class is not approval for another role.
    with pytest.raises(ByokError, match="no approved execution route"):
        byok_routing.resolve_decision_route(
            "unapproved_class", provider="jev", model=approval.model, mode="primer"
        )
    # A generative-only key is never decision approval.
    with pytest.raises(ByokError, match="not approved"):
        byok_routing.resolve_decision_route(
            "skirmish_action", provider="openai", model=approval.model, mode="primer"
        )
    # Evaluation promotion can approve direct execution explicitly.
    byok_routing.register_decision_approval(
        byok_routing.DecisionRoleApproval(
            decision_class="skirmish_action", adapter="jev", model=approval.model,
            policy_version=approval.policy_version,
            candidate_schema_version=approval.candidate_schema_version,
            allowed_modes=("shadow", "primer", "direct_execute"),
            byok_eligible=True, evaluation_ref="test-promotion",
        )
    )
    try:
        promoted = byok_routing.resolve_decision_route(
            "skirmish_action", provider="jev", model=approval.model,
            mode="direct_execute",
        )
        assert promoted.mode == "direct_execute"
    finally:
        byok_routing.register_decision_approval(
            byok_routing.DecisionRoleApproval(
                decision_class="skirmish_action", adapter="jev", model=approval.model,
                policy_version=approval.policy_version,
                candidate_schema_version=approval.candidate_schema_version,
                allowed_modes=("shadow", "primer"),
                byok_eligible=True, evaluation_ref="seed",
            )
        )


# ── 8. proxy key isolation ──────────────────────────────────────────────────

def test_proxy_uses_credential_without_platform_key(monkeypatch):
    for var in ("META_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    from app.byok.adapters import wrap_generative_adapter

    proxy = wrap_generative_adapter("meta", "sk-user-secret-abc123")
    proxy.require_config("muse-spark-1.3-contributor")
    headers = proxy.build_headers()
    assert headers["Authorization"] == "Bearer sk-user-secret-abc123"
    assert proxy.api_key() == "sk-user-secret-abc123"


# ── 9. generative BYOK execution ────────────────────────────────────────────

def _packet():
    pkt = types.SimpleNamespace()
    pkt.serialize_for_adjudication = lambda: "{}"
    return pkt


def _respond_content(text="Torchlight gutters as something stirs."):
    from app.dm.contract import CONTRACT_VERSION

    return json.dumps({
        "contract_version": CONTRACT_VERSION, "mode": "respond",
        "reason": "story continuation",
        "beats": [{"id": "beat_1", "type": "narration", "claims": [{
            "text": text, "claim_kind": "observation",
            "origin": "dm_adjudication", "visibility": "public"}]}],
        "open_player_choice": "What do you do?",
    })


def test_generative_byok_execution_marks_run_and_ledger(monkeypatch):
    _key(monkeypatch)
    monkeypatch.setenv("META_API_KEY", "platform-key-not-used")
    factory = _factory()
    db = factory()
    owner, member = _users(db, 2)
    camp = _campaign(db, owner)
    _add_member(db, camp, member)
    row = _credential(db, member)
    db.commit()
    byok_service.set_campaign_policy(
        db, campaign_id=camp, credential_id=row.id, authorized_by=owner
    )
    db.commit()

    from app.providers import policy as role_policy

    primary = role_policy.get_role_policy("forward_dm")
    seen = {}

    def fake_execute(adapter, request):
        seen["auth"] = adapter.build_headers()["Authorization"]
        seen["adapter"] = type(adapter).__name__
        return types.SimpleNamespace(
            content=_respond_content(), usage={"prompt_tokens": 10, "completion_tokens": 5}
        )

    monkeypatch.setattr("app.providers.execute_chat", fake_execute)

    from app.dm import adjudication as adj

    active = byok_service.active_campaign_credential(db, camp)
    db.add(OperationTrace(trace_id="byok-trace-1", operation_id="byok-op-1",
                          campaign_id=camp, submitted_at=datetime.now(timezone.utc)))
    db.commit()
    byok = ByokExecution(
        credential_id=active.row.id, campaign_id=camp,
        secret=active.secret, provider=active.row.provider,
    )
    contract, info = adj.adjudicate_with_failover(
        _packet(), db=db, role="forward_dm", trace_id="byok-trace-1", byok=byok,
    )
    assert contract.mode == "respond"
    assert seen["auth"] == f"Bearer {active.secret}"
    assert "platform-key-not-used" not in seen["auth"]
    assert info["credential_id"] == str(row.id)

    runs = factory().execute(
        select(AIRun).where(AIRun.trace_id == "byok-trace-1")
    ).scalars().all()
    assert len(runs) == 1
    assert runs[0].billable is False
    assert str(runs[0].credential_id) == str(row.id)

    # Observability identifies execution class/role; accounting excludes
    # platform cost and records the zero-amount marker.
    from app.billing import ledger as _ledger
    from app.billing.ledger import charge_completed_run
    from app.observability.service import get_trace

    trace = get_trace(factory(), "byok-trace-1")
    assert trace is not None
    assert trace["ai_runs"][0]["credential_id"] == str(row.id)

    check = factory()
    assert charge_completed_run(check, run_id=runs[0].id, campaign_id=camp) is None
    summary = _ledger.get_capacity_summary(check, camp)
    assert summary["consumed_cents"] == 0
    assert summary["byok_run_markers"] == 1
    from models.usage import CampaignUsageEntry

    entries = check.execute(select(CampaignUsageEntry)).scalars().all()
    byok_markers = [e for e in entries if e.entry_type == "byok_marker"]
    assert len(byok_markers) == 1
    assert byok_markers[0].amount_cents == 0
    assert byok_markers[0].entry_metadata["credential_id"] == str(row.id)
    assert byok_markers[0].entry_metadata["execution_class"] == "generative"
    assert byok_markers[0].entry_metadata["role"] == "forward_dm"


def test_generative_byok_auth_failure_recovers_to_platform(monkeypatch):
    _key(monkeypatch)
    monkeypatch.setenv("META_API_KEY", "platform-fallback-key")
    factory = _factory()
    db = factory()
    owner, member = _users(db, 2)
    camp = _campaign(db, owner)
    _add_member(db, camp, member)
    row = _credential(db, member)
    db.commit()

    from app.byok.adapters import ByokAdapterProxy
    from app.providers.contracts import ProviderError

    calls = []

    def fake_execute(adapter, request):
        calls.append(type(adapter).__name__)
        if isinstance(adapter, ByokAdapterProxy):
            raise ProviderError("401 Unauthorized: invalid api key", kind="http",
                                status_code=401, retryable=False)
        return types.SimpleNamespace(content=_respond_content(), usage={})

    monkeypatch.setattr("app.providers.execute_chat", fake_execute)

    from app.dm import adjudication as adj

    active = byok_service.active_campaign_credential(db, camp) or types.SimpleNamespace(
        row=row, secret=byok_service.decrypt_for_execution(row)
    )
    byok = ByokExecution(
        credential_id=row.id, campaign_id=camp,
        secret=byok_service.decrypt_for_execution(row), provider=row.provider,
    )
    contract, info = adj.adjudicate_with_failover(
        _packet(), db=db, role="forward_dm", trace_id="byok-trace-2", byok=byok,
    )
    assert contract.mode == "respond"
    # BYOK attempt failed, platform path completed: no secret in the winner.
    assert info.get("credential_id") is None
    assert info["provider"] == "meta"
    assert factory().get(ProviderCredential, row.id).status == "invalid"
    assert is_auth_failure(ProviderError("x", kind="http", status_code=401))


# ── 10. decision-role BYOK execution ─────────────────────────────────────────

def _choice_request():
    from app.decisions.contracts import (
        ChoiceQuestion,
        DecisionCandidate,
        DecisionRequest,
    )

    return DecisionRequest(
        questions=(
            ChoiceQuestion(
                question_id="q1", instructions="Pick the action.",
                candidates=(
                    DecisionCandidate(id="a", description="first"),
                    DecisionCandidate(id="b", description="second"),
                ),
            ),
        ),
        state={"scene": "torchlit hall"},
    )


def test_decision_byok_execution_and_markers(monkeypatch):
    _key(monkeypatch)
    factory = _factory()
    db = factory()
    owner, member = _users(db, 2)
    camp = _campaign(db, owner)
    _add_member(db, camp, member)
    row = _credential(db, member, provider="jev", secret="ts-test-credential-0123456789")
    db.commit()

    from app.byok.adapters import ByokAdapterProxy
    from app.decisions.adapters.fake import FakeDecisionAdapter
    from app.decisions.runtime import DecisionService

    fake = FakeDecisionAdapter(answers={"q1": "a"})
    monkeypatch.setattr(
        "app.byok.adapters.wrap_decision_adapter",
        lambda provider, secret: ByokAdapterProxy(fake, secret),
    )
    service = DecisionService(session_factory=factory)
    byok = ByokExecution(
        credential_id=row.id, campaign_id=camp,
        secret=byok_service.decrypt_for_execution(row), provider="jev",
    )
    response = service.decide(
        _choice_request(), byok=byok, decision_role="skirmish_action", mode="primer"
    )
    assert response.results["q1"].selected_id == "a"

    runs = factory().execute(select(AIRun)).scalars().all()
    assert len(runs) == 1
    assert runs[0].billable is False
    assert str(runs[0].credential_id) == str(row.id)
    assert runs[0].role == "decision"

    from app.billing import ledger as _ledger

    summary = _ledger.get_capacity_summary(factory(), camp)
    assert summary["consumed_cents"] == 0
    assert summary["byok_run_markers"] == 1


def test_decision_byok_requires_role_approval(monkeypatch):
    _key(monkeypatch)
    db = _factory()()
    (owner,) = _users(db, 1)
    row = _credential(db, owner, provider="jev", secret="ts-test-credential-0123456789")
    db.commit()

    from app.decisions.runtime import DecisionService

    service = DecisionService()
    byok = ByokExecution(
        credential_id=row.id, campaign_id=uuid.uuid4(),
        secret=byok_service.decrypt_for_execution(row), provider="jev",
    )
    with pytest.raises(ByokError, match="decision_role is required"):
        service.decide(_choice_request(), byok=byok)
    with pytest.raises(ByokError, match="not approved for mode"):
        service.decide(
            _choice_request(), byok=byok,
            decision_role="skirmish_action", mode="direct_execute",
        )


def test_decision_byok_auth_failure_escalates_and_flags(monkeypatch):
    _key(monkeypatch)
    factory = _factory()
    db = factory()
    (owner,) = _users(db, 1)
    row = _credential(db, owner, provider="jev", secret="ts-test-credential-0123456789")
    db.commit()

    from app.byok.adapters import ByokAdapterProxy
    from app.decisions.adapters.fake import FakeDecisionAdapter
    from app.decisions.errors import DecisionError
    from app.decisions.runtime import DecisionService

    class _AuthFail(FakeDecisionAdapter):
        def execute(self, request, *, model, timeout):
            raise DecisionError("401 unauthorized", provider="jev",
                                status_code=401, kind="http")

    monkeypatch.setattr(
        "app.byok.adapters.wrap_decision_adapter",
        lambda provider, secret: ByokAdapterProxy(_AuthFail(), secret),
    )
    service = DecisionService(session_factory=factory)
    byok = ByokExecution(
        credential_id=row.id, campaign_id=uuid.uuid4(),
        secret=byok_service.decrypt_for_execution(row), provider="jev",
    )
    with pytest.raises(DecisionError):
        service.decide(
            _choice_request(), byok=byok,
            decision_role="skirmish_action", mode="primer",
        )
    assert factory().get(ProviderCredential, row.id).status == "invalid"


# ── 11. paused campaign resume ──────────────────────────────────────────────

def test_paused_campaign_resumes_with_byok_no_state_reset(monkeypatch):
    _key(monkeypatch)
    factory = _factory()
    db = factory()
    owner, member = _users(db, 2)
    camp = _campaign(db, owner)
    _add_member(db, camp, member)

    from app.billing import ledger as _ledger
    from app.billing.resolution_guarantee import evaluate_new_work

    _ledger.record_entry(
        db, campaign_id=camp, entry_type="allocation", amount_cents=100,
        idempotency_key="alloc-1",
    )
    trace_id = f"trace-{uuid.uuid4().hex[:12]}"
    db.add(OperationTrace(trace_id=trace_id, operation_id=f"op-{trace_id}",
                          campaign_id=camp, submitted_at=datetime.now(timezone.utc)))
    db.flush()
    run = AIRun(trace_id=trace_id, operation_id=f"op-{trace_id}",
                logical_operation="forward_dm_adjudicate", role="forward_dm",
                provider="meta", model="m", attempt=1, classification="primary",
                billable=True, status="succeeded",
                started_at=datetime.now(timezone.utc),
                completed_at=datetime.now(timezone.utc), cost_usd=1.00)
    db.add(run)
    db.flush()
    _ledger.record_ai_spend_for_run(db, campaign_id=camp, ai_run=run)
    db.commit()
    revision_before = db.get(Campaign, camp).revision

    paused = evaluate_new_work(db, camp, byok_roles=None)
    assert paused["allowed"] is False and paused["ai_paused"] is True

    row = _credential(db, member)
    db.commit()
    byok_service.set_campaign_policy(
        db, campaign_id=camp, credential_id=row.id, authorized_by=owner
    )
    db.commit()

    resumed = evaluate_new_work(db, camp)
    assert resumed["allowed"] is True
    assert resumed["reason"] == "byok_capacity"
    assert resumed["ai_paused"] is False
    # No state reset: revision untouched, spend history intact.
    assert db.get(Campaign, camp).revision == revision_before
    summary = _ledger.get_capacity_summary(db, camp)
    assert summary["consumed_cents"] == 100


# ── 12. review regressions: decision transport binds the user secret ──────

def test_decision_proxy_execute_sends_user_secret_not_platform_key(monkeypatch):
    """Self-referential decision adapters must not leak the platform key."""
    monkeypatch.setenv("TYPESAFE_API_KEY", "platform-key-XYZ")
    monkeypatch.setenv("TYPESAFE_BASE_URL", "https://jev.example.com/v1/systemone")
    from app.byok.adapters import wrap_decision_adapter

    captured = {}

    def fake_post(url, headers=None, **kwargs):
        captured.update(headers or {})
        raise RuntimeError("stop-after-capture")

    monkeypatch.setattr("requests.post", fake_post)
    proxy = wrap_decision_adapter("jev", "sk-user-secret-abc123")
    with pytest.raises(Exception):
        proxy.execute(_choice_request(), model="jev-latest", timeout=5.0)
    assert captured.get("Authorization") == "Bearer sk-user-secret-abc123"
    assert "platform-key-XYZ" not in (captured.get("Authorization") or "")


def test_decision_byok_end_to_end_uses_user_secret(monkeypatch):
    """Full DecisionService BYOK path through the real Jev adapter."""
    _key(monkeypatch)
    monkeypatch.setenv("TYPESAFE_API_KEY", "platform-key-XYZ")
    monkeypatch.setenv("TYPESAFE_BASE_URL", "https://jev.example.com/v1/systemone")
    factory = _factory()
    db = factory()
    (owner,) = _users(db, 1)
    row = _credential(db, owner, provider="jev", secret="ts-test-credential-0123456789")
    db.commit()

    from app.byok.routing import get_decision_approval

    approval = get_decision_approval("skirmish_action")
    captured = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "answers": {
                    "q1": {
                        "type": "choice", "choice": "a",
                        "probabilities": {"a": 1.0, "b": 0.0},
                        "confidence": 1.0,
                    }
                },
                "usage": {},
                "model": approval.model,
            }

    def fake_post(url, headers=None, **kwargs):
        captured.update(headers or {})
        return _Resp()

    monkeypatch.setattr("requests.post", fake_post)

    from app.decisions.adapters.jev import JevAdapter
    from app.decisions.runtime import DecisionService

    service = DecisionService(JevAdapter(), session_factory=factory)
    byok = ByokExecution(
        credential_id=row.id, campaign_id=uuid.uuid4(),
        secret=byok_service.decrypt_for_execution(row), provider="jev",
    )
    response = service.decide(
        _choice_request(), byok=byok,
        decision_role="skirmish_action", mode="primer",
    )
    assert response.results["q1"].selected_id == "a"
    assert captured.get("Authorization") == "Bearer ts-test-credential-0123456789"

    runs = factory().execute(select(AIRun)).scalars().all()
    assert len(runs) == 1
    assert runs[0].billable is False
    assert str(runs[0].credential_id) == str(row.id)


# ── 13. review regressions: auth-failure classification stays narrow ──────

def test_is_auth_failure_does_not_kill_good_keys_on_quota():
    from app.providers.contracts import ProviderError

    assert is_auth_failure(ProviderError("401 Unauthorized", kind="http", status_code=401))
    assert is_auth_failure(
        ProviderError("403 invalid api key", kind="http", status_code=403)
    )
    # Bare 403s (quota / geo / rate limit) must not invalidate the key.
    assert not is_auth_failure(
        ProviderError("403 Forbidden: quota exceeded", kind="http", status_code=403)
    )
    assert not is_auth_failure(
        ProviderError("429 rate limited", kind="http", status_code=429)
    )


def test_is_auth_failure_ignores_bare_expired_forbidden_text():
    from app.decisions.errors import DecisionError

    assert not is_auth_failure(DecisionError("trial expired", kind="http"))
    assert not is_auth_failure(DecisionError("request forbidden by policy", kind="http"))
    assert is_auth_failure(DecisionError("api key expired", kind="http"))


# ── 14. review regressions: input shape validation ────────────────────────

def test_non_string_inputs_rejected_as_malformed(monkeypatch):
    _key(monkeypatch)
    db = _factory()()
    (owner,) = _users(db, 1)
    with pytest.raises(ByokError, match="provider is required"):
        byok_service.create_credential(
            db, owner_id=owner, provider=123, secret="sk-test-credential-0123456789"
        )
    with pytest.raises(ByokError, match="label must be a string"):
        byok_service.create_credential(
            db, owner_id=owner, provider="meta",
            secret="sk-test-credential-0123456789", label=123,
        )
    with pytest.raises(ByokError, match="too long"):
        byok_service.create_credential(
            db, owner_id=owner, provider="meta",
            secret="sk-" + "x" * 2000,
        )
    with pytest.raises(ByokError, match="model is required"):
        byok_routing.resolve_generative_route(
            "forward_dm", provider="meta", model=123,
        )


# ── 15. review regressions: server-side execution resolver ────────────────

def test_resolve_byok_execution_requires_authorized_policy(monkeypatch):
    _key(monkeypatch)
    factory = _factory()
    db = factory()
    owner, member = _users(db, 2)
    camp = _campaign(db, owner)
    _add_member(db, camp, member)

    # No policy: fail-soft None (funded/provider routing proceeds).
    assert byok_service.resolve_byok_execution(db, camp, role="forward_dm") is None

    row = _credential(db, member)
    db.commit()
    byok_service.set_campaign_policy(
        db, campaign_id=camp, credential_id=row.id, authorized_by=owner
    )
    db.commit()
    resolved = byok_service.resolve_byok_execution(db, camp, role="forward_dm")
    assert resolved is not None
    assert resolved.credential_id == row.id
    assert resolved.campaign_id == camp
    assert resolved.secret == "sk-test-credential-0123456789"
    assert "sk-test-credential" not in repr(resolved)

    # A credential with no approved route for the role never decrypts.
    jev_row = _credential(db, member, provider="jev", secret="ts-test-credential-0123456789")
    db.commit()
    byok_service.set_campaign_policy(
        db, campaign_id=camp, credential_id=jev_row.id, authorized_by=owner
    )
    db.commit()
    assert byok_service.resolve_byok_execution(db, camp, role="forward_dm") is None


def test_test_connection_unverified_without_probe_shape(monkeypatch):
    _key(monkeypatch)
    db = _factory()()
    (owner,) = _users(db, 1)
    row = _credential(db, owner, provider="meta")
    db.commit()
    result = byok_service.test_credential(db, credential_id=row.id, owner_id=owner)
    assert result["result"] == "unverified"
    assert db.get(ProviderCredential, row.id).status == "active"


# ── 16. review regressions: pinned attempt + byok is an explicit error ────

def test_pinned_adapter_and_byok_conflict_raises(monkeypatch):
    _key(monkeypatch)
    db = _factory()()
    (owner,) = _users(db, 1)
    row = _credential(db, owner)
    db.commit()
    byok = ByokExecution(
        credential_id=row.id, campaign_id=uuid.uuid4(),
        secret=byok_service.decrypt_for_execution(row), provider=row.provider,
    )
    from app.dm import adjudication as adj

    class _Adapter:
        name = "meta"

    with pytest.raises(ByokError, match="pinned"):
        adj.adjudicate_with_failover(
            _packet(), db=db, role="forward_dm", adapter=_Adapter(),
            model="m", byok=byok,
        )
    with pytest.raises(ByokError, match="pinned"):
        adj.build_provider_narrator(adapter=_Adapter(), model="m", byok=byok)

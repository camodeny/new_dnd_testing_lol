"""Issue #270: first cross-layer deterministic fault-injection slice.

No real provider, credentials, or production data are used. Also hosts the
shared disposable-database guard (``_safe_engine``) and campaign seeding used
by other reliability tests.
"""
from __future__ import annotations

import os
import uuid
from urllib.parse import urlparse

import pytest
import requests
from sqlalchemy import create_engine, text
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from database import Base
from app.providers import (
    LLMProviderAdapter,
    NormalizedChatResponse,
    ProviderError,
    ProviderRequest,
    TransportHooks,
    execute_chat,
)
from models.campaigns import Campaign
from models.profiles import Profile
from tests.reliability.faults import FaultScenario


def _safe_engine(tmp_path):
    """Use the disposable CI DB when explicitly supplied, otherwise SQLite."""
    url = os.getenv("FAULT_TEST_DATABASE_URL")
    if not url:
        engine = create_engine(
            f"sqlite:///{tmp_path / 'fault-injection.sqlite'}",
            connect_args={"check_same_thread": False, "timeout": 10},
        )
        Base.metadata.create_all(engine)
        return engine

    parsed = urlparse(url)
    database = parsed.path.lstrip("/")
    local_hosts = {"localhost", "127.0.0.1", "postgres"}
    if parsed.hostname not in local_hosts or database not in {"ci_test", "test", "reliability_test"}:
        pytest.fail(
            "FAULT_TEST_DATABASE_URL must target an explicitly named disposable "
            "database on localhost/127.0.0.1/postgres"
        )
    return create_engine(url)


def _seed_campaign(factory):
    owner_id = uuid.uuid4()
    campaign_id = uuid.uuid4()
    with factory() as db:
        if db.bind.dialect.name == "postgresql":
            db.execute(
                text("INSERT INTO auth.users (id) VALUES (:id) ON CONFLICT (id) DO NOTHING"),
                {"id": owner_id},
            )
            db.flush()
        db.add(Profile(id=owner_id, email=f"fault-{owner_id}@example.invalid"))
        db.commit()
    with factory() as db:
        db.add(Campaign(id=campaign_id, owner_id=owner_id, name="Synthetic reliability campaign"))
        db.commit()
    return campaign_id


def test_fault_database_guard_rejects_nonlocal_target(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "FAULT_TEST_DATABASE_URL",
        "postgresql://synthetic:synthetic@production.example.com/customer_data",
    )
    with pytest.raises(pytest.fail.Exception, match="disposable"):
        _safe_engine(tmp_path)


class _FakeAdapter(LLMProviderAdapter):
    name = "fault_fake"
    env_prefix = "FAULT_FAKE"
    default_base_url = "https://provider.invalid/chat"

    def require_config(self, model=None):
        return None

    def classify_error(self, error):
        if isinstance(error, ProviderError):
            return error
        return ProviderError(str(error), provider=self.name, retryable=False, original=error)

    def parse_response(self, data):
        return NormalizedChatResponse(
            provider=self.name,
            model=data["model"],
            content=data["content"],
            tool_calls=[],
            finish_reason="stop",
            usage=data.get("usage", {}),
            reasoning=None,
            reasoning_details=None,
            raw=data,
        )


class _Hooks(TransportHooks):
    def __init__(self):
        self.retries = []
        self.errors = []

    def on_retry(self, attempt, max_attempts, delay_seconds, error):
        self.retries.append((attempt, max_attempts, delay_seconds, str(error)))

    def on_error(self, error):
        self.errors.append(str(error))


class _Response:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


@pytest.mark.parametrize(
    ("retryable", "expected_calls", "expect_success"),
    [(True, 2, True), (False, 1, False)],
    ids=["transient-recovers", "terminal-stops"],
)
def test_provider_failure_policy_without_network_or_paid_calls(
    monkeypatch, retryable, expected_calls, expect_success
):
    scenario = FaultScenario(f"provider_retryable_{retryable}")
    adapter = _FakeAdapter()
    hooks = _Hooks()
    calls = 0

    def fake_post(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            scenario.record("provider_failure", retryable=retryable, attempt=calls)
            raise ProviderError(
                "synthetic provider failure",
                provider=adapter.name,
                retryable=retryable,
                kind="timeout" if retryable else "malformed",
            )
        return _Response({"model": "fake-model", "content": "recovered", "usage": {}})

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr("app.providers.transport.time.sleep", lambda _: None)
    request = ProviderRequest(messages=[{"role": "user", "content": "synthetic"}], model="fake-model", max_attempts=3)

    if expect_success:
        response = execute_chat(adapter, request, hooks=hooks)
        assert response.content == "recovered"
        assert len(hooks.retries) == 1 and hooks.errors == []
    else:
        with pytest.raises(ProviderError, match="synthetic provider failure"):
            execute_chat(adapter, request, hooks=hooks)
        assert hooks.retries == [] and hooks.errors == ["synthetic provider failure"]

    assert calls == expected_calls
    scenario.record(
        "provider_policy_complete",
        calls=calls,
        retries=len(hooks.retries),
        terminal_errors=len(hooks.errors),
        network_used=False,
        successful_completions=1 if expect_success else 0,
    )

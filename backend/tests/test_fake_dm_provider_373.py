"""Issue #373 — deterministic fake-provider mode unit coverage.

Proves the fake answers by logical step/input (not call order), is
byte-identical across repeated runs, fails loudly on unexpected calls
with the logical request identified, records which fixture satisfied
each AI role call, extends without a new runtime, and leaves real
providers untouched outside explicitly installed test mode.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.dm.fake_provider import (
    FAKE_MODEL_NAME,
    FAKE_PROVIDER_NAME,
    FakeDMProvider,
    FakeProviderUsageError,
    build_phase0_provider,
    extract_player_input_texts,
    infer_role,
)
from app.providers.contracts import ProviderRequest


def _dm_request(input_texts: list[str]) -> ProviderRequest:
    lanes = [
        {
            "name": "player_inputs",
            "records": [
                {
                    "value": {
                        "segments": [
                            {"position": i, "segment_type": "ic", "text": text}
                            for i, text in enumerate(input_texts)
                        ]
                    }
                }
            ],
        }
    ]
    return ProviderRequest(
        messages=[
            {"role": "system", "content": "fake system"},
            {"role": "user", "content": json.dumps({"lanes": lanes})},
        ],
        model=FAKE_MODEL_NAME,
        json_schema={"type": "object"},
        json_schema_name="dm_turn_contract_v1",
    )


def _adapter():
    return SimpleNamespace(name=FAKE_PROVIDER_NAME)


def test_matches_by_logical_input_not_call_order():
    provider = FakeDMProvider()
    provider.register_step(
        "second", {"mode": "respond", "marker": "second"}, inputs=("bravo",)
    )
    provider.register_step(
        "first", {"mode": "respond", "marker": "first"}, inputs=("alpha",)
    )

    # Asked in reverse registration order, each input still resolves its own step.
    first = provider.execute_chat(_adapter(), _dm_request(["alpha goes first"]))
    second = provider.execute_chat(_adapter(), _dm_request(["bravo goes second"]))
    assert json.loads(first.content)["marker"] == "first"
    assert json.loads(second.content)["marker"] == "second"
    assert [call["fixture_step"] for call in provider.calls] == ["first", "second"]


def test_repeated_runs_are_byte_identical():
    provider = build_phase0_provider(
        freeform_turns=("I look around the tavern.",),
        post_reconnect_turn="I step outside.",
    )
    request = _dm_request(["I look around the tavern."])
    once = provider.execute_chat(_adapter(), request)
    twice = provider.execute_chat(
        _adapter(), _dm_request(["I look around the tavern."])
    )
    assert once.content == twice.content
    assert provider.calls_for_step("play-1")[0]["role"] == "forward_dm"


def test_opening_matches_input_free_request():
    provider = build_phase0_provider(freeform_turns=(), post_reconnect_turn="Later.")
    response = provider.execute_chat(_adapter(), _dm_request([]))
    assert "phase0-reply-opening" in response.content
    assert provider.calls[-1]["fixture_step"] == "opening"


def test_unexpected_call_fails_loudly_with_logical_request():
    provider = build_phase0_provider(
        freeform_turns=("I look around the tavern.",),
        post_reconnect_turn="I step outside.",
    )
    with pytest.raises(FakeProviderUsageError) as exc_info:
        provider.execute_chat(_adapter(), _dm_request(["I befriend the moon."]))
    message = str(exc_info.value)
    assert "forward_dm" in message
    assert "I befriend the moon." in message
    assert "play-1" in message  # registered steps listed for repair
    assert provider.calls == []  # nothing silently satisfied


def test_removed_fixture_fails_instead_of_falling_back():
    provider = build_phase0_provider(
        freeform_turns=("I look around the tavern.",),
        post_reconnect_turn="I step outside.",
    )
    provider._fixtures = [f for f in provider._fixtures if f.step != "play-1"]
    with pytest.raises(FakeProviderUsageError, match="no fixture"):
        provider.execute_chat(_adapter(), _dm_request(["I look around the tavern."]))


def test_calls_record_fixture_per_role_call():
    provider = build_phase0_provider(
        freeform_turns=("I look around the tavern.",),
        post_reconnect_turn="I step outside.",
    )
    provider.execute_chat(_adapter(), _dm_request([]))
    provider.execute_chat(_adapter(), _dm_request(["I step outside."]))
    assert [(c["fixture_step"], c["role"]) for c in provider.calls] == [
        ("opening", "forward_dm"),
        ("post-reconnect", "forward_dm"),
    ]
    assert all(c["provider"] == FAKE_PROVIDER_NAME for c in provider.calls)


def test_later_scenarios_extend_without_new_runtime():
    """A new logical step (rolls/combat/completion) registers on the same provider."""
    provider = build_phase0_provider(freeform_turns=(), post_reconnect_turn="Later.")
    provider.register_step(
        "combat-1",
        {"mode": "respond", "marker": "combat-fixture"},
        inputs=("I roll initiative",),
        roles=("forward_dm",),
    )
    response = provider.execute_chat(_adapter(), _dm_request(["I roll initiative!"]))
    assert json.loads(response.content)["marker"] == "combat-fixture"
    assert provider.calls_for_step("combat-1")[0]["role"] == "forward_dm"


def test_role_inference_and_input_extraction():
    assert infer_role(_dm_request(["hi"])) == "forward_dm"
    assert extract_player_input_texts(_dm_request(["a", "b"]).messages) == ["a", "b"]
    assert extract_player_input_texts([]) == []
    assert extract_player_input_texts([{"role": "user", "content": "not json"}]) == []


def test_real_providers_unaffected_outside_test_mode():
    """Installing nothing changes nothing: the real registry has no fake."""
    from app.providers.registry import provider_registry

    assert FAKE_PROVIDER_NAME not in provider_registry.names()
    assert provider_registry.get("meta").name == "meta"
    import app.providers as providers_pkg
    import app.providers.transport as transport

    assert providers_pkg.execute_chat is transport.execute_chat


def test_install_is_explicit_and_test_scoped(monkeypatch):
    provider = build_phase0_provider(freeform_turns=(), post_reconnect_turn="Later.")
    provider.install(monkeypatch)
    import app.dm.adjudication as adjudication
    import app.providers as providers_pkg

    adapter, model, name = adjudication.resolve_dm_provider()
    assert name == FAKE_PROVIDER_NAME
    assert model == FAKE_MODEL_NAME
    response = providers_pkg.execute_chat(adapter, _dm_request([]))
    assert "phase0-reply-opening" in response.content
    # monkeypatch undoes both patches at test end — asserted implicitly by
    # test_real_providers_unaffected_outside_test_mode running uninstalled.


def test_install_refuses_production(monkeypatch):
    provider = FakeDMProvider()
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(FakeProviderUsageError, match="test-scoped"):
        provider.install(monkeypatch)


def test_unmatched_fixture_is_terminal_before_failover(monkeypatch):
    """A missing fixture surfaces as the fake usage error even with failover.

    With a same-model failover provider configured but unconfigured (no
    API key), the unmatched fixture must be terminal at the provider
    boundary: failover must never be attempted and the surfaced error
    must stay the actionable fixture diagnostic, not a config error.
    """
    from app.dm import adjudication as adj
    from app.providers import policy as role_policy
    from app.providers import registry as reg

    provider = FakeDMProvider()  # no fixtures: every request is unmatched
    provider.install(monkeypatch)
    monkeypatch.setattr(
        role_policy,
        "execution_path",
        lambda role: [
            (FAKE_PROVIDER_NAME, FAKE_MODEL_NAME),
            ("openai", FAKE_MODEL_NAME),
        ],
    )
    monkeypatch.setattr(role_policy, "is_model_approved", lambda r, p, m: True)
    failover_attempted: list[str] = []
    real_get = reg.provider_registry.get

    def _watch_get(name: str):
        failover_attempted.append(name)
        return real_get(name)

    monkeypatch.setattr(reg.provider_registry, "get", _watch_get)
    packet = {
        "lanes": [
            {
                "name": "player_inputs",
                "records": [
                    {"value": {"segments": [{"text": "I befriend the moon."}]}}
                ],
            }
        ]
    }
    with pytest.raises(FakeProviderUsageError, match="no fixture"):
        adj.adjudicate_with_failover(packet, role="forward_dm")
    assert failover_attempted == []

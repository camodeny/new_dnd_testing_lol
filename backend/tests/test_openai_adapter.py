"""OpenAI adapter request shaping for GPT-6 strict structured outputs."""

import copy
import json

from app.dm.contract import CONTRACT_VERSION
from app.providers.adapters.openai import OpenAIAdapter, strict_schema_for_openai
from app.providers.contracts import ProviderRequest


def _request(**over):
    base = dict(
        messages=[{"role": "user", "content": "hi"}],
        model="gpt-6-luna",
        json_schema={
            "type": "object",
            "properties": {"target": {"$ref": "#/$defs/Ref", "description": "who"}},
            "$defs": {"Ref": {"type": "object", "description": "A ref", "properties": {}}},
        },
        json_schema_name="contract",
        temperature=0,
    )
    base.update(over)
    return ProviderRequest(**base)


def test_ref_siblings_are_stripped_for_strict_schema():
    request = _request()
    original = copy.deepcopy(request.json_schema)
    schema = OpenAIAdapter().build_payload(request)["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["target"] == {"$ref": "#/$defs/Ref"}
    # Definitions keep their own documentation; the caller's schema is untouched.
    assert schema["$defs"]["Ref"]["description"] == "A ref"
    assert request.json_schema == original


def test_temperature_dropped_once_the_model_reasons():
    payload = OpenAIAdapter().build_payload(_request(reasoning_effort="low"))
    assert payload["reasoning_effort"] == "low"
    assert "temperature" not in payload


def test_temperature_kept_without_reasoning():
    assert OpenAIAdapter().build_payload(_request(reasoning_effort="none"))["temperature"] == 0
    assert OpenAIAdapter().build_payload(_request())["temperature"] == 0


def test_dm_contract_schema_has_no_ref_siblings_after_shaping():
    from app.dm.contract import contract_json_schema_strict

    def ref_siblings(node):
        if isinstance(node, dict):
            if "$ref" in node and len(node) > 1:
                return True
            return any(ref_siblings(v) for v in node.values())
        if isinstance(node, list):
            return any(ref_siblings(v) for v in node)
        return False

    assert ref_siblings(contract_json_schema_strict())
    assert not ref_siblings(strict_schema_for_openai(contract_json_schema_strict()))


class _StubPacket:
    def serialize_for_adjudication(self):
        return "{}"


def test_dm_contract_via_openai_choices_envelope(monkeypatch):
    """The DM area reaches normalize_contract() through the OpenAI adapter."""
    from app.dm.adjudication import adjudicate_with_failover

    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    envelope = {
        "id": "resp_dm",
        "model": "gpt-6-luna",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": json.dumps({
                "contract_version": CONTRACT_VERSION, "mode": "silent",
                "reason": "openai envelope test", "beats": [],
            })},
            "finish_reason": "stop",
        }],
        "usage": {},
    }

    def _fake_execute(adapter, request, **kwargs):
        assert adapter.name == "openai"
        payload = adapter.build_payload(request)
        assert payload["reasoning_effort"] == "low"
        assert "temperature" not in payload
        assert payload["response_format"]["json_schema"]["strict"] is True
        return adapter.parse_response(envelope)

    monkeypatch.setattr("app.dm.adjudication.execute_chat", _fake_execute)
    contract, _ = adjudicate_with_failover(_StubPacket())
    assert contract.mode == "silent"

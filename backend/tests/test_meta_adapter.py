"""Meta (Llama API) native envelope normalization + DM end-to-end.

Covers the review finding that Meta returns ``completion_message``
(not OpenAI ``choices``): adapter parsing, native payload params,
native SSE streaming, and a Meta-shaped response reaching
``normalize_contract()`` through ``adjudicate_with_provider``.
"""

import json

import pytest

from app.dm.contract import CONTRACT_VERSION
from app.providers.adapters.meta import MetaAdapter
from app.providers.contracts import ProviderError

TEXT_PAYLOAD = {
    "id": "resp_123",
    "completion_message": {
        "content": {"type": "text", "text": "The gate stands open."},
        "role": "assistant",
        "stop_reason": "stop",
        "tool_calls": [],
    },
    "metrics": [
        {"metric": "num_completion_tokens", "value": 25, "unit": "tokens"},
        {"metric": "num_prompt_tokens", "value": 25, "unit": "tokens"},
        {"metric": "num_total_tokens", "value": 50, "unit": "tokens"},
    ],
}

TOOL_PAYLOAD = {
    "id": "resp_456",
    "completion_message": {
        "content": {"type": "text", "text": ""},
        "role": "assistant",
        "stop_reason": "tool_calls",
        "tool_calls": [
            {
                "id": "466d49b7-8641-43bd-844e-ecac6a818974",
                "function": {"name": "get_weather", "arguments": '{"location":"Menlo Park"}'},
            }
        ],
    },
}


def test_parse_text_response():
    res = MetaAdapter().parse_response(TEXT_PAYLOAD)
    assert res.provider == "meta"
    assert res.content == "The gate stands open."
    assert res.finish_reason == "stop"
    assert res.tool_calls == []
    assert res.usage["metrics"][0]["metric"] == "num_completion_tokens"


def test_parse_tool_calls_response():
    res = MetaAdapter().parse_response(TOOL_PAYLOAD)
    assert res.content == ""
    assert res.finish_reason == "tool_calls"
    assert len(res.tool_calls) == 1
    assert res.tool_calls[0].name == "get_weather"
    assert json.loads(res.tool_calls[0].arguments)["location"] == "Menlo Park"


def test_parse_string_content():
    payload = {"completion_message": {"content": "plain", "stop_reason": "stop"}}
    assert MetaAdapter().parse_response(payload).content == "plain"


def test_parse_rejects_openai_envelope():
    with pytest.raises(ProviderError, match="without completion_message"):
        MetaAdapter().parse_response({"choices": [{"message": {"content": "hi"}}]})


def test_build_payload_uses_native_params():
    from app.providers.contracts import ProviderRequest

    payload = MetaAdapter().build_payload(ProviderRequest(
        messages=[{"role": "user", "content": "hi"}],
        model="Llama-4-Maverick-17B-128E-Instruct-FP8",
        max_tokens=512,
        stream=True,
    ))
    assert payload["max_completion_tokens"] == 512
    assert "max_tokens" not in payload
    assert payload["stream"] is True
    assert "parallel_tool_calls" not in payload
    assert "thinking" not in payload


class _FakeStream:
    def __init__(self, lines):
        self._lines = lines

    def iter_lines(self, decode_unicode=False):
        return iter(self._lines)


def test_iter_stream_events_native_sse():
    lines = [
        'data: {"id":"r1","event":{"event_type":"progress","delta":{"text":"The "}}}',
        'data: {"id":"r1","event":{"event_type":"progress","delta":{"text":"gate"}}}',
        'data: {"id":"r1","event":{"event_type":"complete"}}',
    ]
    events = list(MetaAdapter().iter_stream_events(_FakeStream(lines)))
    assert [e.text for e in events if e.kind == "token"] == ["The ", "gate"]
    assert events[-1].kind == "done"


class _StubPacket:
    def serialize_for_adjudication(self):
        return "{}"


def test_dm_contract_via_meta_envelope(monkeypatch):
    import app.providers as providers_pkg
    from app.dm.adjudication import adjudicate_with_provider

    monkeypatch.setenv("META_API_KEY", "dummy")
    contract_json = json.dumps({
        "contract_version": CONTRACT_VERSION,
        "mode": "silent",
        "reason": "meta envelope test",
        "beats": [],
    })
    envelope = {
        "id": "resp_dm",
        "completion_message": {
            "content": {"type": "text", "text": contract_json},
            "role": "assistant",
            "stop_reason": "stop",
            "tool_calls": [],
        },
    }

    def _fake_execute(adapter, request, **kwargs):
        assert adapter.name == "meta"
        assert request.json_schema is not None
        return MetaAdapter().parse_response(envelope)

    monkeypatch.setattr(providers_pkg, "execute_chat", _fake_execute)
    contract = adjudicate_with_provider(_StubPacket())
    assert contract.mode == "silent"

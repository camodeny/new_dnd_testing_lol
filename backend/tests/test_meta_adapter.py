"""Meta Model API (OpenAI-compatible) adapter + DM end-to-end.

Meta serves Muse Spark from ``https://api.meta.ai/v1`` with standard
``choices`` envelopes, so these tests pin the ``choices[0].message`` /
``choices[].delta`` shapes and the ``MODEL_API_KEY`` credential, plus a
Meta-routed response reaching ``normalize_contract()`` through
``adjudicate_with_provider``.
"""

import json

import pytest

from app.dm.contract import CONTRACT_VERSION
from app.providers.adapters.meta import MetaAdapter

TEXT_PAYLOAD = {
    "id": "resp_123",
    "model": "muse-spark-1.3",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "The gate stands open.",
            },
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 25, "completion_tokens": 25, "total_tokens": 50},
}

TOOL_PAYLOAD = {
    "id": "resp_456",
    "model": "muse-spark-1.3",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "inspect_scene",
                            "arguments": '{"area":"gate"}',
                        },
                    }
                ],
            },
            "finish_reason": "tool_calls",
        }
    ],
    "usage": {},
}


def test_effective_url_and_key_aliases(monkeypatch):
    adapter = MetaAdapter()
    assert adapter.base_url() == "https://api.meta.ai/v1/chat/completions"
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("META_API_KEY", raising=False)
    monkeypatch.delenv("LLAMA_API_KEY", raising=False)
    monkeypatch.setenv("MODEL_API_KEY", "m-key")
    assert adapter.api_key() == "m-key"
    monkeypatch.delenv("MODEL_API_KEY")
    monkeypatch.setenv("META_API_KEY", "meta-key")
    assert adapter.api_key() == "meta-key"


def test_parse_text_response():
    res = MetaAdapter().parse_response(TEXT_PAYLOAD)
    assert res.provider == "meta"
    assert res.content == "The gate stands open."
    assert res.finish_reason == "stop"
    assert res.tool_calls == []
    assert res.usage["total_tokens"] == 50


def test_parse_tool_calls_response():
    res = MetaAdapter().parse_response(TOOL_PAYLOAD)
    assert res.finish_reason == "tool_calls"
    assert len(res.tool_calls) == 1
    assert res.tool_calls[0].name == "inspect_scene"
    assert json.loads(res.tool_calls[0].arguments)["area"] == "gate"


def test_build_payload_keeps_openai_shape():
    from app.providers.contracts import ProviderRequest

    payload = MetaAdapter().build_payload(ProviderRequest(
        messages=[{"role": "user", "content": "hi"}],
        model="muse-spark-1.3",
        max_tokens=512,
        stream=True,
    ))
    assert payload["max_tokens"] == 512
    assert payload["messages"] == [{"role": "user", "content": "hi"}]
    assert payload["stream"] is True


class _FakeStream:
    def __init__(self, lines):
        self._lines = lines

    def iter_lines(self, decode_unicode=False):
        return iter(self._lines)


def test_iter_stream_events_openai_sse():
    lines = [
        'data: {"choices":[{"delta":{"role":"assistant"},"index":0}]}',
        'data: {"choices":[{"delta":{"content":"The "},"index":0}]}',
        'data: {"choices":[{"delta":{"content":"gate"},"index":0}]}',
        'data: [DONE]',
    ]
    events = list(MetaAdapter().iter_stream_events(_FakeStream(lines)))
    assert [e.text for e in events if e.kind == "token"] == ["The ", "gate"]
    assert events[-1].kind == "done"


def test_dm_area_uses_direct_model_api_pair(monkeypatch):
    """Provider contract: DM resolves to the exact direct-API URL+model."""
    from app.providers.areas import resolve_area

    monkeypatch.setenv("MODEL_API_KEY", "dummy")
    adapter, model, name = resolve_area("dm")
    assert name == "meta"
    assert adapter.base_url() == "https://api.meta.ai/v1/chat/completions"
    assert model == "muse-spark-1.3"
    # Gateway aliases must not silently re-enter this path.
    assert "contributor" not in model
    assert "llama.com" not in adapter.base_url()
    assert "opencode" not in adapter.base_url()


class _StubPacket:
    def serialize_for_adjudication(self):
        return "{}"


def test_dm_contract_via_meta_choices_envelope(monkeypatch):
    import app.providers as providers_pkg
    from app.dm.adjudication import adjudicate_with_provider

    monkeypatch.setenv("MODEL_API_KEY", "dummy")
    contract_json = json.dumps({
        "contract_version": CONTRACT_VERSION,
        "mode": "silent",
        "reason": "meta envelope test",
        "beats": [],
    })
    envelope = {
        "id": "resp_dm",
        "model": "muse-spark-1.3",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": contract_json},
                "finish_reason": "stop",
            }
        ],
        "usage": {},
    }

    def _fake_execute(adapter, request, **kwargs):
        assert adapter.name == "meta"
        assert request.json_schema is not None
        return MetaAdapter().parse_response(envelope)

    monkeypatch.setattr(providers_pkg, "execute_chat", _fake_execute)
    contract = adjudicate_with_provider(_StubPacket())
    assert contract.mode == "silent"

"""Per-call-area provider/model pins."""

import pytest

from app.providers.areas import AREAS, resolve_area


@pytest.fixture
def keys(monkeypatch):
    for key in (
        "OPENAI_API_KEY", "META_API_KEY", "LLAMA_API_KEY", "MODEL_API_KEY",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    monkeypatch.setenv("META_API_KEY", "dummy")
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy")


def test_dm_pinned_to_meta(keys):
    adapter, model, name = resolve_area("dm")
    assert name == "meta"
    assert model == "muse-spark-1.3"
    assert adapter.name == "meta"


def test_narrator_pinned_to_openai(keys):
    adapter, model, name = resolve_area("narrator")
    assert name == "openai"
    assert model == "gpt-5.6-luna"
    assert adapter.name == "openai"


def test_character_chat_pinned(keys):
    _, model, name = resolve_area("character_chat")
    assert name == "openai"
    assert model == "gpt-4o-mini"


def test_areas_known(keys):
    assert set(AREAS) == {"dm", "narrator", "character_chat"}


def test_missing_key_fails_clearly(keys, monkeypatch):
    monkeypatch.delenv("META_API_KEY")
    with pytest.raises(RuntimeError, match="API_KEY"):
        resolve_area("dm")


def test_unknown_area(keys):
    with pytest.raises(RuntimeError, match="Unknown provider area"):
        resolve_area("bogus")

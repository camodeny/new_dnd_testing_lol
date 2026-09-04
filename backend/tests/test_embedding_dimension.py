"""Issue #334 — one indexed embedding dimension (1536).

Doc embeddings, query embeddings, and the vector(1536) column + mandatory
HNSW index must all agree. Wrong-sized vectors fail closed. No paid model
calls: Gemini HTTP is monkeypatched.
"""

import pytest

from sqlalchemy import create_engine
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from database import Base  # noqa: E402
from app.rules import gemini as gemini_mod  # noqa: E402
from app.rules.embeddings import EMBEDDING_DIM as EMB_DIM, build_embeddings  # noqa: E402
from app.rules.store import search_vector  # noqa: E402
from app.rules.ingest import import_fixture_sections  # noqa: E402

TEST_OFFICIAL_HASH = "8974902d109d6e63672d7c490bde9ccf052410503d9cfa768237154fbc5e3d87"

SECTIONS = [
    {
        "document": "playing-the-game",
        "heading_path": ["Combat", "Attack Rolls"],
        "title": "Attack Rolls",
        "body": "When you make an attack, roll a d20 and add modifiers.",
        "structured_tables": None,
    }
]


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    s = Session()
    yield s
    s.close()
    engine.dispose()


def test_single_indexed_dimension_is_1536():
    assert EMB_DIM == 1536
    assert gemini_mod.EMBEDDING_DIM == 1536
    assert EMB_DIM <= 2000


def test_gemini_defaults_to_indexed_dimension(monkeypatch):
    for var in ("GEMINI_EMBEDDING_DIM", "GEMINI_OUTPUT_DIMENSIONALITY"):
        monkeypatch.delenv(var, raising=False)
    assert gemini_mod._output_dim() == 1536


def test_gemini_env_override_must_match_indexed_dimension(monkeypatch):
    monkeypatch.setenv("GEMINI_EMBEDDING_DIM", "768")
    with pytest.raises(ValueError, match="fixed at 1536"):
        gemini_mod._output_dim()
    monkeypatch.setenv("GEMINI_EMBEDDING_DIM", "1536")
    assert gemini_mod._output_dim() == 1536


def test_gemini_explicit_dim_must_match_indexed_dimension(monkeypatch):
    for var in ("GEMINI_EMBEDDING_DIM", "GEMINI_OUTPUT_DIMENSIONALITY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    class _Resp:
        status_code = 200
        request = None

        def raise_for_status(self):
            return None

        def json(self):
            return {"embeddings": [{"values": [0.1] * 1536}]}

    monkeypatch.setattr(gemini_mod.httpx, "post", lambda *a, **k: _Resp())
    vecs = gemini_mod.gemini_embed_texts(["hello"])
    assert len(vecs[0]) == 1536
    with pytest.raises(ValueError, match="fixed at 1536"):
        gemini_mod.gemini_embed_texts(["hello"], output_dimensionality=768)


def test_gemini_rejects_wrong_sized_response(monkeypatch):
    for var in ("GEMINI_EMBEDDING_DIM", "GEMINI_OUTPUT_DIMENSIONALITY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    class _Resp:
        status_code = 200
        request = None

        def raise_for_status(self):
            return None

        def json(self):
            return {"embeddings": [{"values": [0.1, 0.2, 0.3]}]}

    monkeypatch.setattr(gemini_mod.httpx, "post", lambda *a, **k: _Resp())
    with pytest.raises(ValueError, match="expected 1536"):
        gemini_mod.gemini_embed_texts(["hello"])


def test_build_embeddings_rejects_wrong_sized_provider_vectors(db):
    import_fixture_sections(db, SECTIONS, source_artifact_hash=TEST_OFFICIAL_HASH, validate_canaries=False)

    def bad_provider(texts):
        return [[0.1, 0.2, 0.3] for _ in texts]

    with pytest.raises(ValueError, match="expected 1536"):
        build_embeddings(db, embedding_model="stub-hash-v1", build_id="bad-dim", provider=bad_provider)


def test_build_embeddings_rejects_nonstandard_dim_param(db):
    import_fixture_sections(db, SECTIONS, source_artifact_hash=TEST_OFFICIAL_HASH, validate_canaries=False)
    with pytest.raises(ValueError, match="fixed"):
        build_embeddings(db, embedding_model="stub-hash-v1", build_id="bad-dim", dim=768)


def test_search_vector_rejects_wrong_sized_query(db):
    with pytest.raises(ValueError, match="expected 1536"):
        search_vector(db, [0.1, 0.2, 0.3])
    assert search_vector(db, None) == []
    assert search_vector(db, []) == []

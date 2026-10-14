"""pgvector SQL must bind the query vector as a real parameter.

SQLAlchemy ``text()`` does not treat ``:vec`` as a bind when it is followed by
``::vector``, so that spelling sends a literal ``:vec`` to Postgres and every
vector query/write fails. Unit tests run on SQLite and never reach these
branches, so capture the statements and check the binds directly.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.rules_corpus import store
from app.rules_corpus.gemini import EMBEDDING_DIM
from app.world import semantic, semantic_index


def _bind_names(clause) -> set[str]:
    return set(clause._bindparams)


def test_rules_search_vector_binds_vec():
    db = MagicMock()
    db.bind.dialect.name = "postgresql"
    db.execute.return_value.fetchone.return_value = (1,)
    db.execute.return_value.fetchall.return_value = []

    store.search_vector(db, [0.1] * EMBEDDING_DIM, embedding_model="m")

    query = db.execute.call_args_list[-1].args[0]
    assert "rules_embeddings" in str(query)
    assert {"vec", "lim", "model"} <= _bind_names(query)


def test_world_pgvector_candidates_binds_vec(monkeypatch):
    monkeypatch.setattr(semantic, "embedding_column_is_vector", lambda _db: True)
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = []

    result = semantic._pgvector_candidates(
        db, SimpleNamespace(id=uuid.uuid4()), [0.1, 0.2],
        embedding_model="m", embedding_version="v1", limit=5,
    )

    assert result == []
    query = db.execute.call_args.args[0]
    assert {"vec", "cid", "model", "ver", "lim"} <= _bind_names(query)


def test_world_store_native_vector_binds_vec():
    db = MagicMock()

    semantic_index._store_native_vector(db, uuid.uuid4(), [0.1, 0.2])

    query = db.execute.call_args.args[0]
    assert {"vec", "rid"} <= _bind_names(query)

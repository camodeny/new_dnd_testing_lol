"""Issue #197 — durable DM stream chunk persistence invariants (service level)."""
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from database import Base  # noqa: E402
import models  # noqa: E402, F401
from app.dm.streams import (  # noqa: E402
    DMStreamConflictError,
    DMStreamStateError,
    append_chunk,
    complete_stream,
    create_stream,
    fail_stream,
    get_stream_with_chunks,
)
from app.threads.service import get_or_create_campaign_thread  # noqa: E402
from models.campaigns import Campaign  # noqa: E402
from models.profiles import Profile  # noqa: E402


@pytest.fixture
def ctx():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    owner_id, campaign_id = uuid.uuid4(), uuid.uuid4()
    with factory() as db:
        db.add_all([
            Profile(id=owner_id, email="owner@example.com"),
            Campaign(id=campaign_id, owner_id=owner_id, name="Table", revision=0),
        ])
        db.commit()
        thread = get_or_create_campaign_thread(db, campaign_id, created_by=owner_id)
        db.commit()
        thread_id = thread.id
    db = factory()
    try:
        yield db, campaign_id, thread_id
    finally:
        db.close()


def _stream(db, campaign_id, thread_id, key="t"):
    return create_stream(db, campaign_id=campaign_id, thread_id=thread_id,
                         turn_id=f"turn-{key}", attempt_id=f"att-{key}")


def test_duplicate_create_returns_same_stream(ctx):
    db, cid, tid = ctx
    first = _stream(db, cid, tid)
    db.commit()
    again = _stream(db, cid, tid)
    assert again.id == first.id


def test_ordered_idempotent_append_rejects_conflicts(ctx):
    db, cid, tid = ctx
    stream = _stream(db, cid, tid)
    append_chunk(db, stream.id, 0, "Hello ")
    append_chunk(db, stream.id, 1, "world")
    append_chunk(db, stream.id, 1, "world")  # idempotent duplicate
    with pytest.raises(DMStreamConflictError):
        append_chunk(db, stream.id, 1, "WORLD")
    with pytest.raises(DMStreamConflictError):
        append_chunk(db, stream.id, 3, "!")
    row, chunks, text = get_stream_with_chunks(db, stream.id)
    assert [c.sequence for c in chunks] == [0, 1]
    assert text == "Hello world"
    assert row.chunk_count == 2
    assert row.total_bytes == len("Hello world".encode())
    assert row.first_chunk_at is not None


def test_completed_stream_rejects_append_and_failure(ctx):
    db, cid, tid = ctx
    stream = _stream(db, cid, tid)
    append_chunk(db, stream.id, 0, "Done.")
    complete_stream(db, stream.id)
    with pytest.raises(DMStreamStateError):
        append_chunk(db, stream.id, 1, "more")
    with pytest.raises(DMStreamStateError):
        fail_stream(db, stream.id)


def test_failed_stream_retains_partial_chunks_for_audit(ctx):
    db, cid, tid = ctx
    stream = _stream(db, cid, tid)
    append_chunk(db, stream.id, 0, "Partial")
    failed = fail_stream(db, stream.id, reason="provider_error")
    assert failed.status == "failed"
    assert failed.final_text is None
    _, chunks, text = get_stream_with_chunks(db, stream.id)
    assert text == "Partial" and len(chunks) == 1
    with pytest.raises(DMStreamStateError):
        append_chunk(db, stream.id, 1, "more")

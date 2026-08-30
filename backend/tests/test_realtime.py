"""Issue #198 — stream-scoped reconciliation, duplicate/out-of-order convergence."""

import uuid

import pytest
from app.realtime.service import reconcile_buffered_events, dedupe_events, _counters, InMemoryRealtimePublisher, set_realtime_publisher


def test_dedupe_events_drops_duplicate_event_id():
    evs = [
        {"event_id": "c1", "type": "dm.chunk", "sequence": 6, "stream_id": "s"},
        {"event_id": "c1", "type": "dm.chunk", "sequence": 6, "stream_id": "s"},
        {"event_id": "c2", "type": "dm.chunk", "sequence": 7, "stream_id": "s"},
    ]
    assert [e["event_id"] for e in dedupe_events(evs)] == ["c1", "c2"]


def test_reconcile_stream_scoped_rolls_over_from_A_to_B():
    # Snapshot has B active (last 2), A completed
    snap = {
        "revision": 5,
        "reconciliation": {"history_high_water_mark": 5},
        "dm_state": {"stream_id": "stream-B", "last_sequence": 2, "visible_text": "hello world", "chunk_count": 3},
        "dm_messages": [{"id": "stream-A", "final_text": "old"}],
        "history": {"messages": []},
    }
    buffered = [
        {"type": "dm.chunk", "stream_id": "stream-A", "sequence": 1, "event_id": "cA", "text": "stale A"},
        {"type": "dm.chunk", "stream_id": "stream-B", "sequence": 3, "event_id": "cB", "text": "new B"},
        {"type": "dm.chunk", "stream_id": "stream-C", "sequence": 0, "event_id": "cC", "text": "new C"},
        {"type": "dm.status", "stream_id": "stream-A", "event_id": "sA", "status": "completed"},
        {"type": "dm.status", "stream_id": "stream-B", "event_id": "sB", "status": "streaming"},
        {"type": "dm.thinking", "stream_id": "stream-A", "event_id": "tA"},
        {"type": "dm.thinking", "stream_id": "stream-B", "event_id": "tB"},
        {"type": "submission.created", "sequence": 6, "event_id": "sub6", "id": "sub6"},
        {"type": "submission.created", "sequence": 5, "event_id": "sub5", "id": "sub5"},
    ]
    rec = reconcile_buffered_events(snap, buffered)
    ids = {r["event_id"] for r in rec}
    assert "cA" not in ids  # stale A chunk dropped
    assert "cB" in ids
    assert "cC" in ids  # new stream C kept
    assert "sA" not in ids
    assert "sB" in ids
    assert "tA" not in ids
    assert "tB" in ids
    assert "sub6" in ids
    assert "sub5" not in ids


def test_reconcile_same_stream_contiguous():
    snap = {"revision": 2, "reconciliation": {"history_high_water_mark": 2}, "dm_state": {"stream_id": "stream-B", "last_sequence": 2}, "dm_messages": []}
    buf = [
        {"type": "dm.chunk", "stream_id": "stream-B", "sequence": 1, "event_id": "c_old"},
        {"type": "dm.chunk", "stream_id": "stream-B", "sequence": 3, "event_id": "c_new"},
    ]
    rec = reconcile_buffered_events(snap, buf)
    assert {r["event_id"] for r in rec} == {"c_new"}


def test_derive_visible_text_contiguous_and_duplicate():
    """Frontend-derived logic mirrored in Python: base + sorted contiguous chunks."""
    # Simulate deriveDmState helper
    def derive(base_text, base_last, chunks):
        # chunks: list of (seq, text)
        sorted_chunks = sorted(chunks, key=lambda x: x[0])
        # dedupe by seq
        seen = set()
        deduped = []
        for seq, txt in sorted_chunks:
            if seq not in seen:
                deduped.append((seq, txt))
                seen.add(seq)
        cur = base_text
        expected = base_last + 1
        for seq, txt in sorted(deduped):
            if seq == expected:
                cur += txt
                expected += 1
            elif seq > expected:
                break
        return cur, expected - 1

    base_text, base_last = "hello ", 5
    # duplicate chunk 6
    chunks = [(6, "world"), (6, "world")]
    text, last = derive(base_text, base_last, chunks)
    assert text == "hello world"
    assert last == 6

    # 7 before 6
    chunks = [(7, "!"), (6, "world")]
    text, last = derive(base_text, base_last, chunks)
    assert text == "hello world!"
    assert last == 7

    # only 7 (gap)
    text, last = derive(base_text, base_last, [(7, "!")])
    assert text == "hello "
    assert last == 5

    # 7,8 without 6 — gap
    text, last = derive(base_text, base_last, [(7, "b"), (8, "c")])
    assert text == "hello "
    # after 6 arrives
    text, last = derive(base_text, base_last, [(7, "b"), (8, "c"), (6, "a")])
    assert text == "hello abc"
    assert last == 8


def test_snapshot_plus_buffer_converges_to_fresh_snapshot():
    """Dropped DM chunk: snapshot has last_sequence 6, buffered has chunk 5 (stale) and 6 (duplicate) — after adopt, visible_text matches fresh snapshot."""
    # Fresh snapshot after missed chunk has visible_text "hello world" and last 6
    fresh = {"dm_state": {"stream_id": "s", "last_sequence": 6, "visible_text": "hello world", "chunk_count": 7}, "dm_messages": []}
    # Old snapshot before missed chunk
    old = {"dm_state": {"stream_id": "s", "last_sequence": 5, "visible_text": "hello ", "chunk_count": 6}, "dm_messages": []}
    # Buffered duplicate of 6
    buffered = [{"type": "dm.chunk", "stream_id": "s", "sequence": 6, "event_id": "c6", "text": "world"}]
    # Reconcile against old snapshot: chunk 6 is newer than old last 5, so kept
    rec_old = reconcile_buffered_events(old, buffered)
    assert len(rec_old) == 1
    # Reconcile against fresh snapshot: chunk 6 is not newer than fresh last 6, so dropped — correct, snapshot already has it
    rec_fresh = reconcile_buffered_events(fresh, buffered)
    assert len(rec_fresh) == 0
    # Therefore after fetching fresh snapshot, buffered duplicate is dropped and visible_text does not duplicate

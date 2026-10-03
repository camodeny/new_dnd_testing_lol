"""Private Realtime channel naming — issue #198 (subscribe authorization: realtime/router.py).

Invariant: private thread existence + contents are never broadcast to a shared
channel expecting clients to filter. Each thread maps to its own private channel
that only explicit members can subscribe to. Channel name is deterministic and
stable for dedupe/reconciliation.
"""

from __future__ import annotations

import uuid


def live_table_channel(campaign_id: uuid.UUID | str, thread_id: uuid.UUID | str) -> str:
    """Deterministic private channel for a live-table thread.

    Format: ``live-table:campaign:<campaign_id>:thread:<thread_id>``

    - ``campaign_id`` and ``thread_id`` are lower-cased UUID strings.
    - Never embeds secret content; authorization is enforced server-side.
    - Stable so clients can dedupe/reconcile by channel + event id.
    - Fail closed: malformed identifiers raise ``ValueError`` rather than
      emitting a channel name that can never round-trip through
      :func:`parse_live_table_channel`.
    """
    cid = uuid.UUID(str(campaign_id))
    tid = uuid.UUID(str(thread_id))
    return f"live-table:campaign:{cid}:thread:{tid}"


def parse_live_table_channel(channel: str) -> tuple[uuid.UUID, uuid.UUID] | None:
    """Parse a live-table channel back to (campaign_id, thread_id) or None if not matching."""
    # expected: live-table:campaign:<uuid>:thread:<uuid>
    try:
        parts = channel.split(":")
        # ["live-table","campaign","<cid>","thread","<tid>"]
        if len(parts) != 5:
            return None
        if parts[0] != "live-table" or parts[1] != "campaign" or parts[3] != "thread":
            return None
        cid = uuid.UUID(parts[2])
        tid = uuid.UUID(parts[4])
        return cid, tid
    except (ValueError, AttributeError, TypeError):
        return None


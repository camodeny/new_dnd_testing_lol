"""``search_stat_blocks`` evidence tool: find an SRD block that fits the fiction.

The DM describes the creature's nature ("silt water ooze", "veteran soldier");
code returns only blocks this party may face at the campaign difficulty,
ranked by overlap with each block's profile (type, defenses, attacks), with a
one-line descriptor each. A query naming a creature type narrows to it. The
DM then stages ``assign_stat_block`` with one of the returned ids.
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any

from app.characters.service import roster_levels
from app.dm.context import AuthorizationScope, ContextAudience, SourceRef
from app.dm.contract import EvidenceRequest
from app.dm.evidence import EvidenceResult
from app.rules.bestiary import CREATURE_TYPES, describe, encounter_xp_budget, search_blocks
from models.campaigns import Campaign


def handle_search_stat_blocks(request: EvidenceRequest, audience: ContextAudience, *, db: Any = None) -> EvidenceResult:
    t0 = time.monotonic()
    query = (request.query or "").strip()
    campaign_id = uuid.UUID(str(audience.campaign_id))
    campaign = db.get(Campaign, campaign_id) if db is not None else None
    levels = roster_levels(db, campaign_id) if db is not None else []
    difficulty = getattr(campaign, "difficulty", "medium") or "medium"
    budget = encounter_xp_budget(levels or [1], difficulty)
    words = set(re.findall(r"[a-z]+", query.lower()))
    kind = next((k for k in CREATURE_TYPES if k in words or f"{k}s" in words), None)
    blocks = search_blocks(query, max_xp=budget, kind=kind, limit=request.limit or 8)
    return EvidenceResult(
        request_id=request.id,
        tool=request.tool,
        status="ok" if blocks else "missing",
        sources=[SourceRef(source_type="srd_stat_block", source_id=b["id"], source_version="5.2.1") for b in blocks],
        visibility="dm_only",
        authorization=AuthorizationScope(campaign_id=audience.campaign_id, thread_ids=[audience.thread_id]),
        payload={
            "encounter_xp_budget": budget,
            "difficulty": difficulty,
            "creature_type_filter": kind,
            "blocks": [describe(b) for b in blocks],
        },
        result_count=len(blocks),
        latency_ms=(time.monotonic() - t0) * 1000,
    )

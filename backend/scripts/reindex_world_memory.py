"""Queue every live world record for semantic indexing under the current model.

Writers only enqueue records they touch, so records written before an
embedding model change (e.g. stub-hash-v1 → gemini-embedding-2 once
GEMINI_API_KEY is configured) are never re-embedded. This stages a ``stale``
placeholder per record; the post-turn cron sweep embeds them, or pass
``--drain`` to embed in-process now.

    python -m scripts.reindex_world_memory [--campaign ID] [--drain]
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import argparse
import uuid

from sqlalchemy import select

from database import SessionLocal
from app.world.semantic_index import (
    request_semantic_index,
    resolve_embedding_model,
    run_semantic_index_sweep,
)
from models.campaigns import Campaign, CampaignDomainEvent
from models.dm import DmTurn
from models.world import CampaignCurrentScene, WorldEntity, WorldFact, WorldRelation


def _sources(db, campaign_id: uuid.UUID) -> list[tuple[str, uuid.UUID]]:
    queries = [
        ("world_entity", select(WorldEntity.id).where(
            WorldEntity.campaign_id == campaign_id, WorldEntity.superseded_by_id.is_(None))),
        ("world_fact", select(WorldFact.id).where(
            WorldFact.campaign_id == campaign_id, WorldFact.status == "active")),
        ("world_relation", select(WorldRelation.id).where(
            WorldRelation.campaign_id == campaign_id, WorldRelation.status == "active")),
        ("domain_event", select(CampaignDomainEvent.id).where(
            CampaignDomainEvent.campaign_id == campaign_id)),
        ("source_turn", select(DmTurn.id).where(
            DmTurn.campaign_id == campaign_id, DmTurn.status == "succeeded")),
        ("scene", select(CampaignCurrentScene.campaign_id).where(
            CampaignCurrentScene.campaign_id == campaign_id)),
    ]
    return [(stype, source_id) for stype, query in queries
            for source_id in db.execute(query).scalars().all()]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--campaign", default=None, help="only this campaign id")
    parser.add_argument("--drain", action="store_true", help="embed now instead of via cron")
    args = parser.parse_args()
    model = resolve_embedding_model()
    db = SessionLocal()
    try:
        if args.campaign:
            campaign_ids = [uuid.UUID(args.campaign)]
        else:
            campaign_ids = list(db.execute(select(Campaign.id)).scalars().all())
        queued = 0
        for campaign_id in campaign_ids:
            for stype, source_id in _sources(db, campaign_id):
                if request_semantic_index(db, campaign_id, stype, source_id) is not None:
                    queued += 1
        print(f"model={model} campaigns={len(campaign_ids)} queued={queued}")
        if args.drain:
            indexed = failed = 0
            while True:
                result = run_semantic_index_sweep(db, limit=100)
                indexed += len(result["indexed"])
                failed += len(result["failed"])
                if not result["indexed"]:
                    break
            print(f"indexed={indexed} failed={failed}")
    finally:
        db.close()


if __name__ == "__main__":
    main()

"""Keyword (BM25) candidates over authoritative world records.

The keyword half of world-memory search: it needs no embedding provider or
index build, so recall survives a missing Gemini key, an unindexed record,
or a provider outage. Candidates are ranking hints only — callers resolve
and authorize every hit through the same gates as the vector path.

Index text comes from :func:`app.world.semantic_index.build_source_text`
(plus entity aliases), so keyword and vector search read the same words.
Each source type loads in one bounded query; nothing is persisted.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.rules_corpus.bm25 import Bm25Index
from app.world.semantic_index import build_source_text
from models.campaigns import CampaignDomainEvent
from models.dm import DmTurn
from models.world import (
    SEMANTIC_SOURCE_TYPES,
    WorldEntity,
    WorldEntityAlias,
    WorldFact,
    WorldRelation,
)

#: Most recent records per source type considered by one keyword search.
LEXICAL_MAX_RECORDS_PER_TYPE = 1000


def _recent(db: Session, model: Any, *conditions: Any, order_by: Any) -> list[Any]:
    return list(db.execute(
        select(model).where(*conditions).order_by(order_by.desc())
        .limit(LEXICAL_MAX_RECORDS_PER_TYPE)
    ).scalars().all())


def _load_records(
    db: Session, campaign_id: uuid.UUID, source_types: frozenset[str],
) -> list[tuple[str, Any]]:
    records: list[tuple[str, Any]] = []
    if "world_entity" in source_types:
        records += [("world_entity", row) for row in _recent(
            db, WorldEntity, WorldEntity.campaign_id == campaign_id,
            WorldEntity.superseded_by_id.is_(None), order_by=WorldEntity.updated_at)]
    if "world_fact" in source_types:
        records += [("world_fact", row) for row in _recent(
            db, WorldFact, WorldFact.campaign_id == campaign_id,
            WorldFact.status == "active", order_by=WorldFact.updated_at)]
    if "world_relation" in source_types:
        records += [("world_relation", row) for row in _recent(
            db, WorldRelation, WorldRelation.campaign_id == campaign_id,
            WorldRelation.status == "active", order_by=WorldRelation.updated_at)]
    if "domain_event" in source_types:
        records += [("domain_event", row) for row in _recent(
            db, CampaignDomainEvent, CampaignDomainEvent.campaign_id == campaign_id,
            order_by=CampaignDomainEvent.sequence)]
    if "source_turn" in source_types:
        records += [("source_turn", row) for row in _recent(
            db, DmTurn, DmTurn.campaign_id == campaign_id,
            DmTurn.status == "succeeded", order_by=DmTurn.created_at)]
    if "scene" in source_types:
        from models.world import CampaignCurrentScene

        scene = db.get(CampaignCurrentScene, campaign_id)
        if scene is not None:
            records.append(("scene", scene))
    return records


def _entity_names(db: Session, campaign_id: uuid.UUID) -> dict[str, str]:
    rows = db.execute(
        select(WorldEntity.id, WorldEntity.name)
        .where(WorldEntity.campaign_id == campaign_id)
    ).all()
    return {str(entity_id): str(name) for entity_id, name in rows}


def _entity_aliases(db: Session, campaign_id: uuid.UUID) -> dict[str, list[str]]:
    aliases: dict[str, list[str]] = {}
    for entity_id, alias in db.execute(
        select(WorldEntityAlias.entity_id, WorldEntityAlias.alias)
        .where(WorldEntityAlias.campaign_id == campaign_id)
    ).all():
        aliases.setdefault(str(entity_id), []).append(str(alias))
    return aliases


def _submission_texts(
    db: Session, campaign_id: uuid.UUID, turns: list[Any],
) -> dict[str, str]:
    from models.threads import PlayerSubmission

    ids: list[uuid.UUID] = []
    for turn in turns:
        for raw in list(getattr(turn, "submission_ids", None) or []):
            try:
                ids.append(raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw)))
            except (ValueError, AttributeError, TypeError):
                continue
    if not ids:
        return {}
    rows = db.execute(
        select(PlayerSubmission.id, PlayerSubmission.raw_content).where(
            PlayerSubmission.campaign_id == campaign_id,
            PlayerSubmission.id.in_(ids),
        )
    ).all()
    return {str(sid): str(content or "") for sid, content in rows}


def lexical_candidates(
    db: Session,
    campaign_id: uuid.UUID,
    query_text: str,
    *,
    source_types: frozenset[str] | None = None,
    limit: int = 20,
) -> list[tuple[str, uuid.UUID, float]]:
    """BM25-ranked ``(source_type, source_id, score)`` over live records.

    Unauthorized: the caller must resolve and gate every candidate.
    """
    types = frozenset(source_types or SEMANTIC_SOURCE_TYPES) & SEMANTIC_SOURCE_TYPES
    records = _load_records(db, campaign_id, types)
    if not records:
        return []
    names = _entity_names(db, campaign_id) if types & {"world_fact", "world_relation"} else {}
    aliases = _entity_aliases(db, campaign_id) if "world_entity" in types else {}
    turns = [record for stype, record in records if stype == "source_turn"]
    submissions = _submission_texts(db, campaign_id, turns) if turns else {}
    docs: dict[str, str] = {}
    keys: dict[str, tuple[str, uuid.UUID]] = {}
    for stype, record in records:
        text = build_source_text(
            db, stype, record, entity_names=names, submission_texts=submissions)
        if stype == "world_entity":
            text = "\n".join([text, *aliases.get(str(record.id), [])])
        source_id = record.campaign_id if stype == "scene" else record.id
        key = f"{stype}:{source_id}"
        docs[key] = text
        keys[key] = (stype, source_id)
    ranked = Bm25Index.from_texts(docs).rank_scored(query_text, limit)
    return [(*keys[key], score) for key, score in ranked if score > 0]

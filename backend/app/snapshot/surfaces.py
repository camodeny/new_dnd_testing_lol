"""Per-player projections for secret state — issue #250.

One authoritative world, different authorized views per player. Every
surface in this module is filtered server-side BEFORE serialization:
hidden rows are absent from unauthorized payloads, never merely hidden
client-side.

Surfaces:
- ``knowledge``: active facts + relations the viewer may receive
  (``app.world.epistemics`` grant-aware authorization, leak-free counts).
- ``clues``: alias over the visible fact records (clues are content-bearing
  facts; kept as a separate key so UI can render them distinctly without a
  second query).
- ``items``: item/object entities (ownership/details follow entity
  visibility; nested rules details redacted for non-authority, so shared
  appearance can differ from hidden reality).
- ``shops``: shop entities (extensible entity type), same authorization path.
- ``maps``: viewer-filtered encounter map when an encounter is visible to
  this viewer, else a blind ``{"visible": False}`` stub.
- ``clocks``: viewer-filtered pressure/clock indicators.

Fail-closed contract:
- Ambiguous/failed surface projections yield empty records with an
  ``error`` marker — never unfiltered data.
- Denied rows are counted by reason (``denied_reasons``) without leaking
  ids, content, or counts of *which* secret was denied beyond the reason.
- No module-level per-user cache exists here by design. Any caller-side
  cache MUST key by ``(campaign_id, viewer_id, revision)`` so one account's
  secret projection can never bleed into another session.

Realtime interplay: visibility expansion/contraction emits a
``projection.invalidated`` event (see ``app.realtime.service``) carrying no
secret content; clients recover via visibility-safe snapshot reload, which
re-enters through :func:`build_surfaces_for_viewer`.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.orm import Session

from models.campaigns import Campaign

logger = logging.getLogger(__name__)

ITEM_ENTITY_TYPES = frozenset({"item", "object"})
SHOP_ENTITY_TYPES = frozenset({"shop"})

_FACT_SCAN_LIMIT = 200
_RELATION_SCAN_LIMIT = 200
_ENTITY_SCAN_LIMIT = 200


def _empty_surface(reason: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"records": [], "total": 0, "visible": 0, "denied": 0}
    if reason:
        out["error"] = reason
    return out


def _is_member(db: Session, campaign: Campaign, viewer_id: uuid.UUID) -> bool:
    try:
        from app.campaigns.service import is_campaign_member

        return bool(
            campaign.owner_id == viewer_id
            or is_campaign_member(db, campaign.id, viewer_id)
        )
    except Exception:
        return False


def _knowledge_surface(
    db: Session, campaign: Campaign, viewer_id: uuid.UUID
) -> dict[str, Any]:
    from app.world import epistemics as _epistemics
    from app.world import knowledge as _knowledge

    try:
        facts = _knowledge.list_facts(
            db, campaign.id, status="active", limit=_FACT_SCAN_LIMIT
        )
        relations = _knowledge.list_relations(
            db, campaign.id, status="active", limit=_RELATION_SCAN_LIMIT
        )
        fact_proj = _epistemics.project_facts_for_user(db, campaign, viewer_id, facts)
        rel_proj = _epistemics.project_relations_for_user(
            db, campaign, viewer_id, relations
        )
        return {
            "facts": fact_proj,
            "relations": rel_proj,
            "visible": int(fact_proj.get("visible", 0)) + int(rel_proj.get("visible", 0)),
            "denied": int(fact_proj.get("denied", 0)) + int(rel_proj.get("denied", 0)),
        }
    except Exception as exc:
        logger.warning(
            "surfaces knowledge projection failed campaign_id=%s error=%s",
            campaign.id, exc,
        )
        return {"facts": _empty_surface("projection_failed"),
                "relations": _empty_surface("projection_failed"),
                "visible": 0, "denied": 0, "error": "projection_failed"}


def _entity_surface(
    db: Session,
    campaign: Campaign,
    viewer_id: uuid.UUID,
    entity_types: frozenset,
    *,
    is_authority: bool,
) -> dict[str, Any]:
    from app.world import epistemics as _epistemics
    from app.world import service as _world

    try:
        rows = _world.list_entities(db, campaign.id, limit=_ENTITY_SCAN_LIMIT)
        records: list[dict[str, Any]] = []
        denied_reasons: dict[str, int] = {}
        total = 0
        for row in rows:
            if str(getattr(row, "entity_type", "") or "") not in entity_types:
                continue
            total += 1
            try:
                verdict = _epistemics.may_user_receive(
                    db, campaign, "entity", row.id, viewer_id
                )
            except Exception:
                denied_reasons["ambiguous_visibility"] = denied_reasons.get(
                    "ambiguous_visibility", 0
                ) + 1
                continue
            if not verdict.get("allowed"):
                reason = str(verdict.get("reason") or "access_denied")
                denied_reasons[reason] = denied_reasons.get(reason, 0) + 1
                continue
            try:
                records.append(_world.project_entity_for_viewer(row, is_authority))
            except Exception:
                denied_reasons["projection_failed"] = denied_reasons.get(
                    "projection_failed", 0
                ) + 1
        return {
            "records": records,
            "total": total,
            "visible": len(records),
            "denied": total - len(records),
            "denied_reasons": denied_reasons,
        }
    except Exception as exc:
        logger.warning(
            "surfaces entity projection failed campaign_id=%s types=%s error=%s",
            campaign.id, sorted(entity_types), exc,
        )
        return _empty_surface("projection_failed")


def _maps_surface(
    db: Session, campaign: Campaign, viewer_id: uuid.UUID
) -> dict[str, Any]:
    try:
        from app.combat.service import get_snapshot_encounter

        encounter = get_snapshot_encounter(db, campaign.id, viewer_id)
    except Exception as exc:
        logger.warning(
            "surfaces map projection failed campaign_id=%s error=%s",
            campaign.id, exc,
        )
        return {"visible": False, "error": "projection_failed"}
    if not encounter:
        return {"visible": False}
    # The encounter payload is already viewer-filtered (hidden tokens
    # stripped, DM labels redacted); expose only its map slice here so the
    # full encounter state is not duplicated into surfaces.
    return {"visible": True, "map": encounter.get("map")}


def _clocks_surface(
    db: Session, campaign: Campaign, viewer_id: uuid.UUID
) -> dict[str, Any]:
    try:
        from app.world import clocks as _clocks

        return _clocks.project_clocks_for_viewer(db, campaign, viewer_id)
    except Exception as exc:
        logger.warning(
            "surfaces clocks projection failed campaign_id=%s error=%s",
            campaign.id, exc,
        )
        return {"clocks": [], "count": 0, "error": "projection_failed"}


def build_surfaces_for_viewer(
    db: Session, campaign: Campaign, viewer_id: uuid.UUID
) -> dict[str, Any]:
    """Build the per-viewer ``surfaces`` dict for the live-table snapshot.

    Never raises for projection failures: each surface fails closed to
    empty records independently so one broken surface cannot deny reconnect
    or leak unfiltered state. Counts are leak-free (reasons only).
    """
    try:
        viewer = viewer_id if isinstance(viewer_id, uuid.UUID) else uuid.UUID(str(viewer_id))
    except (ValueError, AttributeError, TypeError):
        return {
            "knowledge": _empty_surface("projection_failed"),
            "clues": _empty_surface("projection_failed"),
            "items": _empty_surface("projection_failed"),
            "shops": _empty_surface("projection_failed"),
            "maps": {"visible": False, "error": "projection_failed"},
            "clocks": {"clocks": [], "count": 0, "error": "projection_failed"},
        }
    if not _is_member(db, campaign, viewer):
        logger.info(
            "surfaces denied campaign_id=%s reason=not_member", campaign.id,
        )
        return {
            "knowledge": _empty_surface(),
            "clues": _empty_surface(),
            "items": _empty_surface(),
            "shops": _empty_surface(),
            "maps": {"visible": False},
            "clocks": {"clocks": [], "count": 0},
        }

    from app.world import service as _world

    is_authority = bool(_world.is_world_authority(campaign, viewer))

    knowledge = _knowledge_surface(db, campaign, viewer)
    items = _entity_surface(
        db, campaign, viewer, ITEM_ENTITY_TYPES, is_authority=is_authority
    )
    shops = _entity_surface(
        db, campaign, viewer, SHOP_ENTITY_TYPES, is_authority=is_authority
    )
    maps = _maps_surface(db, campaign, viewer)
    clocks = _clocks_surface(db, campaign, viewer)

    clue_records = list((knowledge.get("facts") or {}).get("records", []))
    clues = {
        "records": clue_records,
        "total": len(clue_records),
        "visible": len(clue_records),
        "denied": int((knowledge.get("facts") or {}).get("denied", 0)),
        "denied_reasons": dict((knowledge.get("facts") or {}).get("denied_reasons", {})),
    }
    if knowledge.get("error"):
        clues["error"] = knowledge["error"]

    surfaces = {
        "knowledge": knowledge,
        "clues": clues,
        "items": items,
        "shops": shops,
        "maps": maps,
        "clocks": clocks,
    }

    logger.info(
        "surfaces built campaign_id=%s authority=%s knowledge_visible=%s knowledge_denied=%s items_visible=%s items_denied=%s shops_visible=%s shops_denied=%s maps_visible=%s clocks=%s",
        campaign.id, is_authority,
        knowledge.get("visible"), knowledge.get("denied"),
        items.get("visible"), items.get("denied"),
        shops.get("visible"), shops.get("denied"),
        maps.get("visible"), clocks.get("count"),
    )
    return surfaces

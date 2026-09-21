"""Authoritative encounter map geometry, placements, terrain, movement — issue #232.

Code-owned authority (never delegated to models):
- Grid geometry (dimensions + diagonal policy) and token placements are
  durable rows; background art references stay opaque and never enter math.
- Terrain zones resolve to per-cell blocked/cost profiles; later zones win
  on overlap so DM edits re-carve earlier terrain deterministically.
- Reachable-space and cheapest-path math lives in
  :mod:`app.combat.geometry` (pure, deterministic). This module only feeds
  it validated inputs and commits validated outcomes.
- Illegal destinations (out of bounds, blocked, occupied, unreachable,
  insufficient movement) are rejected BEFORE any mutation: a failed move
  moves nothing and consumes nothing.
- Position + movement-budget commits are atomic (single transaction with the
  ``encounter.moved`` domain event) and idempotent (movement ledger keyed on
  ``(encounter_id, operation_id)`` — duplicate commands replay the recorded
  outcome instead of moving/spending twice).
- DM-authored terrain/placement changes arrive as validated structured
  arguments (``ensure_map`` / ``update_terrain`` direct, plus
  ``update_terrain_inline`` for the DM turn-commit transaction owned by the
  dm lane). Every change bumps the map revision and emits
  ``encounter.map_updated``.
- Projections strip DM-only terrain labels and hide hidden-entity NPC
  tokens from non-owners (fog/hidden hook; token hiding follows the source
  entity visibility signal, never #230 stat_visibility). Reconnects rebuild
  positions and terrain from these rows via ``map_projection`` / the snapshot.

Movement modes: only ``walk`` resolves against turn-state budgets at launch;
the mode column and validation stay extensible for future modes.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.combat.geometry import (
    FEET_PER_SQUARE,
    GeometryError,
    cheapest_path,
    feet_to_squares,
    reachable_cells,
    squares_to_feet,
    validate_cell,
    validate_diagonal_policy,
    validate_dimensions,
    validate_rect,
    zone_cell_effect,
)
from app.combat.service import EncounterAuthorizationError, EncounterError, list_participants
from app.observability.tracing import structured_log
from models.campaigns import Campaign
from models.combat import (
    DIAGONAL_POLICIES,
    MOVEMENT_MODES,
    TERRAIN_KINDS,
    Encounter,
    EncounterMap,
    EncounterMove,
    EncounterParticipant,
    EncounterPlacement,
    EncounterTerrainZone,
)

logger = logging.getLogger(__name__)

MAP_UPDATED_EVENT = "encounter.map_updated"
MOVED_EVENT = "encounter.moved"

MAX_ZONES_PER_MAP = 200
MAX_LABEL_LENGTH = 160

WALK_MODE = "walk"

#: Rejection reasons surfaced to callers (and observability) for failed moves.
REJECTION_REASONS = frozenset({
    "no_map", "no_placement", "no_turn_state", "not_active_turn",
    "out_of_bounds", "blocked", "occupied", "unreachable",
    "insufficient_movement", "unsupported_mode", "stale_turn",
})


class MapError(EncounterError):
    """Deterministic map/movement validation failure — caller must block."""

    def __init__(self, message: str, *, reason: str = "unreachable"):
        self.reason = reason if reason in REJECTION_REASONS else "unreachable"
        super().__init__(message)


class MapAuthorizationError(PermissionError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _lock_encounter(db: Session, encounter_id: uuid.UUID) -> Encounter:
    encounter = db.execute(
        select(Encounter).where(Encounter.id == encounter_id).with_for_update()
    ).scalars().first()
    if encounter is None:
        raise MapError(f"Encounter {encounter_id} not found", reason="no_map")
    return encounter


def _require_playable(db: Session, campaign_id: uuid.UUID) -> Campaign:
    from app.campaigns.service import require_playable_campaign

    campaign = db.execute(
        select(Campaign).where(Campaign.id == campaign_id).with_for_update()
    ).scalars().first()
    if campaign is None:
        raise MapError(f"Campaign {campaign_id} not found", reason="no_map")
    require_playable_campaign(campaign)
    return campaign


def _is_owner(db: Session, campaign_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    campaign = db.get(Campaign, campaign_id)
    return campaign is not None and str(campaign.owner_id) == str(user_id)


# ── Reads ────────────────────────────────────────────────────────────────────


def get_map(db: Session, encounter_id: uuid.UUID) -> EncounterMap | None:
    return db.execute(
        select(EncounterMap).where(EncounterMap.encounter_id == encounter_id)
    ).scalars().first()


def list_zones(db: Session, map_id: uuid.UUID) -> list[EncounterTerrainZone]:
    # Explicit author order first: same-transaction rows share a server
    # timestamp while UUID ids carry no author order, so (created_at, id)
    # alone can reorder overlapping zones arbitrarily.
    return list(db.execute(
        select(EncounterTerrainZone)
        .where(EncounterTerrainZone.map_id == map_id)
        .order_by(
            EncounterTerrainZone.zone_order.asc(),
            EncounterTerrainZone.created_at.asc(),
            EncounterTerrainZone.id.asc(),
        )
    ).scalars().all())


def _next_zone_order(db: Session, map_id: uuid.UUID) -> int:
    """Running max zone_order + 1 so appended batches extend author order."""
    from sqlalchemy import func as sa_func

    current = db.execute(
        select(sa_func.max(EncounterTerrainZone.zone_order)).where(
            EncounterTerrainZone.map_id == map_id
        )
    ).scalar()
    return int(current or 0) + (1 if current is not None else 0)


def _hidden_token_ids(db: Session, encounter_id: uuid.UUID) -> set[str]:
    """Participant ids whose map tokens stay hidden from non-owners.

    Token hiding follows the source entity/map visibility signal — a
    ``dm_only`` (or otherwise hidden-visibility) NPC/monster entity — and
    NOT ``stat_visibility``: #230 forces ``stat_visibility=dm_private`` for
    every NPC/monster because it protects combat *stats*, so ordinary
    visible enemies must keep their tokens, reachable reads, and movement
    (only their stats stay private). Tokens default to visible when the
    entity row is missing.
    """
    from models.combat import HIDDEN_ENTITY_VISIBILITIES
    from models.world import WorldEntity

    hidden: set[str] = set()
    for participant in list_participants(db, encounter_id):
        if participant.kind not in ("npc", "monster"):
            continue
        if participant.npc_entity_id is None:
            continue
        entity = db.get(WorldEntity, participant.npc_entity_id)
        if entity is not None and str(entity.visibility or "") in HIDDEN_ENTITY_VISIBILITIES:
            hidden.add(str(participant.id))
    return hidden


def list_placements(db: Session, encounter_id: uuid.UUID) -> list[EncounterPlacement]:
    return list(db.execute(
        select(EncounterPlacement).where(EncounterPlacement.encounter_id == encounter_id)
    ).scalars().all())


def get_placement(
    db: Session, encounter_id: uuid.UUID, participant_id: uuid.UUID
) -> EncounterPlacement | None:
    return db.execute(
        select(EncounterPlacement).where(
            EncounterPlacement.encounter_id == encounter_id,
            EncounterPlacement.participant_id == participant_id,
        )
    ).scalars().first()


def find_move_by_operation(
    db: Session, encounter_id: uuid.UUID, operation_id: str
) -> EncounterMove | None:
    if not operation_id:
        return None
    return db.execute(
        select(EncounterMove).where(
            EncounterMove.encounter_id == encounter_id,
            EncounterMove.operation_id == operation_id,
        )
    ).scalars().first()


def _zone_dicts(zones: list[EncounterTerrainZone]) -> list[dict]:
    return [
        {
            "kind": z.kind,
            "rect": {
                "col": int(z.rect_col), "row": int(z.rect_row),
                "width": int(z.rect_width), "height": int(z.rect_height),
            },
            "cost_multiplier": int(z.cost_multiplier),
        }
        for z in zones
    ]


def _occupied_cells(
    db: Session, encounter_id: uuid.UUID,
    *,
    exclude_participant_id: uuid.UUID | None = None,
    include_hidden: bool = True,
) -> set[tuple[int, int]]:
    """Cells blocked by other tokens. Reader-facing reads pass
    ``include_hidden=False`` for non-owners so hidden-token cells do not
    shape the reachable set (movement commits always use the full
    authoritative set)."""
    hidden = set() if include_hidden else _hidden_token_ids(db, encounter_id)
    occupied: set[tuple[int, int]] = set()
    for placement in list_placements(db, encounter_id):
        if exclude_participant_id is not None and placement.participant_id == exclude_participant_id:
            continue
        if str(placement.participant_id) in hidden:
            continue
        occupied.add((int(placement.col), int(placement.row)))
    return occupied


# ── Validation for DM-authored structured terrain ────────────────────────────


def _validate_zone_args(
    width: int, height: int, raw: Any, *, index: int
) -> dict:
    """Validate one DM-authored zone object into canonical zone fields."""
    if not isinstance(raw, dict):
        raise MapError(f"terrain zone {index} must be an object", reason="blocked")
    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in TERRAIN_KINDS:
        raise MapError(
            f"terrain zone {index} kind must be one of {list(TERRAIN_KINDS)}",
            reason="blocked",
        )
    rect = raw.get("rect")
    try:
        col, row, rect_width, rect_height = validate_rect(width, height, rect)
    except GeometryError as exc:
        raise MapError(f"terrain zone {index}: {exc}", reason="out_of_bounds") from exc
    multiplier = raw.get("cost_multiplier", 2)
    try:
        multiplier = int(multiplier)
    except (TypeError, ValueError) as exc:
        raise MapError(f"terrain zone {index} cost_multiplier must be an integer") from exc
    if kind == "difficult" and not 1 <= multiplier <= 10:
        raise MapError(
            f"terrain zone {index} cost_multiplier must be between 1 and 10", reason="blocked"
        )
    if kind != "difficult":
        multiplier = 2 if kind == "blocked" else 1
    label = raw.get("label")
    if label is not None:
        label = str(label).strip() or None
        if label is not None and len(label) > MAX_LABEL_LENGTH:
            raise MapError(
                f"terrain zone {index} label must be at most {MAX_LABEL_LENGTH} characters"
            )
    visibility = str(raw.get("visibility") or "public").strip().lower()
    if visibility not in ("public", "dm_only"):
        raise MapError(f"terrain zone {index} visibility must be public or dm_only")
    return {
        "kind": kind, "rect_col": col, "rect_row": row,
        "rect_width": rect_width, "rect_height": rect_height,
        "cost_multiplier": multiplier, "label": label, "visibility": visibility,
    }


def _validate_placements_args(
    db: Session,
    encounter: Encounter,
    encounter_map: EncounterMap,
    zones: list[EncounterTerrainZone],
    raw: Any,
) -> dict[str, tuple[int, int]]:
    """Validate DM-authored placements: known participants, in-bounds, unblocked, unique."""
    if raw is None:
        return {}
    if not isinstance(raw, list):
        raise MapError("placements must be a list of {participant_id, col, row}", reason="out_of_bounds")
    participants = {str(p.id): p for p in list_participants(db, encounter.id)}
    if not participants:
        raise MapError("encounter has no participants to place", reason="no_placement")
    profiles = zone_cell_effect(_zone_dicts(zones))
    resolved: dict[str, tuple[int, int]] = {}
    seen_cells: set[tuple[int, int]] = set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise MapError(f"placement {index} must be an object", reason="out_of_bounds")
        try:
            participant_id = uuid.UUID(str(entry.get("participant_id") or ""))
        except ValueError as exc:
            raise MapError(f"placement {index} participant_id must be a UUID") from exc
        if str(participant_id) not in participants:
            raise MapError(
                f"placement {index} references unknown participant {participant_id}",
                reason="no_placement",
            )
        if str(participant_id) in resolved:
            raise MapError(f"placement {index} duplicates participant {participant_id}")
        try:
            col, row = validate_cell(
                int(encounter_map.width), int(encounter_map.height),
                entry.get("col"), entry.get("row"), label=f"placement {index}",
            )
        except GeometryError as exc:
            raise MapError(str(exc), reason="out_of_bounds") from exc
        from app.combat.geometry import cell_profile

        if cell_profile(profiles, col, row)["blocked"]:
            raise MapError(
                f"placement {index} cell ({col}, {row}) is blocked by terrain", reason="blocked"
            )
        if (col, row) in seen_cells:
            raise MapError(
                f"placement {index} cell ({col}, {row}) is already occupied", reason="occupied"
            )
        seen_cells.add((col, row))
        resolved[str(participant_id)] = (col, row)
    return resolved


def _revalidate_preserved_placements(
    db: Session,
    encounter: Encounter,
    encounter_map: EncounterMap,
    zones: list[EncounterTerrainZone],
    preserved: dict[str, tuple[int, int]],
) -> dict[str, tuple[int, int]]:
    """Revalidate carried-over positions against replacement geometry/terrain.

    A geometry redefine without explicit placements keeps every current
    token where it stands. Cells pushed out of bounds reject the redefine
    (a token cannot strand off-grid); cells newly covered by blocking
    terrain are kept as stranded (movement *out* stays legal, same as
    :func:`update_terrain`). Rows for participants no longer in the
    encounter are dropped.
    """
    participants = {str(p.id) for p in list_participants(db, encounter.id)}
    width, height = int(encounter_map.width), int(encounter_map.height)
    kept: dict[str, tuple[int, int]] = {}
    for participant_id, (col, row) in preserved.items():
        if participant_id not in participants:
            continue
        if not (0 <= col < width and 0 <= row < height):
            raise MapError(
                f"preserved placement for {participant_id} at ({col}, {row}) "
                f"is outside the replacement {width}x{height} geometry",
                reason="out_of_bounds",
            )
        kept[participant_id] = (col, row)
    return kept


def _default_placements(
    db: Session,
    encounter: Encounter,
    encounter_map: EncounterMap,
    zones: list[EncounterTerrainZone],
    *,
    keep: dict[str, tuple[int, int]] | None = None,
) -> dict[str, tuple[int, int]]:
    """Deterministic fallback placements for participants missing explicit cells.

    Scans row-major from the top-left for the first free unblocked cell, so
    identical map states always place late joiners identically.
    """
    from app.combat.geometry import cell_profile

    keep = dict(keep or {})
    profiles = zone_cell_effect(_zone_dicts(zones))
    taken = set(keep.values())
    width, height = int(encounter_map.width), int(encounter_map.height)
    for participant in list_participants(db, encounter.id):
        if str(participant.id) in keep:
            continue
        placed = False
        for row in range(height):
            for col in range(width):
                if (col, row) in taken:
                    continue
                if cell_profile(profiles, col, row)["blocked"]:
                    continue
                keep[str(participant.id)] = (col, row)
                taken.add((col, row))
                placed = True
                break
            if placed:
                break
        if not placed:
            raise MapError(
                f"no free unblocked cell for participant {participant.id}", reason="blocked"
            )
    return keep


# ── Map init + DM terrain changes ────────────────────────────────────────────


def _bump_map_revision(encounter_map: EncounterMap) -> None:
    encounter_map.revision = int(encounter_map.revision or 1) + 1


def _emit_map_event(
    db: Session,
    campaign: Campaign,
    encounter: Encounter,
    *,
    expected_revision: int,
    operation_id: str,
    payload: dict,
    commit: bool,
):
    from app.campaigns.events import commit_campaign_mutation

    _, event = commit_campaign_mutation(
        db,
        campaign.id,
        expected_revision=int(expected_revision),
        event_type=MAP_UPDATED_EVENT,
        payload={"encounter_id": str(encounter.id), "thread_id": encounter.thread_id, **payload},
        operation_id=operation_id,
        actor_id=campaign.owner_id,
        outbox_event_type=MAP_UPDATED_EVENT,
        outbox_payload={
            "encounter_id": str(encounter.id),
            "campaign_id": str(encounter.campaign_id),
            "thread_id": encounter.thread_id,
            "map_revision": payload.get("map_revision"),
        },
        outbox_operation_id=operation_id,
        commit=commit,
    )
    return event


def ensure_map(
    db: Session,
    encounter_id: uuid.UUID,
    *,
    actor_id: uuid.UUID,
    width: int,
    height: int,
    diagonal_policy: str = "no_corner_cut",
    background_art_ref: str | None = None,
    terrain: list[dict] | None = None,
    placements: list[dict] | None = None,
    expected_revision: int,
    operation_id: str,
    commit: bool = True,
) -> tuple[EncounterMap, Any]:
    """Create (or deterministically re-assert) the encounter's authoritative map.

    Owner-only: map geometry is DM-authored state. Re-running with the same
    ``operation_id`` replays the recorded map without duplicating zones or
    placements. Geometry replacement re-validates every placement against the
    new bounds/terrain so a shrink can never strand a token out of bounds.
    """
    operation_id = (operation_id or "").strip()
    if not operation_id or len(operation_id) > 128:
        raise MapError("operation_id is required (1-128 characters)")
    encounter = _lock_encounter(db, encounter_id)
    campaign = _require_playable(db, encounter.campaign_id)
    if not _is_owner(db, encounter.campaign_id, actor_id):
        raise MapAuthorizationError("Only the campaign owner may define encounter map geometry")
    if encounter.status not in ("pending_initiative", "active"):
        raise MapError(f"maps require a pending or active encounter (status {encounter.status})")
    try:
        width, height = validate_dimensions(width, height)
    except GeometryError as exc:
        raise MapError(str(exc), reason="out_of_bounds") from exc
    try:
        diagonal_policy = validate_diagonal_policy(diagonal_policy or "no_corner_cut")
    except GeometryError as exc:
        raise MapError(str(exc)) from exc
    if background_art_ref is not None:
        background_art_ref = str(background_art_ref).strip() or None
        if background_art_ref is not None and len(background_art_ref) > 512:
            raise MapError("background_art_ref must be at most 512 characters")
    terrain = terrain if terrain is not None else []
    if not isinstance(terrain, list) or len(terrain) > MAX_ZONES_PER_MAP:
        raise MapError(f"terrain must be a list of at most {MAX_ZONES_PER_MAP} zones")
    validated_zones = [_validate_zone_args(width, height, raw, index=i) for i, raw in enumerate(terrain)]

    existing = get_map(db, encounter.id)

    holder: dict[str, Any] = {}

    def _mutate(locked: Campaign) -> None:
        nonlocal existing
        redefining = existing is not None
        # Snapshot current positions BEFORE the delete below: re-defining
        # geometry/art/terrain without explicit placements must preserve
        # (and revalidate) authoritative positions, never silently reseed.
        preserved: dict[str, tuple[int, int]] = {}
        if redefining and placements is None:
            for row in list_placements(db, encounter.id):
                preserved[str(row.participant_id)] = (int(row.col), int(row.row))
        encounter_map = existing
        if encounter_map is None:
            encounter_map = EncounterMap(
                id=uuid.uuid4(),
                encounter_id=encounter.id,
                campaign_id=encounter.campaign_id,
                width=width,
                height=height,
                diagonal_policy=diagonal_policy,
                background_art_ref=background_art_ref,
                revision=1,
            )
            db.add(encounter_map)
            db.flush()
        else:
            encounter_map.width = width
            encounter_map.height = height
            encounter_map.diagonal_policy = diagonal_policy
            encounter_map.background_art_ref = background_art_ref
            db.query(EncounterTerrainZone).filter(
                EncounterTerrainZone.map_id == encounter_map.id
            ).delete(synchronize_session=False)
            db.query(EncounterPlacement).filter(
                EncounterPlacement.encounter_id == encounter.id
            ).delete(synchronize_session=False)
            db.flush()
            _bump_map_revision(encounter_map)
        zone_rows: list[EncounterTerrainZone] = []
        for index, fields in enumerate(validated_zones):
            zone_rows.append(EncounterTerrainZone(
                id=uuid.uuid4(),
                map_id=encounter_map.id,
                encounter_id=encounter.id,
                campaign_id=encounter.campaign_id,
                zone_order=index,
                **fields,
            ))
        db.add_all(zone_rows)
        db.flush()
        if placements is None and redefining:
            explicit = _revalidate_preserved_placements(
                db, encounter, encounter_map, zone_rows, preserved
            )
        else:
            explicit = _validate_placements_args(db, encounter, encounter_map, zone_rows, placements)
        full = _default_placements(db, encounter, encounter_map, zone_rows, keep=explicit)
        for participant_id, (col, row) in full.items():
            db.add(EncounterPlacement(
                encounter_id=encounter.id,
                campaign_id=encounter.campaign_id,
                participant_id=uuid.UUID(participant_id),
                col=col, row=row,
            ))
        db.flush()
        holder["map"] = encounter_map
        holder["zone_count"] = len(zone_rows)
        holder["placement_count"] = len(full)

    from app.campaigns.events import commit_campaign_mutation

    try:
        campaign_after, event = commit_campaign_mutation(
            db,
            campaign.id,
            expected_revision=int(expected_revision),
            event_type=MAP_UPDATED_EVENT,
            operation_id=operation_id,
            actor_id=actor_id,
            mutate=_mutate,
            commit=False,
            payload_builder=lambda: {
                "encounter_id": str(encounter.id),
                "thread_id": encounter.thread_id,
                "map_revision": int(holder["map"].revision or 1),
                "width": width, "height": height,
                "diagonal_policy": diagonal_policy,
                "zone_count": holder["zone_count"],
                "placement_count": holder["placement_count"],
                "init": existing is None,
            },
            outbox_event_type=MAP_UPDATED_EVENT,
            outbox_payload={
                "encounter_id": str(encounter.id),
                "campaign_id": str(encounter.campaign_id),
                "thread_id": encounter.thread_id,
            },
            outbox_operation_id=operation_id,
        )
    except IntegrityError as exc:
        db.rollback()
        raise MapError(f"map init conflict: {exc}") from exc

    encounter_map = holder["map"]
    db.flush()
    if commit:
        db.commit()
        db.refresh(encounter_map)
        db.refresh(event)
    structured_log(
        logger, logging.INFO, "encounter_map_initialized",
        encounter_id=str(encounter.id), campaign_id=str(encounter.campaign_id),
        width=width, height=height, diagonal_policy=diagonal_policy,
        map_revision=int(encounter_map.revision or 1),
        zone_count=holder["zone_count"], operation_id=operation_id,
    )
    if commit:
        from app.realtime.service import publish_encounter_map

        publish_encounter_map(db, encounter)
    return encounter_map, event


def update_terrain(
    db: Session,
    encounter_id: uuid.UUID,
    *,
    actor_id: uuid.UUID,
    zones: list[dict] | None = None,
    clear_zone_ids: list[str] | None = None,
    expected_revision: int,
    operation_id: str,
    commit: bool = True,
) -> tuple[EncounterMap, Any]:
    """Apply a DM-authored terrain change through validated structured arguments.

    ``zones`` appends new validated zones (later rows win on overlap);
    ``clear_zone_ids`` removes named zones (an ``open``-kind audit trail stays
    in the event payload). Placements standing on newly blocked cells are
    left in place but flagged in the response — movement *out* is still
    legal (start cells never deny exit), movement *in* is denied.
    """
    operation_id = (operation_id or "").strip()
    if not operation_id or len(operation_id) > 128:
        raise MapError("operation_id is required (1-128 characters)")
    encounter = _lock_encounter(db, encounter_id)
    campaign = _require_playable(db, encounter.campaign_id)
    if not _is_owner(db, encounter.campaign_id, actor_id):
        raise MapAuthorizationError("Only the campaign owner may change encounter terrain")
    encounter_map = get_map(db, encounter.id)
    if encounter_map is None:
        raise MapError("encounter has no map yet; initialize geometry first", reason="no_map")
    zones = zones or []
    if not isinstance(zones, list) or len(zones) > MAX_ZONES_PER_MAP:
        raise MapError(f"zones must be a list of at most {MAX_ZONES_PER_MAP} entries")
    width, height = int(encounter_map.width), int(encounter_map.height)
    validated = [_validate_zone_args(width, height, raw, index=i) for i, raw in enumerate(zones)]
    clear_ids: list[uuid.UUID] = []
    for raw in (clear_zone_ids or []):
        try:
            clear_ids.append(uuid.UUID(str(raw)))
        except ValueError as exc:
            raise MapError(f"clear_zone_ids must be UUIDs (got {raw!r})") from exc

    holder: dict[str, Any] = {}

    def _mutate(locked: Campaign) -> None:
        if clear_ids:
            db.query(EncounterTerrainZone).filter(
                EncounterTerrainZone.map_id == encounter_map.id,
                EncounterTerrainZone.id.in_(clear_ids),
            ).delete(synchronize_session=False)
        base_order = _next_zone_order(db, encounter_map.id)
        for index, fields in enumerate(validated):
            db.add(EncounterTerrainZone(
                id=uuid.uuid4(),
                map_id=encounter_map.id,
                encounter_id=encounter.id,
                campaign_id=encounter.campaign_id,
                zone_order=base_order + index,
                **fields,
            ))
        db.flush()
        _bump_map_revision(encounter_map)
        db.flush()
        holder["zone_count"] = len(list_zones(db, encounter_map.id))
        holder["stranded"] = _stranded_placements(db, encounter, encounter_map)

    from app.campaigns.events import commit_campaign_mutation

    def _terrain_payload() -> dict:
        # The domain event is thread-scoped/shared history: stranded flags
        # carry exact cells, so hidden-entity tokens are filtered here too
        # (the owner reads full state via the ledger + projection).
        hidden = _hidden_token_ids(db, encounter.id)
        return {
            "encounter_id": str(encounter.id),
            "thread_id": encounter.thread_id,
            "map_revision": int(encounter_map.revision or 1),
            "zones_added": len(validated),
            "zones_cleared": len(clear_ids),
            "zone_count": holder["zone_count"],
            "stranded_placements": [
                s for s in holder["stranded"] if s["participant_id"] not in hidden
            ],
        }

    campaign_after, event = commit_campaign_mutation(
        db,
        campaign.id,
        expected_revision=int(expected_revision),
        event_type=MAP_UPDATED_EVENT,
        operation_id=operation_id,
        actor_id=actor_id,
        mutate=_mutate,
        commit=False,
        payload_builder=_terrain_payload,
        outbox_event_type=MAP_UPDATED_EVENT,
        outbox_payload={
            "encounter_id": str(encounter.id),
            "campaign_id": str(encounter.campaign_id),
            "thread_id": encounter.thread_id,
            "map_revision": int(encounter_map.revision or 1),
        },
        outbox_operation_id=operation_id,
    )
    db.flush()
    if commit:
        db.commit()
        db.refresh(encounter_map)
        db.refresh(event)
    structured_log(
        logger, logging.INFO, "encounter_terrain_changed",
        encounter_id=str(encounter.id), campaign_id=str(encounter.campaign_id),
        map_revision=int(encounter_map.revision or 1),
        zones_added=len(validated), zones_cleared=len(clear_ids),
        operation_id=operation_id,
    )
    if commit:
        from app.realtime.service import publish_encounter_map

        publish_encounter_map(db, encounter)
    return encounter_map, event


def update_terrain_inline(
    db: Session,
    campaign: Campaign,
    encounter: Encounter,
    args: Mapping,
    operation_key: str,
) -> EncounterMap:
    """DM structured-effect path: validated terrain change inside the turn-commit txn.

    No commit here — the outer turn commit owns the transaction (registered by
    the dm lane; this module never imports dm code). Duplicate effect replays
    return the current map untouched.
    """
    if not isinstance(args, Mapping):
        raise MapError("terrain effect arguments must be an object")
    encounter_map = get_map(db, encounter.id)
    if encounter_map is None:
        raise MapError("encounter has no map yet; initialize geometry first", reason="no_map")
    zones = args.get("zones") or []
    clear_ids = args.get("clear_zone_ids") or []
    if not isinstance(zones, list) or not isinstance(clear_ids, list):
        raise MapError("terrain effect zones/clear_zone_ids must be lists")
    width, height = int(encounter_map.width), int(encounter_map.height)
    validated = [_validate_zone_args(width, height, raw, index=i) for i, raw in enumerate(zones)]
    parsed_clear: list[uuid.UUID] = []
    for raw in clear_ids:
        try:
            parsed_clear.append(uuid.UUID(str(raw)))
        except ValueError as exc:
            raise MapError(f"clear_zone_ids must be UUIDs (got {raw!r})") from exc
    if parsed_clear:
        db.query(EncounterTerrainZone).filter(
            EncounterTerrainZone.map_id == encounter_map.id,
            EncounterTerrainZone.id.in_(parsed_clear),
        ).delete(synchronize_session=False)
    base_order = _next_zone_order(db, encounter_map.id)
    for index, fields in enumerate(validated):
        db.add(EncounterTerrainZone(
            id=uuid.uuid4(),
            map_id=encounter_map.id,
            encounter_id=encounter.id,
            campaign_id=encounter.campaign_id,
            zone_order=base_order + index,
            **fields,
        ))
    db.flush()
    _bump_map_revision(encounter_map)
    db.flush()
    structured_log(
        logger, logging.INFO, "encounter_terrain_changed",
        encounter_id=str(encounter.id), campaign_id=str(encounter.campaign_id),
        map_revision=int(encounter_map.revision or 1),
        zones_added=len(validated), zones_cleared=len(parsed_clear),
        operation_id=operation_key,
    )
    return encounter_map


def update_placements_inline(
    db: Session,
    campaign: Campaign,
    encounter: Encounter,
    args: Mapping,
    operation_key: str,
) -> EncounterMap:
    """DM structured-effect path: validated placement change inside the turn-commit txn.

    No commit here — the outer turn commit owns the transaction (registered by
    the dm lane; this module never imports dm code). Explicit staged cells win;
    unmentioned participants keep their current cells; participants with no
    cell yet fall back to the deterministic row-major default (same legality
    as :func:`ensure_map`: known participants, in-bounds, unblocked, unique).
    """
    if not isinstance(args, Mapping):
        raise MapError("placement effect arguments must be an object")
    encounter_map = get_map(db, encounter.id)
    if encounter_map is None:
        raise MapError("encounter has no map yet; initialize geometry first", reason="no_map")
    raw = args.get("placements")
    if raw is None:
        raw = []
    zones = list_zones(db, encounter_map.id)
    validated = _validate_placements_args(db, encounter, encounter_map, zones, raw)
    current = {
        str(p.participant_id): (int(p.col), int(p.row))
        for p in list_placements(db, encounter.id)
    }
    for participant_id in validated:
        current.pop(participant_id, None)
    if set(validated.values()) & set(current.values()):
        raise MapError("placement effect target cell is already occupied", reason="occupied")
    full = _default_placements(
        db, encounter, encounter_map, zones, keep={**current, **validated}
    )
    db.query(EncounterPlacement).filter(
        EncounterPlacement.encounter_id == encounter.id
    ).delete(synchronize_session=False)
    db.flush()
    for participant_id, (col, row) in full.items():
        db.add(EncounterPlacement(
            encounter_id=encounter.id,
            campaign_id=encounter.campaign_id,
            participant_id=uuid.UUID(participant_id),
            col=col, row=row,
        ))
    db.flush()
    _bump_map_revision(encounter_map)
    db.flush()
    structured_log(
        logger, logging.INFO, "encounter_placements_changed",
        encounter_id=str(encounter.id), campaign_id=str(encounter.campaign_id),
        map_revision=int(encounter_map.revision or 1),
        placements_moved=len(validated), placement_count=len(full),
        operation_id=operation_key,
    )
    return encounter_map


def _stranded_placements(
    db: Session, encounter: Encounter, encounter_map: EncounterMap
) -> list[dict]:
    """Placements now standing on blocked cells (flagged, never auto-moved)."""
    profiles = zone_cell_effect(_zone_dicts(list_zones(db, encounter_map.id)))
    stranded = []
    for placement in list_placements(db, encounter.id):
        from app.combat.geometry import cell_profile

        if cell_profile(profiles, int(placement.col), int(placement.row))["blocked"]:
            stranded.append({
                "participant_id": str(placement.participant_id),
                "col": int(placement.col), "row": int(placement.row),
            })
    return stranded


# ── Reachable-space reads ────────────────────────────────────────────────────


def reachable_for(
    db: Session,
    encounter_id: uuid.UUID,
    participant_id: uuid.UUID,
    *,
    movement_mode: str = WALK_MODE,
    viewer_id: uuid.UUID | None = None,
    is_owner: bool = False,
) -> dict:
    """Reachable cells + cheapest costs for a participant's remaining budget.

    Read-only: safe to call before any move. Output carries coordinates and
    square costs only — never DM-only terrain labels — so it is safe to serve
    to any encounter reader, with one boundary: a hidden-entity NPC/monster
    token's position (``from`` + reachable cells) is visible to the owner
    only. Non-owners querying another hidden token get 403 via
    MapAuthorizationError, and hidden tokens do not carve non-owner
    reachable shapes; every other reader keeps working.
    """
    mode = str(movement_mode or WALK_MODE).strip().lower()
    if mode not in MOVEMENT_MODES:
        raise MapError(
            f"movement_mode must be one of {list(MOVEMENT_MODES)}", reason="unsupported_mode"
        )
    encounter = db.get(Encounter, encounter_id)
    if encounter is None:
        raise MapError(f"Encounter {encounter_id} not found", reason="no_map")
    encounter_map = get_map(db, encounter.id)
    if encounter_map is None:
        raise MapError("encounter has no map yet", reason="no_map")
    participant = db.get(EncounterParticipant, participant_id)
    if participant is None or participant.encounter_id != encounter.id:
        raise MapError("participant not found in this encounter", reason="no_placement")
    if not is_owner and str(participant.id) in _hidden_token_ids(db, encounter.id):
        raise MapAuthorizationError(
            "Only the campaign owner may read a hidden token's reachable space"
        )
    placement = get_placement(db, encounter.id, participant.id)
    if placement is None:
        raise MapError("participant has no token placement", reason="no_placement")
    from app.combat.turns import get_turn_state_row

    state = get_turn_state_row(db, encounter.id, participant.id)
    if state is None:
        raise MapError("participant has no turn state in this encounter", reason="no_turn_state")

    started = time.perf_counter()
    max_squares = feet_to_squares(int(state.movement_remaining))
    zones = _zone_dicts(list_zones(db, encounter_map.id))
    # Visibility-aware occupancy: a non-owner's reachable shape must not be
    # carved by tokens they cannot see (movement commits still enforce the
    # full authoritative collision below).
    occupied = _occupied_cells(
        db, encounter.id, exclude_participant_id=participant.id,
        include_hidden=is_owner,
    )
    # Occupied cells deny entry; fold them as single-cell blocked overlays on
    # top of DM terrain (later wins, same as DM re-carves).
    overlays = list(zones) + [
        {"kind": "blocked", "rect": {"col": c, "row": r, "width": 1, "height": 1},
         "cost_multiplier": 1}
        for c, r in sorted(occupied)
        if 0 <= c < int(encounter_map.width) and 0 <= r < int(encounter_map.height)
    ]
    try:
        distances = reachable_cells(
            width=int(encounter_map.width),
            height=int(encounter_map.height),
            zones=overlays,
            start=(int(placement.col), int(placement.row)),
            max_squares=max_squares,
            diagonal_policy=encounter_map.diagonal_policy,
        )
    except GeometryError as exc:
        raise MapError(str(exc), reason="blocked") from exc
    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    structured_log(
        logger, logging.INFO, "encounter_reachable_computed",
        encounter_id=str(encounter.id), participant_id=str(participant.id),
        map_revision=int(encounter_map.revision or 1),
        movement_remaining_ft=int(state.movement_remaining),
        max_squares=max_squares, reachable_cells=len(distances),
        path_calc_latency_ms=latency_ms,
    )
    return {
        "encounter_id": str(encounter.id),
        "participant_id": str(participant.id),
        "from": {"col": int(placement.col), "row": int(placement.row)},
        "movement_remaining_ft": int(state.movement_remaining),
        "max_squares": max_squares,
        "diagonal_policy": encounter_map.diagonal_policy,
        "map_revision": int(encounter_map.revision or 1),
        "cells": [
            {"col": c, "row": r, "cost_squares": cost, "cost_feet": squares_to_feet(cost)}
            for (c, r), cost in sorted(distances.items())
        ],
    }


# ── Movement commit (atomic + idempotent) ────────────────────────────────────


def move_participant(
    db: Session,
    encounter_id: uuid.UUID,
    participant_id: uuid.UUID,
    *,
    actor_id: uuid.UUID,
    to_col: int,
    to_row: int,
    movement_mode: str = WALK_MODE,
    expected_turn_sequence: int,
    expected_revision: int,
    operation_id: str,
    commit: bool = True,
) -> tuple[EncounterMove, Encounter, Any]:
    """Move a token along the cheapest legal path, spending movement exactly once.

    Validation order (all before any mutation):
    turn binding → actor/ownership → mode → map/placement/budget reads →
    destination legality (bounds, blocked, occupied, reachable, budget).
    Geometry or path failure raises MapError with the exact reason and moves
    nothing / consumes nothing. The placement write, budget debit, ledger
    insert, and ``encounter.moved`` event commit atomically; a duplicate
    ``operation_id`` replays the recorded move instead of double-spending.
    """
    started = time.perf_counter()
    operation_id = (operation_id or "").strip()
    if not operation_id or len(operation_id) > 128:
        raise MapError("operation_id is required (1-128 characters)")
    mode = str(movement_mode or WALK_MODE).strip().lower()
    if mode not in MOVEMENT_MODES:
        raise MapError(
            f"movement_mode must be one of {list(MOVEMENT_MODES)}", reason="unsupported_mode"
        )
    try:
        expected_turn_sequence = int(expected_turn_sequence)
    except (TypeError, ValueError) as exc:
        raise MapError("expected_turn_sequence must be an integer", reason="stale_turn") from exc

    encounter = _lock_encounter(db, encounter_id)
    campaign = _require_playable(db, encounter.campaign_id)

    # Idempotent replay first: a duplicate command returns the recorded
    # outcome without touching position or budget (duplicate_retry signal).
    prior = find_move_by_operation(db, encounter.id, operation_id)
    if prior is not None:
        structured_log(
            logger, logging.INFO, "encounter_move_duplicate_retry",
            encounter_id=str(encounter.id), participant_id=str(prior.participant_id),
            operation_id=operation_id, turn_sequence=int(prior.turn_sequence or 0),
        )
        return prior, encounter, None

    if encounter.status != "active":
        raise MapError("movement requires an active encounter", reason="not_active_turn")
    if expected_turn_sequence != int(encounter.turn_sequence or 0):
        from app.combat.turns import StaleTurnError

        raise StaleTurnError(expected_turn_sequence, int(encounter.turn_sequence or 0))
    participant = db.get(EncounterParticipant, participant_id)
    if participant is None or participant.encounter_id != encounter.id:
        raise MapError("participant not found in this encounter", reason="no_placement")

    from app.combat.turns import _active_or_raise, _check_actor_for, get_turn_state_row

    try:
        active = _active_or_raise(db, encounter)
    except EncounterError as exc:
        raise MapError(str(exc), reason="not_active_turn") from exc
    if participant.id != active.id:
        encounter.invalid_attempt_count = int(encounter.invalid_attempt_count or 0) + 1
        db.flush()
        if commit:
            db.commit()
        raise MapError("only the active participant may move", reason="not_active_turn")
    try:
        _check_actor_for(db, encounter, participant, actor_id)
    except PermissionError as exc:
        raise MapAuthorizationError(str(exc)) from exc

    encounter_map = get_map(db, encounter.id)
    if encounter_map is None:
        raise MapError("encounter has no map yet", reason="no_map")
    placement = get_placement(db, encounter.id, participant.id)
    if placement is None:
        raise MapError("participant has no token placement", reason="no_placement")
    state = get_turn_state_row(db, encounter.id, participant.id)
    if state is None:
        raise MapError("participant has no turn state in this encounter", reason="no_turn_state")

    width, height = int(encounter_map.width), int(encounter_map.height)
    from_cell = (int(placement.col), int(placement.row))
    try:
        goal = validate_cell(width, height, to_col, to_row, label="destination")
    except GeometryError as exc:
        _log_rejection(encounter, participant, operation_id, "out_of_bounds", started, from_cell,
                        {"col": to_col, "row": to_row})
        raise MapError(str(exc), reason="out_of_bounds") from exc

    max_squares = feet_to_squares(int(state.movement_remaining))
    zones = _zone_dicts(list_zones(db, encounter_map.id))
    # Commit geometry uses the same visibility-aware occupancy as the
    # reachable preview: hidden-entity tokens do not block non-owner actors.
    # Preview/commit consistency (same inputs, same occupancy) removes the
    # probing oracle where a reachable cell fails only because a hidden
    # token stands there. Owners keep the full authoritative collision set.
    actor_is_owner = _is_owner(db, encounter.campaign_id, actor_id)
    occupied = _occupied_cells(
        db, encounter.id, exclude_participant_id=participant.id,
        include_hidden=actor_is_owner,
    )
    if goal in occupied:
        _log_rejection(encounter, participant, operation_id, "occupied", started, from_cell,
                        {"col": goal[0], "row": goal[1]})
        raise MapError(
            f"destination ({goal[0]}, {goal[1]}) is occupied by another token", reason="occupied"
        )
    overlays = list(zones) + [
        {"kind": "blocked", "rect": {"col": c, "row": r, "width": 1, "height": 1},
         "cost_multiplier": 1}
        for c, r in sorted(occupied)
        if 0 <= c < width and 0 <= r < height
    ]
    try:
        path, cost_squares = cheapest_path(
            width=width,
            height=height,
            zones=overlays,
            start=from_cell,
            goal=goal,
            max_squares=max_squares,
            diagonal_policy=encounter_map.diagonal_policy,
        )
    except GeometryError as exc:
        message = str(exc)
        if "blocked by terrain" in message:
            reason = "blocked"
        elif "unreachable" in message:
            # Exact cause: reachable with unbounded budget means the budget
            # is the blocker, otherwise geometry walls it off entirely.
            reason = (
                "insufficient_movement"
                if _reachable_unbounded(
                    width, height, overlays, from_cell, goal,
                    encounter_map.diagonal_policy,
                )
                else "unreachable"
            )
        else:
            reason = "unreachable"
        _log_rejection(encounter, participant, operation_id, reason, started, from_cell,
                        {"col": goal[0], "row": goal[1]})
        raise MapError(message, reason=reason) from exc
    cost_feet = squares_to_feet(cost_squares)
    if cost_feet > int(state.movement_remaining):
        # Defensive: cheapest_path already budgets in squares, so this is
        # unreachable in practice — fail closed rather than overspend.
        _log_rejection(encounter, participant, operation_id, "insufficient_movement",
                        started, from_cell, {"col": goal[0], "row": goal[1]})
        raise MapError(
            f"insufficient movement: {state.movement_remaining} ft remaining, "
            f"{cost_feet} ft required",
            reason="insufficient_movement",
        )

    holder: dict[str, Any] = {}

    def _mutate(locked: Campaign) -> None:
        # Re-check the ledger inside the mutation: a concurrent same-operation
        # commit that won the race replays instead of double-spending.
        existing = find_move_by_operation(db, encounter.id, operation_id)
        if existing is not None:
            holder["replay"] = existing
            return
        placement.col, placement.row = goal[0], goal[1]
        state.movement_remaining = int(state.movement_remaining) - cost_feet
        move = EncounterMove(
            id=uuid.uuid4(),
            encounter_id=encounter.id,
            campaign_id=encounter.campaign_id,
            participant_id=participant.id,
            operation_id=operation_id,
            movement_mode=mode,
            from_col=from_cell[0], from_row=from_cell[1],
            to_col=goal[0], to_row=goal[1],
            cost_squares=cost_squares, cost_feet=cost_feet,
            path=[{"col": c, "row": r} for c, r in path],
            turn_sequence=int(encounter.turn_sequence or 0),
        )
        db.add(move)
        db.flush()
        holder["move"] = move

    from app.campaigns.events import commit_campaign_mutation

    def _moved_payload() -> dict:
        # A hidden token's coordinates must not ride the shared/thread-scoped
        # event: non-owners get a position-free invalidation (the movement
        # ledger row keeps the authoritative cells for the owner/DM path).
        if str(participant.id) in _hidden_token_ids(db, encounter.id):
            return {
                "encounter_id": str(encounter.id),
                "thread_id": encounter.thread_id,
                "participant_id": str(participant.id),
                "movement_mode": mode,
                "position_redacted": True,
                "turn_sequence": int(encounter.turn_sequence or 0),
                "map_revision": int(encounter_map.revision or 1),
            }
            return {
                "encounter_id": str(encounter.id),
                "thread_id": encounter.thread_id,
                "participant_id": str(participant.id),
                "movement_mode": mode,
                "position_redacted": True,
                "turn_sequence": int(encounter.turn_sequence or 0),
                "map_revision": int(encounter_map.revision or 1),
            }
        return {
            "encounter_id": str(encounter.id),
            "thread_id": encounter.thread_id,
            "participant_id": str(participant.id),
            "movement_mode": mode,
            "from": {"col": from_cell[0], "row": from_cell[1]},
            "to": {"col": goal[0], "row": goal[1]},
            "cost_squares": cost_squares,
            "cost_feet": cost_feet,
            "turn_sequence": int(encounter.turn_sequence or 0),
            "map_revision": int(encounter_map.revision or 1),
        }

    hidden_mover = str(participant.id) in _hidden_token_ids(db, encounter.id)
    try:
        _, event = commit_campaign_mutation(
            db,
            campaign.id,
            expected_revision=int(expected_revision),
            event_type=MOVED_EVENT,
            operation_id=operation_id,
            actor_id=actor_id,
            mutate=_mutate,
            commit=False,
            payload_builder=_moved_payload,
            outbox_event_type=MOVED_EVENT,
            outbox_payload={
                "encounter_id": str(encounter.id),
                "campaign_id": str(encounter.campaign_id),
                "thread_id": encounter.thread_id,
                "participant_id": str(participant.id),
                **({} if hidden_mover else {"to": {"col": goal[0], "row": goal[1]}}),
            },
            outbox_operation_id=operation_id,
        )
    except IntegrityError as exc:
        # Lost the ledger race: the winner's row is the authoritative outcome.
        db.rollback()
        winner = find_move_by_operation(db, encounter.id, operation_id)
        if winner is not None:
            structured_log(
                logger, logging.INFO, "encounter_move_duplicate_retry",
                encounter_id=str(encounter.id), participant_id=str(winner.participant_id),
                operation_id=operation_id, turn_sequence=int(winner.turn_sequence or 0),
            )
            return winner, encounter, None
        raise MapError(f"movement commit conflict: {exc}") from exc

    if "replay" in holder:
        # Same-transaction replay path: the outer idempotent command owns the
        # commit; nothing was mutated above.
        return holder["replay"], encounter, None

    move = holder["move"]
    db.flush()
    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    if commit:
        db.commit()
        db.refresh(move)
        db.refresh(encounter)
        db.refresh(event)
    structured_log(
        logger, logging.INFO, "encounter_moved",
        encounter_id=str(encounter.id), participant_id=str(participant.id),
        operation_id=operation_id, movement_mode=mode,
        from_col=from_cell[0], from_row=from_cell[1],
        to_col=goal[0], to_row=goal[1],
        distance_squares=cost_squares, cost_feet=cost_feet,
        movement_remaining_ft=int(state.movement_remaining),
        turn_sequence=int(encounter.turn_sequence or 0),
        map_revision=int(encounter_map.revision or 1),
        path_calc_latency_ms=latency_ms,
    )
    if commit:
        from app.realtime.service import publish_encounter_moved

        publish_encounter_moved(db, encounter, participant.id, move_id=str(move.id))
    return move, encounter, event


def _reachable_unbounded(
    width: int,
    height: int,
    overlays: list[dict],
    start: tuple[int, int],
    goal: tuple[int, int],
    diagonal_policy: str,
) -> bool:
    """True when geometry alone (ignoring budget) connects start to goal."""
    try:
        distances = reachable_cells(
            width=width,
            height=height,
            zones=overlays,
            start=start,
            max_squares=width * height * 10,
            diagonal_policy=diagonal_policy,
        )
    except GeometryError:
        return False
    return goal in distances


def _log_rejection(
    encounter: Encounter,
    participant: EncounterParticipant,
    operation_id: str,
    reason: str,
    started: float,
    from_cell: tuple[int, int],
    to_cell: Mapping,
) -> None:
    structured_log(
        logger, logging.WARNING, "encounter_move_rejected",
        encounter_id=str(encounter.id), participant_id=str(participant.id),
        operation_id=operation_id, rejection_reason=reason,
        from_col=from_cell[0], from_row=from_cell[1],
        to_col=to_cell.get("col"), to_row=to_cell.get("row"),
        turn_sequence=int(encounter.turn_sequence or 0),
        path_calc_latency_ms=round((time.perf_counter() - started) * 1000, 2),
    )


# ── Projection (snapshot / reconnect, visibility-filtered) ───────────────────


def map_projection(
    db: Session,
    encounter: Encounter,
    *,
    viewer_id: uuid.UUID | None = None,
    is_owner: bool = False,
) -> dict | None:
    """Full map/terrain/placement projection; None when no map exists.

    Viewer-aware privacy: DM-only terrain zones are omitted for non-owners
    entirely — kind, rect, and label are all absent, not merely unlabeled
    (issue #250: hidden map geometry must never reach unauthorized
    payloads; movement legality stays server-side in commit geometry, so
    clients never need hidden rects). Tokens of hidden-entity NPC/monster
    participants are hidden from non-owners entirely (fog/hidden hook).
    Token hiding follows the source entity visibility signal, never
    ``stat_visibility`` (#230 stats-privacy stays separate). The AI is the
    only DM: ownership here means the campaign owner on the runtime path.
    """
    encounter_map = get_map(db, encounter.id)
    if encounter_map is None:
        return None
    zones = [
        z.to_dict(include_dm_label=is_owner)
        for z in list_zones(db, encounter_map.id)
        if is_owner or str(z.visibility or "") != "dm_only"
    ]
    participants = {str(p.id): p for p in list_participants(db, encounter.id)}
    hidden_ids = set() if is_owner else _hidden_token_ids(db, encounter.id)
    placements = []
    for placement in list_placements(db, encounter.id):
        participant = participants.get(str(placement.participant_id))
        if participant is None:
            continue
        if str(placement.participant_id) in hidden_ids:
            continue
        item = placement.to_dict()
        item["display_name"] = participant.display_name
        item["kind"] = participant.kind
        placements.append(item)
    stranded = _stranded_placements(db, encounter, encounter_map)
    if not is_owner:
        # A stranded flag carries the token's exact cell: hide it for
        # hidden-entity tokens exactly like placements above.
        stranded = [s for s in stranded if s["participant_id"] not in hidden_ids]
    return {
        **encounter_map.to_dict(),
        "zones": zones,
        "placements": placements,
        "stranded_placements": stranded,
    }


def get_snapshot_map(
    db: Session, campaign_id: uuid.UUID, viewer_id: uuid.UUID, *, is_owner: bool = False
) -> dict | None:
    """Reconnect-safe map projection for the live-table snapshot."""
    from app.combat.service import get_active_encounter

    encounter = get_active_encounter(db, campaign_id)
    if encounter is None:
        return None
    return map_projection(db, encounter, viewer_id=viewer_id, is_owner=is_owner)

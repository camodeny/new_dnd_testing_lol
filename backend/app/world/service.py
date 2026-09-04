"""Authoritative world service — issue #209.

Typed lookup/read APIs + transactional writers for canonical world entities
and the current-scene row. All fictional writers go through
``commit_campaign_mutation`` so scene/entity changes participate in campaign
revision/event ordering; stale revisions fail safely via
``RevisionConflictError`` (HTTP 409 at the boundary).

JIT promotion: ``promote_new_entities_from_contract`` assigns durable
canonical identity to committed ``new_entities`` proposals exactly once,
keyed by a stable idempotency key per (attempt, temp_id). Duplicate retry
returns the existing row without creating a duplicate.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.observability.tracing import structured_log
from models.campaigns import Campaign
from models.world import CampaignCurrentScene, WorldEntity

logger = logging.getLogger(__name__)

# Canonical entity types (at least NPC/location/faction/object + landmark);
# any other lowercase snake_case type is accepted as extensible.
CANONICAL_ENTITY_TYPES = frozenset({
    "npc", "location", "faction", "organization",
    "object", "item", "landmark", "character",
})
_ENTITY_TYPE_RE = re.compile(r"^[a-z0-9_]{2,32}$")
ENTITY_STATUSES = frozenset({"active", "inactive", "archived", "dead", "destroyed"})
SCENE_VISIBILITIES = frozenset({"public", "campaign", "private", "dm_only"})

# Staged-effect visibility vocabulary (issue #206) maps onto scene/entity
# visibility hooks without broadening disclosure.
_VISIBILITY_ALIASES = {"dm_private": "dm_only", "party_known": "campaign"}

# Visibility values that must never reach an ordinary campaign member
# (PR #348 re-review): only the campaign owner / DM authority may see them.
RESTRICTED_VISIBILITIES = frozenset({"private", "dm_only"})

# Sentinel for key-presence patch semantics (PR #348 re-review): distinguishes
# "field omitted" (preserve) from "explicit null" (clear). Used for
# location_entity_id so a null clears the canonical reference while omission
# preserves it — same pattern as the actors/environment clear-state fix.
UNSET: Any = object()


def normalize_visibility(value: Any, *, default: str = "campaign") -> str:
    raw = str(value or default).strip() or default
    canonical = _VISIBILITY_ALIASES.get(raw, raw)
    if canonical not in SCENE_VISIBILITIES:
        raise ValueError(f"visibility must be one of {sorted(SCENE_VISIBILITIES)}")
    return canonical


def validate_entity_type(value: Any) -> str:
    t = str(value or "").strip().lower()
    if not _ENTITY_TYPE_RE.fullmatch(t or ""):
        raise ValueError("entity_type must be 2-32 chars of [a-z0-9_]")
    return t


def validate_entity_name(value: Any) -> str:
    name = str(value or "").strip()
    if not name:
        raise ValueError("entity name is required")
    if len(name) > 160:
        raise ValueError("entity name must be 160 characters or fewer")
    return name


def validate_entity_status(value: Any) -> str:
    s = str(value or "active").strip().lower() or "active"
    if s not in ENTITY_STATUSES:
        raise ValueError(f"status must be one of {sorted(ENTITY_STATUSES)}")
    return s


# ── Viewer-aware reads (PR #348 re-review) ──────────────────────────────────

def is_world_authority(campaign: Campaign, viewer_id: uuid.UUID) -> bool:
    """Owner / DM authority sees restricted records; ordinary members do not.

    Mirrors the ``campaign.owner_id == profile.id`` authority check used by
    the world writers and the private-roll-evidence projection (#247).
    """
    return campaign.owner_id == viewer_id


def entity_visible_to_viewer(entity: WorldEntity, is_authority: bool) -> bool:
    return bool(is_authority) or entity.visibility not in RESTRICTED_VISIBILITIES


def scene_visible_to_viewer(scene: CampaignCurrentScene, is_authority: bool) -> bool:
    return bool(is_authority) or scene.visibility not in RESTRICTED_VISIBILITIES


def filter_entities_for_viewer(
    entities: list[WorldEntity], is_authority: bool
) -> list[WorldEntity]:
    """Restricted entities are filtered (not redacted) for ordinary members."""
    if is_authority:
        return list(entities)
    return [e for e in entities if e.visibility not in RESTRICTED_VISIBILITIES]


# ── Typed reads ─────────────────────────────────────────────────────────────

def get_entity(db: Session, entity_id: uuid.UUID) -> WorldEntity | None:
    return db.get(WorldEntity, entity_id)


def get_entity_strict(db: Session, campaign_id: uuid.UUID, entity_id: uuid.UUID) -> WorldEntity:
    entity = db.get(WorldEntity, entity_id)
    if entity is None or entity.campaign_id != campaign_id:
        raise ValueError(f"World entity {entity_id} not found in campaign {campaign_id}")
    return entity


def list_entities(
    db: Session,
    campaign_id: uuid.UUID,
    *,
    entity_type: str | None = None,
    status: str | None = None,
    search: str | None = None,
    limit: int = 100,
) -> list[WorldEntity]:
    q = select(WorldEntity).where(WorldEntity.campaign_id == campaign_id)
    if entity_type:
        q = q.where(WorldEntity.entity_type == validate_entity_type(entity_type))
    if status:
        q = q.where(WorldEntity.status == validate_entity_status(status))
    if search and str(search).strip():
        like = f"%{str(search).strip()[:80]}%"
        q = q.where(WorldEntity.name.ilike(like))
    q = q.order_by(WorldEntity.created_at.asc()).limit(max(1, min(int(limit or 100), 200)))
    return list(db.execute(q).scalars().all())


def get_current_scene(db: Session, campaign_id: uuid.UUID) -> CampaignCurrentScene | None:
    started = time.monotonic()
    scene = db.get(CampaignCurrentScene, campaign_id)
    elapsed_ms = (time.monotonic() - started) * 1000
    structured_log(
        logger, logging.INFO, "world_current_scene_read",
        campaign_id=str(campaign_id), found=scene is not None,
        read_ms=round(elapsed_ms, 3),
    )
    return scene


def get_current_scene_dict(db: Session, campaign_id: uuid.UUID) -> dict | None:
    scene = get_current_scene(db, campaign_id)
    return scene.to_dict() if scene else None


# ── Internal writers (no revision bump; caller owns the transaction) ─────────

def _find_by_idempotency(
    db: Session, campaign_id: uuid.UUID, idempotency_key: str | None
) -> WorldEntity | None:
    if not idempotency_key or not str(idempotency_key).strip():
        return None
    return db.execute(
        select(WorldEntity).where(
            WorldEntity.campaign_id == campaign_id,
            WorldEntity.idempotency_key == str(idempotency_key).strip(),
        )
    ).scalars().first()


def _dialect_upsert_insert(db: Session):
    """``INSERT ... ON CONFLICT DO NOTHING`` construct for the bound dialect.

    Postgres and SQLite both support it. Returns None on dialects without
    upsert support (callers fall back to the savepoint-isolated insert).
    """
    try:
        dialect_name = db.get_bind().dialect.name
    except Exception:
        return None
    if dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        return pg_insert
    if dialect_name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        return sqlite_insert
    return None


def create_entity_inline(
    db: Session,
    campaign: Campaign,
    *,
    entity_type: str,
    name: str,
    summary: str | None = None,
    status: str = "active",
    visibility: str = "campaign",
    details: dict | None = None,
    source_turn_id: uuid.UUID | None = None,
    source_attempt_id: uuid.UUID | None = None,
    operation_id: str | None = None,
    idempotency_key: str | None = None,
) -> tuple[WorldEntity, bool]:
    """Insert one canonical entity inside the caller's transaction.

    Returns (entity, created). When idempotency_key matches an existing row
    for the campaign, returns the existing row with created=False (duplicate
    retry creates nothing). Flushes; never commits — the outer
    ``commit_campaign_mutation`` owns commit/rollback so entity/scene commit
    is transactional with its source turn.
    """
    etype = validate_entity_type(entity_type)
    ename = validate_entity_name(name)
    estate = validate_entity_status(status)
    evis = normalize_visibility(visibility)
    key = str(idempotency_key).strip() if idempotency_key else None
    if key and len(key) > 128:
        raise ValueError("idempotency_key must be 128 characters or fewer")

    if key:
        existing = _find_by_idempotency(db, campaign.id, key)
        if existing is not None:
            structured_log(
                logger, logging.INFO, "world_entity_duplicate_conflict",
                campaign_id=str(campaign.id), entity_id=str(existing.id),
                entity_type=existing.entity_type, idempotency_key=key,
            )
            return existing, False

        upsert_insert = _dialect_upsert_insert(db)
        if upsert_insert is not None:
            # Exactly-once insert without savepoints: a concurrent inserter's
            # unique-key race is absorbed by ON CONFLICT DO NOTHING instead of
            # failing the revision transaction, so the race path is genuinely
            # recoverable on every backend (Postgres and SQLite alike — no
            # SAVEPOINT whose release could commit the outer transaction).
            entity_id = uuid.uuid4()
            db.execute(
                upsert_insert(WorldEntity)
                .values(
                    id=entity_id,
                    campaign_id=campaign.id,
                    entity_type=etype,
                    name=ename,
                    summary=str(summary)[:2000] if summary else None,
                    status=estate,
                    visibility=evis,
                    details=dict(details or {}),
                    source_turn_id=source_turn_id,
                    source_attempt_id=source_attempt_id,
                    operation_id=(str(operation_id)[:128] if operation_id else None),
                    idempotency_key=key,
                )
                .on_conflict_do_nothing(index_elements=["campaign_id", "idempotency_key"])
            )
            stored = _find_by_idempotency(db, campaign.id, key)
            if stored is None:  # pragma: no cover — defensive; the row must exist
                raise RuntimeError(f"idempotent entity insert for key {key!r} left no row")
            if str(stored.id) == str(entity_id):
                structured_log(
                    logger, logging.INFO, "world_entity_created",
                    campaign_id=str(campaign.id), entity_id=str(stored.id),
                    entity_type=etype, status=estate, visibility=evis,
                    source_turn_id=str(source_turn_id) if source_turn_id else None,
                    source_attempt_id=str(source_attempt_id) if source_attempt_id else None,
                    operation_id=str(operation_id) if operation_id else None,
                )
                return stored, True
            structured_log(
                logger, logging.INFO, "world_entity_duplicate_conflict",
                campaign_id=str(campaign.id), entity_id=str(stored.id),
                entity_type=stored.entity_type, idempotency_key=key,
                path="race_winner",
            )
            return stored, False

    entity = WorldEntity(
        id=uuid.uuid4(),
        campaign_id=campaign.id,
        entity_type=etype,
        name=ename,
        summary=str(summary)[:2000] if summary else None,
        status=estate,
        visibility=evis,
        details=dict(details or {}),
        source_turn_id=source_turn_id,
        source_attempt_id=source_attempt_id,
        operation_id=(str(operation_id)[:128] if operation_id else None),
        idempotency_key=key,
    )
    if key is None:
        # No idempotency constraint can fire — plain flush keeps the outer
        # transaction's atomicity on every backend.
        db.add(entity)
        db.flush()
    else:
        # Dialect without upsert support: isolate the candidate insert in a
        # SAVEPOINT so a unique-key race fails inside (not outside) it —
        # add() must happen INSIDE because SQLAlchemy flushes pending state
        # when the nested transaction starts. The savepoint rolls back only
        # the failed insert; the outer revision transaction stays intact.
        try:
            with db.begin_nested():
                db.add(entity)
                db.flush()
        except IntegrityError:
            # Lost a race with a concurrent inserter using the same key —
            # return the winner instead of duplicating. Expunge the loser
            # first so a later autoflush cannot retry the failed insert
            # (and so the winner lookup below does not flush it either).
            # Already-detached after the savepoint rollback on some
            # backends — tolerate that (cf. coordinate_turn).
            try:
                db.expunge(entity)
            except Exception:
                pass
            winner = _find_by_idempotency(db, campaign.id, key)
            if winner is not None:
                structured_log(
                    logger, logging.INFO, "world_entity_duplicate_conflict",
                    campaign_id=str(campaign.id), entity_id=str(winner.id),
                    entity_type=winner.entity_type, idempotency_key=key,
                    path="race_winner",
                )
                return winner, False
            raise
    structured_log(
        logger, logging.INFO, "world_entity_created",
        campaign_id=str(campaign.id), entity_id=str(entity.id),
        entity_type=etype, status=estate, visibility=evis,
        source_turn_id=str(source_turn_id) if source_turn_id else None,
        source_attempt_id=str(source_attempt_id) if source_attempt_id else None,
        operation_id=str(operation_id) if operation_id else None,
    )
    return entity, True


def apply_scene_update_inline(
    db: Session,
    campaign: Campaign,
    *,
    new_revision: int,
    location_entity_id: uuid.UUID | str | None | Any = UNSET,
    location_name: str | None = None,
    fictional_time: str | None = None,
    fictional_time_details: dict | None = None,
    present_actors: list | None = None,
    environment: dict | None = None,
    visibility: str | None = None,
    source_turn_id: uuid.UUID | None = None,
    source_attempt_id: uuid.UUID | None = None,
    operation_id: str | None = None,
) -> CampaignCurrentScene:
    """Upsert the transient scene row inside the caller's revision transaction.

    Never deletes durable WorldEntity rows — location_entity_id is only a
    reference. Fictional-time changes are logged for observability.

    Key-presence semantics for the entity reference: UNSET (omitted) preserves
    the current reference, explicit None clears it, and a value re-points it.
    """
    scene = db.get(CampaignCurrentScene, campaign.id)
    prior_time = scene.fictional_time if scene else None
    prior_location = scene.location_name if scene else None

    # Tri-state: UNSET → preserve; None → clear; value → validate + assign.
    # Truthiness checks would conflate explicit null with omission and let a
    # location_name change silently retain a stale canonical ID.
    loc_eid: uuid.UUID | None | Any = UNSET
    if location_entity_id is not UNSET:
        if location_entity_id is None or (
            isinstance(location_entity_id, str) and not location_entity_id.strip()
        ):
            loc_eid = None
        else:
            try:
                loc_eid = location_entity_id if isinstance(location_entity_id, uuid.UUID) else uuid.UUID(str(location_entity_id))
            except ValueError as exc:
                raise ValueError(f"Invalid location_entity_id {location_entity_id!r}") from exc
            ref = db.get(WorldEntity, loc_eid)
            if ref is None or ref.campaign_id != campaign.id:
                raise ValueError(f"location_entity_id {loc_eid} not found in campaign {campaign.id}")

    actors: list | None = None
    if present_actors is not None:
        if not isinstance(present_actors, list):
            raise ValueError("present_actors must be a list")
        if len(present_actors) > 64:
            raise ValueError("present_actors must have at most 64 entries")
        actors = []
        for entry in present_actors:
            if isinstance(entry, str):
                if not entry.strip() or len(entry) > 160:
                    raise ValueError("present_actors entries must be 1-160 chars")
                actors.append({"name": entry.strip()})
            elif isinstance(entry, dict):
                name = str(entry.get("name") or entry.get("entity_id") or "").strip()
                if not name or len(name) > 160:
                    raise ValueError("present_actors entries must have a 1-160 char name/entity_id")
                clean = {k: v for k, v in entry.items() if k in {"entity_id", "name", "kind", "role"}}
                clean["name"] = name
                actors.append(clean)
            else:
                raise ValueError("present_actors entries must be strings or objects")

    if location_name is not None and len(str(location_name)) > 256:
        raise ValueError("location_name must be 256 characters or fewer")
    if fictional_time is not None and len(str(fictional_time)) > 256:
        raise ValueError("fictional_time must be 256 characters or fewer")

    if scene is None:
        scene = CampaignCurrentScene(campaign_id=campaign.id, present_actors=[])
        db.add(scene)
        db.flush()

    if loc_eid is not UNSET:
        scene.location_entity_id = loc_eid
    if location_name is not None:
        scene.location_name = str(location_name).strip() or None
    if fictional_time is not None:
        scene.fictional_time = str(fictional_time).strip() or None
    if fictional_time_details is not None:
        if not isinstance(fictional_time_details, dict):
            raise ValueError("fictional_time_details must be an object")
        scene.fictional_time_details = dict(fictional_time_details)
    if actors is not None:
        scene.present_actors = actors
    if environment is not None:
        if not isinstance(environment, dict):
            raise ValueError("environment must be an object")
        scene.environment = dict(environment)
    if visibility is not None:
        scene.visibility = normalize_visibility(visibility)
    scene.revision = int(new_revision)
    scene.source_turn_id = source_turn_id
    scene.source_attempt_id = source_attempt_id
    if operation_id:
        scene.operation_id = str(operation_id)[:128]
    db.flush()

    if prior_time != scene.fictional_time:
        structured_log(
            logger, logging.INFO, "world_fictional_time_changed",
            campaign_id=str(campaign.id), prior=prior_time,
            current=scene.fictional_time, revision=int(new_revision),
        )
    structured_log(
        logger, logging.INFO, "world_scene_revised",
        campaign_id=str(campaign.id), revision=int(new_revision),
        location=scene.location_name, prior_location=prior_location,
        operation_id=str(operation_id) if operation_id else None,
    )
    return scene


# ── Authoritative writers (bump campaign revision + emit domain event) ───────

def create_entity_authoritative(
    db: Session,
    campaign_id: uuid.UUID,
    expected_revision: int,
    *,
    entity_type: str,
    name: str,
    summary: str | None = None,
    status: str = "active",
    visibility: str = "campaign",
    details: dict | None = None,
    operation_id: str | None = None,
    actor_id: uuid.UUID | None = None,
    idempotency_key: str | None = None,
    source_turn_id: uuid.UUID | None = None,
    source_attempt_id: uuid.UUID | None = None,
) -> tuple[WorldEntity, Any]:
    """Direct-API entity creation with revision ordering + idempotent retry."""
    from app.campaigns.events import commit_campaign_mutation

    key = str(idempotency_key or operation_id or "").strip() or None
    if key:
        existing = _find_by_idempotency(db, campaign_id, key)
        if existing is not None:
            structured_log(
                logger, logging.INFO, "world_entity_duplicate_conflict",
                campaign_id=str(campaign_id), entity_id=str(existing.id),
                idempotency_key=key, path="authoritative_precheck",
            )
            campaign = db.get(Campaign, campaign_id)
            return existing, None

    holder: dict[str, Any] = {}

    def _mutate(campaign: Campaign):
        entity, _ = create_entity_inline(
            db, campaign, entity_type=entity_type, name=name, summary=summary,
            status=status, visibility=visibility, details=details,
            source_turn_id=source_turn_id, source_attempt_id=source_attempt_id,
            operation_id=operation_id, idempotency_key=key,
        )
        holder["entity_id"] = entity.id

    # Payload/targets are built AFTER mutate via builders so the persisted
    # event carries the real allocated entity_id (building them upfront from
    # holder would persist entity_id: "").
    validated_type = validate_entity_type(entity_type)
    validated_name = validate_entity_name(name)

    def _payload() -> dict[str, Any]:
        return {
            "entity_id": str(holder["entity_id"]),
            "entity_type": validated_type,
            "name": validated_name,
            "idempotency_key": key,
        }

    def _targets() -> dict[str, Any]:
        return {"entity_id": str(holder["entity_id"])}

    campaign_after, event = commit_campaign_mutation(
        db, campaign_id, int(expected_revision),
        event_type="world.entity_created",
        payload_builder=_payload,
        operation_id=operation_id,
        actor_id=actor_id,
        targets_builder=_targets,
        visibility="public",
        provenance={"source": "world_api", "idempotency_key": key},
        mutate=_mutate,
    )
    entity = db.get(WorldEntity, holder["entity_id"])
    return entity, event


def set_scene_authoritative(
    db: Session,
    campaign_id: uuid.UUID,
    expected_revision: int,
    *,
    location_entity_id: uuid.UUID | str | None | Any = UNSET,
    location_name: str | None = None,
    fictional_time: str | None = None,
    fictional_time_details: dict | None = None,
    present_actors: list | None = None,
    environment: dict | None = None,
    visibility: str | None = None,
    operation_id: str | None = None,
    actor_id: uuid.UUID | None = None,
    source_turn_id: uuid.UUID | None = None,
    source_attempt_id: uuid.UUID | None = None,
) -> tuple[CampaignCurrentScene, Any]:
    """Direct-API scene transition with revision ordering (stale → 409)."""
    from app.campaigns.events import commit_campaign_mutation

    holder: dict[str, Any] = {}

    def _mutate(campaign: Campaign):
        # new_revision is prior+1 — mirrors commit_campaign_mutation's bump.
        prior = int(campaign.revision) if campaign.revision is not None else 0
        scene = apply_scene_update_inline(
            db, campaign, new_revision=prior + 1,
            location_entity_id=location_entity_id, location_name=location_name,
            fictional_time=fictional_time, fictional_time_details=fictional_time_details,
            present_actors=present_actors, environment=environment,
            visibility=visibility, source_turn_id=source_turn_id,
            source_attempt_id=source_attempt_id, operation_id=operation_id,
        )
        holder["scene"] = scene.to_dict()

    campaign_after, event = commit_campaign_mutation(
        db, campaign_id, int(expected_revision),
        event_type="world.scene_updated",
        # Built AFTER mutate so the persisted event carries the real scene
        # snapshot (building it upfront from holder would persist scene: {}).
        payload_builder=lambda: {
            "scene": holder["scene"],
            "location_name": location_name,
            "fictional_time": fictional_time,
        },
        operation_id=operation_id,
        actor_id=actor_id,
        targets={"campaign_id": str(campaign_id)},
        visibility="public",
        provenance={"source": "world_api"},
        mutate=_mutate,
    )
    scene = db.get(CampaignCurrentScene, campaign_id)
    return scene, event


# ── JIT promotion from a committed structured turn ──────────────────────────

def _stable_jit_key(attempt_id: uuid.UUID, temp_id: str) -> str:
    return f"jit:{attempt_id}:{str(temp_id).strip()}"[:128]


def promote_new_entities_from_contract(
    db: Session,
    campaign: Campaign,
    turn: Any,
    attempt: Any,
) -> list[WorldEntity]:
    """Promote ``new_entities`` proposals to durable canonical identity.

    Called inside the turn-commit revision transaction (no commit here), so
    promotion is transactional with its source turn: failed commit leaves no
    half-created authority. Each proposal gets a stable idempotency key per
    (attempt, temp_id) → committed exactly once; duplicate retry returns the
    existing row.
    """
    snapshot = getattr(attempt, "contract_snapshot", None) or {}
    if isinstance(snapshot, dict):
        proposals = snapshot.get("new_entities") or []
    else:
        proposals = getattr(snapshot, "new_entities", None) or []
    if not proposals:
        return []
    if len(proposals) > 8:
        raise ValueError("new_entities proposals exceed bound of 8")

    promoted: list[WorldEntity] = []
    turn_id = getattr(turn, "id", None)
    attempt_id = getattr(attempt, "id", None)
    operation_id = getattr(attempt, "commit_operation_id", None) or (str(attempt_id) if attempt_id else None)

    for raw in proposals:
        if isinstance(raw, dict):
            temp_id = str(raw.get("temp_id") or "").strip()
            kind = str(raw.get("kind") or "npc").strip().lower() or "npc"
            public_name = raw.get("public_name")
            public_summary = raw.get("public_summary")
            role = raw.get("role")
            location_ref = raw.get("location_ref")
        else:
            temp_id = str(getattr(raw, "temp_id", "") or "").strip()
            kind = str(getattr(raw, "kind", "npc") or "npc").strip().lower()
            public_name = getattr(raw, "public_name", None)
            public_summary = getattr(raw, "public_summary", None)
            role = getattr(raw, "role", None)
            location_ref = getattr(raw, "location_ref", None)
        if not temp_id:
            raise ValueError("new_entities proposal missing temp_id")
        entity, _ = create_entity_inline(
            db, campaign,
            entity_type=kind,
            name=validate_entity_name(public_name),
            summary=str(public_summary)[:2000] if public_summary else None,
            status="active",
            visibility="campaign",
            details={
                "temp_id": temp_id,
                "role": role,
                "location_ref": (
                    location_ref if isinstance(location_ref, dict)
                    else (location_ref.model_dump(mode="json") if hasattr(location_ref, "model_dump") else location_ref)
                ),
                "promoted_from": "dm_turn_contract",
            },
            source_turn_id=turn_id,
            source_attempt_id=attempt_id,
            operation_id=str(operation_id) if operation_id else None,
            idempotency_key=_stable_jit_key(attempt_id, temp_id),
        )
        promoted.append(entity)
    return promoted


# ── Runtime context assembly helper ─────────────────────────────────────────

def build_current_scene_context_record(
    db: Session, campaign: Campaign, *, thread_id: str | None = None
) -> dict | None:
    """Authoritative current-scene value for the CURRENT_SCENE context lane.

    Returns None when no scene has been established (caller keeps the lane
    not_applicable/unavailable per #202 fail-closed rules). Answers current
    location/time/present actors without parsing chat history.
    """
    scene = db.get(CampaignCurrentScene, campaign.id)
    if scene is None:
        return None
    present = list(scene.present_actors or [])
    actor_names = [
        str(a.get("name") if isinstance(a, dict) else a)
        for a in present if str(a.get("name") if isinstance(a, dict) else a).strip()
    ]
    return {
        "campaign_id": str(campaign.id),
        "location_entity_id": str(scene.location_entity_id) if scene.location_entity_id else None,
        "location_name": scene.location_name,
        "fictional_time": scene.fictional_time,
        "fictional_time_details": scene.fictional_time_details or {},
        "present_actors": present,
        "present_actor_names": actor_names,
        "environment": scene.environment or {},
        "visibility": scene.visibility,
        "revision": int(scene.revision),
        "source_turn_id": str(scene.source_turn_id) if scene.source_turn_id else None,
        "source_attempt_id": str(scene.source_attempt_id) if scene.source_attempt_id else None,
    }


def count_entities(db: Session, campaign_id: uuid.UUID) -> int:
    return int(db.scalar(
        select(func.count()).select_from(WorldEntity).where(WorldEntity.campaign_id == campaign_id)
    ) or 0)

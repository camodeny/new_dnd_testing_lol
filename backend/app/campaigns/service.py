"""Campaigns application/domain helpers — no FastAPI imports.

These helpers are importable without importing the routes (circular-safe),
so new gameplay modules can reuse validation without pulling in transport.
Command rejections raise :class:`CampaignCommandError`, which the app maps
to an HTTP error with the same status and detail.
"""
import logging
import random
import json
import secrets
import string as _string
import uuid as uuid_lib

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.characters.service import latest_sheet
from models.campaigns import Campaign, CampaignMember
from models.threads import CampaignThread

logger = logging.getLogger(__name__)

RANDOM_CAMPAIGN_NAMES = [
    "The Whispering Hollow", "Embers of the Forgotten Keep", "Tides of Shadowfen",
    "The Clockwork Sanctum", "Wolves of Winter's Edge", "The Sunken Archive",
    "Ashen Crown", "The Starless Citadel", "Echoes of the Barrowlands",
]
RANDOM_CAMPAIGN_DESCS = [
    "Ancient ruins stir as a forgotten power awakens beneath the earth.",
    "A coastal town hires brave souls to investigate lights beyond the fog.",
    "Rival factions race to claim a relic that could reshape the realm.",
    "Whispers from another plane bleed into the forests—something is watching.",
]

CAMPAIGN_STATUSES = frozenset({"lobby", "starting", "active", "archived"})
CAMPAIGN_TRANSITIONS = {
    "lobby": frozenset({"starting", "archived"}),
    "starting": frozenset({"lobby", "active", "archived"}),
    "active": frozenset({"archived"}),
    # Issue #265 — restore reactivates the exact same persistent campaign
    # (same ID/world/canon, no reseed). Archive is dormancy, not closure.
    # Restore always returns to the pre-archive status recorded on the
    # archive event, so archiving can never bypass start eligibility.
    "archived": frozenset({"lobby", "starting", "active"}),
}
DIFFICULTIES = frozenset({"easy", "medium", "hard", "deadly"})
#: How generous loot boxes are (#463); ``app.loot.service.LOOT_MODE_RULES``
#: gives each its draws, rarity odds, and coin purse.
LOOT_MODES = frozenset({"frequent_gamble", "rare_treasure", "generous", "scarce"})


def generate_invite_code(length: int = 8) -> str:
    alphabet = _string.ascii_uppercase + _string.digits
    alphabet = alphabet.replace("0", "").replace("O", "").replace("1", "").replace("I", "")
    return "".join(secrets.choice(alphabet) for _ in range(length))


def is_campaign_member(db: Session, campaign_id: uuid_lib.UUID, user_id: uuid_lib.UUID) -> bool:
    return db.get(CampaignMember, {"campaign_id": campaign_id, "user_id": user_id}) is not None


def parse_campaign_id(campaign_id: str) -> uuid_lib.UUID:
    # Raises HTTPException-like ValueError handling is done by router mapping.
    # Domain helper raises ValueError so caller can map to HTTP 404.
    return uuid_lib.UUID(str(campaign_id))


def random_brief(seed: str | None = None) -> dict:
    return {
        "name": random.choice(RANDOM_CAMPAIGN_NAMES),
        "description": random.choice(RANDOM_CAMPAIGN_DESCS),
        "random_seed": seed or generate_invite_code(6),
    }


def validate_campaign_name(name: str) -> str:
    stripped = (name or "").strip()
    if not stripped:
        raise ValueError("Campaign name is required")
    if len(stripped) > 128:
        raise ValueError("Campaign name must be 128 characters or fewer")
    return stripped


def validate_seed(raw) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if s and len(s) > 128:
        raise ValueError("Seed must be 128 characters or fewer")
    return s or None


def normalize_required_players(val) -> int:
    if isinstance(val, bool):
        raise ValueError("Required players must be an integer from 1 to 6")
    if isinstance(val, float):
        raise ValueError("Required players must be an integer from 1 to 6")
    # Reject non-integral numeric strings like "2.5" explicitly via strict integer parsing;
    # int("2.5") already raises, but we also guard string floats early for clarity.
    if isinstance(val, str):
        stripped = val.strip()
        if stripped == "":
            raise ValueError("Required players must be an integer from 1 to 6")
        # Allow optional leading +/- but require pure integer digits after
        test = stripped.lstrip("+-")
        if not test.isdigit():
            raise ValueError("Required players must be an integer from 1 to 6")
    try:
        n = int(val) if val is not None else 1
    except (TypeError, ValueError):
        raise ValueError("Required players must be an integer from 1 to 6")
    if n < 1 or n > 6:
        raise ValueError("Required players must be between 1 and 6")
    return n


def normalize_loot_mode(val) -> str:
    loot_mode = str(val or "frequent_gamble").strip().lower()
    if loot_mode not in LOOT_MODES:
        raise ValueError("Invalid loot mode")
    return loot_mode


def validate_difficulty(val) -> str:
    difficulty = str(val or "medium").strip().lower()
    if difficulty not in DIFFICULTIES:
        raise ValueError("Difficulty must be easy, medium, hard, or deadly")
    return difficulty


def validate_optional_text(val, *, field: str, max_length: int) -> str | None:
    if val is None:
        return None
    value = str(val).strip()
    if len(value) > max_length:
        raise ValueError(f"{field} must be {max_length} characters or fewer")
    return value or None


def validate_content_boundaries(val) -> dict:
    if val is None:
        return {}
    if not isinstance(val, dict):
        raise ValueError("Content boundaries must be a JSON object")
    if len(json.dumps(val, ensure_ascii=False, separators=(",", ":"))) > 16_384:
        raise ValueError("Content boundaries must be 16384 characters or fewer")
    if len(val) > 32:
        raise ValueError("Content boundaries must have at most 32 entries")
    for key, value in val.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError("Content boundaries keys must be non-empty strings")
        if len(key) > 128:
            raise ValueError("Content boundaries key must be 128 characters or fewer")
        if isinstance(value, str):
            if len(value) > 2000:
                raise ValueError("Content boundaries string values must be 2000 characters or fewer")
        elif isinstance(value, list):
            if len(value) > 64:
                raise ValueError("Content boundaries lists must have at most 64 entries")
            for entry in value:
                if not isinstance(entry, str):
                    raise ValueError("Content boundaries list entries must be strings")
                if len(entry) > 500:
                    raise ValueError("Content boundaries list entries must be 500 characters or fewer")
                if len(entry.strip()) == 0:
                    raise ValueError("Content boundaries list entries must be non-empty strings")
        elif isinstance(value, dict):
            # Nested objects not allowed — keep boundaries structurally flat for generation.
            raise ValueError("Content boundaries values must be strings or arrays of strings")
        elif value is not None:
            raise ValueError("Content boundaries values must be strings or arrays of strings")
    return val


def validate_lifecycle_transition(current: str, target) -> str:
    target_status = str(target or "").strip().lower()
    if target_status not in CAMPAIGN_STATUSES:
        raise ValueError("Status must be lobby, starting, active, or archived")
    if target_status not in CAMPAIGN_TRANSITIONS.get(current, frozenset()):
        raise ValueError(f"Campaign cannot transition from {current} to {target_status}")
    return target_status


def is_launch_locked(status: str) -> bool:
    """Launch character assignment is locked once the lobby closes."""
    return str(status or "").strip().lower() != "lobby"


def is_archived(campaign) -> bool:
    """True when the campaign is dormant (issue #265)."""
    return str(getattr(campaign, "status", "") or "").strip().lower() == "archived"


class CampaignArchivedError(ValueError):
    """Raised when a fictional write targets an archived (dormant) campaign.

    Subclasses ValueError so existing validation handlers keep working;
    routers map it explicitly to HTTP 409 (conflict with dormancy).
    """


class CampaignCommandError(Exception):
    """A campaign command rejected by domain rules, with its HTTP status.

    Deliberately not a ``ValueError``: the idempotency layer maps stray
    ``ValueError`` to 400, while these carry their own canonical status.
    """

    status_code = 409

    def __init__(self, detail, *, status_code: int | None = None, headers: dict | None = None):
        super().__init__(detail if isinstance(detail, str) else str(detail))
        if status_code is not None:
            self.status_code = status_code
        self.detail = detail
        self.headers = headers


def lock_campaign_row(db: Session, campaign_id: uuid_lib.UUID) -> Campaign | None:
    """Lock the campaign lifecycle row — issue #265.

    Archive/restore serializes on this same row via commit_campaign_mutation,
    so holding the lock until commit means a write that observed ``active``
    cannot commit after archive has committed (and vice versa). Consistent
    lock order everywhere is request/turn locks first, then the campaign row;
    archive only ever takes the campaign row.
    """
    return db.execute(
        select(Campaign).where(Campaign.id == campaign_id).with_for_update()
        .execution_options(populate_existing=True)
    ).scalars().first()


def require_playable_campaign(campaign) -> None:
    """Reject fictional writes while a campaign is archived.

    Archive freezes fictional time/clocks/NPC plans: no new submissions,
    world mutations, or autonomous execution may advance an archived table.
    Call on the locked campaign row inside the mutation transaction so a
    concurrent archive cannot slip past an earlier transport-level check.
    """
    if is_archived(campaign):
        raise CampaignArchivedError("Campaign is archived; restore it before continuing play")


def character_launch_validity(character, sheet) -> dict:
    """Authoritative character setup/progress — issues #241 and #425.

    Valid launch PC requires: non-empty name, non-empty race, and a class
    (scalar char_class or non-empty classes list), plus a completed draft. Returns
    {is_valid, missing, progress}.
    """
    missing: list[str] = []
    name = (getattr(character, "name", "") or "").strip() if character is not None else ""
    if character is None or not name:
        missing.append("name")
    race = (getattr(sheet, "race", None) or "").strip() if sheet is not None else ""
    if not race:
        missing.append("race")
    char_class = (getattr(sheet, "char_class", None) or "").strip() if sheet is not None else ""
    classes = getattr(sheet, "classes", None) if sheet is not None else None
    has_class = bool(char_class) or (
        isinstance(classes, list)
        and any(isinstance(c, dict) and str(c.get("class_name") or "").strip() for c in classes)
    )
    if not has_class:
        missing.append("class")
    is_draft = getattr(character, "status", "complete") == "draft"
    reported_missing = [*missing, "draft"] if is_draft else missing
    total = 3
    completed = total - len(missing)
    return {
        "is_valid": not missing and not is_draft,
        "missing": reported_missing,
        "is_draft": is_draft,
        "progress": {
            "completed": completed,
            "total": total,
            "percent": int(completed * 100 / total),
        },
    }


def compute_start_eligibility(campaign, members: list, db: Session) -> dict:
    """Server-side start eligibility from authoritative lobby/character state."""
    from models.characters import Character
    from models.profiles import Profile

    def member_label(m) -> str:
        prof = db.get(Profile, m.user_id)
        return prof.username if prof and prof.username else "adventurer"

    blockers: list[str] = []
    required = int(getattr(campaign, "required_players", 1) or 1)
    if len(members) < required:
        blockers.append(f"Campaign requires {required} members before starting (have {len(members)})")
    for m in members:
        label = member_label(m)
        char_id = getattr(m, "selected_character_id", None)
        if char_id is None:
            blockers.append(f"{label} has no selected character")
            continue
        char = db.get(Character, char_id)
        if char is None:
            blockers.append(f"{label} selected character is missing")
            continue
        if char.owner_id != m.user_id:
            blockers.append(f"{label} selected character is not owned by the member")
            continue
        if char.status != "complete":
            blockers.append(f"{label} character is still a draft")
            continue
        sheet = latest_sheet(db, char.id)
        validity = character_launch_validity(char, sheet)
        if not validity["is_valid"]:
            blockers.append(
                f"{label} character incomplete: missing {', '.join(validity['missing'])}"
            )
        if not getattr(m, "is_ready", False):
            blockers.append(f"{label} is not ready")
    return {"eligible": not blockers, "blockers": blockers}


def parse_character_id(raw) -> uuid_lib.UUID:
    """Body ``character_id``: 400 when absent, 404 when malformed."""
    if not raw:
        raise CampaignCommandError("character_id is required", status_code=400)
    try:
        return uuid_lib.UUID(str(raw))
    except ValueError as exc:
        raise CampaignCommandError("Character not found", status_code=404) from exc


def validated_setup(payload: dict, *, creation: bool = False) -> dict:
    """Validated campaign setup fields present in ``payload`` (all on creation)."""
    changes = {}
    try:
        if creation or "required_players" in payload:
            changes["required_players"] = normalize_required_players(payload.get("required_players"))
        if creation or "loot_mode" in payload:
            changes["loot_mode"] = normalize_loot_mode(payload.get("loot_mode"))
        if creation or "difficulty" in payload:
            changes["difficulty"] = validate_difficulty(payload.get("difficulty"))
        if "theme" in payload:
            changes["theme"] = validate_optional_text(payload.get("theme"), field="Theme", max_length=128)
        if "brief" in payload:
            changes["brief"] = validate_optional_text(payload.get("brief"), field="Brief", max_length=4000)
        if creation or "content_boundaries" in payload:
            changes["content_boundaries"] = validate_content_boundaries(payload.get("content_boundaries"))
    except ValueError as exc:
        raise CampaignCommandError(status_code=400, detail=str(exc)) from exc
    return changes


def campaign_settings_changes(payload: dict) -> dict:
    """Validated lobby-settings update (setup fields + name/description/seed)."""
    changes = validated_setup(payload)
    try:
        if "name" in payload and payload["name"] is not None:
            changes["name"] = validate_campaign_name(str(payload["name"]))
        if "description" in payload:
            changes["description"] = payload["description"]
        if "random_seed" in payload:
            changes["random_seed"] = validate_seed(payload.get("random_seed") or "")
    except ValueError as exc:
        raise CampaignCommandError(status_code=400, detail=str(exc)) from exc
    return changes


def create_campaign(
    db: Session,
    owner_id: uuid_lib.UUID,
    *,
    name: str,
    description: str | None,
    random_seed: str | None,
    setup: dict,
) -> Campaign:
    """Create a campaign with its owner membership and both shared threads.

    The live-table ``campaign`` thread is created eagerly so snapshot GET is
    retrieval-only (#196); the pre-start OOC ``lobby`` thread likewise keeps
    lobby chat GET retrieval-only for fresh campaigns (#243).
    """
    campaign = Campaign(
        owner_id=owner_id,
        name=name,
        description=description,
        random_seed=random_seed,
        **setup,
    )
    db.add(campaign)
    db.flush()
    db.add(CampaignMember(campaign_id=campaign.id, user_id=owner_id, role="owner"))
    db.flush()
    for thread_type, title in (("campaign", "Campaign"), ("lobby", "Lobby")):
        db.add(CampaignThread(
            id=uuid_lib.uuid4(),
            campaign_id=campaign.id,
            thread_type=thread_type,
            title=title,
            created_by=owner_id,
        ))
    db.commit()
    db.refresh(campaign)
    return campaign


def visible_campaigns(db: Session, user_id: uuid_lib.UUID, *, include_archived: bool) -> list[Campaign]:
    """Non-deleted campaigns the user owns or belongs to, newest first.

    Archived campaigns are dormant and hidden unless ``include_archived``
    (issue #265).
    """
    member_ids = set(db.execute(
        select(CampaignMember.campaign_id).where(CampaignMember.user_id == user_id)
    ).scalars().all())
    rows = db.execute(
        select(Campaign)
        .where(Campaign.is_deleted.is_(False))
        .order_by(Campaign.updated_at.desc())
    ).scalars().all()
    visible = [c for c in rows if c.owner_id == user_id or c.id in member_ids]
    if not include_archived:
        visible = [c for c in visible if not is_archived(c)]
    return visible


def member_count(db: Session, campaign_id: uuid_lib.UUID) -> int:
    return int(db.scalar(
        select(func.count()).select_from(CampaignMember).where(CampaignMember.campaign_id == campaign_id)
    ) or 0)


def update_campaign_settings(
    db: Session,
    campaign_id: uuid_lib.UUID,
    *,
    actor_id: uuid_lib.UUID,
    expected_revision: int,
    operation_id: str,
    changes: dict,
) -> dict:
    """Apply lobby settings changes as a revisioned campaign mutation."""
    from app.campaigns.events import commit_campaign_mutation

    def _mutate(campaign: Campaign):
        if campaign.status != "lobby":
            logger.warning(
                "campaign settings lock rejection campaign_id=%s actor_id=%s status=%s",
                campaign.id, actor_id, campaign.status,
            )
            raise CampaignCommandError(status_code=409, detail="Campaign settings are locked after the lobby")
        if "required_players" in changes and changes["required_players"] < member_count(db, campaign.id):
            raise CampaignCommandError(
                status_code=409, detail="Required players cannot be lower than current membership; remove members first",
            )
        for field, value in changes.items():
            setattr(campaign, field, value)

    campaign, event = commit_campaign_mutation(
        db,
        campaign_id,
        expected_revision,
        event_type="campaign.settings_updated",
        operation_id=operation_id,
        actor_id=actor_id,
        payload={"changes": changes},
        mutate=_mutate,
        commit=False,
    )
    return {"campaign": campaign.to_dict(), "event": event.to_dict()}

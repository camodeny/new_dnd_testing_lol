"""Optional player epilogues as canonical post-adventure play — issue #262.

After an adventure completes, each relevant player may optionally submit what
their character does next (or explicitly skip) without blocking completion.
Entries are player-authored through deterministic code:

- ``simple`` entries resolve immediately into a canonical
  ``adventure.epilogue`` domain event (normal event/memory/world-state input).
- ``adjudicated`` entries wait for a human roll fulfilled through the
  deterministic epilogue roll cycle (die 1–20 + modifier vs DC; code-owned
  arithmetic), then resolve into the same canonical event path.

Deterministic code owns canon/commit/idempotency here
(``commit_campaign_mutation`` + operation ids + one-row-per-PC). The DM never
invents voluntary PC choices: submissions/rolls are accepted only from the
character's owning player. Phase open/close is lifecycle bookkeeping (no
revision bump, mirroring ``closing_status``); only resolutions are fictional
mutations.

Explicitly out of scope (owned by open issues): generative DM narration of
epilogues (remainder of #177), XP/rewards/progression and clock dispositions
(#261), and the continuation transition into a new adventure (#264).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.campaigns import Adventure, AdventureEpilogue, Campaign, CampaignMember

logger = logging.getLogger(__name__)

#: Canonical domain event for a resolved epilogue. Resolved outcomes are
#: normal campaign canon and post-turn input like any other domain event.
EPILOGUE_EVENT = "adventure.epilogue"

MAX_CONTENT_LEN = 5000
MAX_LABEL_LEN = 120
MAX_ABILITY_LEN = 64

ROLL_KINDS = {"check", "save", "attack", "ability", "other"}


class EpilogueError(ValueError):
    pass


class EpilogueStateError(EpilogueError):
    """Phase or entry is not in a state that allows the requested transition."""


class EpilogueAuthorizationError(PermissionError):
    """Someone other than the PC's owning player tried to author its epilogue."""


class EpilogueDuplicateError(EpilogueError):
    """A second epilogue for the same PC, or a conflicting operation replay."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _get_adventure(db: Session, campaign_id: uuid.UUID, adventure_id: uuid.UUID) -> Adventure:
    adv = db.get(Adventure, adventure_id)
    if adv is None or str(adv.campaign_id) != str(campaign_id):
        raise EpilogueError(f"Adventure {adventure_id} not found in campaign {campaign_id}")
    return adv


def _require_completed(adv: Adventure) -> None:
    if adv.status != "completed":
        raise EpilogueStateError(
            f"Adventure {adv.id} is not completed (status={adv.status}); "
            "epilogues open only after DM-declared completion"
        )


def _require_open_phase(adv: Adventure) -> None:
    _require_completed(adv)
    if adv.epilogue_status != "open":
        raise EpilogueStateError(
            f"Epilogue phase is {adv.epilogue_status!r} for adventure {adv.id}; "
            "submit/skip/roll require an open phase"
        )


def _is_participant(db: Session, camp: Campaign, user_id: uuid.UUID) -> bool:
    if camp.owner_id == user_id:
        return True
    return (
        db.get(CampaignMember, {"campaign_id": camp.id, "user_id": user_id}) is not None
    )


def _validate_content(content: str) -> str:
    clean = (content or "").strip()
    if not clean:
        raise EpilogueError("Epilogue content must be a non-empty string")
    if len(clean) > MAX_CONTENT_LEN:
        raise EpilogueError(f"Epilogue content must be at most {MAX_CONTENT_LEN} characters")
    return clean


def _validate_visibility(visibility: str) -> str:
    clean = (visibility or "public").strip().lower()
    if clean not in ("public", "private"):
        raise EpilogueError(f"Invalid epilogue visibility {visibility!r}; must be 'public' or 'private'")
    return clean


def _validate_roll_spec(spec: dict | None) -> dict:
    if not isinstance(spec, dict):
        raise EpilogueError("Adjudicated epilogues require a roll_spec with dc, ability_or_skill, and label")
    kind = str(spec.get("roll_kind") or "check").strip().lower()
    if kind not in ROLL_KINDS:
        raise EpilogueError(f"Invalid roll_kind {kind!r}; must be one of {sorted(ROLL_KINDS)}")
    ability = str(spec.get("ability_or_skill") or "").strip()
    if not ability or len(ability) > MAX_ABILITY_LEN:
        raise EpilogueError(f"ability_or_skill must be between 1 and {MAX_ABILITY_LEN} characters")
    label = str(spec.get("label") or "").strip()
    if not label or len(label) > MAX_LABEL_LEN:
        raise EpilogueError(f"label must be between 1 and {MAX_LABEL_LEN} characters")
    try:
        dc = int(spec.get("dc"))
    except (TypeError, ValueError):
        raise EpilogueError("roll_spec.dc must be an integer") from None
    if not 1 <= dc <= 1000:
        raise EpilogueError("roll_spec.dc must be between 1 and 1000")
    return {"roll_kind": kind, "ability_or_skill": ability, "label": label, "dc": dc}


def _find_by_operation(
    db: Session, campaign_id: uuid.UUID, operation_id: str
) -> AdventureEpilogue | None:
    if not operation_id:
        return None
    return (
        db.execute(
            select(AdventureEpilogue).where(
                AdventureEpilogue.campaign_id == campaign_id,
                AdventureEpilogue.operation_id == operation_id,
            )
        )
        .scalars()
        .first()
    )


def _event_for(db: Session, row: AdventureEpilogue):
    if row.source_event_id is None:
        return None
    from models.campaigns import CampaignDomainEvent

    return db.get(CampaignDomainEvent, row.source_event_id)


# ── Phase lifecycle (bookkeeping; no revision bump) ───────────────────────────


def open_epilogues(
    db: Session,
    campaign_id: uuid.UUID,
    adventure_id: uuid.UUID,
    *,
    commit: bool = True,
) -> Adventure:
    """Open the optional epilogue phase for a completed adventure.

    Idempotent: reopening an open phase returns the adventure unchanged.
    A closed phase never reopens (single canonical pass).
    """
    from app.campaigns.service import require_playable_campaign

    camp = db.get(Campaign, campaign_id)
    if camp is None:
        raise EpilogueError(f"Campaign {campaign_id} not found")
    require_playable_campaign(camp)
    adv = _get_adventure(db, campaign_id, adventure_id)
    _require_completed(adv)
    if adv.epilogue_status == "closed":
        raise EpilogueStateError(f"Epilogue phase for adventure {adventure_id} is already closed")
    if adv.epilogue_status == "open":
        return adv
    adv.epilogue_status = "open"
    adv.epilogues_opened_at = _now()
    db.flush()
    if commit:
        db.commit()
        db.refresh(adv)
    logger.info(
        "epilogues opened campaign_id=%s adventure_id=%s", campaign_id, adventure_id
    )
    return adv


def close_epilogues(
    db: Session,
    campaign_id: uuid.UUID,
    adventure_id: uuid.UUID,
    *,
    commit: bool = True,
) -> dict:
    """Close the epilogue phase without requiring full participation.

    Missing/declining players never block closure: the returned stats report
    who participated, who skipped, and whose roster PCs simply never answered.
    Idempotent: closing a closed phase returns the same stats shape.
    """
    from app.campaigns.service import require_playable_campaign

    camp = db.get(Campaign, campaign_id)
    if camp is None:
        raise EpilogueError(f"Campaign {campaign_id} not found")
    require_playable_campaign(camp)
    adv = _get_adventure(db, campaign_id, adventure_id)
    _require_completed(adv)
    if adv.epilogue_status == "none":
        raise EpilogueStateError(
            f"Epilogue phase for adventure {adventure_id} was never opened"
        )
    if adv.epilogue_status != "closed":
        adv.epilogue_status = "closed"
        adv.epilogues_closed_at = _now()
        db.flush()
        if commit:
            db.commit()
            db.refresh(adv)
        logger.info(
            "epilogues closed campaign_id=%s adventure_id=%s", campaign_id, adventure_id
        )
    stats = epilogue_stats(db, adventure_id)
    stats["phase"] = "closed"
    return stats


# ── Player submission (agency-guarded) ────────────────────────────────────────


def submit_epilogue(
    db: Session,
    campaign_id: uuid.UUID,
    adventure_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
    character_id: uuid.UUID,
    content: str,
    visibility: str = "public",
    needs_adjudication: bool = False,
    roll_spec: dict | None = None,
    operation_id: str | None = None,
    expected_revision: int | None = None,
    commit: bool = True,
) -> tuple[AdventureEpilogue, Any | None]:
    """Submit a player's voluntary epilogue choice for their own PC.

    Agency guard: ``character_id`` must be owned by ``user_id`` — the DM (or
    another player) cannot author voluntary epilogue actions for human PCs.

    Simple entries resolve immediately into a canonical ``adventure.epilogue``
    domain event. Adjudicated entries persist as ``awaiting_roll`` until
    :func:`fulfill_epilogue_roll`. Duplicate ``operation_id`` replays return
    the original row + event; a second epilogue for the same PC raises
    :class:`EpilogueDuplicateError` (a prior ``skipped`` row converts to the
    new submission — players may change their mind, still one row).
    """
    from app.campaigns.service import require_playable_campaign
    from models.characters import Character

    camp = db.get(Campaign, campaign_id)
    if camp is None:
        raise EpilogueError(f"Campaign {campaign_id} not found")
    require_playable_campaign(camp)
    adv = _get_adventure(db, campaign_id, adventure_id)
    _require_open_phase(adv)

    character = db.get(Character, character_id)
    if character is None or str(character.owner_id) != str(user_id):
        raise EpilogueAuthorizationError(
            "Epilogues must be authored by the PC's owning player; "
            "the DM does not invent voluntary epilogue actions for human PCs"
        )
    if not _is_participant(db, camp, user_id):
        raise EpilogueAuthorizationError("Only campaign participants may submit epilogues")

    clean_content = _validate_content(content)
    clean_visibility = _validate_visibility(visibility)
    clean_spec = _validate_roll_spec(roll_spec) if needs_adjudication else None
    kind = "adjudicated" if needs_adjudication else "simple"

    op = (operation_id or "").strip() or None
    if op:
        prior = _find_by_operation(db, campaign_id, op)
        if prior is not None:
            if str(prior.adventure_id) != str(adventure_id) or str(prior.character_id) != str(
                character_id
            ):
                raise EpilogueDuplicateError(
                    f"operation_id {op!r} was already used for a different epilogue"
                )
            logger.info(
                "epilogue duplicate_operation_hit campaign_id=%s epilogue_id=%s op=%s",
                campaign_id, prior.id, op,
            )
            return prior, _event_for(db, prior)

    existing = (
        db.execute(
            select(AdventureEpilogue).where(
                AdventureEpilogue.adventure_id == adventure_id,
                AdventureEpilogue.character_id == character_id,
            )
        )
        .scalars()
        .first()
    )
    if existing is not None and existing.status != "skipped":
        raise EpilogueDuplicateError(
            f"Character {character_id} already has an epilogue for adventure {adventure_id}"
        )

    if existing is not None:  # skipped -> convert to a real submission (one row)
        row = existing
        row.content = clean_content
        row.kind = kind
        row.visibility = clean_visibility
        row.roll_spec = clean_spec
        row.roll_result = None
        row.outcome_text = None
        row.source_event_id = None
        row.operation_id = op
        row.attempts = 0
        row.last_error = None
        row.resolved_at = None
        row.status = "awaiting_roll" if needs_adjudication else "submitted"
    else:
        row = AdventureEpilogue(
            adventure_id=adventure_id,
            campaign_id=campaign_id,
            character_id=character_id,
            user_id=user_id,
            kind=kind,
            status="awaiting_roll" if needs_adjudication else "submitted",
            visibility=clean_visibility,
            content=clean_content,
            roll_spec=clean_spec,
            operation_id=op,
        )
        db.add(row)
    db.flush()

    if needs_adjudication:
        if commit:
            db.commit()
            db.refresh(row)
        logger.info(
            "epilogue submitted awaiting_roll campaign_id=%s adventure_id=%s epilogue_id=%s",
            campaign_id, adventure_id, row.id,
        )
        return row, None
    event = _resolve_simple(db, camp, row, expected_revision=expected_revision, commit=False)
    db.flush()
    if commit:
        db.commit()
        db.refresh(row)
        if event is not None:
            db.refresh(event)
    return row, event


def _resolve_simple(
    db: Session,
    camp: Campaign,
    row: AdventureEpilogue,
    *,
    expected_revision: int | None = None,
    commit: bool = False,
):
    """Commit a simple entry's canonical effect as an authoritative mutation."""
    row.outcome_text = (row.content or "").strip()
    return _commit_epilogue_event(db, camp, row, expected_revision=expected_revision, commit=commit)


def _commit_epilogue_event(
    db: Session,
    camp: Campaign,
    row: AdventureEpilogue,
    *,
    expected_revision: int | None = None,
    commit: bool = False,
):
    """Commit the canonical ``adventure.epilogue`` domain event.

    The resolved outcome feeds normal events/memory/world state as post-turn
    input. Any failure rolls back the whole mutation and never touches the
    already-completed adventure.
    """
    from app.campaigns.events import commit_campaign_mutation

    expected = (
        int(expected_revision)
        if expected_revision is not None
        else int(camp.revision or 0)
    )
    outcome = (row.outcome_text or "").strip()

    def _payload() -> dict:
        return {
            "adventure_id": str(row.adventure_id),
            "epilogue_id": str(row.id),
            "character_id": str(row.character_id),
            "kind": row.kind,
            "outcome_text": outcome,
            "roll_result": row.roll_result,
        }

    campaign_after, event = commit_campaign_mutation(
        db,
        row.campaign_id,
        expected_revision=expected,
        event_type=EPILOGUE_EVENT,
        operation_id=f"epilogue-resolve-{row.id}" if row.operation_id is None else None,
        actor_id=row.user_id,
        targets={
            "adventure_id": str(row.adventure_id),
            "epilogue_id": str(row.id),
            "character_id": str(row.character_id),
        },
        visibility=row.visibility,
        provenance={
            "source": "epilogue",
            "declared_by": "player",
            "epilogue_id": str(row.id),
        },
        payload_builder=_payload,
        commit=False,
    )
    row.source_event_id = event.id
    row.status = "resolved"
    row.resolved_at = _now()
    row.last_error = None
    db.flush()
    if commit:
        db.commit()
        db.refresh(row)
        db.refresh(event)
    logger.info(
        "epilogue resolved campaign_id=%s adventure_id=%s epilogue_id=%s kind=%s revision=%s event_id=%s",
        row.campaign_id, row.adventure_id, row.id, row.kind,
        campaign_after.revision, event.id,
    )
    return event


# ── Deterministic roll fulfillment ────────────────────────────────────────────


def fulfill_epilogue_roll(
    db: Session,
    epilogue_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
    die_value: int,
    modifier: int = 0,
    expected_revision: int | None = None,
    commit: bool = True,
) -> tuple[AdventureEpilogue, Any]:
    """Fulfill a human roll for an adjudicated epilogue (code-owned arithmetic).

    Only the owning player may fulfill their PC's roll. Natural 20 always
    succeeds, natural 1 always fails; otherwise total (die + modifier) meets
    or beats the DC. The resolved outcome commits through the same canonical
    event path as simple entries.
    """
    from app.campaigns.service import require_playable_campaign

    row = db.get(AdventureEpilogue, epilogue_id)
    if row is None:
        raise EpilogueError(f"Epilogue {epilogue_id} not found")
    if str(row.user_id) != str(user_id):
        raise EpilogueAuthorizationError("Only the owning player may roll for their PC's epilogue")
    if row.kind != "adjudicated" or row.status != "awaiting_roll":
        raise EpilogueStateError(
            f"Epilogue {epilogue_id} is not awaiting a roll (kind={row.kind}, status={row.status})"
        )
    adv = db.get(Adventure, row.adventure_id)
    if adv is None:
        raise EpilogueError(f"Adventure {row.adventure_id} not found")
    if adv.epilogue_status != "open":
        raise EpilogueStateError(
            f"Epilogue phase is {adv.epilogue_status!r}; rolls require an open phase"
        )
    camp = db.get(Campaign, row.campaign_id)
    if camp is None:
        raise EpilogueError(f"Campaign {row.campaign_id} not found")
    require_playable_campaign(camp)

    try:
        die = int(die_value)
    except (TypeError, ValueError):
        raise EpilogueError("die_value must be an integer between 1 and 20") from None
    if not 1 <= die <= 20:
        raise EpilogueError("die_value must be an integer between 1 and 20")
    try:
        mod = int(modifier)
    except (TypeError, ValueError):
        raise EpilogueError("modifier must be an integer") from None
    if not -10 <= mod <= 30:
        raise EpilogueError("modifier must be between -10 and +30")

    spec = row.roll_spec or {}
    try:
        dc = int(spec.get("dc"))
    except (TypeError, ValueError):
        raise EpilogueError("Epilogue roll_spec is missing a valid dc") from None

    total = die + mod
    if die == 20:
        success, natural = True, "crit"
    elif die == 1:
        success, natural = False, "fumble"
    else:
        success, natural = total >= dc, None
    row.attempts = int(row.attempts or 0) + 1
    row.roll_result = {
        "die_value": die,
        "modifier": mod,
        "total": total,
        "dc": dc,
        "success": success,
        "natural": natural,
    }
    label = str(spec.get("label") or "epilogue roll")
    verdict = "success" if success else "failure"
    row.outcome_text = (
        f"{(row.content or '').strip()} "
        f"[{label}: d20({die}){mod:+d} = {total} vs DC {dc} — {verdict}]"
    ).strip()
    db.flush()

    event = _commit_epilogue_event(db, camp, row, expected_revision=expected_revision, commit=False)
    db.flush()
    if commit:
        db.commit()
        db.refresh(row)
        db.refresh(event)
    return row, event


# ── Skip (explicit decline; never blocks closure) ─────────────────────────────


def skip_epilogue(
    db: Session,
    campaign_id: uuid.UUID,
    adventure_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
    character_id: uuid.UUID,
    commit: bool = True,
) -> AdventureEpilogue:
    """Record an explicit decline for a PC.

    The PC's owner records their own skip; the campaign owner may record a
    skip for a non-responsive player's roster PC (roster bookkeeping only —
    no fiction is authored). Skipping after submitting is rejected.
    """
    from app.campaigns.service import require_playable_campaign
    from models.characters import Character

    camp = db.get(Campaign, campaign_id)
    if camp is None:
        raise EpilogueError(f"Campaign {campaign_id} not found")
    require_playable_campaign(camp)
    adv = _get_adventure(db, campaign_id, adventure_id)
    _require_open_phase(adv)

    character = db.get(Character, character_id)
    if character is None:
        raise EpilogueError(f"Character {character_id} not found")
    if str(character.owner_id) != str(user_id) and str(camp.owner_id) != str(user_id):
        raise EpilogueAuthorizationError(
            "Only the PC's owner or the campaign owner may record an epilogue skip"
        )

    existing = (
        db.execute(
            select(AdventureEpilogue).where(
                AdventureEpilogue.adventure_id == adventure_id,
                AdventureEpilogue.character_id == character_id,
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        if existing.status != "skipped":
            raise EpilogueDuplicateError(
                f"Character {character_id} already submitted an epilogue; cannot skip after submitting"
            )
        return existing
    row = AdventureEpilogue(
        adventure_id=adventure_id,
        campaign_id=campaign_id,
        character_id=character_id,
        user_id=character.owner_id,
        kind="simple",
        status="skipped",
        visibility="public",
    )
    db.add(row)
    db.flush()
    if commit:
        db.commit()
        db.refresh(row)
    logger.info(
        "epilogue skipped campaign_id=%s adventure_id=%s character_id=%s",
        campaign_id, adventure_id, character_id,
    )
    return row


# ── Reads + observability ─────────────────────────────────────────────────────


def list_epilogues(
    db: Session,
    adventure_id: uuid.UUID,
    *,
    viewer_id: uuid.UUID | None = None,
    is_owner: bool = False,
) -> list[dict]:
    """Visibility-filtered epilogue roster.

    The owner sees everything. Other viewers see full entries for public
    rows and their own rows; private rows belonging to other players project
    to participation metadata only (mirrors #211 private semantics).
    """
    rows = list(
        db.execute(
            select(AdventureEpilogue)
            .where(AdventureEpilogue.adventure_id == adventure_id)
            .order_by(AdventureEpilogue.created_at.asc())
        )
        .scalars()
        .all()
    )
    out: list[dict] = []
    for row in rows:
        if is_owner or row.visibility == "public" or (
            viewer_id is not None and str(row.user_id) == str(viewer_id)
        ):
            out.append(row.to_dict())
        else:
            out.append(row.to_public_dict())
    return out


def _roster_character_ids(db: Session, campaign_id: uuid.UUID) -> set[str]:
    """Selected (launch-assigned) PCs across campaign members — the epilogue roster."""
    member_rows = list(
        db.execute(
            select(CampaignMember).where(CampaignMember.campaign_id == campaign_id)
        )
        .scalars()
        .all()
    )
    return {
        str(m.selected_character_id)
        for m in member_rows
        if m.selected_character_id is not None
    }


def epilogue_stats(db: Session, adventure_id: uuid.UUID) -> dict:
    """Participation/adjudication/canonical-effect counters for observability."""
    adv = db.get(Adventure, adventure_id)
    if adv is None:
        raise EpilogueError(f"Adventure {adventure_id} not found")
    rows = list(
        db.execute(
            select(AdventureEpilogue).where(AdventureEpilogue.adventure_id == adventure_id)
        )
        .scalars()
        .all()
    )
    answered = {str(r.character_id) for r in rows}
    roster = _roster_character_ids(db, adv.campaign_id)
    stats = {
        "adventure_id": str(adventure_id),
        "phase": adv.epilogue_status,
        "total": len(rows),
        "submitted": sum(1 for r in rows if r.status == "submitted"),
        "awaiting_roll": sum(1 for r in rows if r.status == "awaiting_roll"),
        "resolved": sum(1 for r in rows if r.status == "resolved"),
        "skipped": sum(1 for r in rows if r.status == "skipped"),
        "simple": sum(1 for r in rows if r.kind == "simple" and r.status == "resolved"),
        "adjudicated": sum(
            1 for r in rows if r.kind == "adjudicated" and r.status == "resolved"
        ),
        "canonical_effects": sum(1 for r in rows if r.source_event_id is not None),
        "private": sum(1 for r in rows if r.visibility == "private"),
        "roll_attempts": sum(int(r.attempts or 0) for r in rows if r.kind == "adjudicated"),
        "missing": sorted(c for c in roster - answered),
    }
    return stats

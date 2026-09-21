"""Production world-seed generation — issue #245.

Replaces the temporary #355 solo bootstrap as the authoritative campaign-start
input. Once the launch party is complete and ready, this module derives a
small validated world seed (durable canon: location/NPC/faction/character
entities, relations, facts, knowledge, starting scene, one pressure clock)
from campaign settings + public party composition + DM-authorized private
character lore, then moves the campaign lobby -> starting.

Transaction boundary: every mutation here is flush-only (commit=False).
The caller — ``execute_http_idempotent()`` — owns the single atomic commit
of the idempotency record + all seed state. Never commit from inside this
module. Failed/invalid seeds raise before mutating, so the campaign stays
safely pre-start and the client can fix inputs and retry.

Privacy: private lore content is consumed ONLY through
``get_seed_lore_bundle`` into DM-restricted seed inputs. Raw lore text never
enters public entities, scene environment, event payloads, logs, or the
response body — hooks derived from lore are metadata references (character
name + lore version), never content copies.
"""

from __future__ import annotations

import hashlib
import logging

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: Marker for production seed state — the #355 scaffold tag must NOT appear
#: in any seed created here.
WORLD_SEED_TAG = "world-seed-245"

#: Versioned seed contract. Bump when the spec shape or derivation changes;
#: stored on the seed event payload and scene environment.
WORLD_SEED_CONTRACT_VERSION = 1

#: Domain event emitted exactly once per seeded campaign.
WORLD_SEEDED_EVENT = "world.seeded_245"

#: Bounded in-process regeneration budget: on content-boundary rejection the
#: seed job tries alternate deterministic candidates (rotated picks) before
#: failing. Retries with a new operation converge on the first passing
#: candidate for the same inputs, so staging stays idempotent.
MAX_SEED_CANDIDATES = 6

#: Campaign statuses the seed job accepts. ``lobby`` is the normal path;
#: ``starting`` without a seed event converges (campaigns that transitioned
#: before this path existed). Anything else is rejected.
SEEDABLE_STATUSES = frozenset({"lobby", "starting"})

# ── Deterministic seed content ──────────────────────────────────────────────
# Curated small lists indexed by a stable hash of the campaign id: every
# retry for one campaign derives identical canon (idempotent), while
# different campaigns vary. Deliberately small — JIT worldbuilding expands
# after start rather than over-generating upfront.

_SEED_LOCATIONS = (
    ("Thornhollow Crossing", "A weathered crossroads hamlet where two old trade roads meet at a lantern-lit bridge."),
    ("Lanternmarsh Outpost", "A stilted marsh outpost guarding a causeway, its signal lanterns burning through the mist."),
    ("Cinderfell Chapel", "A half-ruined hill chapel whose bell rings on its own when travelers approach."),
    ("Saltrow Market", "A noisy canal-side market town where barges unload goods nobody ordered."),
)

_SEED_NPCS = (
    ("Mira Voss", "A sharp-eyed courier who carries letters she refuses to explain."),
    ("Bram Adler", "A retired watchman who tends the lanterns and remembers every face."),
    ("Sable Quill", "A soft-spoken scribe buying up old maps at suspicious prices."),
    ("Tommick Rye", "A cheerful ferryman who overhears far more than he lets on."),
)

_SEED_FACTIONS = (
    ("The Grey Ledger", "A quiet network of informants trading in debts and favors."),
    ("The Ember Compact", "A sworn alliance of road wardens stretched thin across the frontier."),
    ("The Hollow Lanterns", "A secretive order that marks doors for reasons it will not share."),
)

_SEED_PRESSURES = (
    ("The Rotting Fog", "A pale fog creeps closer each night, and things move inside it."),
    ("The Tithe Collectors", "Armed riders demand an old debt the town insists was settled."),
    ("The Dry Wells", "The wells are failing one by one, and someone is buying the last water rights."),
)

#: Difficulty shapes situation pressure (tighter clock), never rules.
_DIFFICULTY_THRESHOLD = {"easy": 6, "medium": 5, "hard": 4, "deadly": 3}

# ── DM-private hook classification ──────────────────────────────────────────
# Private lore content informs the seed ONLY through a coarse hook kind
# label: deterministic keyword classification over the lore text. Different
# lore produces different DM-restricted hook state, while no raw lore
# substring ever enters canon, payloads, logs, or responses. Labels are
# fixed vocabulary so they carry no recoverable content.

_HOOK_KINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("oath", ("oath", "swear", "vow", "promise")),
    ("debt", ("debt", "owe", "creditor", "ledger", "borrowed")),
    ("loss", ("murder", "killed", "dead", "grave", "mourning", "lost")),
    ("hidden_foe", ("enemy", "hunt", "revenge", "rival", "assassin")),
    ("secret_kin", ("brother", "sister", "father", "mother", "daughter", "son", "family")),
    ("quest", ("search", "seek", "find", "recover", "artifact", "map")),
)


def _classify_hook_kind(content: str) -> str:
    lowered = str(content or "").lower()
    for kind, keywords in _HOOK_KINDS:
        if any(kw in lowered for kw in keywords):
            return kind
    return "past"


class WorldSeedError(ValueError):
    """Seed eligibility/validation failure — mapped to 409 at the boundary."""


def _stable_index(key: str, salt: str, count: int) -> int:
    digest = hashlib.sha256(f"{WORLD_SEED_TAG}:{salt}:{key}".encode("utf-8")).hexdigest()
    return int(digest, 16) % count


def _deny_phrases(content_boundaries: dict | None) -> list[str]:
    """Flatten free-form boundaries into deny phrases (fail closed on shape)."""
    if not content_boundaries:
        return []
    if not isinstance(content_boundaries, dict):
        raise WorldSeedError("Campaign content boundaries are malformed")
    phrases: list[str] = []
    for _key, value in content_boundaries.items():
        if isinstance(value, str):
            text = value.strip().lower()
            if text:
                phrases.append(text)
        elif isinstance(value, list):
            for entry in value:
                if not isinstance(entry, str):
                    raise WorldSeedError("Campaign content boundaries are malformed")
                text = entry.strip().lower()
                if text:
                    phrases.append(text)
        elif value is not None:
            raise WorldSeedError("Campaign content boundaries are malformed")
    return phrases


def _check_boundaries(text_fields: dict[str, str], phrases: list[str], *, source: str) -> None:
    for field, text in text_fields.items():
        lowered = str(text or "").lower()
        for phrase in phrases:
            if phrase and phrase in lowered:
                logger.warning(
                    "world_seed boundary_rejection source=%s field=%s", source, field,
                )
                raise WorldSeedError(
                    f"World seed rejected by content boundary ({field}); "
                    "adjust campaign settings or character lore and retry"
                )


def build_seed_spec(
    *,
    campaign_id: str,
    theme: str | None,
    brief: str | None,
    difficulty: str,
    content_boundaries: dict | None,
    composition: dict,
    lore_bundle: list[dict],
    slot: int = 0,
) -> dict:
    """Derive the versioned seed spec (pure — no DB, no side effects).

    ``slot`` rotates the deterministic curated picks for bounded
    reject/regenerate cycles; slot 0 is the primary candidate.

    Raises ``WorldSeedError`` when inputs cannot seed (unready party,
    malformed boundaries, boundary rejection).

    Privacy: raw private lore content is NEVER boundary-scanned — lore
    shapes only DM-restricted hook metadata (character + version), never
    generated text, so scanning it would build an owner-visible substring
    oracle over another player's secrets (#244). Only generated seed output
    (derived from owner-controlled settings + public composition) is
    boundary-checked.
    """
    members = list((composition or {}).get("members") or [])
    pcs = [
        {"character_id": m.get("character_id"), "character_name": m.get("character_name")}
        for m in members
        if m.get("character_id")
    ]
    if not pcs:
        raise WorldSeedError("World seed requires at least one selected party character")
    for pc in pcs:
        if not str(pc.get("character_name") or "").strip():
            raise WorldSeedError("World seed requires every party character to be named")

    phrases = _deny_phrases(content_boundaries)
    pick = lambda name, n: _stable_index(campaign_id, f"{name}:{slot}", n)
    loc_name, loc_summary = _SEED_LOCATIONS[pick("location", len(_SEED_LOCATIONS))]
    npc_pick = _SEED_NPCS[pick("npc", len(_SEED_NPCS))]
    npc2_pick = _SEED_NPCS[(pick("npc", len(_SEED_NPCS)) + 2) % len(_SEED_NPCS)]
    faction_name, faction_summary = _SEED_FACTIONS[pick("faction", len(_SEED_FACTIONS))]
    pressure_name, pressure_desc = _SEED_PRESSURES[pick("pressure", len(_SEED_PRESSURES))]

    # Multiplayer seeds get a second NPC so hooks can distribute; solo stays lean.
    npc_specs = [npc_pick] if len(pcs) < 2 else [npc_pick, npc2_pick]

    threshold = _DIFFICULTY_THRESHOLD.get(str(difficulty or "medium").lower(), 5)
    theme_text = str(theme or "").strip()
    brief_text = str(brief or "").strip()
    grounding = theme_text or brief_text or "A frontier on the edge of strange events"
    pc_names = ", ".join(str(p["character_name"]) for p in pcs)
    situation = (
        f"{pc_names} arrive at {loc_name} as {pressure_desc.lower()} "
        f"The {faction_name} watches every newcomer."
    )
    premise = f"{grounding}. {situation}"

    # Lore shapes DM-private hooks as (character, version, kind) metadata —
    # raw lore content is NEVER copied into the spec. The kind label is a
    # coarse content-derived signal, so different lore seeds different
    # DM-restricted hook state without leaking recoverable content.
    lore_by_char = {str(entry.get("character_id")): entry for entry in (lore_bundle or [])}
    hooks = []
    for pc in pcs:
        entry = lore_by_char.get(str(pc["character_id"]))
        if entry is not None:
            hooks.append({
                "character_id": str(pc["character_id"]),
                "character_name": str(pc["character_name"]),
                "lore_version": int(entry.get("version") or 0),
                "kind": _classify_hook_kind(entry.get("content")),
            })

    spec = {
        "contract_version": WORLD_SEED_CONTRACT_VERSION,
        "location": {"name": loc_name, "summary": loc_summary},
        "npcs": [{"name": n, "summary": s} for n, s in npc_specs],
        "faction": {"name": faction_name, "summary": faction_summary},
        "pressure": {"name": pressure_name, "description": pressure_desc, "threshold": threshold},
        "situation": situation,
        "premise": premise,
        "grounding": grounding,
        "pcs": pcs,
        "hooks": hooks,
        "difficulty": str(difficulty or "medium").lower(),
        "fictional_time": f"Evening of the first day at {loc_name}",
    }
    # Boundary validation covers text derived from owner-controlled and
    # public inputs only. DM-private hook kinds are secret-derived metadata
    # and must never participate in owner-observable rejection (#244 oracle).
    _check_boundaries(
        {
            "location": loc_name, "location_summary": loc_summary,
            "npcs": " ".join(f"{n} {s}" for n, s in npc_specs),
            "faction": f"{faction_name} {faction_summary}",
            "pressure": f"{pressure_name} {pressure_desc}",
            "situation": situation, "premise": premise, "grounding": grounding,
        },
        phrases, source="generated",
    )
    return spec


def generate_seed_spec(
    *,
    campaign_id: str,
    theme: str | None,
    brief: str | None,
    difficulty: str,
    content_boundaries: dict | None,
    composition: dict,
    lore_bundle: list[dict],
    required_pc_ids: list[str],
) -> tuple[dict, int]:
    """Build + validate the first boundary-passing candidate (bounded regen).

    Tries deterministic slots in order and returns ``(spec, candidates_tried)``.
    Raises the last ``WorldSeedError`` when every candidate is rejected.
    """
    last_error: WorldSeedError | None = None
    for slot in range(max(1, MAX_SEED_CANDIDATES)):
        try:
            spec = build_seed_spec(
                campaign_id=campaign_id, theme=theme, brief=brief,
                difficulty=difficulty, content_boundaries=content_boundaries,
                composition=composition, lore_bundle=lore_bundle, slot=slot,
            )
            validate_seed_spec(spec, required_pc_ids=required_pc_ids)
            if slot:
                logger.info("world_seed regenerated campaign_id=%s slot=%s", campaign_id, slot)
            return spec, slot + 1
        except WorldSeedError as exc:
            last_error = exc
    raise last_error if last_error is not None else WorldSeedError("World seed generation failed")


def validate_seed_spec(spec: dict, *, required_pc_ids: list[str]) -> None:
    """Fail-closed structural validation before any write."""
    from app.world.service import (
        validate_entity_name, validate_entity_status, validate_entity_type,
    )
    from app.world.knowledge import (
        validate_epistemic_state, validate_fact_content, validate_relation_type,
    )
    from app.world.clocks import (
        validate_advancement_criteria, validate_completion_criteria, validate_stages,
    )
    from app.world.epistemics import normalize_record_visibility

    if not isinstance(spec, dict) or spec.get("contract_version") != WORLD_SEED_CONTRACT_VERSION:
        raise WorldSeedError("World seed spec contract version mismatch")
    validate_entity_type("location")
    validate_entity_name(spec["location"]["name"])
    for npc in spec["npcs"]:
        validate_entity_type("npc")
        validate_entity_name(npc["name"])
    validate_entity_type("faction")
    validate_entity_name(spec["faction"]["name"])
    for pc in spec["pcs"]:
        validate_entity_type("character")
        validate_entity_name(pc["character_name"])
    validate_entity_status("active")
    if not spec["npcs"]:
        raise WorldSeedError("World seed requires at least one NPC")
    if not spec["situation"].strip() or not spec["premise"].strip():
        raise WorldSeedError("World seed requires a starting situation")
    # Party integration: every selected PC must be seeded as canon.
    seeded_ids = {str(p["character_id"]) for p in spec["pcs"]}
    missing = [c for c in (required_pc_ids or []) if str(c) not in seeded_ids]
    if missing:
        raise WorldSeedError("World seed does not cover every selected party character")
    # Visibility vocabulary fails closed.
    normalize_record_visibility("campaign")
    normalize_record_visibility("dm_only")
    validate_relation_type("member_of")
    validate_relation_type("present_at")
    validate_epistemic_state("confirmed")
    validate_fact_content(spec["situation"])
    # Required pressure clock with explicit criteria.
    pressure = spec["pressure"]
    if not pressure.get("name"):
        raise WorldSeedError("World seed requires an initial pressure/clock")
    threshold = int(pressure["threshold"])
    validate_advancement_criteria({
        "kind": "deterministic",
        "event_types": ["dm.turn_committed"],
        "required_count": 2,
        "max_advance": 1,
    })
    validate_completion_criteria({
        "kind": "semantic",
        "description": f"Decisively end {pressure['name']} through play",
        "event_types": ["dm.turn_committed"],
    })
    validate_stages(
        [{"at": max(1, threshold - 1), "label": f"{pressure['name']} escalates"}],
        threshold,
    )


def _provenance() -> dict:
    return {"source": WORLD_SEED_TAG, "issue": 245, "contract_version": WORLD_SEED_CONTRACT_VERSION}


def run_world_seed(
    db: Session,
    campaign_id,
    *,
    actor_id,
    operation_id: str,
) -> dict:
    """Generate, validate, and stage the production world seed (flush-only).

    Owner-only. Requires a fully ready launch party (any size 1..6). Stages
    canon + scene + clock and moves lobby -> starting in one atomic outer
    transaction. Re-running after a seed event converges (idempotent replay).
    Raises ``WorldSeedError`` on any misuse/validation failure — the campaign
    stays pre-start and the client retries with corrected inputs.
    """
    from app.campaigns.events import commit_campaign_mutation, has_domain_event
    from app.campaigns.service import (
        CampaignArchivedError, compute_start_eligibility, require_playable_campaign,
    )
    from app.world.clocks import create_clock_inline
    from app.world.epistemics import assert_knowledge_inline
    from app.world.knowledge import create_fact_inline, create_relation_inline
    from app.world.service import apply_scene_update_inline, create_entity_inline
    from models.campaigns import Campaign, CampaignMember
    from models.world import CampaignCurrentScene

    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise WorldSeedError("Campaign not found")
    if campaign.owner_id != actor_id:
        raise WorldSeedError("Only the owner can seed the campaign world")
    try:
        require_playable_campaign(campaign)
    except CampaignArchivedError as exc:
        raise WorldSeedError("Archived campaigns cannot be seeded") from exc

    # Serialize concurrent different-key seeds on the campaign row.
    from sqlalchemy import select as _select

    db.execute(_select(Campaign).where(Campaign.id == campaign.id).with_for_update())
    db.refresh(campaign)

    if str(campaign.status or "").lower() not in SEEDABLE_STATUSES:
        raise WorldSeedError(
            f"Campaign status {campaign.status} cannot be seeded (world seed runs pre-start)"
        )
    if has_domain_event(db, campaign.id, WORLD_SEEDED_EVENT):
        return _seeded_snapshot(db, campaign, replayed=True)

    members = db.execute(
        _select(CampaignMember).where(CampaignMember.campaign_id == campaign.id)
    ).scalars().all()
    # Deterministic member order so derived hook indices (and their
    # idempotency keys) are stable across retries.
    members = sorted(members, key=lambda m: (str(m.user_id), str(m.selected_character_id)))
    eligibility = compute_start_eligibility(campaign, members, db)
    if not eligibility.get("eligible"):
        raise WorldSeedError(
            f"World seed not eligible: {'; '.join(eligibility.get('blockers') or [])}"
        )

    from app.campaigns.party_lore import build_party_composition, get_seed_lore_bundle

    composition = build_party_composition(db, members)
    lore_bundle = get_seed_lore_bundle(db, campaign_id=campaign.id)
    required_pc_ids = [
        str(m.selected_character_id) for m in members
        if getattr(m, "selected_character_id", None) is not None
    ]
    spec, candidates_tried = generate_seed_spec(
        campaign_id=str(campaign.id),
        theme=getattr(campaign, "theme", None),
        brief=getattr(campaign, "brief", None),
        difficulty=getattr(campaign, "difficulty", None) or "medium",
        content_boundaries=getattr(campaign, "content_boundaries", None),
        composition=composition,
        lore_bundle=lore_bundle,
        required_pc_ids=required_pc_ids,
    )

    # Lore-presence counts stay server-side only: they never enter the
    # public event payload or the HTTP projection, so one player's lore
    # presence is not inferable from another player's visible output (#244).
    lore_consumed = len(lore_bundle)
    # Secret-derived hooks whose rendered text violates the campaign's
    # content boundaries are silently suppressed at stage time: boundaries
    # constrain generated material before acceptance, but suppression must
    # not change any owner-visible outcome (#244 oracle).
    hook_deny = _deny_phrases(getattr(campaign, "content_boundaries", None))
    # Raw lore text for boundary matching only: a lore-derived hook is
    # suppressed when a deny phrase matches either the restricted lore input
    # or the rendered hook text. This map never leaves the server and
    # suppression never surfaces in public output (#244 oracle).
    lore_text_by_char = {
        str(entry.get("character_id")): str(entry.get("content") or "")
        for entry in (lore_bundle or [])
    }

    holder: dict = {}
    from_status = str(campaign.status)

    def _mutate(locked):
        prior = int(locked.revision) if locked.revision is not None else 0
        prov = _provenance()
        ck = lambda slot: f"{WORLD_SEED_TAG}:{slot}:{locked.id}"

        location, _ = create_entity_inline(
            db, locked, entity_type="location", name=spec["location"]["name"],
            summary=spec["location"]["summary"], status="active", visibility="campaign",
            details={"seed": WORLD_SEED_TAG, "contract_version": WORLD_SEED_CONTRACT_VERSION},
            operation_id=f"{operation_id}:location", idempotency_key=ck("location"),
        )
        faction, _ = create_entity_inline(
            db, locked, entity_type="faction", name=spec["faction"]["name"],
            summary=spec["faction"]["summary"], status="active", visibility="campaign",
            details={"seed": WORLD_SEED_TAG, "contract_version": WORLD_SEED_CONTRACT_VERSION},
            operation_id=f"{operation_id}:faction", idempotency_key=ck("faction"),
        )
        npc_rows = []
        for idx, npc in enumerate(spec["npcs"]):
            row, _ = create_entity_inline(
                db, locked, entity_type="npc", name=npc["name"],
                summary=npc["summary"], status="active", visibility="campaign",
                details={"seed": WORLD_SEED_TAG, "contract_version": WORLD_SEED_CONTRACT_VERSION},
                operation_id=f"{operation_id}:npc:{idx}", idempotency_key=ck(f"npc:{idx}"),
            )
            npc_rows.append(row)
            create_relation_inline(
                db, locked, subject_entity_id=row.id, relation_type="member_of",
                object_entity_id=faction.id, epistemic_state="confirmed", visibility="campaign",
                provenance=prov, operation_id=f"{operation_id}:rel:{idx}",
                idempotency_key=ck(f"rel:{idx}"),
            )
            create_relation_inline(
                db, locked, subject_entity_id=row.id, relation_type="present_at",
                object_entity_id=location.id, epistemic_state="confirmed", visibility="campaign",
                provenance=prov, operation_id=f"{operation_id}:at:{idx}",
                idempotency_key=ck(f"at:{idx}"),
            )
        char_rows = []
        for pc in spec["pcs"]:
            row, _ = create_entity_inline(
                db, locked, entity_type="character", name=pc["character_name"],
                summary=f"Player character adventuring from {spec['location']['name']}.",
                status="active", visibility="campaign",
                details={"seed": WORLD_SEED_TAG, "character_id": str(pc["character_id"])},
                operation_id=f"{operation_id}:pc:{pc['character_id']}",
                idempotency_key=ck(f"pc:{pc['character_id']}"),
            )
            char_rows.append(row)

        situation_fact, _ = create_fact_inline(
            db, locked, content=spec["situation"], entity_refs=[location.id],
            epistemic_state="confirmed", visibility="campaign", provenance=prov,
            operation_id=f"{operation_id}:situation", idempotency_key=ck("situation"),
        )
        secret_fact, _ = create_fact_inline(
            db, locked,
            content=f"Hidden scheme behind {spec['pressure']['name']}: {spec['pressure']['description']}",
            entity_refs=[location.id], epistemic_state="confirmed", visibility="dm_only",
            provenance=prov, operation_id=f"{operation_id}:secret",
            idempotency_key=ck("secret"),
        )
        hook_fact_by_char: dict[str, str] = {}
        hook_count = 0
        suppressed_hooks = 0
        for idx, hook in enumerate(spec["hooks"]):
            # Kind-labeled metadata reference only — raw lore content never
            # enters canon; the kind makes lore differences visible to the
            # DM without exposing recoverable text. Hooks violating the
            # campaign's content boundaries are silently dropped (no
            # owner-visible signal either way).
            hook_text = (
                f"Unrevealed {hook['kind']} hook for {hook['character_name']} "
                f"(private lore v{hook['lore_version']}); the DM may surface it through play."
            )
            lore_source = lore_text_by_char.get(str(hook["character_id"]), "")
            if any(
                phrase and (phrase in hook_text.lower() or phrase in lore_source.lower())
                for phrase in hook_deny
            ):
                suppressed_hooks += 1
                continue
            hook_fact, _ = create_fact_inline(
                db, locked, content=hook_text,
                epistemic_state="confirmed", visibility="dm_only", provenance=prov,
                operation_id=f"{operation_id}:hook:{idx}", idempotency_key=ck(f"hook:{idx}"),
            )
            hook_count += 1
            hook_fact_by_char[str(hook["character_id"])] = str(hook_fact.id)
        if suppressed_hooks:
            logger.info(
                "world_seed hooks_suppressed_by_boundary campaign_id=%s count=%s",
                campaign.id, suppressed_hooks,
            )

        # Party knowledge: every seeded PC knows the starting situation;
        # each hooked PC holds its own unrevealed hook (DM-restricted record).
        for row in char_rows:
            assert_knowledge_inline(
                db, locked, subject_kind="character", subject_entity_id=row.id,
                target_kind="fact", target_fact_id=situation_fact.id,
                knowledge_state="knows", acquisition_source="world_seed",
                visibility="campaign", provenance=prov,
                operation_id=f"{operation_id}:know:{row.id}",
                idempotency_key=ck(f"know:{row.id}"),
            )
        char_by_pc_id = {
            str(pc["character_id"]): row for pc, row in zip(spec["pcs"], char_rows)
        }
        for hook in spec["hooks"]:
            row = char_by_pc_id.get(str(hook["character_id"]))
            hook_fact_id = hook_fact_by_char.get(str(hook["character_id"]))
            if hook_fact_id is None:
                # Boundary-suppressed hook: no staged fact, no knowledge row.
                continue
            if row is None:
                raise WorldSeedError("World seed hook does not resolve to a seeded character")
            assert_knowledge_inline(
                db, locked, subject_kind="character", subject_entity_id=row.id,
                target_kind="fact", target_fact_id=hook_fact_id,
                knowledge_state="knows", acquisition_source="private_lore",
                visibility="dm_only", provenance=prov,
                operation_id=f"{operation_id}:hookknow:{row.id}",
                idempotency_key=ck(f"hookknow:{row.id}"),
            )

        threshold = int(spec["pressure"]["threshold"])
        clock, _ = create_clock_inline(
            db, locked, name=spec["pressure"]["name"], threshold=threshold,
            advancement_criteria={
                "kind": "deterministic",
                "event_types": ["dm.turn_committed"],
                "required_count": 2,
                "max_advance": 1,
            },
            status="active", progress=0,
            stages=[{"at": max(1, threshold - 1), "label": f"{spec['pressure']['name']} escalates"}],
            completion_criteria={
                "kind": "semantic",
                "description": f"Decisively end {spec['pressure']['name']} through play",
                "event_types": ["dm.turn_committed"],
            },
            visibility="campaign", provenance=prov,
            operation_id=f"{operation_id}:clock", idempotency_key=ck("clock"),
        )
        scene = apply_scene_update_inline(
            db, locked, new_revision=prior + 1, location_entity_id=location.id,
            location_name=spec["location"]["name"], fictional_time=spec["fictional_time"],
            present_actors=[{"name": p["character_name"], "kind": "pc"} for p in spec["pcs"]],
            environment={
                "premise": spec["premise"],
                "seed": WORLD_SEED_TAG,
                "contract_version": WORLD_SEED_CONTRACT_VERSION,
            },
            visibility="campaign", operation_id=f"{operation_id}:scene",
        )
        if from_status == "lobby":
            locked.status = "starting"
        holder.update({
            "location_id": str(location.id), "faction_id": str(faction.id),
            "npc_ids": [str(r.id) for r in npc_rows],
            "character_ids": [str(r.id) for r in char_rows],
            "situation_fact_id": str(situation_fact.id),
            "secret_fact_id": str(secret_fact.id),
            "clock_id": str(clock.id), "hook_count": hook_count,
            "scene": scene.to_dict(),
        })

    expected = int(campaign.revision)
    campaign_after, _event = commit_campaign_mutation(
        db, campaign.id, expected, event_type=WORLD_SEEDED_EVENT,
        payload_builder=lambda: {
            "contract_version": WORLD_SEED_CONTRACT_VERSION,
            "location_entity_id": holder.get("location_id"),
            "npc_count": len(holder.get("npc_ids") or []),
            "character_count": len(holder.get("character_ids") or []),
            "clock_id": holder.get("clock_id"),
            "candidates_tried": candidates_tried,
            "seed": WORLD_SEED_TAG,
        },
        operation_id=f"{operation_id}:seeded",
        actor_id=actor_id,
        targets={"campaign_id": str(campaign.id)},
        visibility="public",
        provenance=_provenance(),
        mutate=_mutate,
        commit=False,
    )
    db.refresh(campaign_after)
    logger.info(
        "world_seed staged campaign_id=%s status=%s location=%s npcs=%s clock=%s candidates=%s hooks=%s lore=%s revision=%s",
        campaign.id, campaign_after.status, spec["location"]["name"],
        len(holder.get("npc_ids") or []), holder.get("clock_id"),
        candidates_tried, holder.get("hook_count", 0), lore_consumed,
        campaign_after.revision,
    )
    snapshot = _seeded_snapshot(db, campaign_after, replayed=False)
    snapshot["seed"].update({
        "candidates_tried": candidates_tried,
        "location": {"entity_id": holder.get("location_id"), "name": spec["location"]["name"]},
        "npcs": [
            {"entity_id": eid, "name": npc["name"]}
            for eid, npc in zip(holder.get("npc_ids") or [], spec["npcs"])
        ],
        "faction": {"entity_id": holder.get("faction_id"), "name": spec["faction"]["name"]},
        "clock": {"id": holder.get("clock_id"), "name": spec["pressure"]["name"]},
        "scene": holder.get("scene"),
    })
    return snapshot


def _seeded_snapshot(db: Session, campaign, *, replayed: bool) -> dict:
    """Secret-free seed snapshot (converged replay or fresh seed base).

    Lore-presence counts are deliberately absent: they stay in server-side
    logs only, so one player's private lore is not inferable from shared
    or owner-visible seed output.
    """
    from app.campaigns.service import compute_start_eligibility
    from models.campaigns import CampaignMember
    from models.world import CampaignClock, CampaignCurrentScene
    from sqlalchemy import select as _select

    members = db.execute(
        _select(CampaignMember).where(CampaignMember.campaign_id == campaign.id)
    ).scalars().all()
    eligibility = compute_start_eligibility(campaign, list(members), db)
    scene = db.get(CampaignCurrentScene, campaign.id)
    clocks = db.execute(
        _select(CampaignClock).where(CampaignClock.campaign_id == campaign.id)
    ).scalars().all()
    clocks = list(clocks)
    snapshot_seed: dict = {
        "contract_version": WORLD_SEED_CONTRACT_VERSION,
        "tag": WORLD_SEED_TAG,
        "replayed": replayed,
        "clock_count": len(clocks),
        "scene_present": scene is not None,
        "eligibility": eligibility,
    }
    if clocks:
        snapshot_seed["clock"] = {"id": str(clocks[0].id), "name": clocks[0].name}
    if scene is not None:
        snapshot_seed["scene"] = scene.to_dict()
    return {
        "campaign": campaign.to_dict(),
        "seed": snapshot_seed,
        "eligibility": eligibility,
    }

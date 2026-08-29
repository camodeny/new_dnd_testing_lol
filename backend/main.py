import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from auth import get_current_profile
from database import Base, db_healthcheck, engine, get_database_url, get_db
from models import Campaign, CampaignInvite, CampaignMember, Character, CharacterChatMessage as DbChatMessage, Dnd5eCharacterSheet, Profile

load_dotenv()

import uuid as uuid_lib
from sqlalchemy.orm import Session
from sqlalchemy import select

logger = logging.getLogger(__name__)


def _run_migrations():
    """Run `alembic upgrade head` on startup when DATABASE_URL is set.

    This makes deploys self-migrating — no manual `alembic upgrade head` needed.
    Uses Alembic's Python API (same as CLI) and is safe to run concurrently;
    Alembic acquires a row lock on alembic_version.
    """
    db_url = get_database_url()
    if not db_url:
        logger.info("Skipping migrations — DATABASE_URL not set")
        return
    try:
        from alembic.config import Config
        from alembic import command

        alembic_ini = os.path.join(os.path.dirname(__file__), "alembic.ini")
        if not os.path.exists(alembic_ini):
            logger.warning("alembic.ini not found at %s — falling back to create_all", alembic_ini)
            Base.metadata.create_all(bind=engine)
            return

        cfg = Config(alembic_ini)
        cfg.set_main_option("sqlalchemy.url", db_url.replace("%", "%%"))
        # Ensure backend/ is on path for env.py imports when running from Vercel's wd
        cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "alembic"))
        command.upgrade(cfg, "head")
        logger.info("Alembic migrations applied (head)")
    except Exception as e:
        # Don't crash boot — log and fallback to create_all so health still works
        logger.warning("Alembic upgrade failed (%s) — falling back to create_all: %s", type(e).__name__, e)
        try:
            if engine is not None:
                Base.metadata.create_all(bind=engine)
        except Exception as ce:
            logger.warning("create_all fallback also failed: %s", ce)

APP_NAME = "dnd-backend"
APP_VERSION = "0.1.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Auto-migrate on boot so deploys don't require manual `alembic upgrade head`.
    # Checks DATABASE_URL/POSTGRES_URL inside _run_migrations so it works even if
    # engine was None at import time (e.g. env injected after module load).
    _run_migrations()
    yield


app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health():
    db_ok = db_healthcheck()
    has_db_env = any(
        os.getenv(k) for k in ("POSTGRES_URL", "POSTGRES_PRISMA_URL", "POSTGRES_URL_NON_POOLING", "DATABASE_URL")
    )
    return {
        "status": "ok",
        "service": APP_NAME,
        "version": APP_VERSION,
        "db": "ok" if db_ok else "unconfigured" if not has_db_env else "unreachable",
    }


@app.get("/api/hello")
def hello(name: str = "world"):
    return {"message": f"Hello, {name}! From the Python (FastAPI) backend."}


# ── Auth (Supabase JWT -> profiles) ──────────────────────────────────────


@app.get("/api/me")
def me(profile: Profile = Depends(get_current_profile)):
    """Return the authenticated user's profile. Verifies Supabase JWT via Authorization header."""
    return {"user": profile.to_dict()}


@app.get("/api/auth/config")
def auth_config():
    supabase_url = os.getenv("SUPABASE_URL") or os.getenv("NEXT_PUBLIC_SUPABASE_URL") or ""
    has_key = bool(
        os.getenv("SUPABASE_PUBLISHABLE_KEY")
        or os.getenv("NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY")
        or os.getenv("SUPABASE_SECRET_KEY")
    )
    return {
        "sso_enabled": False,
        "supabase_url": supabase_url or None,
        "supabase_configured": bool(supabase_url and has_key),
    }


@app.get("/api/db/ping")
def db_ping():
    """Quick SQLAlchemy sanity check — remove after wiring."""
    if engine is None:
        return {"db": "unconfigured", "hint": "Set POSTGRES_URL in Vercel env"}
    try:
        with engine.connect() as conn:
            val = conn.execute(text("SELECT 1")).scalar()
        return {"db": "ok", "result": val}
    except Exception as e:
        return {"db": "error", "error": str(e)}


# ── Characters CRUD (Supabase) ───────────────────────────────────────────

# Use real auth user for mock (FK to auth.users)
MOCK_USER_ID = uuid_lib.UUID("23f3b2d1-efb6-4785-9a67-fa7ca57d72a3")


def _get_or_create_mock_profile(db: Session) -> Profile:
    profile = db.get(Profile, MOCK_USER_ID)
    if not profile:
        # ensure FK exists: auth.users already has this id per DB
        try:
            profile = Profile(id=MOCK_USER_ID, email="camdenpendergrass@gmail.com", username="dev")
            db.add(profile)
            db.commit()
            db.refresh(profile)
        except Exception:
            db.rollback()
            # fallback: try to fetch any existing profile
            existing = db.execute(select(Profile)).scalars().first()
            if existing:
                return existing
            raise
    return profile


def _is_mock_auth_allowed() -> bool:
    # Explicit local-dev gate; invalid credentials never fall through to mock.
    # Set ALLOW_MOCK_AUTH=true in backend/.env for local dev (never in production).
    if os.getenv("ALLOW_MOCK_AUTH", "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    # Legacy dev flag used by frontend; also allow when explicitly set
    if os.getenv("NEXT_PUBLIC_MOCK_USER", "").strip().lower() in ("1", "true"):
        return True
    # Vercel production should not allow mock
    if os.getenv("VERCEL_ENV") == "production":
        return False
    # Default: allow mock only when no prod env is set (local dev)
    return os.getenv("VERCEL_ENV") is None and os.getenv("NODE_ENV") != "production"


def _resolve_profile(request: Request, db: Session) -> Profile:
    """Resolve profile via verified JWT; mock only when no credentials and mock is explicitly allowed."""
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    has_credentials = bool(auth and auth.strip())
    if has_credentials:
        if not auth.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="Invalid Authorization header")
        token = auth[7:].strip()
        if not token:
            raise HTTPException(status_code=401, detail="Missing token")
        # Any supplied credentials must verify; never fall through to mock.
        from auth import verify_supabase_jwt

        payload = verify_supabase_jwt(token)  # raises 401 on failure
        sub = payload.get("sub")
        if not sub:
            raise HTTPException(status_code=401, detail="Token missing sub")
        try:
            uid = uuid_lib.UUID(str(sub))
        except ValueError:
            raise HTTPException(status_code=401, detail="Invalid sub format")
        prof = db.get(Profile, uid)
        if prof:
            return prof
        email = payload.get("email")
        md = payload.get("user_metadata") or {}
        username = md.get("username") or md.get("full_name") or md.get("name")
        prof = Profile(id=uid, email=email, username=username)
        db.add(prof)
        db.commit()
        db.refresh(prof)
        return prof

    # No credentials supplied — allow mock only behind explicit flag
    if _is_mock_auth_allowed():
        return _get_or_create_mock_profile(db)
    raise HTTPException(status_code=401, detail="Missing Authorization header")


def _character_with_sheet(db: Session, char: Character):
    sheet = db.execute(select(Dnd5eCharacterSheet).where(Dnd5eCharacterSheet.character_id == char.id).order_by(Dnd5eCharacterSheet.updated_at.desc())).scalars().first()
    data = char.to_dict()
    if sheet:
        sheet_data = sheet.to_dict()
        # don't overwrite character identity fields with sheet's own ids
        for k in ("id", "character_id", "owner_id", "created_at", "updated_at"):
            sheet_data.pop(k, None)
        data.update(sheet_data)
        data["sheet"] = sheet.to_dict()
        # ensure top-level id stays as character id
        data["id"] = str(char.id)
    return data


@app.get("/api/characters")
def list_characters(request: Request, db: Session = Depends(get_db)):
    profile = _resolve_profile(request, db)
    chars = db.execute(select(Character).where(Character.owner_id == profile.id).order_by(Character.updated_at.desc())).scalars().all()
    result = []
    for c in chars:
        result.append(_character_with_sheet(db, c))
    return {"characters": result}


@app.post("/api/characters")
def create_character(request: Request, payload: dict, db: Session = Depends(get_db)):
    profile = _resolve_profile(request, db)
    # payload is from toCharacterPayload (flat + lists)
    name = payload.get("name") or payload.get("character_name") or "Unnamed Hero"
    char = Character(owner_id=profile.id, name=name, system=payload.get("system") or "dnd5e")
    db.add(char)
    db.flush()  # get char.id
    try:
        sheet = Dnd5eCharacterSheet.from_frontend(payload, owner_id=profile.id)
        sheet.character_id = char.id
        # ensure character_name matches
        if not sheet.character_name:
            sheet.character_name = name
        db.add(sheet)
        db.commit()
        db.refresh(char)
        # refresh sheet
        sheet = db.execute(select(Dnd5eCharacterSheet).where(Dnd5eCharacterSheet.character_id == char.id)).scalars().first()
    except Exception as e:
        db.rollback()
        logger.exception("create character failed")
        raise HTTPException(status_code=400, detail=str(e))
    return {"character": _character_with_sheet(db, char)}


@app.get("/api/characters/{character_id}")
def get_character(character_id: str, request: Request, db: Session = Depends(get_db)):
    profile = _resolve_profile(request, db)
    try:
        cid = uuid_lib.UUID(character_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid character id")
    char = db.get(Character, cid)
    if not char or char.owner_id != profile.id:
        raise HTTPException(status_code=404, detail="Character not found")
    return {"character": _character_with_sheet(db, char)}


@app.put("/api/characters/{character_id}")
def update_character(character_id: str, payload: dict, request: Request, db: Session = Depends(get_db)):
    profile = _resolve_profile(request, db)
    try:
        cid = uuid_lib.UUID(character_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid character id")
    char = db.get(Character, cid)
    if not char or char.owner_id != profile.id:
        raise HTTPException(status_code=404, detail="Character not found")
    # update character name
    new_name = payload.get("name") or payload.get("character_name")
    if new_name:
        char.name = new_name
    # update sheet
    sheet = db.execute(select(Dnd5eCharacterSheet).where(Dnd5eCharacterSheet.character_id == char.id)).scalars().first()
    if sheet:
        # simple: update fields from from_frontend mapping but keep same row
        try:
            updated = Dnd5eCharacterSheet.from_frontend(payload, owner_id=profile.id)
            for col in Dnd5eCharacterSheet.__table__.columns:
                key = col.name
                if key in ("id", "character_id", "owner_id", "created_at"):
                    continue
                val = getattr(updated, key, None)
                # only overwrite if payload had that key (check payload presence heuristically)
                # for now overwrite if not None/default
                if val is not None:
                    setattr(sheet, key, val)
            if new_name:
                sheet.character_name = new_name
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))
    else:
        sheet = Dnd5eCharacterSheet.from_frontend(payload, owner_id=profile.id)
        sheet.character_id = char.id
        db.add(sheet)
    db.commit()
    db.refresh(char)
    return {"character": _character_with_sheet(db, char)}


@app.delete("/api/characters/{character_id}")
def delete_character(character_id: str, request: Request, db: Session = Depends(get_db)):
    profile = _resolve_profile(request, db)
    try:
        cid = uuid_lib.UUID(character_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid character id")
    char = db.get(Character, cid)
    if not char or char.owner_id != profile.id:
        raise HTTPException(status_code=404, detail="Character not found")
    db.delete(char)
    db.commit()
    return {"ok": True}


# ── Campaigns ─────────────────────────────────────────────────────────────

import random
import secrets
import string as _string


def _generate_invite_code(length: int = 8) -> str:
    alphabet = _string.ascii_uppercase + _string.digits
    # avoid ambiguous 0/O 1/I
    alphabet = alphabet.replace("0", "").replace("O", "").replace("1", "").replace("I", "")
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _is_campaign_member(db: Session, campaign_id: uuid_lib.UUID, user_id: uuid_lib.UUID) -> bool:
    return db.get(CampaignMember, {"campaign_id": campaign_id, "user_id": user_id}) is not None


def _parse_campaign_id(campaign_id: str) -> uuid_lib.UUID:
    try:
        return uuid_lib.UUID(str(campaign_id))
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid campaign id")


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


def _random_brief(seed: str | None = None) -> dict:
    return {
        "name": random.choice(RANDOM_CAMPAIGN_NAMES),
        "description": random.choice(RANDOM_CAMPAIGN_DESCS),
        "random_seed": seed or _generate_invite_code(6),
    }


@app.get("/api/campaigns")
def list_campaigns(request: Request, db: Session = Depends(get_db)):
    profile = _resolve_profile(request, db)
    # campaigns where user is owner or member
    member_rows = db.execute(select(CampaignMember.campaign_id).where(CampaignMember.user_id == profile.id)).scalars().all()
    member_ids = set(member_rows)
    rows = db.execute(select(Campaign).order_by(Campaign.updated_at.desc())).scalars().all()
    visible = [c for c in rows if c.owner_id == profile.id or c.id in member_ids]
    return {"campaigns": [c.to_dict() for c in visible]}


@app.post("/api/campaigns")
def create_campaign(payload: dict, request: Request, db: Session = Depends(get_db)):
    profile = _resolve_profile(request, db)
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Campaign name is required")
    if len(name) > 128:
        raise HTTPException(status_code=400, detail="Campaign name must be 128 characters or fewer")
    description = payload.get("description")
    random_seed = payload.get("random_seed") or payload.get("seed")
    if random_seed is not None:
        random_seed = str(random_seed).strip()
        if random_seed and len(random_seed) > 128:
            raise HTTPException(status_code=400, detail="Seed must be 128 characters or fewer")
        random_seed = random_seed or None
    try:
        required_players = int(payload.get("required_players") or payload.get("requiredPlayers") or 1)
    except Exception:
        required_players = 1
    required_players = max(1, min(8, required_players))
    loot_mode = str(payload.get("loot_mode") or payload.get("lootMode") or "frequent_gamble")
    if loot_mode not in ("frequent_gamble", "rare_treasure", "generous", "scarce", "rare_quality", "frequent", "rare"):
        loot_mode = "frequent_gamble"
    camp = Campaign(
        owner_id=profile.id,
        name=name,
        description=description,
        random_seed=random_seed,
        required_players=required_players,
        loot_mode=loot_mode,
    )
    db.add(camp)
    db.flush()
    # owner is first member
    member = CampaignMember(campaign_id=camp.id, user_id=profile.id, role="owner")
    db.add(member)
    db.commit()
    db.refresh(camp)
    return {"campaign": camp.to_dict()}


@app.post("/api/campaigns/random-brief")
def random_campaign_brief(payload: dict, request: Request, db: Session = Depends(get_db)):
    _resolve_profile(request, db)
    seed = payload.get("random_seed") or payload.get("seed") or None
    brief = _random_brief(seed if isinstance(seed, str) else None)
    # also include aliases used by legacy CampaignForm
    brief["seed"] = brief["random_seed"]
    return brief


@app.post("/api/campaigns/quick-create")
def quick_create_campaign(payload: dict, request: Request, db: Session = Depends(get_db)):
    profile = _resolve_profile(request, db)
    brief = _random_brief()
    try:
        required_players = int(payload.get("required_players") or 1)
    except Exception:
        required_players = 1
    required_players = max(1, min(8, required_players))
    loot_mode = str(payload.get("loot_mode") or "frequent_gamble")
    camp = Campaign(
        owner_id=profile.id,
        name=brief["name"],
        description=brief["description"],
        random_seed=brief["random_seed"],
        required_players=required_players,
        loot_mode=loot_mode,
    )
    db.add(camp)
    db.flush()
    db.add(CampaignMember(campaign_id=camp.id, user_id=profile.id, role="owner"))
    db.commit()
    db.refresh(camp)
    return {"campaign": camp.to_dict(), "brief": brief}


@app.get("/api/campaigns/{campaign_id}")
def get_campaign(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    profile = _resolve_profile(request, db)
    cid = _parse_campaign_id(campaign_id)
    camp = db.get(Campaign, cid)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if camp.owner_id != profile.id and not _is_campaign_member(db, camp.id, profile.id):
        raise HTTPException(status_code=403, detail="Not a member of this campaign")
    return {"campaign": camp.to_dict()}


@app.delete("/api/campaigns/{campaign_id}")
def delete_campaign(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    profile = _resolve_profile(request, db)
    cid = _parse_campaign_id(campaign_id)
    camp = db.get(Campaign, cid)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if camp.owner_id != profile.id:
        raise HTTPException(status_code=403, detail="Only the owner can delete this campaign")
    db.delete(camp)
    db.commit()
    return {"ok": True}


@app.put("/api/campaigns/{campaign_id}")
def update_campaign(campaign_id: str, payload: dict, request: Request, db: Session = Depends(get_db)):
    profile = _resolve_profile(request, db)
    cid = _parse_campaign_id(campaign_id)
    camp = db.get(Campaign, cid)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if camp.owner_id != profile.id:
        raise HTTPException(status_code=403, detail="Only owner can update")
    if "name" in payload and payload["name"] is not None:
        new_name = str(payload["name"]).strip()
        if not new_name:
            raise HTTPException(status_code=400, detail="Campaign name is required")
        if len(new_name) > 128:
            raise HTTPException(status_code=400, detail="Campaign name must be 128 characters or fewer")
        camp.name = new_name
    if "description" in payload:
        camp.description = payload["description"]
    if "random_seed" in payload or "seed" in payload:
        raw_seed = str(payload.get("random_seed") or payload.get("seed") or "").strip()
        if raw_seed and len(raw_seed) > 128:
            raise HTTPException(status_code=400, detail="Seed must be 128 characters or fewer")
        camp.random_seed = raw_seed or None
    db.commit()
    db.refresh(camp)
    return {"campaign": camp.to_dict()}


# members / invites

@app.get("/api/campaigns/{campaign_id}/members")
def list_campaign_members(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    profile = _resolve_profile(request, db)
    cid = _parse_campaign_id(campaign_id)
    camp = db.get(Campaign, cid)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if camp.owner_id != profile.id and not _is_campaign_member(db, cid, profile.id):
        raise HTTPException(status_code=403, detail="Not a member")
    members = db.execute(select(CampaignMember).where(CampaignMember.campaign_id == cid)).scalars().all()
    out = []
    for m in members:
        prof = db.get(Profile, m.user_id)
        out.append({
            "user_id": str(m.user_id),
            "username": prof.username if prof else "adventurer",
            "email": prof.email if prof else None,
            "role": m.role,
        })
    return {"members": out}


@app.get("/api/campaigns/{campaign_id}/characters")
def list_campaign_characters(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    _resolve_profile(request, db)
    # stub: no campaign-characters yet
    return {"characters": []}


@app.get("/api/campaigns/{campaign_id}/invites")
def get_campaign_invite(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    profile = _resolve_profile(request, db)
    cid = _parse_campaign_id(campaign_id)
    camp = db.get(Campaign, cid)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if camp.owner_id != profile.id:
        raise HTTPException(status_code=403, detail="Only owner can view invite")
    inv = db.get(CampaignInvite, cid)
    if not inv:
        return {"code": None}
    return {"code": inv.code}


@app.post("/api/campaigns/{campaign_id}/invites")
def create_campaign_invite(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    profile = _resolve_profile(request, db)
    cid = _parse_campaign_id(campaign_id)
    camp = db.get(Campaign, cid)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if camp.owner_id != profile.id:
        raise HTTPException(status_code=403, detail="Only owner can create invite")
    existing = db.get(CampaignInvite, cid)
    if existing:
        return {"code": existing.code}
    # ensure unique code
    for _ in range(5):
        code = _generate_invite_code()
        if not db.execute(select(CampaignInvite).where(CampaignInvite.code == code)).scalars().first():
            inv = CampaignInvite(campaign_id=cid, code=code)
            db.add(inv)
            db.commit()
            return {"code": code}
    raise HTTPException(status_code=500, detail="Failed to generate invite")


@app.get("/api/invites/lookup")
def lookup_invite(code: str, request: Request, db: Session = Depends(get_db)):
    _resolve_profile(request, db)
    clean = code.strip().upper()
    if not clean:
        raise HTTPException(status_code=400, detail="Code required")
    inv = db.execute(select(CampaignInvite).where(CampaignInvite.code == clean)).scalars().first()
    if not inv:
        raise HTTPException(status_code=404, detail="Invite not found")
    camp = db.get(Campaign, inv.campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return {"campaign_id": str(camp.id), "campaign": camp.to_dict()}


@app.post("/api/campaigns/{campaign_id}/join")
def join_campaign(campaign_id: str, payload: dict, request: Request, db: Session = Depends(get_db)):
    profile = _resolve_profile(request, db)
    cid = _parse_campaign_id(campaign_id)
    camp = db.get(Campaign, cid)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    code = str(payload.get("code") or "").strip().upper()
    if not code:
        raise HTTPException(status_code=400, detail="Invite code required")
    inv = db.get(CampaignInvite, cid)
    if not inv or inv.code != code:
        # also allow lookup by code matching any campaign
        inv2 = db.execute(select(CampaignInvite).where(CampaignInvite.code == code)).scalars().first()
        if not inv2 or inv2.campaign_id != cid:
            raise HTTPException(status_code=403, detail="Invalid invite code")
    if not _is_campaign_member(db, cid, profile.id):
        db.add(CampaignMember(campaign_id=cid, user_id=profile.id, role="player"))
        db.commit()
    return {"ok": True, "campaign": camp.to_dict()}


# ── sessions stubs (so lobby doesn't 500) ─────────────────────────────────
@app.post("/api/campaigns/{campaign_id}/sessions")
def stub_start_session(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    raise HTTPException(status_code=501, detail="Sessions not yet implemented")

@app.get("/api/campaigns/{campaign_id}/world")
def stub_get_world(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    return {"world": None}

@app.get("/api/campaigns/{campaign_id}/encounter-maps/current")
def stub_encounter_map(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    return {"map": None}


# ── Character chat (SSE via opencode_go) ─────────────────────────────────

CHARACTER_CHAT_SYSTEM = (
    "You are a D&D 5e character creation assistant embedded in a character creator form. "
    "Help the user build their character via friendly chat. Keep replies concise (1-2 paragraphs), "
    "conversational, and supportive. Offer specific 5e suggestions when helpful.\n"
    "The frontend form has 5 steps: identity (name/race/classes/background/alignment), "
    "scores (ability scores/skills/saves), combat (HP/AC/speed etc), magic_gear (spells/equipment/currency), "
    "story (personality/appearance/backstory/features). "
    "When the user describes a character, infer sensible stats but do not hallucinate more than needed. "
    "Ask clarifying questions if the idea is vague."
)

CHARACTER_PATCH_TOOL = {
    "type": "function",
    "function": {
        "name": "apply_character_patch",
        "description": "Apply a patch to the D&D character draft form. Call when you have confident fields to fill in.",
        "parameters": {
            "type": "object",
            "properties": {
                "form_patch": {
                    "type": "object",
                    "description": "Partial CharacterDraft with only confident fields. Valid keys: name, player_name, race, subrace, alignment, background, experience_points, total_level, ability_scores {strength/dexterity/constitution/intelligence/wisdom/charisma 3-20}, combat {max_hp, current_hp, temp_hp, armor_class, initiative_bonus, speed}, general {proficiency_bonus, passive_perception etc}, spellcasting, currency, personality {personality_traits, ideals, bonds, flaws}, appearance {age,height,weight,eyes,skin,hair,character_appearance}, background_details {backstory, allies_organizations etc}, and lists: classes [{class_name, subclass, level, hit_die_type}], skills, saving_throws, proficiencies, features, weapons, equipment, spells, resources, companions, conditions.",
                    "additionalProperties": True,
                },
                "active_page": {
                    "type": "string",
                    "enum": ["identity", "scores", "combat", "magic_gear", "story"],
                    "description": "Most relevant form step for current idea",
                },
            },
            "required": ["form_patch"],
            "additionalProperties": False,
        },
    },
}


class CharacterChatMessage(BaseModel):
    role: str = Field(description="user | assistant | system")
    content: str


class CharacterChatRequest(BaseModel):
    content: str = Field(min_length=1, description="Current user message")
    history: list[CharacterChatMessage] = Field(default_factory=list)
    draft_character: Optional[dict] = Field(default=None, description="Current frontend draft for context")
    active_page: Optional[str] = None


def _get_character_chat_model() -> str:
    return os.getenv("OPENCODE_GO_MODEL", "").strip() or "muse-spark-1.2-contributor"


def _build_chat_messages(req: CharacterChatRequest) -> list[dict]:
    msgs: list[dict] = [{"role": "system", "content": CHARACTER_CHAT_SYSTEM}]
    # include draft context as subtle hint
    if req.draft_character:
        try:
            draft_hint = json.dumps(req.draft_character)[:4000]
            msgs.append({"role": "system", "content": f"Current draft (for context, do not echo raw JSON): {draft_hint}"})
        except Exception:
            pass
    for h in req.history[-12:]:
        role = h.role if h.role in ("user", "assistant") else "user"
        msgs.append({"role": role, "content": h.content})
    msgs.append({"role": "user", "content": req.content})
    return msgs


def _resolve_character_uuid(character_id: str) -> uuid_lib.UUID | None:
    if character_id == "new":
        return None
    try:
        return uuid_lib.UUID(character_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid character id")


def _save_chat_message(owner_id: uuid_lib.UUID, character_uuid: uuid_lib.UUID | None, role: str, content: str):
    try:
        from database import SessionLocal

        if SessionLocal is None:
            return
        db = SessionLocal()
        try:
            msg = DbChatMessage(
                owner_id=owner_id,
                character_id=character_uuid,
                role=role,
                content=content,
            )
            db.add(msg)
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.warning("failed to save chat message: %s", e)


def _character_chat_sync_generator(
    req: CharacterChatRequest, owner_id: uuid_lib.UUID, character_uuid: uuid_lib.UUID | None
):
    try:
        from llm_providers import ProviderRequest, provider_registry
    except Exception as e:
        yield f"event: error\ndata: {json.dumps({'error': f'llm_providers not available: {e}'})}\n\n"
        return

    model = _get_character_chat_model()
    messages = _build_chat_messages(req)

    try:
        adapter = provider_registry.get("opencode_go")
    except Exception as e:
        yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"
        return

    full_text = ""
    try:
        from llm_providers import ProviderRequest as PR, execute_chat

        pr = PR(
            messages=messages,
            model=model,
            tools=[CHARACTER_PATCH_TOOL],
            tool_choice="auto",
            allow_thinking=False,
            timeout_seconds=60,
        )
        resp = execute_chat(adapter, pr)
        full_text = resp.content or ""
        if full_text:
            for i in range(0, len(full_text), 24):
                chunk = full_text[i : i + 24]
                if chunk:
                    yield f"data: {json.dumps({'type': 'token', 'text': chunk})}\n\n"
        for tc in resp.tool_calls or []:
            try:
                args_raw = tc.arguments
                if isinstance(args_raw, str):
                    args = json.loads(args_raw) if args_raw else {}
                elif isinstance(args_raw, dict):
                    args = args_raw
                else:
                    args = {}
                if not isinstance(args, dict):
                    continue
                patch = args.get("form_patch") if isinstance(args.get("form_patch"), dict) else None
                if patch is None:
                    patch = {k: v for k, v in args.items() if k != "active_page"}
                ap = args.get("active_page")
                active_page = ap if ap in ("identity", "scores", "combat", "magic_gear", "story") else None
                if patch:
                    yield f"data: {json.dumps({'type': 'patch', 'patch': patch, 'active_page': active_page})}\n\n"
                    break
            except Exception as e:
                logger.warning("patch tool parse failed: %s", e)
        if not full_text:
            fallback = "I had trouble reaching the AI, but I can still help — tell me more about your character idea."
            full_text = fallback
            yield f"data: {json.dumps({'type': 'token', 'text': fallback})}\n\n"
    except Exception as e:
        logger.exception("character chat failed")
        yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"
        fallback = "I had trouble reaching the AI, but I can still help — tell me more about your character idea."
        full_text = fallback
        yield f"data: {json.dumps({'type': 'token', 'text': fallback})}\n\n"
    finally:
        if full_text:
            _save_chat_message(owner_id, character_uuid, "assistant", full_text)

    yield f"data: {json.dumps({'type': 'done'})}\n\n"


@app.get("/api/characters/{character_id}/chat")
def get_character_chat(character_id: str, request: Request, db: Session = Depends(get_db)):
    profile = _resolve_profile(request, db)
    character_uuid = _resolve_character_uuid(character_id)
    if character_uuid is not None:
        char = db.get(Character, character_uuid)
        if not char or char.owner_id != profile.id:
            raise HTTPException(status_code=404, detail="Character not found")
    msgs = (
        db.execute(
            select(DbChatMessage)
            .where(
                DbChatMessage.owner_id == profile.id,
                DbChatMessage.character_id == character_uuid,
            )
            .order_by(DbChatMessage.created_at.asc())
        )
        .scalars()
        .all()
    )
    return {
        "messages": [
            {"role": m.role, "content": m.content, "created_at": m.created_at.isoformat() if m.created_at else None}
            for m in msgs
        ]
    }


@app.delete("/api/characters/{character_id}/chat")
def delete_character_chat(character_id: str, request: Request, db: Session = Depends(get_db)):
    profile = _resolve_profile(request, db)
    character_uuid = _resolve_character_uuid(character_id)
    if character_uuid is not None:
        char = db.get(Character, character_uuid)
        if not char or char.owner_id != profile.id:
            raise HTTPException(status_code=404, detail="Character not found")
    from sqlalchemy import delete as sa_delete

    db.execute(
        sa_delete(DbChatMessage).where(
            DbChatMessage.owner_id == profile.id, DbChatMessage.character_id == character_uuid
        )
    )
    db.commit()
    return {"ok": True}


@app.post("/api/characters/{character_id}/chat")
async def character_chat(character_id: str, req: CharacterChatRequest, request: Request, db: Session = Depends(get_db)):
    """
    SSE chat for character creator. Requires verified profile.

    - character_id: "new" for unsaved draft, or existing character UUID (ownership validated)
    - streams tokens via text/event-stream: {type:"token", text} + {type:"patch", patch} + {type:"done"}
    - persists user + assistant messages for refresh persistence
    """
    profile = _resolve_profile(request, db)
    character_uuid = _resolve_character_uuid(character_id)
    if character_uuid is not None:
        char = db.get(Character, character_uuid)
        if not char or char.owner_id != profile.id:
            raise HTTPException(status_code=404, detail="Character not found")

    # persist user message before streaming (so history survives even if stream fails)
    _save_chat_message(profile.id, character_uuid, "user", req.content)

    return StreamingResponse(
        _character_chat_sync_generator(req, profile.id, character_uuid),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

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
from models import Character, Dnd5eCharacterSheet, Profile

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

PATCH_SYSTEM = (
    "You are a D&D 5e character sheet patch extractor. Given a conversation about a character, "
    "return ONLY valid JSON with keys: form_patch (object), active_page (string or null). "
    "form_patch should contain ONLY fields you are confident about for the CharacterDraft shape. "
    "Valid top-level keys: name, player_name, race, subrace, alignment, background, experience_points, "
    "total_level, ability_scores (strength/dexterity/constitution/intelligence/wisdom/charisma 3-20), "
    "combat (max_hp, current_hp, temp_hp, armor_class, initiative_bonus, speed), "
    "general (proficiency_bonus, passive_perception etc), spellcasting, currency, personality (personality_traits, ideals, bonds, flaws), "
    "appearance (age,height,weight,eyes,skin,hair,character_appearance), background_details (backstory, allies_organizations etc), "
    "and lists: classes [{class_name, subclass, level, hit_die_type}], skills, saving_throws, proficiencies, features, weapons, equipment, spells, resources, companions, conditions. "
    "Omit uncertain fields. active_page is one of identity, scores, combat, magic_gear, story or null - choose the most relevant step for the current idea."
)


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


def _character_chat_sync_generator(req: CharacterChatRequest):
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

    # --- stream chat message (true streaming) ---
    full_text = ""
    try:
        from llm_providers import ProviderRequest as PR, stream_chat

        pr = PR(
            messages=messages,
            model=model,
            stream=True,
            timeout_seconds=60,
        )
        for ev in stream_chat(adapter, pr):
            if ev.kind == "token" and ev.text:
                full_text += ev.text
                yield f"data: {json.dumps({'type': 'token', 'text': ev.text})}\n\n"
            elif ev.kind == "done":
                break
    except Exception as e:
        logger.exception("character chat stream failed")
        yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"
        if not full_text:
            full_text = "I had trouble reaching the AI, but I can still help — tell me more about your character idea."
            # send fallback as token so UI shows something
            yield f"data: {json.dumps({'type': 'token', 'text': full_text})}\n\n"

    # --- extract patch via second LLM call (json mode, blocking but fast) ---
    try:
        from llm_providers import ProviderRequest as PR, execute_chat

        patch_messages = [
            {"role": "system", "content": PATCH_SYSTEM},
            {"role": "user", "content": f"Conversation:\nUser: {req.content}\nAssistant: {full_text}\n\nCurrent draft: {json.dumps(req.draft_character or {})[:3000]}\n\nReturn form_patch + active_page."},
        ]
        pr = PR(
            messages=patch_messages,
            model=model,
            json_mode=True,
            timeout_seconds=30,
        )
        resp = execute_chat(adapter, pr)
        try:
            parsed = json.loads(resp.content) if resp.content else {}
        except Exception:
            parsed = {}
        if isinstance(parsed, dict):
            patch = parsed.get("form_patch") if isinstance(parsed.get("form_patch"), dict) else {}
            ap = parsed.get("active_page")
            active_page = ap if ap in ("identity", "scores", "combat", "magic_gear", "story") else None
            if patch:
                yield f"data: {json.dumps({'type': 'patch', 'patch': patch, 'active_page': active_page})}\n\n"
    except Exception as e:
        logger.warning("patch extraction failed: %s", e)

    yield f"data: {json.dumps({'type': 'done'})}\n\n"


@app.post("/api/characters/{character_id}/chat")
async def character_chat(character_id: str, req: CharacterChatRequest, request: Request, db: Session = Depends(get_db)):
    """
    SSE chat for character creator. Requires verified profile.

    - character_id: "new" for unsaved draft, or existing character UUID (ownership validated)
    - streams tokens via text/event-stream: {type:"token", text} + {type:"patch", patch} + {type:"done"}
    """
    profile = _resolve_profile(request, db)
    if character_id != "new":
        try:
            cid = uuid_lib.UUID(character_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="Invalid character id")
        char = db.get(Character, cid)
        if not char or char.owner_id != profile.id:
            raise HTTPException(status_code=404, detail="Character not found")

    return StreamingResponse(
        _character_chat_sync_generator(req),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

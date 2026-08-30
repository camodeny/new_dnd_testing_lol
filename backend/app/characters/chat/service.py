"""Character chat service — no FastAPI router import.

Provider interaction is isolated to `app.providers` abstraction; workflows
consume `provider_registry` and `stream_chat` without branching on provider.
"""
import json
import logging
import os
import uuid as uuid_lib
from typing import Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

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


def get_character_chat_model() -> str:
    return os.getenv("OPENCODE_GO_MODEL", "").strip() or "muse-spark-1.2-contributor"


def build_chat_messages(req: CharacterChatRequest) -> list[dict]:
    msgs: list[dict] = [{"role": "system", "content": CHARACTER_CHAT_SYSTEM}]
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


def resolve_character_uuid(character_id: str) -> uuid_lib.UUID | None:
    if character_id == "new":
        return None
    try:
        return uuid_lib.UUID(character_id)
    except ValueError as e:
        raise ValueError("Invalid character id") from e


def save_chat_message(owner_id: uuid_lib.UUID, character_uuid: uuid_lib.UUID | None, role: str, content: str):
    try:
        from database import SessionLocal

        if SessionLocal is None:
            return
        from models import CharacterChatMessage as DbChatMessage

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


def character_chat_sync_generator(
    req: CharacterChatRequest, owner_id: uuid_lib.UUID, character_uuid: uuid_lib.UUID | None
):
    try:
        from app.providers import provider_registry
    except Exception as e:
        yield f"event: error\ndata: {json.dumps({'error': f'llm_providers not available: {e}'})}\n\n"
        return

    model = get_character_chat_model()
    messages = build_chat_messages(req)

    try:
        adapter = provider_registry.get("opencode_go")
    except Exception as e:
        yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"
        return

    full_text = ""
    has_patch = False
    try:
        from app.providers import ProviderRequest as PR, stream_chat

        pr = PR(
            messages=messages,
            model=model,
            tools=[CHARACTER_PATCH_TOOL],
            tool_choice="auto",
            allow_thinking=False,
            timeout_seconds=60,
            stream=True,
        )
        pending_tool_calls: list = []
        for ev in stream_chat(adapter, pr):
            if ev.kind == "token" and ev.text:
                full_text += ev.text
                yield f"data: {json.dumps({'type': 'token', 'text': ev.text})}\n\n"
            elif ev.kind == "tool_call" and ev.tool_call:
                pending_tool_calls.append(ev.tool_call)
            elif ev.kind == "done":
                break
        for tc in pending_tool_calls:
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
                    has_patch = True
                    yield f"data: {json.dumps({'type': 'patch', 'patch': patch, 'active_page': active_page})}\n\n"
                    break
            except Exception as e:
                logger.warning("patch tool parse failed: %s", e)
        if has_patch and not full_text:
            canonical = "Draft applied to the form \u2192 review the steps and hit Create when ready."
            full_text = canonical
            yield f"data: {json.dumps({'type': 'token', 'text': canonical})}\n\n"
        if not full_text and not has_patch:
            fallback = "I had trouble reaching the AI, but I can still help \u2014 tell me more about your character idea."
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
            save_chat_message(owner_id, character_uuid, "assistant", full_text)

    yield f"data: {json.dumps({'type': 'done'})}\n\n"


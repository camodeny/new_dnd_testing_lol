"""Private lore-DM setup chat — issue #244 follow-up.

A guided back-and-forth that helps a player (especially a new one unsure
what private lore is) write character backstory before launch. Context is
the player's own sheet identity + public party composition + campaign
brief — never another player's secrets, and never a seeded world (the seed
job runs at Begin, so during the lobby there is no world to spoil).

Ask-don't-tell is the load-bearing rule: the assistant asks questions and
drafts from the player's ideas, but must never assert unseeded world facts
(named NPCs/places/factions as established truth). Canon flows exactly one
way: the player approves a proposal, which is written through the standard
lore endpoint (versioned, lobby-locked). This table is advisory-only and
the seed job never reads it.
"""

from __future__ import annotations

import json
import logging
import uuid as uuid_lib

from app.campaigns.service import CampaignCommandError

logger = logging.getLogger(__name__)

#: Chat message bound (chat is conversation, not the lore doc itself).
LORE_CHAT_MAX_LENGTH = 2000

#: Proposal bound — must always fit the lore doc it may become.
LORE_PROPOSAL_MAX_LENGTH = 1500

LORE_DM_CHAT_SYSTEM = (
    "You are a friendly D&D setup assistant helping a player write private "
    "backstory lore for their character before the campaign starts. The player "
    "may be brand new — explain what private lore is when they seem unsure "
    "(secrets, debts, fears, and goals only the player and the DM will ever see), "
    "and guide them with one focused question at a time. Keep replies short "
    "(a few sentences) and conversational.\n"
    "CRITICAL — ask, never tell: the campaign world has NOT been generated yet, "
    "so you know nothing established about it. Never state world facts and never "
    "invent named NPCs, places, or factions as truth. Frame everything as the "
    "player's ideas ('perhaps…', 'what if…'). Ask questions; do not answer them "
    "with canon.\n"
    "When the player's idea lands, call propose_lore_draft with a concise draft "
    f"(a few sentences, at most {LORE_PROPOSAL_MAX_LENGTH} characters) written in "
    "second person about their character. One proposal per reply at most."
)

LORE_PROPOSAL_TOOL = {
    "type": "function",
    "function": {
        "name": "propose_lore_draft",
        "description": "Offer a private-lore draft for the player to approve. Call when the player's idea is concrete enough to draft.",
        "parameters": {
            "type": "object",
            "properties": {
                "lore_text": {
                    "type": "string",
                    "description": "Draft lore text, a few sentences, second person.",
                },
            },
            "required": ["lore_text"],
            "additionalProperties": False,
        },
    },
}


class LoreChatValidationError(CampaignCommandError):
    """Malformed lore-DM chat payload."""

    status_code = 422


def validate_lore_chat_content(payload: object) -> str:
    if not isinstance(payload, dict):
        raise LoreChatValidationError("Request body must be an object")
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        raise LoreChatValidationError("content must be a non-empty string")
    stripped = content.strip()
    if len(stripped) > LORE_CHAT_MAX_LENGTH:
        raise LoreChatValidationError(
            f"content must be {LORE_CHAT_MAX_LENGTH} characters or fewer"
        )
    return stripped


def build_lore_dm_context(
    *,
    campaign_name: str,
    campaign_description: str | None,
    campaign_seed: str | None,
    character_name: str,
    character_identity: str,
    party_advisory: str | None,
) -> str:
    """Deterministic context bundle — code-owned, never model-written."""
    lines = [
        f"Campaign: {campaign_name}",
        f"Brief: {(campaign_description or '').strip() or 'No brief.'}",
        f"Seed: {(campaign_seed or '').strip() or 'none'}",
        f"Player character: {character_name} ({character_identity or 'no sheet details yet'})",
    ]
    if party_advisory:
        lines.append(f"Party context (public only): {party_advisory[:1500]}")
    return "\n".join(lines)


def build_lore_dm_messages(content: str, history: list[dict], context: str) -> list[dict]:
    msgs: list[dict] = [{"role": "system", "content": LORE_DM_CHAT_SYSTEM}]
    msgs.append({"role": "system", "content": f"Lobby context (do not echo raw): {context[:4000]}"})
    for h in history[-12:]:
        role = h.get("role") if h.get("role") in ("user", "assistant") else "user"
        text = str(h.get("content") or "")
        if text:
            msgs.append({"role": role, "content": text})
    msgs.append({"role": "user", "content": content})
    return msgs


def save_lore_dm_message(
    campaign_id: uuid_lib.UUID,
    character_id: uuid_lib.UUID,
    user_id: uuid_lib.UUID,
    role: str,
    content: str,
    proposal_text: str | None = None,
) -> None:
    try:
        from database import SessionLocal

        if SessionLocal is None:
            return
        from models.campaigns import CampaignLoreChatMessage as DbMsg

        db = SessionLocal()
        try:
            db.add(DbMsg(
                campaign_id=campaign_id,
                character_id=character_id,
                user_id=user_id,
                role=role,
                content=content,
                proposal_text=proposal_text,
            ))
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.warning("failed to save lore-DM chat message: %s", e)


def get_lore_dm_model(adapter=None) -> str:
    from app.providers.areas import resolve_area

    if adapter is not None:
        model = (adapter.env_model() or "").strip()
        if model:
            return model
    _, model, _ = resolve_area("lore_dm_chat")
    return model


def _extract_proposal(tool_call) -> str | None:
    try:
        args_raw = tool_call.arguments
        if isinstance(args_raw, str):
            args = json.loads(args_raw) if args_raw else {}
        elif isinstance(args_raw, dict):
            args = args_raw
        else:
            return None
        if not isinstance(args, dict):
            return None
        text = args.get("lore_text")
        if not isinstance(text, str) or not text.strip():
            return None
        text = text.strip()
        if len(text) > LORE_PROPOSAL_MAX_LENGTH:
            logger.warning("lore-DM proposal overlong (%d chars), dropping", len(text))
            return None
        return text
    except Exception as e:
        logger.warning("lore-DM proposal parse failed: %s", e)
        return None


def lore_dm_chat_sync_generator(
    *,
    campaign_id: uuid_lib.UUID,
    character_id: uuid_lib.UUID,
    user_id: uuid_lib.UUID,
    content: str,
    history: list[dict],
    context: str,
):
    try:
        from app.providers.areas import resolve_area

        adapter, model, _ = resolve_area("lore_dm_chat")
    except Exception as e:
        yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"
        return

    messages = build_lore_dm_messages(content, history, context)

    full_text = ""
    proposal: str | None = None
    try:
        from app.providers import ProviderRequest as PR, stream_chat

        pr = PR(
            messages=messages,
            model=model,
            tools=[LORE_PROPOSAL_TOOL],
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
            proposal = _extract_proposal(tc)
            if proposal:
                yield f"data: {json.dumps({'type': 'proposal', 'lore_text': proposal})}\n\n"
                break
        if not full_text and not proposal:
            fallback = "Hmm, I didn't catch that — tell me a little about your character and we'll find a secret worth keeping."
            full_text = fallback
            yield f"data: {json.dumps({'type': 'token', 'text': fallback})}\n\n"
    except Exception:
        logger.exception("lore-DM chat failed")
        yield f"event: error\ndata: {json.dumps({'error': 'chat unavailable'})}\n\n"
        fallback = "I had trouble reaching the DM side — tell me about your character and we'll try again."
        full_text = fallback
        yield f"data: {json.dumps({'type': 'token', 'text': fallback})}\n\n"
    finally:
        if full_text or proposal:
            save_lore_dm_message(
                campaign_id, character_id, user_id, "assistant",
                full_text or "(lore draft proposed)",
                proposal_text=proposal,
            )

    yield f"data: {json.dumps({'type': 'done'})}\n\n"


def list_lore_chat_messages(
    db, *, campaign_id: uuid_lib.UUID, character_id: uuid_lib.UUID, user_id: uuid_lib.UUID,
) -> list[dict]:
    from sqlalchemy import select

    from models.campaigns import CampaignLoreChatMessage

    rows = db.execute(
        select(CampaignLoreChatMessage)
        .where(
            CampaignLoreChatMessage.campaign_id == campaign_id,
            CampaignLoreChatMessage.character_id == character_id,
            CampaignLoreChatMessage.user_id == user_id,
        )
        .order_by(CampaignLoreChatMessage.created_at.asc())
    ).scalars().all()
    return [m.to_dict() for m in rows]


def _character_identity(sheet) -> str:
    if sheet is None:
        return ""
    classes = ", ".join(
        str(c.get("class_name") or "") for c in (getattr(sheet, "classes", None) or [])
        if isinstance(c, dict) and str(c.get("class_name") or "")
    ) or getattr(sheet, "char_class", None)
    bits = []
    for label, val in (
        ("race", getattr(sheet, "race", None)),
        ("classes", classes),
        ("level", getattr(sheet, "level", None)),
        ("background", getattr(sheet, "background", None)),
        ("alignment", getattr(sheet, "alignment", None)),
    ):
        if val:
            bits.append(f"{label} {val}")
    return "; ".join(bits)


def start_lore_chat_turn(db, *, campaign, character, user_id: uuid_lib.UUID, content: str):
    """Persist the player's message and return the reply stream (lobby-only).

    The DM side is advisory only: proposals become canon solely through the
    standard lore PUT.
    """
    from sqlalchemy import select

    from app.campaigns.members import campaign_members
    from app.campaigns.party_lore import build_party_advice, build_party_composition, require_lore_writable
    from app.characters.chat.service import build_party_advisory_text
    from models.campaigns import CampaignLoreChatMessage
    from models.characters import Dnd5eCharacterSheet

    require_lore_writable(campaign)
    user_message = CampaignLoreChatMessage(
        campaign_id=campaign.id, character_id=character.id, user_id=user_id,
        role="user", content=content,
    )
    db.add(user_message)
    db.commit()
    prior = db.execute(
        select(CampaignLoreChatMessage)
        .where(
            CampaignLoreChatMessage.campaign_id == campaign.id,
            CampaignLoreChatMessage.character_id == character.id,
            CampaignLoreChatMessage.user_id == user_id,
            CampaignLoreChatMessage.id != user_message.id,
        )
        .order_by(CampaignLoreChatMessage.created_at.desc())
        .limit(12)
    ).scalars().all()
    history = [{"role": m.role, "content": m.content} for m in reversed(prior)]
    sheet = db.execute(
        select(Dnd5eCharacterSheet)
        .where(Dnd5eCharacterSheet.character_id == character.id)
        .order_by(Dnd5eCharacterSheet.updated_at.desc())
    ).scalars().first()
    composition = build_party_composition(db, campaign_members(db, campaign.id))
    context = build_lore_dm_context(
        campaign_name=campaign.name or "",
        campaign_description=campaign.description,
        campaign_seed=campaign.random_seed,
        character_name=character.name or "",
        character_identity=_character_identity(sheet),
        party_advisory=build_party_advisory_text(composition, build_party_advice(composition)),
    )
    logger.info(
        "lore_dm_chat campaign_id=%s actor_id=%s character_id=%s content_length=%s",
        campaign.id, user_id, character.id, len(content),
    )
    return lore_dm_chat_sync_generator(
        campaign_id=campaign.id, character_id=character.id, user_id=user_id,
        content=content, history=history, context=context,
    )

"""Read-only, non-canonical AI clarification conversations."""

import hashlib
import json

from models import (
    db,
    CampaignClarification,
    CampaignClarificationFork,
    CampaignClarificationForkMessage,
    CampaignWorld,
    SessionMessage,
)
from openrouter import _post_chat_normalized
from services.dm_tools import build_session_hot_context
from time_utils import utcnow


MAX_CANONICAL_MESSAGES = 24
MAX_CANONICAL_CHARS = 12000
KEEP_FORK_DELTA_MESSAGES = 8
COMPACT_FORK_DELTA_AFTER = 12
MAX_COMPACTED_SUMMARY_CHARS = 6000
FROZEN_CONTEXT_KEYS = (
    "campaign",
    "session",
    "current_user",
    "current_character",
    "current_player_character",
    "protected_player_characters",
    "party",
    "current_scene",
    "current_encounter_map",
    "combat_coordinates",
    "active_clocks",
    "established_public_facts",
    "recent_public_world_events",
    "open_public_threads",
    "visible_naming_constraints",
)

FORK_SYSTEM_PROMPT = """You are the AI Dungeon Master resolving an internal campaign-memory ambiguity.
This is a private, non-canonical clarification branch. It cannot alter the live campaign.
Use only the frozen context and branch conversation supplied here. Do not narrate a live scene,
invent new campaign facts, issue player instructions, or claim that any decision has been applied.
State the best-supported interpretation, the evidence, and any uncertainty concisely."""


def _canonical_message_window(session_id, anchor_message_id):
    if not anchor_message_id:
        return []
    rows = SessionMessage.query.filter(
        SessionMessage.session_id == session_id,
        SessionMessage.id <= anchor_message_id,
    ).order_by(SessionMessage.id.desc()).limit(MAX_CANONICAL_MESSAGES).all()
    selected = []
    char_count = 0
    for row in rows:
        next_size = len(row.content or "")
        if selected and char_count + next_size > MAX_CANONICAL_CHARS:
            break
        selected.append(row)
        char_count += next_size
    return list(reversed(selected))


def _frozen_snapshot(campaign, session, current_user, canonical_messages, clarification):
    hot_context = build_session_hot_context(
        campaign,
        session,
        current_user,
        recent_messages_override=canonical_messages[-8:],
    )
    frozen_context = {key: hot_context.get(key) for key in FROZEN_CONTEXT_KEYS}
    frozen_context["strategy"] = "clarification_fork_frozen_context_v1"
    frozen_context["canonical_message_count"] = len(canonical_messages)
    frozen_context["frozen_at"] = utcnow().isoformat()
    world = CampaignWorld.query.filter_by(campaign_id=campaign.id).first()
    memory_revision = world.memory_revision if world else 0
    snapshot = {
        "context": frozen_context,
        "clarification": clarification.to_dict() if clarification else None,
        "memory_revision": memory_revision or 0,
    }
    context_hash = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, ensure_ascii=True, default=str).encode("utf-8"),
    ).hexdigest()
    return snapshot, memory_revision or 0, context_hash


def _fork_messages(fork):
    return CampaignClarificationForkMessage.query.filter_by(fork_id=fork.id).order_by(
        CampaignClarificationForkMessage.id.asc(),
    ).all()


def _compact_fork_delta(fork):
    rows = _fork_messages(fork)
    if len(rows) <= COMPACT_FORK_DELTA_AFTER:
        return
    older = rows[:-KEEP_FORK_DELTA_MESSAGES]
    older = [row for row in older if row.id > (fork.compacted_through_message_id or 0)]
    if not older:
        return
    additions = "\n".join(f"{row.role}: {row.content}" for row in older)
    summary = "\n".join(item for item in (fork.compacted_summary, additions) if item)
    fork.compacted_summary = summary[-MAX_COMPACTED_SUMMARY_CHARS:]
    fork.compacted_through_message_id = older[-1].id


def _canonical_messages(fork):
    if not fork.base_start_message_id or not fork.anchor_message_id:
        return []
    return SessionMessage.query.filter(
        SessionMessage.session_id == fork.session_id,
        SessionMessage.id >= fork.base_start_message_id,
        SessionMessage.id <= fork.anchor_message_id,
    ).order_by(SessionMessage.id.asc()).all()


def _generate_reply(fork):
    snapshot = fork.snapshot_json if isinstance(fork.snapshot_json, dict) else {}
    canonical_messages = _canonical_messages(fork)
    messages = [
        {"role": "system", "content": FORK_SYSTEM_PROMPT},
        {
            "role": "system",
            "content": "Frozen campaign context:\n" + json.dumps(snapshot.get("context") or {}, ensure_ascii=False),
        },
    ]
    clarification = snapshot.get("clarification")
    if clarification:
        messages.append({
            "role": "system",
            "content": "Clarification record:\n" + json.dumps(clarification, ensure_ascii=False),
        })
    for message in canonical_messages:
        role = "assistant" if message.role == "dm" else "user"
        messages.append({"role": role, "content": message.content})
    messages.append({"role": "user", "content": "Clarification question:\n" + fork.question})
    if fork.compacted_summary:
        messages.append({"role": "system", "content": "Earlier fork discussion:\n" + fork.compacted_summary})
    for message in _fork_messages(fork):
        if message.id <= (fork.compacted_through_message_id or 0):
            continue
        role = "assistant" if message.role == "ai" else "user"
        messages.append({"role": role, "content": message.content})

    audit_metadata = {
        "fork_id": fork.id,
        "base_start_message_id": fork.base_start_message_id,
        "anchor_message_id": fork.anchor_message_id,
        "canonical_message_count": len(canonical_messages),
        "fork_delta_message_count": len(_fork_messages(fork)),
        "memory_revision": fork.memory_revision,
        "context_hash": fork.context_hash,
        "estimated_message_tokens": sum(len(item.get("content") or "") for item in messages) // 4,
    }
    normalized = _post_chat_normalized(
        messages,
        audit_context={
            "campaign_id": fork.campaign_id,
            "operation": "clarification_fork_response",
            "actor": "clarification_fork_dm",
            "trace_label": f"clarification_fork: {fork.id}",
            "full_world_graph_included": False,
            "audit_messages": [{"role": "system", "content": "Clarification fork request metadata: " + json.dumps(audit_metadata)}],
            "token_estimate": audit_metadata,
        },
        tools=[],
        allow_thinking=False,
    )
    reply = str(normalized.content or "").strip()
    if not reply:
        raise RuntimeError("Clarification fork AI returned an empty response.")
    return reply


def _run_generation(fork):
    try:
        reply = _generate_reply(fork)
    except Exception as err:
        fork.status = "failed"
        fork.generation_error = str(err)
        db.session.commit()
        return fork
    db.session.add(CampaignClarificationForkMessage(fork_id=fork.id, role="ai", content=reply))
    fork.status = "active"
    fork.generation_error = None
    db.session.commit()
    return fork


def _begin_generation(fork):
    _compact_fork_delta(fork)
    fork.status = "pending"
    fork.generation_error = None
    fork.generation_attempt_count = (fork.generation_attempt_count or 0) + 1
    # Commit before the provider call so failures retain a single retryable state.
    db.session.commit()
    return _run_generation(fork)


def create_fork(campaign, session, current_user, question, anchor_message_id=None, clarification_id=None):
    clarification = None
    if clarification_id:
        clarification = CampaignClarification.query.filter_by(
            campaign_id=campaign.id,
            clarification_id=clarification_id,
        ).first()
        if not clarification:
            raise ValueError("Clarification not found.")

    latest = SessionMessage.query.filter_by(session_id=session.id).order_by(SessionMessage.id.desc()).first()
    anchor_message_id = anchor_message_id or (latest.id if latest else None)
    if anchor_message_id and (not latest or anchor_message_id != latest.id):
        raise ValueError("Clarification forks must anchor to the latest canonical session message.")
    canonical_messages = _canonical_message_window(session.id, anchor_message_id)
    snapshot, memory_revision, context_hash = _frozen_snapshot(
        campaign,
        session,
        current_user,
        canonical_messages,
        clarification,
    )
    fork = CampaignClarificationFork(
        campaign_id=campaign.id,
        session_id=session.id,
        clarification_id=clarification_id,
        anchor_message_id=anchor_message_id,
        base_start_message_id=canonical_messages[0].id if canonical_messages else None,
        created_by_user_id=current_user.id,
        question=question,
        snapshot_json=snapshot,
        memory_revision=memory_revision,
        context_hash=context_hash,
        status="pending",
    )
    db.session.add(fork)
    db.session.commit()
    return _begin_generation(fork)


def add_message(fork, content):
    if fork.status != "active":
        raise ValueError(f"Cannot add messages to a {fork.status} clarification fork.")
    db.session.add(CampaignClarificationForkMessage(fork_id=fork.id, role="operator", content=content))
    return _begin_generation(fork)


def retry_generation(fork):
    if fork.status != "failed":
        raise ValueError(f"Cannot retry a {fork.status} clarification fork.")
    return _begin_generation(fork)


def resolve_fork(fork, resolution):
    if fork.status not in ("active", "failed"):
        raise ValueError(f"Cannot resolve a {fork.status} clarification fork.")
    if not isinstance(resolution, dict):
        raise ValueError("resolution must be a JSON object.")
    fork.resolution_json = resolution
    fork.status = "resolved"
    fork.resolved_at = utcnow()
    db.session.commit()
    return fork


def archive_fork(fork):
    if fork.status == "archived":
        return fork
    fork.status = "archived"
    fork.archived_at = utcnow()
    db.session.commit()
    return fork

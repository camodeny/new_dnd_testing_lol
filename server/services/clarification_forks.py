"""Read-only, non-canonical AI clarification conversations."""

import json

from models import (
    db,
    CampaignClarification,
    CampaignClarificationFork,
    CampaignClarificationForkMessage,
    SessionMessage,
)
from openrouter import _post_chat_normalized
from services.dm_tools import build_session_hot_context
from time_utils import utcnow


FORK_SYSTEM_PROMPT = """You are the AI Dungeon Master resolving an internal campaign-memory ambiguity.
This is a private, non-canonical clarification branch. It cannot alter the live campaign.
Use only the frozen context and branch conversation supplied here. Do not narrate a live scene,
invent new campaign facts, issue player instructions, or claim that any decision has been applied.
State the best-supported interpretation, the evidence, and any uncertainty concisely."""


def _snapshot(campaign, session, current_user, anchor_message_id, clarification):
    query = SessionMessage.query.filter_by(session_id=session.id)
    if anchor_message_id:
        query = query.filter(SessionMessage.id <= anchor_message_id)
    messages = query.order_by(SessionMessage.id.asc()).all()
    return {
        "hot_context": build_session_hot_context(campaign, session, current_user),
        "transcript": [message.to_dict() for message in messages],
        "clarification": clarification.to_dict() if clarification else None,
    }


def _generate_reply(fork):
    snapshot = fork.snapshot_json if isinstance(fork.snapshot_json, dict) else {}
    messages = [
        {"role": "system", "content": FORK_SYSTEM_PROMPT},
        {
            "role": "system",
            "content": "Frozen campaign context:\n" + json.dumps(snapshot.get("hot_context") or {}, ensure_ascii=False),
        },
    ]
    clarification = snapshot.get("clarification")
    if clarification:
        messages.append({
            "role": "system",
            "content": "Clarification record:\n" + json.dumps(clarification, ensure_ascii=False),
        })
    for message in snapshot.get("transcript") or []:
        role = "assistant" if message.get("role") == "dm" else "user"
        messages.append({"role": role, "content": message.get("content") or ""})
    messages.append({"role": "user", "content": "Clarification question:\n" + fork.question})
    fork_messages = CampaignClarificationForkMessage.query.filter_by(fork_id=fork.id).order_by(
        CampaignClarificationForkMessage.id.asc(),
    ).all()
    for message in fork_messages:
        role = "assistant" if message.role == "ai" else "user"
        messages.append({"role": role, "content": message.content})

    normalized = _post_chat_normalized(
        messages,
        audit_context={
            "campaign_id": fork.campaign_id,
            "operation": "clarification_fork_response",
            "actor": "clarification_fork_dm",
            "trace_label": f"clarification_fork: {fork.id}",
            "full_world_graph_included": False,
        },
        tools=[],
        allow_thinking=False,
    )
    return str(normalized.content or "").strip()


def create_fork(campaign, session, current_user, question, anchor_message_id=None, clarification_id=None):
    if clarification_id:
        clarification = CampaignClarification.query.filter_by(
            campaign_id=campaign.id,
            clarification_id=clarification_id,
        ).first()
        if not clarification:
            raise ValueError("Clarification not found.")
    else:
        clarification = None

    anchor = None
    if anchor_message_id:
        anchor = SessionMessage.query.filter_by(id=anchor_message_id, session_id=session.id).first()
        if not anchor:
            raise ValueError("Anchor message not found in this session.")

    fork = CampaignClarificationFork(
        campaign_id=campaign.id,
        session_id=session.id,
        clarification_id=clarification_id,
        anchor_message_id=anchor.id if anchor else None,
        created_by_user_id=current_user.id,
        question=question,
        snapshot_json=_snapshot(campaign, session, current_user, anchor.id if anchor else None, clarification),
    )
    db.session.add(fork)
    db.session.flush()
    reply = _generate_reply(fork)
    if reply:
        db.session.add(CampaignClarificationForkMessage(fork_id=fork.id, role="ai", content=reply))
    db.session.commit()
    return fork


def add_message(fork, content):
    if fork.status != "active":
        raise ValueError(f"Cannot add messages to a {fork.status} clarification fork.")
    db.session.add(CampaignClarificationForkMessage(fork_id=fork.id, role="operator", content=content))
    db.session.flush()
    reply = _generate_reply(fork)
    if reply:
        db.session.add(CampaignClarificationForkMessage(fork_id=fork.id, role="ai", content=reply))
    db.session.commit()
    return fork


def resolve_fork(fork, resolution):
    if fork.status != "active":
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

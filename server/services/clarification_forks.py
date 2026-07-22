"""Read-only, non-canonical AI clarification conversations."""

import hashlib
import json

from models import (
    db,
    CampaignClarification,
    CampaignClarificationFork,
    CampaignClarificationForkMessage,
    CampaignClock,
    CampaignMemoryLog,
    CampaignWorld,
    NPCActor,
    SessionMessage,
    WorldEvent,
)
from openrouter import _post_chat_normalized
from services.dm_tools import build_session_hot_context
from time_utils import utcnow


MAX_CANONICAL_MESSAGES = 24
MAX_CANONICAL_CHARS = 12000
KEEP_FORK_DELTA_MESSAGES = 8
COMPACT_FORK_DELTA_AFTER = 12
MAX_COMPACTED_SUMMARY_CHARS = 6000
MAX_EVIDENCE_CANDIDATES = 8
MAX_EVIDENCE_RELATIONS = 12
MAX_EVIDENCE_FACTS = 12
MAX_EVIDENCE_MEMORY_LOGS = 8
MAX_EVIDENCE_EVENTS = 4
MAX_EVIDENCE_CLOCKS = 8
MAX_EVIDENCE_TEXT = 1200
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


def _compact_text(value, limit=MAX_EVIDENCE_TEXT):
    text = str(value or "")
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def _json_object(value):
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}


def _event_mentions_candidate(event, candidate_id):
    payload = _json_object(event.payload)
    return candidate_id in json.dumps(payload, ensure_ascii=True) or candidate_id in (event.summary or "")


def _build_evidence_bundle(campaign, clarification, world):
    if not clarification:
        return None
    graph = _json_object(world.knowledge_graph if world else None)
    entities = [item for item in graph.get("entities", []) if isinstance(item, dict)]
    relations = [item for item in graph.get("relations", []) if isinstance(item, dict)]
    facts = [item for item in graph.get("facts", []) if isinstance(item, dict)]
    candidate_ids = []
    for raw_id in [clarification.mention_entity_id, *(clarification.candidate_ids or [])]:
        candidate_id = str(raw_id or "").strip()
        if candidate_id and candidate_id not in candidate_ids:
            candidate_ids.append(candidate_id)
    surface_form = str(clarification.surface_form or "").strip().lower()
    if surface_form:
        for entity in entities:
            if str(entity.get("name") or "").strip().lower() == surface_form:
                candidate_id = str(entity.get("id") or "").strip()
                if candidate_id and candidate_id not in candidate_ids:
                    candidate_ids.append(candidate_id)
    candidate_ids = candidate_ids[:MAX_EVIDENCE_CANDIDATES]
    npcs = {
        npc.actor_id: npc
        for npc in NPCActor.query.filter_by(campaign_id=campaign.id).all()
    }
    events = WorldEvent.query.filter_by(campaign_id=campaign.id).order_by(WorldEvent.id.desc()).limit(50).all()
    bundle = {
        "schema_version": "1.0",
        "mention_entity_id": clarification.mention_entity_id,
        "mention_surface_form": clarification.surface_form,
        "candidates": {},
        "clocks": [
            clock.to_dict(include_private=True)
            for clock in CampaignClock.query.filter_by(campaign_id=campaign.id).order_by(CampaignClock.id.desc()).limit(MAX_EVIDENCE_CLOCKS).all()
        ],
    }
    for candidate_id in candidate_ids:
        npc = npcs.get(candidate_id)
        entity = next((item for item in entities if item.get("id") == candidate_id), None)
        candidate_relations = [
            {
                "id": item.get("id"),
                "type": item.get("type"),
                "source_id": item.get("source_id"),
                "target_id": item.get("target_id"),
                "summary": _compact_text(item.get("summary")),
                "visibility": item.get("visibility"),
            }
            for item in relations
            if candidate_id in (item.get("source_id"), item.get("target_id"))
        ][:MAX_EVIDENCE_RELATIONS]
        candidate_facts = [
            {
                "id": item.get("id"),
                "text": _compact_text(item.get("text")),
                "certainty": item.get("certainty"),
                "visibility": item.get("visibility"),
            }
            for item in facts
            if candidate_id in (item.get("entity_ids") or [])
        ][:MAX_EVIDENCE_FACTS]
        logs = CampaignMemoryLog.query.filter_by(
            campaign_id=campaign.id,
            target_id=candidate_id,
        ).order_by(CampaignMemoryLog.id.desc()).limit(MAX_EVIDENCE_MEMORY_LOGS).all()
        candidate_events = [event for event in events if _event_mentions_candidate(event, candidate_id)][:MAX_EVIDENCE_EVENTS]
        dossier = _json_object(npc.dossier) if npc else {}
        bundle["candidates"][candidate_id] = {
            "graph_entity": {
                "id": candidate_id,
                "type": entity.get("type") if entity else "npc" if npc else None,
                "name": entity.get("name") if entity else npc.name if npc else None,
                "summary": _compact_text(entity.get("summary")) if entity else _compact_text(npc.public_summary) if npc else None,
                "visibility": entity.get("visibility") if entity else "dm_private" if npc else None,
                "tags": entity.get("tags") if entity else [],
            },
            "npc_dossier": {
                "role": npc.role if npc else None,
                "voice": _compact_text(dossier.get("voice")),
                "background": _compact_text(dossier.get("background")),
                "wants": dossier.get("wants") if isinstance(dossier.get("wants"), list) else [],
                "fears": dossier.get("fears") if isinstance(dossier.get("fears"), list) else [],
                "secrets": dossier.get("secrets") if isinstance(dossier.get("secrets"), list) else [],
                "relationships": dossier.get("relationships") if isinstance(dossier.get("relationships"), dict) else {},
                "recent_offscreen_activity": dossier.get("recent_offscreen_activity") if isinstance(dossier.get("recent_offscreen_activity"), list) else [],
            } if npc else None,
            "graph_relations": candidate_relations,
            "graph_facts": candidate_facts,
            "memory_logs": [
                {
                    "memory_id": row.memory_id,
                    "operation": row.operation,
                    "memory_type": row.memory_type,
                    "reason": _compact_text(row.reason),
                    "provenance": row.provenance_json,
                    "evidence_status": row.evidence_status,
                }
                for row in logs
            ],
            "world_events": [
                {
                    "event_type": event.event_type,
                    "summary": _compact_text(event.summary),
                    "payload": _json_object(event.payload),
                    "visibility": event.visibility,
                    "created_at": event.created_at.isoformat() if event.created_at else None,
                }
                for event in candidate_events
            ],
        }
    return bundle


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
    evidence_bundle = _build_evidence_bundle(campaign, clarification, world)
    if evidence_bundle:
        snapshot["evidence_bundle"] = evidence_bundle
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
    evidence_bundle = snapshot.get("evidence_bundle")
    if evidence_bundle:
        messages.append({
            "role": "system",
            "content": "Frozen private clarification evidence:\n" + json.dumps(evidence_bundle, ensure_ascii=False),
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

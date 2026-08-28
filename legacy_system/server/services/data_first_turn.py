"""Opt-in data-first session turn planning and streamed prose expansion.

The turn attempt is the semantic contract.  The expander only receives its
player-visible projection, never the campaign's private context.
"""

import json
import os
import re


SCHEMA_VERSION = "data_first_turn_attempt_v2_2"
MAX_BEATS = 8
MAX_FACTS_PER_BEAT = 5
DEFAULT_MAX_WORDS = 80
MAX_EVIDENCE_REQUESTS = 3
MAX_ACTIONS = 4
MAX_NEW_ACTORS = 2
STAGED_ACTION_TOOLS = {
    "record_world_event",
    "update_current_scene",
    "reveal_fact",
    "propose_sheet_update",
    # Produced only from the top-level new_actors contract.
    "register_npc_actor",
}
READ_ONLY_EVIDENCE_TOOLS = {
    "ask_character_sheet",
    "get_current_scene",
    "search_campaign_memory",
}

ENTITY_REF_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["character", "npc", "location", "object", "entity"]},
        "id": {"type": ["string", "integer"]},
    },
    "required": ["type", "id"],
    "additionalProperties": False,
}


TURN_ATTEMPT_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "schema_version": {"type": "string", "const": SCHEMA_VERSION},
        "mode": {"type": "string", "enum": ["speak", "await_player_roll", "table_chat", "silent", "resolve", "fallback"]},
        "reason": {"type": "string"},
        "beats": {
            "type": "array",
            "maxItems": MAX_BEATS,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "type": {"type": "string", "enum": ["narration", "npc_dialogue"]},
                    "speaker_entity_id": {"type": ["string", "null"]},
                    "speaker_public_name": {"type": ["string", "null"]},
                    "visible_claims": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": MAX_FACTS_PER_BEAT,
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string"},
                                "claim_kind": {
                                    "type": "string",
                                    "enum": [
                                        "observation", "world_fact", "npc_utterance",
                                        "player_declaration", "roll_instruction", "roll_outcome",
                                    ],
                                },
                                "actor_ref": {"anyOf": [ENTITY_REF_SCHEMA, {"type": "null"}]},
                                "target_refs": {"type": "array", "items": ENTITY_REF_SCHEMA},
                                "topic_refs": {"type": "array", "items": ENTITY_REF_SCHEMA},
                                "location_ref": {"anyOf": [ENTITY_REF_SCHEMA, {"type": "null"}]},
                                "evidence_refs": {"type": "array", "items": {"type": "string"}},
                                "trigger_refs": {"type": "array", "items": {"type": "string"}},
                                "origin": {
                                    "type": "string",
                                    "enum": [
                                        "player_transcript", "established_state", "resolver_evidence",
                                        "dm_adjudication", "roll_adjudication",
                                    ],
                                },
                                "roll_request_id": {"type": ["string", "null"]},
                            },
                            "required": [
                                "text", "claim_kind", "actor_ref", "target_refs", "topic_refs",
                                "location_ref", "evidence_refs", "trigger_refs", "origin",
                                "roll_request_id",
                            ],
                            "additionalProperties": False,
                        },
                    },
                    "delivery": {"type": ["string", "null"]},
                    "truth_status": {
                        "type": ["string", "null"],
                        "enum": ["truthful", "mistaken", "deceptive", "incomplete", "unknown", None],
                    },
                    "dm_private_context": {"type": ["string", "null"]},
                },
                "required": [
                    "id", "type", "speaker_entity_id", "speaker_public_name",
                    "visible_claims", "delivery", "truth_status", "dm_private_context",
                ],
                "additionalProperties": False,
            },
        },
        "open_player_choice": {"type": ["string", "null"]},
        "max_words": {"type": "integer", "minimum": 30, "maximum": 300},
        "safe_prelude": {"type": ["string", "null"]},
        "table_chat_intent": {"type": ["string", "null"]},
        "evidence_requests": {
            "type": "array",
            "maxItems": MAX_EVIDENCE_REQUESTS,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "tool": {
                        "type": "string",
                        "enum": sorted(READ_ONLY_EVIDENCE_TOOLS),
                    },
                    "question": {"type": ["string", "null"]},
                    "scope": {
                        "type": ["string", "null"],
                        "enum": ["current_player", "party", "character_id", None],
                    },
                    "character_id": {"type": ["integer", "null"]},
                    "query": {"type": ["string", "null"]},
                    "limit": {"type": ["integer", "null"], "minimum": 1, "maximum": 20},
                    "include_private": {"type": ["boolean", "null"]},
                },
                "required": [
                    "id", "tool", "question", "scope", "character_id",
                    "query", "limit", "include_private",
                ],
                "additionalProperties": False,
            },
        },
        "actions": {
            "type": "array",
            "maxItems": MAX_ACTIONS,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "tool": {"type": "string", "enum": sorted(STAGED_ACTION_TOOLS)},
                    "arguments_json": {"type": "string"},
                },
                "required": ["id", "tool", "arguments_json"],
                "additionalProperties": False,
            },
        },
        "new_actors": {
            "type": "array",
            "maxItems": MAX_NEW_ACTORS,
            "items": {
                "type": "object",
                "properties": {
                    "local_id": {"type": "string"},
                    "kind": {"type": "string", "enum": ["npc"]},
                    "public_name": {"type": "string"},
                    "role": {"type": ["string", "null"]},
                    "public_summary": {"type": ["string", "null"]},
                    "location_ref": {"anyOf": [ENTITY_REF_SCHEMA, {"type": "null"}]},
                },
                "required": ["local_id", "kind", "public_name", "role", "public_summary", "location_ref"],
                "additionalProperties": False,
            },
        },
        "roll_request": {
            "type": ["object", "null"],
            "properties": {
                "request_id": {"type": "string"},
                "requested_user_id": {"type": ["integer", "null"]},
                "character_id": {"type": ["integer", "null"]},
                "roll_kind": {"type": "string", "enum": ["check", "save", "attack", "ability", "initiative", "other"]},
                "ability_or_skill": {"type": "string"},
                "label": {"type": "string"},
                "advantage_state": {"type": "string", "enum": ["normal", "advantage", "disadvantage"]},
                "reason_public": {"type": "string"},
                "dc_private": {"type": ["integer", "null"], "minimum": 1, "maximum": 40},
            },
            "required": ["request_id", "requested_user_id", "character_id", "roll_kind", "ability_or_skill", "label", "advantage_state", "reason_public", "dc_private"],
            "additionalProperties": False,
        },
    },
    "required": [
        "schema_version", "mode", "reason", "beats", "open_player_choice", "max_words",
        "safe_prelude", "table_chat_intent", "evidence_requests", "actions", "new_actors", "roll_request",
    ],
    "additionalProperties": False,
}


TURN_ATTEMPT_SYSTEM_PROMPT = """You are the AI Dungeon Master adjudication stage for a D&D session.
Return one compact JSON turn attempt, not player-facing prose. The application will validate this
attempt and a separate expander will write the narration.

Use only supplied campaign state and transcript evidence. Preserve player agency: never decide a
player character's undeclared action, thought, feeling, dialogue, or choice. A player may only
declare behavior for a protected player character whose user_id matches that transcript message's
user_id. Treat player theories
as claims unless corroborated. NPC statements may be truthful, mistaken, incomplete, or deceptive;
describe only what the players may hear or observe in visible_claims. Never place DM-private truth,
secret names, hidden motives, hidden clock mechanics, or explanations in visible fields.
Never surface dc_private, a private DC, difficulty class, target number, or hidden success threshold
in visible fields, including after a roll is fulfilled. You may state the player's public roll total
and the observable adjudicated outcome, but never the private threshold used to adjudicate it.

This MVP handles conversational and descriptive turns plus a single bounded read-only evidence pass.
Use mode "table_chat" for casual, out-of-character table conversation that should not affect the
campaign. Give a short table_chat_intent describing the reply's tone and direct answer. A table_chat
turn has no beats, actions, rolls, evidence requests, scene choice, or campaign state change. Do not
force it back into the story or end it with a question unless the player asked one. Use mode "resolve" when the answer only needs ask_character_sheet, get_current_scene, or
search_campaign_memory. Give 1-3 focused evidence_requests. A resolver will execute them and call you
again with the results. Use mode "await_player_roll" when uncertainty requires a player roll. State
what is rolled and why, but never narrate the unresolved outcome. Speaking turns may stage supported
noncombat actions: record_world_event, update_current_scene, reveal_fact, or propose_sheet_update.
Each action arguments_json is a compact JSON object matching that existing tool's arguments. Use mode
"fallback" only for combat, maps, loot/shop generation, unsupported mechanics, or other tools.
Supported argument shapes are:
- record_world_event: {"event_type":string,"summary":string,"payload":object,"visibility":"public|party_known|dm_private"}.
  Public or party_known events require payload.source_facet_ids naming already-revealed canonical
  facets that support the full summary; otherwise use dm_private, prefix the summary with
  "Unverified player claim:", and set payload.epistemic_status to "player_claim". A player's theory,
  even when repeated confidently or after a failed/inconclusive roll, is not a discovered world fact.
- update_current_scene: {"scene_patch":object,"reason":string}
- reveal_fact: {"item_type":"entity|relation|fact","item_id":string,"visibility":"public|party_known|dm_private","reason":string}
- propose_sheet_update: {"character_id":integer,"reason":string,"changes":[{"field":string,"operation":"add|subtract|set","value":number}]}
Only propose a sheet update after supplied context or resolver evidence establishes the current value.
Use reveal_fact only with an exact canonical item id present in supplied context, never an invented id,
event type, or world-event id. Do not reveal an item that is already public or party_known. A
propose_sheet_update action creates a pending proposal; visible facts must say proposed and pending
approval, never recorded, deducted, applied, updated, or completed.
On a resolver retry, keep provenance lanes separate within visible facts: player requests and declared
actions cite their session_message source; sheet/world values cite resolver evidence. Do not claim an
action was staged, proposed, or committed in a beat—the application will add authoritative pending
action wording only after staging succeeds. Never use a player message or unrelated sheet evidence to
author NPC dialogue, prices, offers, permission, or testimony.

Return exactly this shape:
{
  "schema_version": "data_first_turn_attempt_v2_2",
  "mode": "speak|await_player_roll|table_chat|silent|resolve|fallback",
  "reason": "short internal reason",
  "beats": [
    {
      "id": "beat_1",
      "type": "narration|npc_dialogue",
      "speaker_entity_id": "exact supplied NPC/entity id, or null only when no canonical id exists",
      "speaker_public_name": "required only for npc_dialogue",
      "visible_claims": [{
        "text": "atomic player-visible fact or utterance",
        "claim_kind": "observation|world_fact|npc_utterance|player_declaration|roll_instruction|roll_outcome",
        "actor_ref": {"type":"character|npc|location|object|entity","id":"exact supplied id"},
        "target_refs": [{"type":"character|npc|location|object|entity","id":"exact supplied id"}],
        "topic_refs": [{"type":"character|npc|location|object|entity","id":"exact supplied id"}],
        "location_ref": null,
        "evidence_refs": ["source ids proving an existing claim"],
        "trigger_refs": ["source ids that prompted a newly adjudicated reaction"],
        "origin": "player_transcript|established_state|resolver_evidence|dm_adjudication|roll_adjudication",
        "roll_request_id": null
      }],
      "delivery": "optional style direction containing no world facts",
      "truth_status": "truthful|mistaken|deceptive|incomplete|unknown",
      "dm_private_context": "required authoritative interpretation when truth_status is not truthful"
    }
  ],
  "open_player_choice": "question or decision left to the players, or null",
  "max_words": 80,
  "safe_prelude": "resolve mode only: short player-visible progress update such as Checking Mara's character sheet..., otherwise null",
  "table_chat_intent": "table_chat only: brief direct conversational reply intent, otherwise null",
  "evidence_requests": [
    {
      "id": "evidence_1",
      "tool": "ask_character_sheet|get_current_scene|search_campaign_memory",
      "question": "ask_character_sheet question or null",
      "scope": "current_player|party|character_id or null",
      "character_id": null,
      "query": "search_campaign_memory query or null",
      "limit": 8,
      "include_private": null
    }
  ],
  "actions": [{"id":"action_1","tool":"record_world_event","arguments_json":"{...}"}],
  "new_actors": [{"local_id":"new_npc_1","kind":"npc","public_name":"a dock clerk","role":"clerk","public_summary":"A clerk steps from the customs shed.","location_ref":null}],
  "roll_request": {
    "request_id": "roll_1", "requested_user_id": null, "character_id": null,
    "roll_kind": "check", "ability_or_skill": "Investigation", "label": "Investigation check",
    "advantage_state": "normal", "reason_public": "Determine what the damaged seal reveals.",
    "dc_private": 14
  }
}

For table_chat, beats must be empty and table_chat_intent is required. For every other mode,
table_chat_intent must be null. For ordinary conversational, descriptive, or single-roll turns, set max_words to 80 or less and
use only the claims needed to move the current exchange forward. Raise max_words above 80 only when
the player needs genuinely detailed instructions, a multi-part ruling, or several independent NPC
answers; 160 is normally ample and 300 is the hard maximum. Do not add recap or atmosphere merely
to use the budget. For mode speak or await_player_roll, provide 1-8 ordered beats. Each visible_claims item must be an atomic semantic claim,
not polished prose. NPC utterances belong in an npc_dialogue beat and speaker_public_name must be a
publicly safe name or descriptor. An npc_dialogue beat requires an exact known speaker_entity_id,
may contain only npc_utterance claims, and each claim actor_ref must be the same NPC id. Narration
beats may not contain npc_utterance claims. Never invent an entity id. To introduce a previously
unknown NPC, add a new_actors descriptor with a unique local_id such as new_npc_1. That local_id may
be used as speaker_entity_id and actor_ref.id within this one turn only; the application assigns its
durable literal ID atomically if it accepts the turn. New actors must be NPCs with a public name or
descriptor; never introduce a player character, monster, secret identity, or duplicate existing NPC.
Do not put registration in actions. Put existing entities in their literal
semantic roles: actor_ref performs the action or speech, target_refs receive it, topic_refs are what
it concerns, and location_ref is where it occurs. Only characters and NPCs may be actor_ref; put
objects in topic_refs and locations in location_ref. Use player_declaration with actor_ref type character
whenever text repeats or completes a player-declared action or dialogue. Use roll_outcome with the
fulfilled roll_request_id and origin roll_adjudication for both the public total and every observable
consequence decided by that roll. Do not attach roll_request_id to any other claim kind. roll_instruction is valid only in
await_player_roll mode. Put provenance on each claim. evidence_refs prove existing information;
trigger_refs only explain what prompted a newly authored response. A new NPC response uses
origin dm_adjudication, places the player's message in trigger_refs rather than evidence_refs, and
may have empty evidence_refs. A player_declaration uses origin player_transcript and its owner's
message in evidence_refs. Resolver facts use origin resolver_evidence. For NPC dialogue,
truth_status is required. When it is mistaken,
deceptive, incomplete, or unknown, dm_private_context must explain the authoritative interpretation
without changing visible_claims. Any player_declaration claim must cite the player-authored transcript
source that declared it, using the exact supplied
session_message:<id> source_id. Never cite a different player's message as authority for that
character. For resolve, beats must be empty. safe_prelude must be a brief
out-of-character progress update that names what is being checked without stating an outcome,
new world fact, or player-character behavior. evidence_requests must contain only the read-only tools above. On a retry, use the
provided evidence and do not request the same evidence again. For speak/await_player_roll/silent/fallback,
evidence_requests must be empty and safe_prelude must be null. Only await_player_roll may contain a
roll_request; its beats ask for the roll without resolving it. Only speak may contain actions; never
stage an outcome mutation while awaiting a roll. For silent/resolve/fallback, beats must be empty. Resolver evidence with complete=false is insufficient: return fallback rather than
guessing. Every claim derived from resolver evidence must cite one or more evidence_refs in the exact
form evidence:<request_id>. Never derive a sensory property, DC outcome, or hidden-world fact from a
character statistic alone. On a resolver retry, answer the latest player request directly. Do not
append unrelated scene recap, atmosphere, or next-step suggestions merely because they appear in
the original context. Output JSON only."""


EXPANDER_SYSTEM_PROMPT = """You expand an approved, player-visible D&D turn contract into concise
player-facing prose. You are not adjudicating the turn. Add no facts, actions, outcomes, motives,
sensory details, certainty, speakers, or player-character behavior beyond the packet. Entity IDs and
claim kinds are semantic controls, not text to show the player. Preserve every visible claim, open
player choice, and typed roll request. When mode is await_player_roll, clearly ask
for that roll using roll_request.instruction exactly for the mechanical wording, preserve its reason,
and do not invent or resolve an outcome.
A delivery field controls style only.

Claims with claim_kind player_declaration were already declared by that character's player. Render
them only as concise third-person attribution (for example, "Mara says that the gate is trapped").
Never turn them into first-person prose, quoted dialogue, a **Character Name:** speaker label, or new
body language. Do not make a protected player character an NPC speaker even when the packet contains
the character's exact words.

Pending action notices are authoritative. A pending sheet proposal is not an applied sheet change:
say that it was proposed and remains pending approval. Never say it was recorded, deducted, applied,
updated, or completed. Pending bookkeeping does not undo or weaken a separately approved narrative
fact: if a beat says a character pays, spends, receives, is hurt, or heals, preserve that fictional
event as written, then separately say the corresponding sheet change is proposed and pending.

Write plain Markdown only, with no JSON, XML, HTML, hidden commentary, headings, or code fences.
When mode is table_chat, respond naturally to table_chat_intent as a brief out-of-character chat
message. Do not narrate the scene, advance the campaign, write memory-worthy facts, or steer the
player back toward the story.
Prefer short responses. Respond as an AI Dungeon Master would: give only as much information as the
moment needs, and expand only when the scene needs more words to paint a good picture. Do not restate
a player declaration unless it is necessary to orient the NPC response. Default to fewer than 80 words;
exceed that only when the packet itself requires detailed instructions, a multi-part ruling, several
independent NPC answers, or richer scene painting. Render NPC dialogue as **Public Speaker:** followed
by the utterance. Stay within max_words."""


class DataFirstTurnError(ValueError):
    def __init__(self, code, message, *, details=None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def data_first_enabled():
    return str(os.environ.get("DND_DATA_FIRST_DM_ENABLED", "false")).strip().lower() in {
        "1", "true", "yes", "on", "enabled",
    }


def _text(value, limit):
    value = str(value or "").strip()
    return value[:limit]


def _normalize_entity_ref(value, *, field, beat_id, claim_index, default_type=None):
    if value is None:
        return None
    if not isinstance(value, dict):
        if default_type is None:
            raise DataFirstTurnError(
                "invalid_entity_ref",
                f"Beat {beat_id} claim {claim_index} {field} must be a typed entity reference.",
            )
        value = {"type": default_type, "id": value}
    ref_type = _text(value.get("type"), 24).lower()
    if ref_type not in {"character", "npc", "location", "object", "entity"}:
        raise DataFirstTurnError(
            "invalid_entity_ref_type",
            f"Beat {beat_id} claim {claim_index} {field} has an invalid entity type.",
        )
    raw_id = value.get("id")
    entity_id = raw_id if isinstance(raw_id, int) and not isinstance(raw_id, bool) else _text(raw_id, 160)
    if entity_id == "":
        raise DataFirstTurnError(
            "blank_entity_ref",
            f"Beat {beat_id} claim {claim_index} {field} requires an id.",
        )
    return {"type": ref_type, "id": entity_id}


def _normalize_ref_list(value, *, field, beat_id, claim_index, default_type=None):
    value = value or []
    if not isinstance(value, list) or len(value) > 12:
        raise DataFirstTurnError(
            "invalid_entity_refs",
            f"Beat {beat_id} claim {claim_index} {field} must be a bounded array.",
        )
    refs = []
    for raw_ref in value:
        ref = _normalize_entity_ref(
            raw_ref,
            field=field,
            beat_id=beat_id,
            claim_index=claim_index,
            default_type=default_type,
        )
        if ref not in refs:
            refs.append(ref)
    return refs


def _normalize_source_refs(value, *, field, beat_id, claim_index):
    value = value or []
    if not isinstance(value, list) or len(value) > 12:
        raise DataFirstTurnError(
            "invalid_source_refs",
            f"Beat {beat_id} claim {claim_index} {field} must be a bounded array.",
        )
    return list(dict.fromkeys(_text(ref, 160) for ref in value if _text(ref, 160)))


def _message_dict(message):
    if isinstance(message, dict):
        return message
    to_dict = getattr(message, "to_dict", None)
    return to_dict() if callable(to_dict) else {}


def entity_id_catalog(hot_context):
    """Collect literal entity IDs already supplied to the planner."""
    hot_context = hot_context if isinstance(hot_context, dict) else {}
    ids = []

    def add(value):
        if isinstance(value, bool) or value is None:
            return
        if isinstance(value, int):
            normalized = value
        else:
            normalized = _text(value, 160)
        if normalized != "" and normalized not in ids:
            ids.append(normalized)

    for key in ("current_character",):
        item = hot_context.get(key)
        if isinstance(item, dict):
            add(item.get("id"))
    for key in ("protected_player_characters", "party"):
        for item in hot_context.get(key) or []:
            if isinstance(item, dict):
                add(item.get("id"))
    for item in hot_context.get("known_npc_actors") or []:
        if isinstance(item, dict):
            add(item.get("id"))

    entity_scalar_keys = {"entity_id", "actor_id", "character_id", "location_id", "npc_id"}
    entity_list_keys = {
        "entity_ids", "active_npc_ids", "departed_npc_ids", "location_ids",
        "subject_entity_ids",
    }

    def visit(value, parent_key=None):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in entity_scalar_keys:
                    add(child)
                elif key in entity_list_keys and isinstance(child, list):
                    for item in child:
                        add(item)
                elif key == "id" and parent_key in {"entities", "npcs", "characters", "locations"}:
                    add(child)
                visit(child, key)
        elif isinstance(value, list):
            for child in value:
                visit(child, parent_key)

    for key in (
        "current_scene", "established_public_facts", "recent_public_world_events",
        "retrieval_packet", "canonical_private_facts",
    ):
        visit(hot_context.get(key), key)
    return ids


def validate_turn_entity_refs(turn_attempt, hot_context):
    """Reject invented actor, subject, or speaker IDs before prose expansion."""
    attempt = normalize_turn_attempt(turn_attempt)
    known = {str(value) for value in entity_id_catalog(hot_context)}
    local_actors = {
        str(actor["local_id"]): actor
        for actor in attempt.get("new_actors") or []
    }
    existing_npc_names = {
        _text(item.get("name"), 160).casefold()
        for item in (hot_context or {}).get("known_npc_actors") or []
        if isinstance(item, dict) and _text(item.get("name"), 160)
    }
    duplicate_names = [
        actor["public_name"] for actor in local_actors.values()
        if actor["public_name"].casefold() in existing_npc_names
    ]
    if duplicate_names:
        raise DataFirstTurnError(
            "duplicate_new_actor",
            "The turn attempted to introduce an NPC whose public name already exists.",
            details={"duplicate_names": duplicate_names},
        )
    unknown = []
    for beat in attempt.get("beats") or []:
        speaker_id = beat.get("speaker_entity_id")
        if speaker_id is not None and str(speaker_id) not in known and str(speaker_id) not in local_actors:
            unknown.append({"beat_id": beat.get("id"), "field": "speaker_entity_id", "id": speaker_id})
        for claim in beat.get("visible_claims") or []:
            claim_refs = [
                *(([claim.get("actor_ref")] if claim.get("actor_ref") else [])),
                *(claim.get("target_refs") or []),
                *(claim.get("topic_refs") or []),
                *(([claim.get("location_ref")] if claim.get("location_ref") else [])),
            ]
            for ref in claim_refs:
                entity_id = ref.get("id") if isinstance(ref, dict) else None
                if entity_id is not None and str(entity_id) not in known and str(entity_id) not in local_actors:
                    unknown.append({
                        "beat_id": beat.get("id"),
                        "field": "entity_ref",
                        "type": ref.get("type"),
                        "id": entity_id,
                    })
    if unknown:
        raise DataFirstTurnError(
            "unknown_entity_reference",
            "The turn attempt used an entity ID that was not supplied in context.",
            details={"unknown_refs": unknown, "known_entity_ids": list(entity_id_catalog(hot_context))},
        )
    return attempt


def canonicalize_turn_character_refs(turn_attempt, hot_context):
    """Translate graph-PC aliases to the literal character IDs used for ownership.

    Graph entities and character rows intentionally use different namespaces. Only
    this narrow, context-supplied mapping is accepted; unknown strings remain
    untouched and are rejected by the normal entity/ownership validators.
    """
    attempt = normalize_turn_attempt(turn_attempt)
    aliases = {
        str(alias): value
        for alias, value in ((hot_context or {}).get("character_ref_aliases") or {}).items()
        if alias not in (None, "") and value is not None
    }
    if not aliases:
        return attempt
    for beat in attempt.get("beats") or []:
        for claim in beat.get("visible_claims") or []:
            refs = [claim.get("actor_ref"), *(claim.get("target_refs") or []), *(claim.get("topic_refs") or []), claim.get("location_ref")]
            for ref in refs:
                if isinstance(ref, dict) and ref.get("type") == "character" and str(ref.get("id")) in aliases:
                    ref["id"] = aliases[str(ref["id"])]
    return attempt


def _normalize_new_actors(raw_actors, mode):
    """Validate ephemeral planner references and make their durable registration actions.

    The local id is deliberately only an in-turn handle; persistence assigns the campaign actor id.
    """
    raw_actors = raw_actors or []
    if not isinstance(raw_actors, list) or len(raw_actors) > MAX_NEW_ACTORS:
        raise DataFirstTurnError("invalid_new_actors", f"new_actors must be an array of at most {MAX_NEW_ACTORS} items.")
    if mode != "speak" and raw_actors:
        raise DataFirstTurnError("unexpected_new_actors", "Only completed speaking attempts may introduce an NPC.")
    actors, seen = [], set()
    for index, raw_actor in enumerate(raw_actors):
        if not isinstance(raw_actor, dict):
            raise DataFirstTurnError("invalid_new_actor", f"New actor {index} must be an object.")
        local_id = _text(raw_actor.get("local_id"), 48)
        if not local_id or not re.fullmatch(r"new_npc_[A-Za-z0-9_-]+", local_id) or local_id in seen:
            raise DataFirstTurnError("invalid_new_actor_id", f"New actor {index} needs a unique local id such as new_npc_1.")
        if _text(raw_actor.get("kind"), 20).lower() != "npc":
            raise DataFirstTurnError("invalid_new_actor_kind", "Only NPC actors may be introduced in a data-first turn.")
        public_name = _text(raw_actor.get("public_name"), 160)
        if not public_name:
            raise DataFirstTurnError("missing_new_actor_name", f"New actor {local_id} needs a public name or descriptor.")
        location_ref = _normalize_entity_ref(raw_actor.get("location_ref"), field="location_ref", beat_id=local_id, claim_index=0)
        if location_ref and location_ref["type"] != "location":
            raise DataFirstTurnError("invalid_new_actor_location", f"New actor {local_id} location_ref must be a location.")
        seen.add(local_id)
        actors.append({
            "local_id": local_id,
            "kind": "npc",
            "public_name": public_name,
            "role": _text(raw_actor.get("role"), 160) or None,
            "public_summary": _text(raw_actor.get("public_summary"), 420) or None,
            "location_ref": location_ref,
        })
    return actors


def validate_turn_claim_provenance(turn_attempt, hot_context, recent_messages=None, evidence_bundle=None):
    """Validate source lanes without interpreting claim prose."""
    attempt = normalize_turn_attempt(turn_attempt)
    messages_by_ref = {}
    for raw_message in [
        *((hot_context or {}).get("recent_messages") or []),
        *(recent_messages or []),
    ]:
        message = _message_dict(raw_message)
        if message.get("id") is not None:
            messages_by_ref[f"session_message:{message['id']}"] = message
    evidence_refs = {
        f"evidence:{item.get('request_id')}"
        for item in ((evidence_bundle or {}).get("evidence") or [])
        if item.get("request_id")
    }
    recent_rolls = {
        str(item.get("request_id")): item
        for item in ((hot_context or {}).get("recent_roll_requests") or [])
        if isinstance(item, dict) and item.get("request_id")
    }
    fulfilled_rolls_by_message_ref = {
        f"session_message:{item.get('result_message_id')}": item
        for item in recent_rolls.values()
        if item.get("status") == "fulfilled" and item.get("result_message_id") is not None
    }
    violations = []
    for beat in attempt.get("beats") or []:
        for claim_index, claim in enumerate(beat.get("visible_claims") or []):
            if claim.get("_legacy_untyped") or claim.get("_legacy_v2"):
                continue
            roll_source = next((
                fulfilled_rolls_by_message_ref.get(ref)
                for ref in [*(claim.get("evidence_refs") or []), *(claim.get("trigger_refs") or [])]
                if fulfilled_rolls_by_message_ref.get(ref)
            ), None)
            if (
                roll_source
                and claim.get("origin") == "dm_adjudication"
                and claim.get("claim_kind") not in {"player_declaration", "npc_utterance"}
            ):
                claim = {
                    **claim,
                    "claim_kind": "roll_outcome",
                    "origin": "roll_adjudication",
                    "roll_request_id": str(roll_source.get("request_id")),
                }
                beat["visible_claims"][claim_index] = claim
                beat["visible_facts"][claim_index] = claim.get("text") or ""
            for lane in ("evidence_refs", "trigger_refs"):
                for ref in claim.get(lane) or []:
                    known = ref in messages_by_ref or ref in evidence_refs
                    if not known:
                        violations.append({
                            "beat_id": beat.get("id"), "claim": claim.get("text"),
                            "lane": lane, "source_ref": ref, "reason": "unknown_source_id",
                        })
            if claim.get("claim_kind") == "npc_utterance":
                for ref in claim.get("evidence_refs") or []:
                    message = messages_by_ref.get(ref)
                    if message and str(message.get("role") or "").lower() == "player":
                        violations.append({
                            "beat_id": beat.get("id"), "claim": claim.get("text"),
                            "lane": "evidence_refs", "source_ref": ref,
                            "reason": "player_message_cannot_evidence_npc_utterance",
                        })
            if claim.get("claim_kind") == "roll_outcome":
                request_id = str(claim.get("roll_request_id") or "")
                request = recent_rolls.get(request_id)
                if not request or request.get("status") != "fulfilled":
                    violations.append({
                        "beat_id": beat.get("id"), "claim": claim.get("text"),
                        "roll_request_id": request_id, "reason": "roll_request_not_fulfilled",
                    })
    if violations:
        raise DataFirstTurnError(
            "invalid_claim_provenance",
            "The turn attempt used an invalid typed provenance edge.",
            details={"violations": violations},
        )
    return attempt


def compact_planner_context(hot_context, recent_messages):
    """Keep planner input bounded while retaining public and private canon lanes."""
    hot_context = hot_context if isinstance(hot_context, dict) else {}
    messages = []
    for raw in (recent_messages or [])[-8:]:
        item = _message_dict(raw)
        messages.append({
            "source_id": f"session_message:{item.get('id')}" if item.get("id") is not None else None,
            "role": item.get("role"),
            "user_id": item.get("user_id"),
            "username": item.get("username"),
            "content": _text(item.get("content"), 900),
        })
    return {
        "campaign": hot_context.get("campaign"),
        "session": hot_context.get("session"),
        "current_character": hot_context.get("current_character"),
        "protected_player_characters": hot_context.get("protected_player_characters") or [],
        "known_entity_ids": entity_id_catalog(hot_context),
        "canonical_character_ref_aliases": hot_context.get("character_ref_aliases") or {},
        "party": hot_context.get("party") or [],
        "known_npc_actors": hot_context.get("known_npc_actors") or [],
        "current_scene": hot_context.get("current_scene") or {},
        "active_clocks": hot_context.get("active_clocks") or [],
        "established_public_facts": hot_context.get("established_public_facts") or [],
        "recent_public_world_events": hot_context.get("recent_public_world_events") or [],
        "open_public_threads": hot_context.get("open_public_threads") or [],
        "canonical_private_facts": hot_context.get("canonical_private_facts") or [],
        "visible_naming_constraints": hot_context.get("visible_naming_constraints") or [],
        "retrieval_packet": hot_context.get("retrieval_packet") or {},
        "recent_roll_requests": hot_context.get("recent_roll_requests") or [],
        "recent_messages": messages,
    }


def build_turn_attempt_messages(hot_context, recent_messages, evidence_bundle=None):
    task = "Produce the next structured turn attempt."
    if evidence_bundle:
        task = (
            "Retry the structured turn attempt using the resolver evidence. Produce a final speak, "
            "await_player_roll, silent, or fallback attempt; do not repeat a satisfied evidence request."
        )
    return [
        {"role": "system", "content": TURN_ATTEMPT_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task": task,
                    "context": compact_planner_context(hot_context, recent_messages),
                    "resolver_evidence": evidence_bundle,
                },
                ensure_ascii=False,
            ),
        },
    ]


def normalize_turn_attempt(raw):
    if not isinstance(raw, dict):
        raise DataFirstTurnError("not_object", "Turn attempt must be a JSON object.")
    mode = _text(raw.get("mode"), 20).lower()
    if mode not in {"speak", "await_player_roll", "table_chat", "silent", "resolve", "fallback"}:
        raise DataFirstTurnError("invalid_mode", "Turn attempt mode is invalid.")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise DataFirstTurnError("invalid_schema_version", f"Turn attempt must use {SCHEMA_VERSION}.")

    raw_beats = raw.get("beats")
    if not isinstance(raw_beats, list):
        raise DataFirstTurnError("invalid_beats", "Turn attempt beats must be an array.")
    if len(raw_beats) > MAX_BEATS:
        raise DataFirstTurnError("too_many_beats", f"Turn attempt may contain at most {MAX_BEATS} beats.")
    if mode in {"speak", "await_player_roll"} and not raw_beats:
        raise DataFirstTurnError("missing_beats", "Visible turn attempts require at least one beat.")
    if mode not in {"speak", "await_player_roll"} and raw_beats:
        raise DataFirstTurnError("unexpected_beats", "Non-visible attempts must not include beats.")

    raw_requests = raw.get("evidence_requests") or []
    if not isinstance(raw_requests, list) or len(raw_requests) > MAX_EVIDENCE_REQUESTS:
        raise DataFirstTurnError(
            "invalid_evidence_requests",
            f"Evidence requests must be an array of at most {MAX_EVIDENCE_REQUESTS} items.",
        )
    if mode == "resolve" and not raw_requests:
        raise DataFirstTurnError("missing_evidence_requests", "Resolve attempts require evidence requests.")
    if mode != "resolve" and raw_requests:
        raise DataFirstTurnError("unexpected_evidence_requests", "Only resolve attempts may request evidence.")

    evidence_requests = []
    seen_request_ids = set()
    for index, request in enumerate(raw_requests):
        if not isinstance(request, dict):
            raise DataFirstTurnError("invalid_evidence_request", f"Evidence request {index} must be an object.")
        request_id = _text(request.get("id"), 48) or f"evidence_{index + 1}"
        tool = _text(request.get("tool"), 80)
        if request_id in seen_request_ids or not re.fullmatch(r"[a-zA-Z0-9_-]+", request_id):
            raise DataFirstTurnError("invalid_evidence_request_id", f"Evidence request {index} has an invalid or duplicate id.")
        if tool not in READ_ONLY_EVIDENCE_TOOLS:
            raise DataFirstTurnError("unsafe_evidence_tool", f"Evidence request {request_id} uses a non-read-only tool.")
        seen_request_ids.add(request_id)
        normalized_request = {"id": request_id, "tool": tool}
        if tool == "ask_character_sheet":
            question = _text(request.get("question"), 600)
            scope = _text(request.get("scope"), 40) or "current_player"
            if not question or scope not in {"current_player", "party", "character_id"}:
                raise DataFirstTurnError("invalid_sheet_request", f"Evidence request {request_id} has invalid sheet arguments.")
            normalized_request.update({"question": question, "scope": scope})
            if scope == "character_id":
                try:
                    normalized_request["character_id"] = int(request.get("character_id"))
                except (TypeError, ValueError):
                    raise DataFirstTurnError("invalid_character_id", f"Evidence request {request_id} requires character_id.")
        elif tool == "search_campaign_memory":
            query = _text(request.get("query"), 240)
            if not query:
                raise DataFirstTurnError("invalid_memory_request", f"Evidence request {request_id} requires a query.")
            try:
                limit = min(20, max(1, int(request.get("limit") or 8)))
            except (TypeError, ValueError):
                limit = 8
            normalized_request.update({"query": query, "limit": limit})
        else:
            normalized_request["include_private"] = request.get("include_private") is not False
        evidence_requests.append(normalized_request)

    safe_prelude = _text(raw.get("safe_prelude"), 240) or None
    if mode == "resolve" and not safe_prelude:
        raise DataFirstTurnError("missing_safe_prelude", "Resolve attempts require a safe prelude.")
    if mode != "resolve" and safe_prelude:
        raise DataFirstTurnError("unexpected_safe_prelude", "Only resolve attempts may include a safe prelude.")

    table_chat_intent = _text(raw.get("table_chat_intent"), 400) or None
    if mode == "table_chat" and not table_chat_intent:
        raise DataFirstTurnError("missing_table_chat_intent", "table_chat attempts require a reply intent.")
    if mode != "table_chat" and table_chat_intent:
        raise DataFirstTurnError("unexpected_table_chat_intent", "Only table_chat attempts may include a reply intent.")

    new_actors = _normalize_new_actors(raw.get("new_actors"), mode)
    raw_actions = raw.get("actions") or []
    generated_registration_ids = {f"register_{actor['local_id']}" for actor in new_actors}
    external_action_count = len(raw_actions) - sum(
        1 for action in raw_actions
        if isinstance(action, dict)
        and action.get("id") in generated_registration_ids
        and action.get("tool") == "register_npc_actor"
    ) if isinstance(raw_actions, list) else MAX_ACTIONS + 1
    if not isinstance(raw_actions, list) or external_action_count + len(new_actors) > MAX_ACTIONS:
        raise DataFirstTurnError("invalid_actions", f"Actions must be an array of at most {MAX_ACTIONS} items.")
    if mode != "speak" and raw_actions:
        raise DataFirstTurnError("unexpected_actions", "Only completed speaking attempts may stage actions.")
    actions = []
    seen_action_ids = set()
    for index, raw_action in enumerate(raw_actions):
        if not isinstance(raw_action, dict):
            raise DataFirstTurnError("invalid_action", f"Action {index} must be an object.")
        action_id = _text(raw_action.get("id"), 48) or f"action_{index + 1}"
        tool = _text(raw_action.get("tool"), 80)
        if action_id in seen_action_ids or not re.fullmatch(r"[A-Za-z0-9_-]+", action_id):
            raise DataFirstTurnError("invalid_action_id", f"Action {index} has an invalid or duplicate id.")
        if tool not in STAGED_ACTION_TOOLS:
            raise DataFirstTurnError("unsafe_action_tool", f"Action {action_id} uses an unsupported mutation tool.")
        if isinstance(raw_action.get("arguments"), dict):
            # normalize_turn_attempt is deliberately idempotent: generated attempts
            # are normalized once at the provider boundary and again by downstream
            # packet/action helpers before they are trusted.
            arguments = dict(raw_action["arguments"])
        else:
            try:
                arguments = json.loads(str(raw_action.get("arguments_json") or ""))
            except (TypeError, ValueError):
                raise DataFirstTurnError("invalid_action_arguments", f"Action {action_id} arguments_json is invalid JSON.")
        if not isinstance(arguments, dict):
            raise DataFirstTurnError("invalid_action_arguments", f"Action {action_id} arguments must decode to an object.")
        if tool == "propose_sheet_update" and isinstance(arguments.get("changes"), list):
            field_aliases = {"gold": "gp", "gold_piece": "gp", "gold_pieces": "gp"}
            arguments["changes"] = [
                {
                    **change,
                    "field": field_aliases.get(
                        str(change.get("field") or "").strip().lower(),
                        change.get("field"),
                    ),
                }
                if isinstance(change, dict) else change
                for change in arguments["changes"]
            ]
        seen_action_ids.add(action_id)
        actions.append({"id": action_id, "tool": tool, "arguments": arguments})
    existing_action_ids = {action["id"] for action in actions}
    for actor in new_actors:
        action_id = f"register_{actor['local_id']}"
        if action_id in existing_action_ids:
            continue
        actions.append({
            "id": action_id,
            "tool": "register_npc_actor",
            "arguments": {
                "local_id": actor["local_id"],
                "name": actor["public_name"],
                "role": actor["role"],
                "public_summary": actor["public_summary"],
                "location_id": (actor["location_ref"] or {}).get("id"),
            },
        })

    raw_roll_request = raw.get("roll_request")
    roll_request = None
    if raw_roll_request is not None:
        if mode != "await_player_roll":
            raise DataFirstTurnError("unexpected_roll_request", "Only await_player_roll may include a roll request.")
        try:
            from services.session_rolls import normalize_roll_request
            roll_request = normalize_roll_request(raw_roll_request)
        except ValueError as error:
            raise DataFirstTurnError("invalid_roll_request", str(error))
    if mode == "await_player_roll" and roll_request is None:
        raise DataFirstTurnError("missing_roll_request", "await_player_roll requires a typed roll request.")

    beats = []
    seen_ids = set()
    for index, raw_beat in enumerate(raw_beats):
        if not isinstance(raw_beat, dict):
            raise DataFirstTurnError("invalid_beat", f"Beat {index} must be an object.")
        beat_id = _text(raw_beat.get("id"), 48) or f"beat_{index + 1}"
        if beat_id in seen_ids or not re.fullmatch(r"[a-zA-Z0-9_-]+", beat_id):
            raise DataFirstTurnError("invalid_beat_id", f"Beat {index} has an invalid or duplicate id.")
        seen_ids.add(beat_id)
        beat_type = _text(raw_beat.get("type"), 24).lower()
        if beat_type not in {"narration", "npc_dialogue"}:
            raise DataFirstTurnError("invalid_beat_type", f"Beat {beat_id} has an invalid type.")
        raw_claims = raw_beat.get("visible_claims")
        legacy_facts = raw_beat.get("visible_facts")
        if raw_claims is None and isinstance(legacy_facts, list):
            # Compatibility for stored/audit fixtures produced before v2. Provider
            # output is constrained by TURN_ATTEMPT_JSON_SCHEMA to use typed claims.
            raw_claims = [{
                "text": fact,
                "claim_kind": "npc_utterance" if beat_type == "npc_dialogue" else "observation",
                "actor_ref": None,
                "target_refs": [],
                "topic_refs": [],
                "location_ref": None,
                "evidence_refs": raw_beat.get("source_refs") or [],
                "trigger_refs": [],
                "origin": "established_state",
                "roll_request_id": None,
                "_legacy_untyped": True,
            } for fact in legacy_facts]
        if not isinstance(raw_claims, list) or not raw_claims or len(raw_claims) > MAX_FACTS_PER_BEAT:
            raise DataFirstTurnError(
                "invalid_visible_claims",
                f"Beat {beat_id} must contain 1-{MAX_FACTS_PER_BEAT} visible claims.",
            )
        visible_claims = []
        for claim_index, raw_claim in enumerate(raw_claims):
            if not isinstance(raw_claim, dict):
                raise DataFirstTurnError("invalid_visible_claim", f"Beat {beat_id} claim {claim_index} must be an object.")
            clean = _text(raw_claim.get("text"), 500)
            if not clean:
                raise DataFirstTurnError("blank_visible_claim", f"Beat {beat_id} contains a blank visible claim.")
            claim_kind = _text(raw_claim.get("claim_kind"), 40).lower()
            if claim_kind not in {
                "observation", "world_fact", "npc_utterance",
                "player_declaration", "roll_instruction", "roll_outcome",
            }:
                raise DataFirstTurnError("invalid_claim_kind", f"Beat {beat_id} claim {claim_index} has an invalid kind.")
            legacy_v2 = any(key in raw_claim for key in ("subject_entity_ids", "actor_character_id", "source_refs"))
            if legacy_v2:
                old_sources = raw_claim.get("source_refs") or []
                old_actor_id = raw_claim.get("actor_character_id")
                raw_claim = {
                    **raw_claim,
                    "actor_ref": (
                        {"type": "character", "id": old_actor_id}
                        if old_actor_id is not None else None
                    ),
                    "target_refs": [],
                    "topic_refs": [
                        {"type": "entity", "id": entity_id}
                        for entity_id in raw_claim.get("subject_entity_ids") or []
                    ],
                    "location_ref": None,
                    "evidence_refs": [] if claim_kind == "roll_instruction" else old_sources,
                    "trigger_refs": old_sources if claim_kind == "roll_instruction" else [],
                    "origin": (
                        "player_transcript" if claim_kind == "player_declaration"
                        else "dm_adjudication" if claim_kind == "roll_instruction"
                        else "established_state"
                    ),
                    "roll_request_id": None,
                    "_legacy_v2": True,
                }
            actor_ref = _normalize_entity_ref(
                raw_claim.get("actor_ref"),
                field="actor_ref",
                beat_id=beat_id,
                claim_index=claim_index,
            )
            target_refs = _normalize_ref_list(
                raw_claim.get("target_refs"), field="target_refs", beat_id=beat_id, claim_index=claim_index,
            )
            topic_refs = _normalize_ref_list(
                raw_claim.get("topic_refs"), field="topic_refs", beat_id=beat_id, claim_index=claim_index,
            )
            location_ref = _normalize_entity_ref(
                raw_claim.get("location_ref"),
                field="location_ref",
                beat_id=beat_id,
                claim_index=claim_index,
            )
            evidence_refs = _normalize_source_refs(
                raw_claim.get("evidence_refs"), field="evidence_refs", beat_id=beat_id, claim_index=claim_index,
            )
            trigger_refs = _normalize_source_refs(
                raw_claim.get("trigger_refs"), field="trigger_refs", beat_id=beat_id, claim_index=claim_index,
            )
            if set(evidence_refs) & set(trigger_refs):
                raise DataFirstTurnError(
                    "ambiguous_claim_provenance",
                    f"Beat {beat_id} claim {claim_index} uses the same source as evidence and trigger.",
                )
            origin = _text(raw_claim.get("origin"), 40).lower()
            if origin not in {
                "player_transcript", "established_state", "resolver_evidence",
                "dm_adjudication", "roll_adjudication",
            }:
                raise DataFirstTurnError("invalid_claim_origin", f"Beat {beat_id} claim {claim_index} has an invalid origin.")
            roll_request_id = _text(raw_claim.get("roll_request_id"), 160) or None
            compatibility_claim = bool(raw_claim.get("_legacy_untyped") or raw_claim.get("_legacy_v2"))
            if not compatibility_claim:
                # Canonicalize explicit structural signals rather than trying to
                # infer them from prose. A roll reference makes the claim a roll
                # outcome; a character-authored transcript claim is a declaration.
                if roll_request_id:
                    claim_kind = "roll_outcome"
                    origin = "roll_adjudication"
                elif origin == "player_transcript" and actor_ref and actor_ref["type"] == "character":
                    claim_kind = "player_declaration"
                if actor_ref and actor_ref["type"] == "location":
                    location_ref = location_ref or actor_ref
                    actor_ref = None
                elif actor_ref and actor_ref["type"] in {"object", "entity"} and claim_kind in {
                    "observation", "world_fact", "roll_outcome",
                }:
                    if actor_ref not in topic_refs:
                        topic_refs.append(actor_ref)
                    actor_ref = None
                if beat_type == "npc_dialogue":
                    if claim_kind != "npc_utterance":
                        raise DataFirstTurnError("invalid_dialogue_claim_kind", f"Beat {beat_id} dialogue may contain only NPC utterances.")
                    if actor_ref is None or actor_ref["type"] != "npc":
                        raise DataFirstTurnError("missing_npc_actor_ref", f"Beat {beat_id} NPC utterance requires an NPC actor_ref.")
                elif claim_kind == "npc_utterance":
                    raise DataFirstTurnError("npc_utterance_in_narration", f"Beat {beat_id} narration cannot contain an NPC utterance.")
                if claim_kind == "player_declaration":
                    if actor_ref is None or actor_ref["type"] != "character":
                        raise DataFirstTurnError("missing_actor_character_id", f"Beat {beat_id} player declaration requires a character actor_ref.")
                    if origin != "player_transcript" or not evidence_refs:
                        raise DataFirstTurnError("invalid_player_declaration_origin", f"Beat {beat_id} player declaration requires transcript evidence.")
                if claim_kind == "roll_instruction" and (mode != "await_player_roll" or origin != "dm_adjudication"):
                    raise DataFirstTurnError("invalid_roll_instruction", f"Beat {beat_id} roll instruction requires await_player_roll DM adjudication.")
                if claim_kind == "roll_outcome" and (
                    mode != "speak" or origin != "roll_adjudication" or not roll_request_id
                ):
                    raise DataFirstTurnError("invalid_roll_outcome", f"Beat {beat_id} roll outcome requires a fulfilled roll request reference.")
                if origin == "roll_adjudication" and claim_kind != "roll_outcome":
                    raise DataFirstTurnError("invalid_roll_outcome_kind", f"Beat {beat_id} roll adjudication must use roll_outcome.")
                if claim_kind != "roll_outcome" and roll_request_id:
                    raise DataFirstTurnError("unexpected_roll_request_ref", f"Beat {beat_id} claim has an unexpected roll request reference.")
                if claim_kind == "npc_utterance" and origin == "dm_adjudication" and not trigger_refs:
                    raise DataFirstTurnError("missing_npc_trigger", f"Beat {beat_id} new NPC utterance requires a trigger source.")
                if origin == "resolver_evidence" and not any(ref.startswith("evidence:") for ref in evidence_refs):
                    raise DataFirstTurnError("missing_resolver_evidence", f"Beat {beat_id} resolver claim requires evidence_refs.")
            visible_claims.append({
                "text": clean,
                "claim_kind": claim_kind,
                "actor_ref": actor_ref,
                "target_refs": target_refs,
                "topic_refs": topic_refs,
                "location_ref": location_ref,
                "evidence_refs": evidence_refs,
                "trigger_refs": trigger_refs,
                "origin": origin,
                "roll_request_id": roll_request_id,
                **({"_legacy_untyped": True} if raw_claim.get("_legacy_untyped") else {}),
                **({"_legacy_v2": True} if raw_claim.get("_legacy_v2") else {}),
            })
        visible_facts = [claim["text"] for claim in visible_claims]
        speaker_entity_id = _text(raw_beat.get("speaker_entity_id"), 160) or None
        speaker = _text(raw_beat.get("speaker_public_name"), 160)
        if beat_type == "npc_dialogue" and not speaker:
            raise DataFirstTurnError("missing_speaker", f"Beat {beat_id} requires a public speaker name.")
        if beat_type == "npc_dialogue" and not speaker_entity_id and not any(
            claim.get("_legacy_untyped") or claim.get("_legacy_v2") for claim in visible_claims
        ):
            raise DataFirstTurnError("missing_speaker_entity_id", f"Beat {beat_id} requires a known speaker entity id.")
        for claim in visible_claims:
            if (
                beat_type == "npc_dialogue"
                and speaker_entity_id
                and not (claim.get("_legacy_untyped") or claim.get("_legacy_v2"))
                and str((claim.get("actor_ref") or {}).get("id")) != str(speaker_entity_id)
            ):
                raise DataFirstTurnError("speaker_actor_mismatch", f"Beat {beat_id} speaker and NPC actor_ref must match.")
        truth_status = _text(raw_beat.get("truth_status"), 24).lower()
        private_context = _text(raw_beat.get("dm_private_context"), 900)
        if beat_type == "npc_dialogue":
            if truth_status not in {"truthful", "mistaken", "deceptive", "incomplete", "unknown"}:
                raise DataFirstTurnError("invalid_truth_status", f"Beat {beat_id} requires a valid truth status.")
            if truth_status == "unknown" and not private_context:
                private_context = "The authoritative truth of this NPC statement is not established by supplied evidence."
            if truth_status not in {"truthful", "unknown"} and not private_context:
                raise DataFirstTurnError(
                    "missing_private_context",
                    f"Beat {beat_id} requires private interpretation for non-truthful dialogue.",
                )
        beats.append({
            "id": beat_id,
            "type": beat_type,
            **({"speaker_entity_id": speaker_entity_id} if speaker_entity_id else {}),
            **({"speaker_public_name": speaker} if speaker else {}),
            "visible_claims": visible_claims,
            # Internal compatibility alias; public packets contain visible_claims only.
            "visible_facts": visible_facts,
            **({"delivery": _text(raw_beat.get("delivery"), 240)} if _text(raw_beat.get("delivery"), 240) else {}),
            "source_refs": list(dict.fromkeys(
                ref for claim in visible_claims
                for ref in [*(claim.get("evidence_refs") or []), *(claim.get("trigger_refs") or [])]
            )),
            **({"truth_status": truth_status} if beat_type == "npc_dialogue" else {}),
            **({"dm_private_context": private_context} if private_context else {}),
        })

    max_words = raw.get("max_words", DEFAULT_MAX_WORDS)
    try:
        max_words = int(max_words)
    except (TypeError, ValueError):
        max_words = DEFAULT_MAX_WORDS
    max_words = min(300, max(30, max_words))
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "reason": _text(raw.get("reason"), 500),
        "beats": beats,
        "open_player_choice": _text(raw.get("open_player_choice"), 500) or None,
        "max_words": max_words,
        "safe_prelude": safe_prelude,
        "table_chat_intent": table_chat_intent,
        "evidence_requests": evidence_requests,
        "actions": actions,
        "new_actors": new_actors,
        "roll_request": roll_request,
    }


def _pending_action_notices(action_buffer):
    notices = []
    for action in (action_buffer or {}).get("actions") or []:
        if action.get("name") != "propose_sheet_update":
            continue
        proposal = ((action.get("preview") or {}).get("proposal") or {})
        changes = []
        for change in proposal.get("changes") or []:
            label = _text(change.get("label") or change.get("field"), 80)
            if not label:
                continue
            changes.append({
                "field": label,
                "before": change.get("before"),
                "after": change.get("after"),
            })
        notices.append({
            "type": "sheet_update_proposal",
            "status": "pending_player_approval",
            "changes": changes,
            **({"reason": _text(proposal.get("reason"), 300)} if proposal.get("reason") else {}),
            "required_wording": (
                "Preserve separately listed fictional events as completed. Describe only the "
                "character-sheet bookkeeping as proposed, pending approval, and not yet applied."
            ),
        })
    return notices


def _remove_applied_proposal_claims(public_beats, pending_notices):
    if not pending_notices:
        return public_beats
    mutation_claim = re.compile(
        r"\b(?:record(?:ed)?|deducted|deduction|appl(?:y|ied)|updat(?:e|ed)|"
        r"chang(?:e|ed)|reduc(?:e|ed)|increas(?:e|ed)|new\s+total|now\s+has)\b",
        flags=re.IGNORECASE,
    )
    filtered = []
    for beat in public_beats:
        claims = []
        for claim in beat.get("visible_claims") or []:
            text = claim.get("text") or ""
            if claim.get("_legacy_untyped") and (
                mutation_claim.search(text) or _PROPOSAL_LIFECYCLE_CLAIM.search(text)
            ):
                continue
            claims.append(claim)
        if claims:
            filtered.append({**beat, "visible_claims": claims})
    return filtered


def _public_roll_instruction(roll_request):
    label = _text((roll_request or {}).get("label"), 160) or "check"
    article = "an" if label[:1].lower() in "aeiou" else "a"
    state = _text((roll_request or {}).get("advantage_state"), 20).lower()
    suffix = " with advantage" if state == "advantage" else " with disadvantage" if state == "disadvantage" else ""
    return f"Make {article} {label}{suffix}."


def public_expansion_packet(turn_attempt, *, authorized_player_facts=None, action_buffer=None):
    """Return the only data the prose expander is allowed to see."""
    attempt = normalize_turn_attempt(turn_attempt)
    if attempt["mode"] == "table_chat":
        return {
            "schema_version": SCHEMA_VERSION,
            "mode": "table_chat",
            "table_chat_intent": attempt["table_chat_intent"],
            "max_words": attempt["max_words"],
        }
    if attempt["mode"] not in {"speak", "await_player_roll"}:
        raise DataFirstTurnError("not_speaking", "Only visible attempts have an expansion packet.")
    public_beats = []
    for beat in attempt["beats"]:
        public_claims = [{
            "text": claim["text"],
            "claim_kind": claim["claim_kind"],
            "actor_ref": claim.get("actor_ref"),
            "target_refs": claim.get("target_refs") or [],
            "topic_refs": claim.get("topic_refs") or [],
            "location_ref": claim.get("location_ref"),
            "origin": claim.get("origin"),
            "roll_request_id": claim.get("roll_request_id"),
            **({"_legacy_untyped": True} if claim.get("_legacy_untyped") else {}),
            **({"_legacy_v2": True} if claim.get("_legacy_v2") else {}),
        } for claim in beat.get("visible_claims") or []]
        public_beats.append({
            "id": beat["id"],
            "type": beat["type"],
            **({"speaker_entity_id": beat["speaker_entity_id"]} if beat.get("speaker_entity_id") else {}),
            **({"speaker_public_name": beat["speaker_public_name"]} if beat.get("speaker_public_name") else {}),
            "visible_claims": public_claims,
            **({"delivery": beat["delivery"]} if beat.get("delivery") else {}),
        })
    pending_notices = _pending_action_notices(action_buffer)
    public_beats = _remove_applied_proposal_claims(public_beats, pending_notices)
    public_beats = [{
        **beat,
        "visible_claims": [
            {key: value for key, value in claim.items() if not key.startswith("_")}
            for claim in beat.get("visible_claims") or []
        ],
    } for beat in public_beats]
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": attempt["mode"],
        "beats": public_beats,
        "open_player_choice": attempt["open_player_choice"],
        "max_words": attempt["max_words"],
        "rendering_constraints": {
            "player_authored_facts": list(dict.fromkeys(authorized_player_facts or [])),
            "player_character_dialogue": "third_person_attribution_only",
        },
        **({"pending_action_notices": pending_notices} if pending_notices else {}),
        **({
            "roll_request": {
                "roll_kind": attempt["roll_request"]["roll_kind"],
                "ability_or_skill": attempt["roll_request"]["ability_or_skill"],
                "label": attempt["roll_request"]["label"],
                "instruction": _public_roll_instruction(attempt["roll_request"]),
                "reason_public": attempt["roll_request"]["reason_public"],
            },
        } if attempt.get("roll_request") else {}),
    }


def _canonical_public_item_ids(hot_context):
    ids = set()
    for item in (hot_context or {}).get("established_public_facts") or []:
        item_id = _text(item.get("id"), 160)
        if item_id:
            ids.add(item_id)
    for item in (hot_context or {}).get("recent_public_world_events") or []:
        item_id = _text(item.get("id"), 160)
        event_type = _text(item.get("event_type"), 160)
        ids.update(value for value in (item_id, event_type, f"fact_{event_type}" if event_type else "") if value)
    return ids


def _public_fact_support_tokens(hot_context, source_ids):
    tokens = set()
    normalized_ids = {
        str(source_id).removeprefix("fact:").removesuffix(":text")
        for source_id in source_ids or []
    }
    for item in (hot_context or {}).get("established_public_facts") or []:
        if str(item.get("id")) in normalized_ids:
            tokens.update(_provenance_tokens(item.get("text")))
    return tokens


def guard_turn_actions(turn_attempt, hot_context):
    """Make redundant reveals idempotent and downgrade unsupported public claims."""
    attempt = normalize_turn_attempt(turn_attempt)
    visible_ids = _canonical_public_item_ids(hot_context)
    guarded_actions = []
    notes = []
    for action in attempt.get("actions") or []:
        args = dict(action.get("arguments") or {})
        if (
            action.get("tool") == "reveal_fact"
            and args.get("visibility") in {"public", "party_known"}
            and _text(args.get("item_id"), 160) in visible_ids
        ):
            notes.append({"action_id": action["id"], "result": "already_visible_reveal_skipped"})
            continue
        if action.get("tool") == "record_world_event" and args.get("visibility") in {"public", "party_known"}:
            payload = dict(args.get("payload") or {})
            summary_tokens = _provenance_tokens(args.get("summary"))
            support_tokens = _public_fact_support_tokens(hot_context, payload.get("source_facet_ids"))
            coverage = len(summary_tokens & support_tokens) / max(1, len(summary_tokens))
            if coverage < 0.5:
                original_visibility = args.get("visibility")
                summary = _text(args.get("summary"), 1100)
                if not re.match(r"^(?:unverified\s+)?player\s+(?:claim|theory|hypothesis)\b", summary, re.I):
                    summary = f"Unverified player claim: {summary}"
                payload.update({
                    "epistemic_status": "player_claim",
                    "requested_visibility": original_visibility,
                })
                args.update({"summary": summary, "payload": payload, "visibility": "dm_private"})
                notes.append({
                    "action_id": action["id"],
                    "result": "unsupported_public_claim_downgraded",
                    "support_coverage": round(coverage, 3),
                })
        guarded_actions.append({**action, "arguments": args})
    return {**attempt, "actions": guarded_actions}, notes


_PROVENANCE_STOP_WORDS = {
    "about", "after", "again", "against", "along", "before", "being", "beside",
    "could", "does", "from", "have", "into", "itself", "more", "only", "other",
    "over", "said", "says", "speaks", "their", "there", "these", "they", "this",
    "through", "toward", "towards", "under", "what", "when", "where", "which",
    "while", "with", "would", "your",
}


def _provenance_tokens(value, character=None):
    """Return stable content tokens for deterministic transcript grounding."""
    value = re.sub(r"<[^>]+>", " ", str(value or "")).lower()
    excluded = set(_PROVENANCE_STOP_WORDS)
    if isinstance(character, dict):
        excluded.update(re.findall(r"[a-z0-9]+", str(character.get("name") or "").lower()))
    tokens = set()
    for token in re.findall(r"[a-z0-9]+", value):
        if len(token) < 4 or token in excluded:
            continue
        # Lightweight stemming makes calls/called and strikes/struck-like surface
        # variations less brittle without turning this into a semantic model call.
        for suffix in ("ing", "ied", "ed", "es", "s"):
            if token.endswith(suffix) and len(token) - len(suffix) >= 4:
                token = token[:-len(suffix)]
                break
        tokens.add(token)
    return tokens


def _fact_grounded_in_player_message(fact, message_content, character):
    fact_tokens = _provenance_tokens(fact, character)
    message_tokens = _provenance_tokens(message_content, character)
    overlap = fact_tokens & message_tokens
    required = 1 if len(fact_tokens) <= 3 else 2
    return len(overlap) >= required, sorted(overlap)


def authorized_player_fact_texts(turn_attempt, hot_context, recent_messages, violation_detector):
    """Authorize typed PC declarations by literal character and message IDs.

    V2 claims do not require name/verb regex interpretation. Legacy untyped claims
    retain the old detector only so historical fixtures and audit payloads remain
    readable during the migration.
    """
    attempt = normalize_turn_attempt(turn_attempt)
    messages_by_ref = {}
    combined_messages = [
        *((hot_context or {}).get("recent_messages") or []),
        *(recent_messages or []),
    ]
    for raw_message in combined_messages:
        message = _message_dict(raw_message)
        message_id = message.get("id")
        if message_id is not None:
            messages_by_ref[f"session_message:{message_id}"] = message

    authorized = []
    protected_by_id = {}
    for item in ((hot_context or {}).get("protected_player_characters") or []):
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        try:
            protected_by_id[int(item["id"])] = item
        except (TypeError, ValueError):
            continue
    for beat in attempt.get("beats") or []:
        for claim in beat.get("visible_claims") or []:
            fact = claim.get("text") or ""
            refs = claim.get("evidence_refs") or []
            if not claim.get("_legacy_untyped"):
                if claim.get("claim_kind") != "player_declaration":
                    continue
                actor_id = (claim.get("actor_ref") or {}).get("id")
                try:
                    actor_id = int(actor_id)
                except (TypeError, ValueError):
                    actor_id = None
                character = protected_by_id.get(actor_id)
                checked = []
                supported = False
                if character is None:
                    checked.append({"reason": "unknown_actor_character_id", "actor_character_id": actor_id})
                else:
                    for source_ref in refs:
                        message = messages_by_ref.get(source_ref)
                        if not message:
                            checked.append({"source_ref": source_ref, "reason": "unknown_source"})
                            continue
                        if str(message.get("role") or "").lower() != "player":
                            checked.append({"source_ref": source_ref, "reason": "not_player_authored"})
                            continue
                        if character.get("user_id") is None or character.get("user_id") != message.get("user_id"):
                            checked.append({"source_ref": source_ref, "reason": "wrong_character_owner"})
                            continue
                        checked.append({"source_ref": source_ref, "reason": "owner_and_source_ids_verified"})
                        supported = True
                        break
                if not supported:
                    raise DataFirstTurnError(
                        "unproven_player_behavior",
                        "A player declaration was not tied to its owner's transcript by literal IDs.",
                        details={
                            "beat_id": beat.get("id"),
                            "fact": fact,
                            "actor_character_id": actor_id,
                            "source_refs": refs,
                            "checked_sources": checked,
                        },
                    )
                authorized.append(fact)
                continue

            # Temporary compatibility path for v1/stored untyped claims.
            violation = violation_detector(fact, hot_context)
            if not violation:
                continue
            character = violation.get("character") or {}
            character_name = str(character.get("name") or "").strip().casefold()
            candidate_characters = [
                item for item in ((hot_context or {}).get("protected_player_characters") or [])
                if str(item.get("name") or "").strip().casefold() == character_name
            ] or [character]
            checked = []
            supported = False
            for source_ref in refs:
                message = messages_by_ref.get(source_ref)
                if not message:
                    checked.append({"source_ref": source_ref, "reason": "unknown_source"})
                    continue
                if str(message.get("role") or "").lower() != "player":
                    checked.append({"source_ref": source_ref, "reason": "not_player_authored"})
                    continue
                source_character = next((
                    item for item in candidate_characters
                    if item.get("user_id") is not None and item.get("user_id") == message.get("user_id")
                ), None)
                if source_character is None:
                    checked.append({"source_ref": source_ref, "reason": "wrong_character_owner"})
                    continue
                grounded, overlap = _fact_grounded_in_player_message(
                    fact, message.get("content"), source_character,
                )
                checked.append({
                    "source_ref": source_ref,
                    "reason": "grounded" if grounded else "insufficient_textual_support",
                    "overlap": overlap,
                })
                if grounded:
                    supported = True
                    break
            if not supported:
                raise DataFirstTurnError(
                    "unproven_player_behavior",
                    "A protected player-character fact was not grounded in its owner's transcript.",
                    details={
                        "beat_id": beat.get("id"),
                        "fact": fact,
                        "agency": violation,
                        "source_refs": refs,
                        "checked_sources": checked,
                    },
                )
            authorized.append(fact)
    return authorized


def memory_private_context(turn_attempt):
    """Combine private beat interpretations for the accepted response sidecar."""
    attempt = normalize_turn_attempt(turn_attempt)
    contexts = []
    for beat in attempt["beats"]:
        private_context = beat.get("dm_private_context")
        if private_context:
            contexts.append(f"{beat['id']}: {private_context}")
    return "\n".join(contexts)


def expansion_basis_text(packet):
    """Stable text used by deterministic privacy/identifier checks and audits."""
    lines = []
    for beat in packet.get("beats") or []:
        speaker = beat.get("speaker_public_name")
        if speaker:
            lines.append(speaker)
        lines.extend(
            claim.get("text") or ""
            for claim in beat.get("visible_claims") or []
            if isinstance(claim, dict)
        )
        if beat.get("delivery"):
            lines.append(beat["delivery"])
    if packet.get("open_player_choice"):
        lines.append(packet["open_player_choice"])
    roll_request = packet.get("roll_request")
    if isinstance(roll_request, dict):
        lines.extend(value for value in [
            roll_request.get("ability_or_skill"),
            roll_request.get("label"),
            roll_request.get("reason_public"),
        ] if value)
    return "\n".join(lines)


def build_expander_messages(packet):
    return [
        {"role": "system", "content": EXPANDER_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": "Expand this approved public packet:\n" + json.dumps(packet, ensure_ascii=False),
        },
    ]


def generate_turn_attempt(hot_context, recent_messages, audit_context=None, evidence_bundle=None):
    """Make the single bounded adjudication call used by the MVP path."""
    from openrouter import _json_loads_or_empty, _post_chat

    raw_text = _post_chat(
        build_turn_attempt_messages(hot_context, recent_messages, evidence_bundle=evidence_bundle),
        json_mode=True,
        json_schema=TURN_ATTEMPT_JSON_SCHEMA,
        json_schema_name="data_first_turn_attempt",
        reasoning_effort=os.environ.get("DND_DATA_FIRST_PLAN_REASONING_EFFORT", "minimal"),
        audit_context={
            **(audit_context or {}),
            "operation": "data_first_turn_replan" if evidence_bundle else "data_first_turn_attempt",
            "actor": "session_dm_turn_planner",
        },
        allow_thinking=False,
        timeout_seconds=max(5.0, float(os.environ.get("DND_DATA_FIRST_PLAN_TIMEOUT_SECONDS", "30"))),
        max_attempts=1,
        max_tokens=max(256, int(os.environ.get("DND_DATA_FIRST_PLAN_MAX_TOKENS", "4000"))),
    )
    return normalize_turn_attempt(_json_loads_or_empty(raw_text))


def _compact_evidence(value, depth=0):
    """Bound tool output before returning it to the planner."""
    if depth >= 5:
        return "[bounded]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:1200]
    if isinstance(value, list):
        return [_compact_evidence(item, depth + 1) for item in value[:12]]
    if isinstance(value, dict):
        return {
            _text(key, 120): _compact_evidence(item, depth + 1)
            for key, item in list(value.items())[:40]
        }
    return _text(value, 1200)


def evidence_request_arguments(request):
    tool = request.get("tool")
    if tool == "ask_character_sheet":
        args = {
            "question": request.get("question"),
            "scope": request.get("scope") or "current_player",
        }
        if request.get("character_id") is not None:
            args["character_id"] = request["character_id"]
        return args
    if tool == "search_campaign_memory":
        return {"query": request.get("query"), "limit": request.get("limit") or 8}
    if tool == "get_current_scene":
        return {"include_private": request.get("include_private") is not False}
    raise DataFirstTurnError("unsafe_evidence_tool", f"Unsupported evidence tool: {tool!r}.")


def stage_turn_actions(turn_attempt, execute_tool, audit_context=None):
    """Validate supported writes through the existing deferred-action layer."""
    attempt = normalize_turn_attempt(turn_attempt)
    action_buffer = {"actions": []}
    commit_action_ids = []
    for action in attempt.get("actions") or []:
        result = execute_tool(
            action["tool"],
            action["arguments"],
            {
                **(audit_context or {}),
                "operation": "data_first_action_staging",
                "actor": "session_dm_turn_planner",
                "pending_action_buffer": action_buffer,
            },
        )
        if not isinstance(result, dict) or result.get("error") or not result.get("pending_action_id"):
            raise DataFirstTurnError(
                "action_staging_failed",
                f"Failed to stage {action['tool']}.",
                details={"action_id": action["id"], "result": result},
            )
        commit_action_ids.append(result["pending_action_id"])
    return action_buffer, commit_action_ids


def gather_turn_evidence(turn_attempt, execute_tool, audit_context=None, on_request=None):
    """Execute one validated, read-only evidence pass for a resolve attempt."""
    attempt = normalize_turn_attempt(turn_attempt)
    if attempt["mode"] != "resolve":
        raise DataFirstTurnError("not_resolving", "Only resolve attempts may gather evidence.")
    evidence = []
    for request in attempt["evidence_requests"]:
        tool = request["tool"]
        arguments = evidence_request_arguments(request)
        if callable(on_request):
            on_request(request, arguments)
        try:
            result = execute_tool(
                tool,
                arguments,
                {
                    **(audit_context or {}),
                    "operation": "data_first_evidence_gathering",
                    "actor": "session_dm_evidence_resolver",
                },
            )
        except Exception as error:
            result = {"error": repr(error)}
        evidence.append({
            "request_id": request["id"],
            "tool": tool,
            "arguments": arguments,
            "result": _compact_evidence(result),
        })
    return {
        "resolution_pass": 1,
        "safe_prelude": attempt.get("safe_prelude"),
        "complete": all(
            not (isinstance(item.get("result"), dict) and item["result"].get("error"))
            for item in evidence
        ),
        "evidence": evidence,
    }


_PROPOSAL_LIFECYCLE_CLAIM = re.compile(
    r"\b(?:has\s+been\s+(?:staged|proposed)|is\s+proposed|proposed\s+and\s+pending|"
    r"pending\s+approval|not\s+yet\s+(?:applied|updated|executed)|sheet\s+not\s+yet|"
    r"would\s+leave)\b",
    flags=re.IGNORECASE,
)


def _resolved_source_tokens(value, stop_words):
    tokens = set()
    for token in re.findall(r"[a-z0-9]+", json.dumps(value, ensure_ascii=False).lower()):
        if token in stop_words:
            continue
        if len(token) < 4:
            continue
        for suffix in ("ing", "ied", "ed", "es", "s"):
            if token.endswith(suffix) and len(token) - len(suffix) >= 4:
                token = token[:-len(suffix)]
                break
        tokens.add(token)
    return tokens


def _resolved_source_numbers(value):
    text = json.dumps(value, ensure_ascii=False).lower()
    numbers = set(re.findall(r"\b\d+\b", text))
    word_numbers = {
        "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
        "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    }
    for word, number in word_numbers.items():
        if re.search(rf"\b{word}\b", text):
            numbers.add(number)
    return numbers


def validate_resolved_attempt_sources(turn_attempt, evidence_bundle, recent_messages=None, hot_context=None):
    """Ground each resolved fact in its appropriate transcript or evidence lane.

    A beat may cite both the player's request and resolver evidence.  Individual
    facts need support from at least one appropriate source, not every source on
    the beat.  Deferred proposal lifecycle facts are omitted here and rebuilt
    later from the successfully staged action preview.
    """
    attempt = normalize_turn_attempt(turn_attempt)
    if attempt["mode"] not in {"speak", "await_player_roll"}:
        return attempt
    if not evidence_bundle.get("complete"):
        raise DataFirstTurnError(
            "incomplete_evidence_used",
            "The planner produced visible claims from an incomplete evidence bundle.",
        )
    evidence_by_ref = {
        f"evidence:{item.get('request_id')}": item.get("result")
        for item in evidence_bundle.get("evidence") or []
        if item.get("request_id")
    }
    valid_refs = set(evidence_by_ref)
    cited_refs = {
        ref
        for beat in attempt["beats"]
        for claim in beat.get("visible_claims") or []
        for ref in claim.get("evidence_refs") or []
        if str(ref).startswith("evidence:")
    }
    if not cited_refs:
        raise DataFirstTurnError(
            "missing_evidence_citation",
            "The resolved speaking attempt did not cite resolver evidence.",
        )
    unknown_refs = cited_refs - valid_refs
    if unknown_refs:
        raise DataFirstTurnError(
            "unknown_evidence_citation",
            "The resolved speaking attempt cited unknown resolver evidence.",
            details={"unknown_refs": sorted(unknown_refs)},
        )
    stop_words = {
        "about", "after", "again", "against", "because", "before", "being", "below",
        "could", "does", "from", "have", "into", "itself", "more", "only", "other",
        "provided", "remains", "sheet", "that", "their", "there", "these", "they",
        "this", "through", "under", "what", "when", "where", "which", "while", "with",
        "would", "your",
    }
    messages_by_ref = {}
    for raw_message in [
        *((hot_context or {}).get("recent_messages") or []),
        *(recent_messages or []),
    ]:
        message = _message_dict(raw_message)
        if message.get("id") is not None:
            messages_by_ref[f"session_message:{message['id']}"] = message

    has_sheet_proposal = any(
        action.get("tool") == "propose_sheet_update"
        for action in attempt.get("actions") or []
    )
    supported_beats = []
    rejected_facts = []
    for beat in attempt["beats"]:
        supported_claims = []
        for claim in beat.get("visible_claims") or []:
            fact = claim.get("text") or ""
            claim_refs = claim.get("evidence_refs") or []
            claim_evidence_refs = [ref for ref in claim_refs if str(ref).startswith("evidence:")]
            claim_message_refs = [
                ref for ref in claim_refs
                if str(ref).startswith("session_message:") and ref in messages_by_ref
            ]
            if has_sheet_proposal and claim.get("_legacy_untyped") and _PROPOSAL_LIFECYCLE_CLAIM.search(fact):
                continue
            source_checks = []
            grounded = False

            # V2 trusts an exact, valid per-claim provenance edge. Meaning is
            # carried by the typed claim and IDs; no regex/word-overlap attempt
            # is made to reverse-engineer it from its rendering text.
            if not claim.get("_legacy_untyped"):
                for ref in claim_evidence_refs:
                    source_checks.append({
                        "source_ref": ref,
                        "source_lane": "resolver_evidence",
                        "reason": "exact_source_id_verified",
                    })
                    grounded = ref in evidence_by_ref
                    if grounded:
                        break
                if not grounded:
                    for ref in claim_message_refs:
                        message = messages_by_ref[ref]
                        if beat.get("type") == "npc_dialogue" and str(message.get("role") or "").lower() == "player":
                            source_checks.append({
                                "source_ref": ref,
                                "source_lane": "player_transcript",
                                "reason": "player_message_cannot_ground_npc_dialogue",
                            })
                            continue
                        source_checks.append({
                            "source_ref": ref,
                            "source_lane": "transcript",
                            "reason": "exact_source_id_verified",
                        })
                        grounded = True
                        break
            else:
                fact_tokens = _resolved_source_tokens(fact, stop_words)
                fact_numbers = _resolved_source_numbers(fact)
                for ref in claim_evidence_refs:
                    source = evidence_by_ref.get(ref)
                    overlap = fact_tokens & _resolved_source_tokens(source, stop_words)
                    numbers_supported = fact_numbers <= _resolved_source_numbers(source)
                    source_checks.append({
                        "source_ref": ref,
                        "source_lane": "resolver_evidence",
                        "overlap": sorted(overlap),
                        "numbers_supported": numbers_supported,
                    })
                    if overlap and numbers_supported:
                        grounded = True
                        break
                if not grounded:
                    for ref in claim_message_refs:
                        message = messages_by_ref[ref]
                        if beat.get("type") == "npc_dialogue" and str(message.get("role") or "").lower() == "player":
                            source_checks.append({
                                "source_ref": ref,
                                "source_lane": "player_transcript",
                                "reason": "player_message_cannot_ground_npc_dialogue",
                            })
                            continue
                        overlap = fact_tokens & _resolved_source_tokens(message.get("content"), stop_words)
                        numbers_supported = fact_numbers <= _resolved_source_numbers(message.get("content"))
                        source_checks.append({
                            "source_ref": ref,
                            "source_lane": "transcript",
                            "overlap": sorted(overlap),
                            "numbers_supported": numbers_supported,
                        })
                        if overlap and numbers_supported:
                            grounded = True
                            break
            if grounded:
                supported_claims.append(claim)
            else:
                rejected_facts.append({
                    "beat_id": beat.get("id"),
                    "fact": fact,
                    "source_checks": source_checks,
                })
        if supported_claims:
            supported_beats.append({
                **beat,
                "visible_claims": supported_claims,
                "visible_facts": [claim["text"] for claim in supported_claims],
                "source_refs": list(dict.fromkeys(
                    ref for claim in supported_claims
                    for ref in [*(claim.get("evidence_refs") or []), *(claim.get("trigger_refs") or [])]
                )),
            })

    if not supported_beats:
        first_rejection = rejected_facts[0] if rejected_facts else {}
        raise DataFirstTurnError(
            "unsupported_evidence_claim",
            "A resolved visible claim is not grounded in its cited sources.",
            details={
                **first_rejection,
                "rejected_facts": rejected_facts,
            },
        )
    return {**attempt, "beats": supported_beats}


def resolve_and_retry_turn_attempt(
    hot_context,
    recent_messages,
    turn_attempt,
    execute_tool,
    audit_context=None,
    on_request=None,
    on_evidence_ready=None,
):
    """Gather read-only evidence and perform exactly one planner retry."""
    bundle = gather_turn_evidence(
        turn_attempt,
        execute_tool,
        audit_context=audit_context,
        on_request=on_request,
    )
    if callable(on_evidence_ready):
        on_evidence_ready(bundle)
    retried = generate_turn_attempt(
        hot_context,
        recent_messages,
        audit_context=audit_context,
        evidence_bundle=bundle,
    )
    if retried["mode"] == "resolve":
        raise DataFirstTurnError(
            "resolution_loop",
            "The planner requested another evidence pass after the bounded retry.",
        )
    return validate_resolved_attempt_sources(
        retried,
        bundle,
        recent_messages=recent_messages,
        hot_context=hot_context,
    ), bundle


def stream_turn_expansion(packet, audit_context=None, on_token=None):
    """Expand an approved public packet while forwarding visible token deltas."""
    from openrouter import _post_chat_stream

    return _post_chat_stream(
        build_expander_messages(packet),
        json_mode=False,
        audit_context={
            **(audit_context or {}),
            "operation": "data_first_prose_expansion",
            "actor": "session_dm_prose_expander",
        },
        allow_thinking=False,
        timeout_seconds=max(5.0, float(os.environ.get("DND_DATA_FIRST_EXPAND_TIMEOUT_SECONDS", "45"))),
        max_attempts=1,
        # The packet's word budget governs prose length. Do not truncate a valid
        # exceptional detailed response at the transport layer.
        max_tokens=None,
        on_token=on_token,
        provider=os.environ.get("DND_DATA_FIRST_EXPAND_PROVIDER", "opencode_go"),
        model=os.environ.get("DND_DATA_FIRST_EXPAND_MODEL", "gpt-5.6-luna"),
        reasoning_effort=os.environ.get("DND_DATA_FIRST_EXPAND_REASONING_EFFORT", "none"),
    )


def validate_expansion_text(text, packet):
    value = str(text or "").strip()
    if not value:
        raise DataFirstTurnError("blank_expansion", "The prose expander returned no visible text.")
    if re.search(r"</?\s*(?:npc|ic|ooc|html|script)\b", value, flags=re.IGNORECASE):
        raise DataFirstTurnError("expansion_markup", "The prose expander returned forbidden markup.")
    max_words = int(packet.get("max_words") or DEFAULT_MAX_WORDS)
    if len(value.split()) > max_words + 12:
        raise DataFirstTurnError("expansion_too_long", "The prose expansion exceeded its bounded allowance.")
    return value

import os
import json
import re
import tempfile
import time
from threading import Lock
from pathlib import Path
from uuid import uuid4
import requests
from dotenv import load_dotenv

from services.audit_service import log_audit_event, log_model_error, log_model_request, log_model_response

load_dotenv()

OPENROUTER_API_KEY = os.environ.get('OPENROUTER_API_KEY', '')
OPENROUTER_MODEL = os.environ.get('OPENROUTER_MODEL', '')
OPENROUTER_BASE_URL = os.environ.get('OPENROUTER_BASE_URL', 'https://openrouter.ai/api/v1/chat/completions')

OPENCODE_GO_API_KEY = os.environ.get('OPENCODE_GO_API_KEY', '')
OPENCODE_GO_MODEL = os.environ.get('OPENCODE_GO_MODEL', '')
OPENCODE_GO_BASE_URL = os.environ.get(
    'OPENCODE_GO_BASE_URL',
    'https://opencode.ai/zen/go/v1/chat/completions',
)
OPENCODE_GO_THINKING = os.environ.get('OPENCODE_GO_THINKING', 'disabled')
OPENCODE_GO_REASONING_EFFORT = os.environ.get('OPENCODE_GO_REASONING_EFFORT', 'high')

LLM_PROVIDER = os.environ.get('LLM_PROVIDER', 'openrouter').strip().lower() or 'openrouter'
SUPPORTED_LLM_PROVIDERS = {'openrouter', 'opencode_go'}
_LLM_MODEL_LOCK = Lock()
LLM_RUNTIME_MODEL_FILE = Path(os.environ.get(
    'LLM_RUNTIME_MODEL_FILE',
    os.environ.get(
        'OPENROUTER_RUNTIME_MODEL_FILE',
        os.path.join(tempfile.gettempdir(), 'new_dnd_testing_lol_llm_model'),
    ),
))
LLM_MAX_ATTEMPTS = max(1, int(os.environ.get('LLM_MAX_ATTEMPTS', os.environ.get('OPENROUTER_MAX_ATTEMPTS', '4'))))
LLM_RETRY_BASE_DELAY_SECONDS = max(
    0.0,
    float(os.environ.get('LLM_RETRY_BASE_DELAY_SECONDS', os.environ.get('OPENROUTER_RETRY_BASE_DELAY_SECONDS', '1'))),
)
LLM_RETRY_MAX_DELAY_SECONDS = max(
    LLM_RETRY_BASE_DELAY_SECONDS,
    float(os.environ.get('LLM_RETRY_MAX_DELAY_SECONDS', os.environ.get('OPENROUTER_RETRY_MAX_DELAY_SECONDS', '8'))),
)
SESSION_PREFLIGHT_TIMEOUT_SECONDS = max(
    1.0,
    float(os.environ.get('SESSION_PREFLIGHT_TIMEOUT_SECONDS', '12')),
)
SESSION_PREFLIGHT_MAX_TOKENS = max(
    64,
    int(os.environ.get('SESSION_PREFLIGHT_MAX_TOKENS', '320')),
)
SESSION_MEMORY_TIMEOUT_SECONDS = max(
    5.0,
    float(os.environ.get('SESSION_MEMORY_TIMEOUT_SECONDS', '45')),
)
SESSION_MEMORY_MAX_ATTEMPTS = max(
    1,
    int(os.environ.get('SESSION_MEMORY_MAX_ATTEMPTS', '1')),
)
SESSION_MEMORY_MAX_TOKENS = max(
    256,
    int(os.environ.get('SESSION_MEMORY_MAX_TOKENS', '8192')),
)
SESSION_MEMORY_MODE = os.environ.get('SESSION_MEMORY_MODE', 'staged').strip().lower() or 'staged'

PC_CONTROL_POLICY = (
    "Player-character control policy: NPCs may speak and act. Player characters are protected. "
    "Do not write exact dialogue for any player character unless that player supplied the exact words. "
    "Do not take over another player's PC by inventing their dialogue, choices, inner state, or consequential behavior. "
    "Keep references to protected PCs light and non-controlling. "
    "For the current player's PC, resolve only the action or words they declared; prefer second person "
    "and do not add unprovided intent, emotion, or quoted speech. If one PC addresses another PC and "
    "no DM adjudication, NPC response, environmental consequence, or clarification is needed, stay silent."
)

SYSTEM_PROMPT = (
    "You are a Dungeon Master for a Dungeons & Dragons campaign. Your creator is phazedrl. "
    "For each response, determine from the current context whether the reply should be "
    "in character or out of character, and use whichever mode best serves the player. "
    "Player messages may contain <ic>...</ic> and <ooc>...</ooc> sections. Treat "
    "<ic> text as the player character's spoken words or direct in-world action, and "
    "<ooc> text as table talk, intent, questions, or instructions from the player. "
    "When in character, narrate the story, describe scenes, play NPCs, and adjudicate "
    "player actions. When speaking as a specific NPC, wrap only that NPC's spoken line "
    "or performance in <npc target=\"NPC name\">...</npc>; leave narration outside the "
    "NPC tag. Every <npc> tag must include target and must close with </npc>. Do not use "
    "<ic>, <ooc>, HTML, XML, or any other angle-bracket tags in visible DM replies. "
    "Write visible replies in English only; do not emit stray non-English glyphs or mixed-language fragments. "
    + PC_CONTROL_POLICY + " "
    "Before drafting a visible reply, identify from context which characters are protected player characters, "
    "which spoken lines are NPC speech, which names or identities are still unrevealed, and what the players "
    "have actually earned the right to know. "
    "Never write dialogue, assent, banter, reactions, emotional tells, or decisions for a protected player "
    "character unless the player already supplied those exact words or actions. This includes short confirmations "
    "like \"okay,\" \"right,\" \"let's go,\" nods, thanks, sighs, and similar implied participation. If a protected "
    "player character response is needed, ask the player, hand off briefly, or stay silent. "
    "When using <npc target=\"...\">...</npc>, the target must be a concrete in-world speaker reference grounded "
    "in the transcript or memory. Never use placeholders or meta labels such as \"the speaker,\" \"speaker,\" "
    "\"NPC,\" \"someone,\" \"voice,\" \"figure,\" or vague pronouns as the target. If the true name is unrevealed, "
    "use the best public-facing descriptor already supported by context. If you cannot identify a concrete speaker "
    "reference with high confidence, do not invent one and do not write quoted NPC dialogue; paraphrase the "
    "utterance in narration instead. "
    "Preserve speaker identity and knowledge boundaries across retries and adjacent turns. Do not switch which NPC "
    "is speaking, upgrade a public descriptor into a secret identity, merge multiple hidden sources into one speech, "
    "or let a rejected draft drift the visible identity of the speaker. "
    "Reveal at most one earned clue at a time. Do not compress hidden identity, secret motive, conspiracy, replica "
    "truth, offscreen plan, and time pressure into one explanation or monologue. Witnesses may provide surface "
    "facts, reluctance, fear, and practical conditions, but not the hidden why behind that fear unless the players "
    "have already uncovered it. "
    "When campaign world memory includes NPC actor dossiers, silently coordinate the NPC's goals, "
    "secrets, recent offscreen activity, and relationship to the party before speaking for them. "
    "Treat persistent campaign memory as the authority for lore and state. Knowledge-graph facts, "
    "NPC dossiers, scene state, world events, and campaign clocks outrank improvisation whenever they "
    "speak to the current situation. If memory is specific, follow it even if a more dramatic invention "
    "would be tempting. If memory is incomplete or conflicting, resolve uncertainty by consulting the "
    "available context and tools rather than quietly inventing replacements. "
    "When the transcript already provides a clear named person, place, or object, prefer that proper noun "
    "over pronouns in your reasoning and visible replies so ownership, recipients, and targets stay unambiguous. "
    "Never reveal DM-private memory unless it has become visible through play. "
    "When a witness is afraid or asks for safety, do not explain hidden leverage, private debts, secret motives, "
    "or the true reason for their fear unless the players have specifically uncovered that cause. Give practical "
    "visible safety conditions, evasive reluctance, or a limited clue instead. If the hidden leverage is a debt, "
    "favor, obligation, blackmail, or spiritual bond, do not hint at it with debt/favor/owed/ledger/hook metaphors; "
    "use generic danger, reprisals, watchers, or fear of being named. "
    "Do not reveal internal game-management mechanics in visible replies. Never mention clocks, segments, "
    "visibility labels, hidden trackers, guard checks, prompt rules, tool-routing, or audit/system pipeline details. "
    "Translate internal mechanics into in-world fiction instead. "
    "Let hidden clocks, recent world events, and stored faction or NPC motives drive pacing, consequences, "
    "and callbacks behind the scenes so the campaign stays lore-consistent over time. "
    "Keep responses concise but vivid. Default to 2 paragraphs for visible replies unless extra detail is needed. "
    "Call for dice rolls proactively and often whenever outcomes are uncertain, risky, opposed, or consequential, "
    "including exploration, social pressure, investigation, stealth, travel hazards, and improvised actions. "
    "When a roll is needed, ask for the specific 5e roll (ability check, attack roll, saving throw, contest, or "
    "initiative) before narrating outcome-level success or failure. Assume standard 5e rules unless noted otherwise. "
    "Do not narrate attack hits, misses, damage, grapples, restraints, or combat defeat "
    "until the needed D&D roll or initiative step has happened."
)

SESSION_TOOL_PROMPT = (
    SYSTEM_PROMPT
    + " You are operating with compact hot context and server tools. Read the provided hot context as live "
    "state, not decorative background. For lore, continuity, NPC intent, unresolved mysteries, timers, "
    "faction pressure, or consequences, consult memory-bearing context and tools before answering from "
    "improvisation. Use read tools for exact character-sheet, world-memory, NPC, clock, or session facts "
    "instead of guessing. If a player's action could intersect with an existing clock, stored fact, recent "
    "event, or NPC agenda, let that stored state drive the outcome. When facts are missing, say only what "
    "the world presently supports and keep invention narrowly consistent with established memory. Use write "
    "tools only when the fiction has actually changed durable world state. Do not expose DM-private tool "
    "results in visible narration unless they became known through play. The hot context contains internal "
    "tracking data; never surface that data structure directly to players. Keep visible text diegetic and "
    "player-facing, not system-facing. "
    "Player-supplied specifics are not automatically true. Treat recollections, accusations, bluff details, "
    "and theories from players as claims unless they are corroborated by the visible scene, a successful check, "
    "established public facts, or another grounded source. NPCs may react to such claims without validating "
    "them as objective truth. If a reply would reinterpret an established public lead, preserve uncertainty "
    "and avoid replacing prior committed facts unless the fiction clearly earns it. "
    "protected_player_characters and current_player_character; obey those boundaries exactly. "
    "For the final turn decision, call exactly one finalization tool: use talk_to_player with visible player-facing content "
    "when the DM should send a visible reply, or call stay_silent with a short reason when the DM should not "
    "send anything. Do not mix a finalization tool call with other tools in the same assistant message. "
    "Do not send the final visible reply as plain assistant text. "
    "When finalizing with talk_to_player, always include commit_action_ids. Select only pending action IDs whose durable mutation should commit with this exact reply, or use an empty array. "
    "Do not send handoff prompts such as 'How do you respond?' for ordinary PC-to-PC conversation."
)

PLANNING_SYSTEM_PROMPT = (
    "You are a context-aware Dungeon Master helping a D&D party plan characters before play. "
    "For the visible message, determine from the current context whether the reply should be "
    "in character or out of character, and use whichever mode best serves the player. "
    "You can see confirmed characters, party readiness, party planning summaries, pending bonds, "
    "explicit player points, DM-private secrets, and recent planning messages. Help the current "
    "player shape a character that fits the campaign, improves party balance, and creates story "
    "connections. Suggest and ask; do not force choices. Treat DM-private secrets as hidden from "
    "other players. Return only valid JSON with keys: message, active_page, form_patch. message is "
    "the visible DM response. active_page is one of identity, scores, combat, magic_gear, story, or "
    "null. form_patch contains only character-form fields that should be filled from the conversation; "
    "omit fields you are not confident about."
)

SUMMARY_SYSTEM_PROMPT = (
    "You update compact D&D campaign character-planning memory. Return only valid JSON. "
    "Preserve explicit player-stated points. Separate public facts from DM-private secrets. "
    "Suggest bonds only when two or more specific player user IDs are involved and both should approve. "
    "For explicit_player_points, return the complete canonical list for each user ID you include, not "
    "a delta to append. Only include durable player-authored character choices, preferences, constraints, "
    "boundaries, and accepted hooks. Exclude greetings, thanks, repeated wording, raw quoted messages, "
    "and operational requests like asking the DM to create, write, generate, fill in, or help. If a "
    "player delegates unspecified details to the DM, keep that as at most one concise durable preference "
    "only when it should affect future character creation. Rewrite duplicates into one clear sentence."
)

WORLD_GENESIS_SYSTEM_PROMPT = (
    "You are a D&D campaign architect creating the first persistent world package after all player "
    "characters are ready. Return only valid JSON. The public_intro must be a spoiler-free elevator "
    "pitch for players: no hidden villains, no true motives, no secret identities, no private clocks, "
    "and no DM-only explanations. Put all secrets, hidden agendas, true causes, NPC secrets, and "
    "pressure mechanics in dm_private, knowledge_graph, npc_actors, clocks, or world_state with "
    "visibility set to dm_private where appropriate. Build around the party's confirmed characters, "
    "accepted hooks, planning summary, campaign seed, description, and tone. Create a playable opening "
    "situation with active pressures, not a complete railroad."
)

SESSION_MEMORY_SYSTEM_PROMPT = (
    "You update durable D&D campaign memory after a visible DM turn. Return only valid JSON. "
    "Emit the JSON object immediately; keep internal reasoning minimal and do not spend output budget on planning. "
    "Extract durable changes from the latest player message and visible DM reply. Create new clocks "
    "when new pressure, deadlines, mysteries, faction moves, or consequences emerge, especially if all "
    "existing active clocks are completed. Retire completed or resolved clocks instead of deleting them. "
    "Update knowledge graph entities, relations, and facts without duplicating existing ids. Preserve "
    "existing entity, relation, and fact ids from context or relevant_memory whenever a patch updates the same "
    "durable concept; create new ids only for distinct concepts not already represented. Preserve "
    "visibility as dm_private, party_known, or public. Use party_known for facts established in the latest "
    "visible player/DM exchange; use public only for broadly public world facts; use dm_private only for "
    "unrevealed secrets, hidden causes, off-screen actions, or DM-only pressure. Do not invent large new lore unless it follows "
    "from the exchange. Only write graph facts for durable truths that should remain useful after the "
    "current scene changes. Put current location, occupants, and tension in scene_patch instead. "
    "Prefer explicit proper nouns over pronouns, and preserve named ownership. "
    "For every memory item created, updated, or retired (in graph entities, relations, facts, clocks, "
    "NPC actors, or events), you must include these metadata fields: "
    "1. 'certainty': 'confirmed | suspected | inferred | false | retconned' "
    "2. 'importance': 1-5 (where 5 is campaign-defining and 1 is a minor detail) "
    "3. 'expires_or_retire_condition': concise description of when this memory expires or should be retired, or null "
    "4. 'reason': concise 1-sentence reason why this change or new memory is warranted "
    "5. 'memory_type': 'npc | fact | relation | clock | location | quest | inventory | money'"
)

SESSION_MEMORY_SUMMARY_SCENE_SYSTEM_PROMPT = (
    "You update only the compact session summary and current scene after a visible DM turn. "
    "Return only valid JSON with keys turn_summary and scene_patch. "
    "turn_summary must be one concise durable summary of what materially changed in the latest exchange. "
    "scene_patch may include only location_id, location_name, time_of_day, active_npc_ids, departed_npc_ids, and immediate_tension. "
    "time_of_day must match the value shown in current_scene unless the visible exchange explicitly describes a time transition. "
    "active_npc_ids is the NPC ids currently on stage. Listing an NPC is encouraged but not required: "
    "NPCs already on stage stay on stage unless you also list them in departed_npc_ids. "
    "departed_npc_ids is the NPC ids who actually left the scene this turn (walked out, fled, were escorted away, "
    "or otherwise exited). Do not infer departure from omission. "
    "Do not include markdown, commentary, or any other keys."
)

SESSION_MEMORY_FACTS_SYSTEM_PROMPT = (
    "You extract only durable campaign facts from the latest visible exchange. "
    "Return only valid JSON with key upsert_graph_facts. "
    "Use an empty array unless the exchange created or clarified a durable fact worth remembering after the scene moves on. "
    "Each fact must include id, entity_ids, text, certainty, visibility, importance, expires_or_retire_condition, reason, and memory_type. "
    "Visibility policy: facts directly established by latest_player_message or latest_dm_message are party_known unless they are broadly public; "
    "public means generally known beyond the party; dm_private is only for unrevealed secrets, hidden causes, off-screen actions, or DM-only pressure. "
    "When relevant_memory includes a matching fact item_id, reuse that id and update/clarify the fact instead of creating a sibling. "
    "When relevant_memory includes matching entity item_ids, use those ids in entity_ids instead of inventing new ids. "
    "Only create a new fact id when no existing relevant_memory fact represents the same durable truth. "
    "Do not include scene-transient occupancy, immediate tension, or repeated facts already obvious from the current scene."
)

SESSION_MEMORY_CLOCKS_SYSTEM_PROMPT = (
    "You manage only campaign clocks after a visible DM turn. "
    "Return only valid JSON with keys create_clocks and retire_clocks. "
    "Prefer empty arrays unless the exchange clearly introduced new durable pressure, a deadline, a mystery clock, or resolved an existing clock. "
    "Do not create clocks for ordinary scene beats or clues that do not change campaign pressure."
)

SESSION_MEMORY_EXTRACTOR_SYSTEM_PROMPT = (
    "You are the extraction stage of a D&D session memory writer. "
    "Return only valid JSON. "
    "Write a fresh running_summary that cleanly replaces the old one. "
    "Do not append fragments to the prior summary. Rewrite the current durable state after the latest visible exchange in one compact paragraph. "
    "Additionally, extract or update the structured memory_anchors representing the current session state: current_goal (string or null), current_scene (string or null), open_clues (array of strings), unresolved_questions (array of strings), npc_observations (array of strings), and recent_offers_promises (array of strings). Do not append fragments; completely rewrite or prune these anchors to reflect the current state. "
    "Extract candidate scene updates, durable fact claims, entity upserts, relation upserts, NPC updates, and event records from the visible exchange. "
    "Do not invent canonical ids. If a name is not already a known id in the prompt, preserve it as a raw label/ref for later resolution. "
    "Scene updates may include location_id, location_name, time_of_day, active_npc_ids, departed_npc_ids, and immediate_tension. "
    "Claims must use source_surface of visible_transcript, hidden_state, or inferred. "
    "Return keys running_summary, memory_anchors, scene_patch, scene_reason, fact_claims, entity_claims, relation_claims, npc_claims, and event_claims. "
    "Each fact_claim must include text, entity_refs, source_surface, intended_visibility, certainty, importance, reason, expires_or_retire_condition, and memory_type. "
    "Each entity_claim must include name, type, summary, tags, source_surface, intended_visibility, certainty, importance, reason, expires_or_retire_condition, and memory_type. "
    "Each relation_claim must include type, source_ref, target_ref, summary, source_surface, intended_visibility, certainty, importance, reason, expires_or_retire_condition, and memory_type. "
    "Each npc_claim must include actor_ref, name, role, public_summary, voice, background, wants, fears, secrets, certainty, importance, reason, and expires_or_retire_condition. "
    "Each event_claim must include event_type, summary, payload, intended_visibility, certainty, importance, reason, and expires_or_retire_condition."
)

SESSION_MEMORY_RESOLVER_SYSTEM_PROMPT = (
    "You are the resolver stage of a D&D session memory writer. "
    "Use read-only tools to resolve scene, entity, and fact references before any durable write is compiled. "
    "Never invent ids. If you cannot resolve a reference with confidence, return it in unresolved_items instead of mutating memory. "
    "Prefer get_entity_candidates, get_scene_candidates, get_fact_candidates, and search_campaign_memory before broad raw-state tools. "
    "Use get_world_state, get_npcs, get_clocks, or transcript tools only when narrower tools are insufficient. "
    "Final response must be exactly one JSON object with keys running_summary, memory_anchors, scene_patch, scene_reason, upsert_graph_entities, upsert_graph_relations, upsert_graph_facts, update_npc_actors, record_events, unresolved_items, evidence_basis, resolved_entity_refs, and resolved_location_refs. "
    "Return the resolved/updated memory_anchors representing the current session state: current_goal (string or null), current_scene (string or null), open_clues (array of strings), unresolved_questions (array of strings), npc_observations (array of strings), and recent_offers_promises (array of strings). "
    "Each upsert_graph_entities item must include id (if reusing or resolving to a canonical id), name, type, summary, tags, source_surface, intended_visibility, certainty, importance, reason, expires_or_retire_condition, and memory_type. "
    "Each upsert_graph_relations item must include id (if reusing/resolving), type, source_id (resolved entity/actor id), target_id (resolved entity/actor id), summary, source_surface, intended_visibility, certainty, importance, reason, expires_or_retire_condition, and memory_type. "
    "Each upsert_graph_facts item must include text, entity_ids (resolved entity/actor ids), id if reusing an existing fact, source_surface, intended_visibility, certainty, importance, reason, expires_or_retire_condition, and memory_type. "
    "Each update_npc_actors item must include id/actor_id (resolved NPC actor id), name, role, public_summary, voice, background, wants, fears, secrets, certainty, importance, reason, and expires_or_retire_condition. "
    "Each record_events item must include event_type, summary, payload, intended_visibility, certainty, importance, reason, and expires_or_retire_condition."
)

SESSION_CLOCK_ADJUDICATOR_SYSTEM_PROMPT = (
    "You adjudicate campaign clock progression after a visible DM turn. "
    "Return only valid JSON with keys create_clocks, advance_clocks, retire_clocks, and no_change_explanations. "
    "Use only the visible player message, visible DM reply, scene transition, and existing active clocks. "
    "Do not invent offscreen developments, hidden actions, or private causes that were not visible in the exchange. "
    "For each existing active clock, either advance it or explain why it did not change. "
    "Use delta 1 for ordinary visible progress or escalation, delta 2 only for major irreversible escalation, and delta -1 only for visible relief or setback. "
    "Do not emit delta 0. Do not advance unrelated clocks. "
    "If a new pressure is already underway, prefer creating the new clock with filled set to 1 instead of 0. "
    "Do not create a new clock if an existing active clock already covers the same pressure. "
    "Each advance_clocks item must include clock_id, delta, reason, and evidence. "
    "Each no_change_explanations item must include clock_id and reason."
)

SESSION_SPOILER_CHECK_SYSTEM_PROMPT = (
    "You are a spoiler-safety checker for visible Dungeon Master replies. "
    "Return only valid JSON. Decide whether the candidate visible DM reply directly reveals or strongly implies "
    "any unrevealed private item. A reply is unsafe when a reasonable player could learn the hidden truth from "
    "the reply itself. Ordinary foreshadowing, mood, uncertainty, and clues that do not effectively answer the "
    "hidden truth are safe. Do not mark a reply unsafe merely because it is thematically related to a private item. "
    "Mystery play progresses through earned clues: if the latest visible player action directly questions a present "
    "witness, examines a clue, or follows up on a lead, a limited in-world clue from that witness or clue is safe "
    "unless it reveals the hidden culprit, full conspiracy, private motive, private plan, or final solution outright. "
    "Treat hidden operational telemetry and pressure cues as spoiler-sensitive when they map to unrevealed private "
    "items, including sensor pings/contacts, launch detections, pursuit signatures, military-grade capability "
    "identification, and clock-like escalation details. "
    "Also flag when the candidate reply implies that events are caused by an internal clock, timer, countdown, "
    "pressure gauge, or other hidden mechanical state tracker, even when no specific private clock name appears. "
    "Internal clocks are hidden narrative-pressure trackers, not something the players should perceive. "
    "The DM must never communicate that in-world events happen because of hidden state mechanics; "
    "all causality should appear to emerge naturally from the fiction."
)

SESSION_MECHANICS_CHECK_SYSTEM_PROMPT = (
    "You are a D&D 5e mechanical-adjudication guard for visible Dungeon Master replies. "
    "Return only valid JSON. The preflight router has already decided that the latest player intent likely "
    "requires a roll, initiative, or other D&D mechanical step before the outcome is known. Decide whether "
    "the candidate visible reply improperly resolves that uncertain mechanical outcome before the roll/step "
    "happens. Unsafe replies narrate hits, misses, damage, restraint, capture, knockdown, forced movement, "
    "combat defeat, or success/failure of a contested or risky action without requesting or reporting the "
    "needed mechanic. Safe replies ask for a roll, call for initiative, clarify intent, or describe only setup "
    "and immediate reactions without deciding the mechanical outcome."
)

SESSION_NPC_TAG_CHECK_SYSTEM_PROMPT = (
    "You are a formatting checker for visible Dungeon Master replies. "
    "Return only valid JSON. Decide whether the candidate visible DM reply contains quoted NPC-performed "
    "speech or performance that should be wrapped in <npc target=\"NPC name\">...</npc> but is currently "
    "plain text. Be conservative. Safe replies include plain narration, unattributed quoted text, signage, "
    "interface text, remembered phrases, ambient sounds, or stylized prose that is not clearly an NPC's "
    "current performed line. Unsafe replies clearly attribute a current spoken line or performed utterance "
    "to a specific NPC or named non-player speaker without the required <npc> wrapper. "
    "If the attributed speech is already correctly wrapped in <npc target=\"NPC name\">...</npc>, "
    "return requires_npc_tag=false."
)

SESSION_PC_CONTROL_CHECK_SYSTEM_PROMPT = (
    "You are a player-agency guard for visible Dungeon Master replies. "
    "Return only valid JSON. Decide whether the candidate visible DM reply improperly takes control of a protected "
    "player character. Unsafe replies invent a protected player's dialogue, strategic choices, intent, major emotions, "
    "interior thoughts, or consequential actions the player did not declare. Safe replies may narrate immediate "
    "environmental consequences, hazards, impacts, forced movement, damage, slips, falls, collisions, or changed "
    "positioning that naturally follow from the player's declared action, the scene, or a rolled outcome. "
    "Safe replies may also add low-stakes follow-through to an action the player already committed to, such as how "
    "they run across the room, fall into step with an ally they agreed to accompany, or make a brief assent gesture "
    "while carrying out the declared action. Safe replies may add minor affective color such as sadness, relief, "
    "tension, or hesitation when it does not impose a new decision, belief, plan, or strategy. "
    "Safe replies may use second person to describe consequences. Do not mark a reply unsafe merely because it "
    "describes what happens to the acting player character after they already attempted something risky. "
    "Be especially careful to allow consequence narration such as losing footing, getting hit, taking damage, or "
    "ending up in a new spot when the world caused it rather than the DM inventing a new choice."
)

SESSION_CANON_DISCIPLINE_CHECK_SYSTEM_PROMPT = (
    "You are an evidence-discipline and continuity guard for visible Dungeon Master replies. "
    "Return only valid JSON. The DM may improvise in open space, but must not present unsupported claims as "
    "objective truth. Unsafe replies do either of these: "
    "1. convert player-supplied specifics, guesses, accusations, or remembered details into confirmed world truth "
    "without corroboration from public evidence, the visible scene, a successful check, or an already-established fact; "
    "2. contradict, discard, or sharply reframe an already-established public lead, clue, ownership claim, relationship, "
    "location truth, or recent visible action outcome without visible support. "
    "Also treat transactional continuity as evidence discipline: if a recent visible exchange established that an object "
    "was already taken, given away, pocketed, spent, opened, broken, or otherwise resolved, the candidate reply must not "
    "re-offer, re-place, or reset that same object state unless the fiction visibly explains the reversal. "
    "Safe replies may react skeptically, conditionally, or provisionally to player claims, may let NPCs say "
    "that a detail sounds familiar, and may leave uncertainty in place. "
    "Be especially cautious when a candidate reply introduces a new proper noun, identity, ownership, or hidden "
    "connection that appears to come only from the player's speculative framing. "
    "If the reply can be made safe by adding uncertainty language or by keeping an NPC reaction non-authoritative, "
    "mark the original unsafe."
)

SESSION_PREFLIGHT_SYSTEM_PROMPT = (
    "You are a fast routing classifier for an AI Dungeon Master turn. "
    "Return only valid JSON. Be extremely conservative about skipping spoiler checks. "
    "Set skip_spoiler_check=true only when it is excruciatingly obvious the final visible reply, if any, "
    "will be silent, purely out-of-character operational help, purely mechanical/rules bookkeeping, "
    "or a clarification with no narrative facts, NPC claims, object ownership, location state, motives, "
    "relations, reveals, clues, or world-state assertions. For any narrative reply, uncertain case, "
    "NPC interaction, scene description, lore answer, object/person/location claim, or likely tool use, "
    "set skip_spoiler_check=false. Set main_call_thinking=false only for high-confidence simple turns "
    "that do not require hidden-state reasoning, rules ambiguity, tool planning, combat adjudication, "
    "or major consequences. Set main_call_thinking=true for uncertain, complex, tool-likely, combat, "
    "private-context, lore-heavy, or consequence-heavy turns. Set latest_player_intent_requires_mechanics=true "
    "when the latest player intent attempts violence, starts a fight, makes an attack, grapples, shoves, "
    "restrains, flees pursuit, performs a contested action, risks injury, or otherwise needs a D&D roll or "
    "initiative before success/failure is known."
)

CHARACTER_SHEET_SYSTEM_PROMPT = (
    "You answer focused questions about D&D character sheets for the session DM. "
    "Use only the provided character-sheet data. Return only valid JSON with keys: "
    "answer, character_ids, missing. Keep answer concise and include only the facts needed "
    "to answer the question; do not dump the full sheet, infer unstated facts, or add advice. "
    "If the sheet data does not contain the answer, set missing to true and say exactly what "
    "is unavailable."
)

JSON_REPAIR_SYSTEM_PROMPT = (
    "You repair malformed JSON. Return only valid JSON with the same intended data and structure as "
    "the original. Make the smallest changes needed to fix syntax errors. Do not add commentary, "
    "markdown fences, or new facts."
)

SESSION_FORMAT_REPAIR_SYSTEM_PROMPT = (
    "You repair malformed visible Dungeon Master replies. Return only the repaired visible reply text. "
    "Preserve the same story meaning, ordering, and wording as closely as possible. Make only the smallest "
    "changes needed to satisfy the reported formatting rules. Do not add explanations, markdown fences, new "
    "facts, new actions, or extra dialogue. Keep narration outside NPC tags. The only allowed angle-bracket "
    "tag is <npc target=\"NPC name\">...</npc>. Write visible replies in English only."
)

SESSION_GUARD_REPAIR_SYSTEM_PROMPT = (
    "You repair unsafe visible Dungeon Master replies. Return only the repaired visible reply text. "
    "Preserve the same scene facts, ordering, and story meaning as closely as possible. Make only the smallest "
    "changes needed to satisfy the reported guard violation. Do not add explanations, markdown fences, tool-call "
    "markup, new facts, new actions, or extra dialogue. Keep narration outside NPC tags. The only allowed "
    "angle-bracket tag is <npc target=\"NPC name\">...</npc>. Write visible replies in English only."
)

LOOT_GENERATION_SYSTEM_PROMPT = (
    "You generate thematic D&D loot boxes for a party of adventurers. "
    "Each character gets their own personal pool of items tailored to their class, level, and identity. "
    "Items should be fun, thematic, and feel earned. Include a mix of consumables, gear, trinkets, "
    "and situational items. Return only valid JSON with no commentary or markdown fences."
)

SHOP_MENU_GENERATION_SYSTEM_PROMPT = (
    "You generate one D&D shop inventory from a merchant summary and current scene context. "
    "Return only valid JSON. Keep prices plausible for standard 5e gold-piece economy unless campaign "
    "context clearly says otherwise. Include mundane goods, useful adventuring supplies, and a few flavorful "
    "local items that fit the merchant. Do not include secret spoilers or player-facing narration."
)


def _env_model_for_provider(provider):
    return OPENCODE_GO_MODEL if provider == 'opencode_go' else OPENROUTER_MODEL


def _api_key_for_provider(provider):
    return OPENCODE_GO_API_KEY if provider == 'opencode_go' else OPENROUTER_API_KEY


def _api_url_for_provider(provider):
    return OPENCODE_GO_BASE_URL if provider == 'opencode_go' else OPENROUTER_BASE_URL


def get_llm_provider():
    if LLM_PROVIDER not in SUPPORTED_LLM_PROVIDERS:
        supported = ', '.join(sorted(SUPPORTED_LLM_PROVIDERS))
        raise RuntimeError(f'LLM_PROVIDER must be one of: {supported}')
    return LLM_PROVIDER


def get_llm_model():
    provider = get_llm_provider()
    with _LLM_MODEL_LOCK:
        return _read_runtime_model_override() or _env_model_for_provider(provider)


def get_llm_settings():
    provider = get_llm_provider()
    with _LLM_MODEL_LOCK:
        override = _read_runtime_model_override()
    env_model = _env_model_for_provider(provider)
    model = override or env_model
    return {
        'provider': provider,
        'model': model,
        'env_model': env_model,
        'source': 'runtime' if override else 'env',
        'is_overridden': bool(override),
        'api_key_configured': bool(_api_key_for_provider(provider)),
        'thinking_enabled': _deepseek_thinking_enabled(provider, model),
        'reasoning_effort': OPENCODE_GO_REASONING_EFFORT if provider == 'opencode_go' else None,
    }


def set_llm_model(model):
    next_model = (model or '').strip()
    if not next_model:
        raise ValueError('Model is required')
    if len(next_model) > 200:
        raise ValueError('Model must be 200 characters or fewer')
    with _LLM_MODEL_LOCK:
        _write_runtime_model_override(next_model)
    return get_llm_settings()


def reset_llm_model():
    with _LLM_MODEL_LOCK:
        LLM_RUNTIME_MODEL_FILE.unlink(missing_ok=True)
    return get_llm_settings()


def get_openrouter_model():
    return get_llm_model()


def get_openrouter_settings():
    return get_llm_settings()


def set_openrouter_model(model):
    return set_llm_model(model)


def reset_openrouter_model():
    return reset_llm_model()


def _read_runtime_model_override():
    try:
        return LLM_RUNTIME_MODEL_FILE.read_text(encoding='utf-8').strip() or None
    except FileNotFoundError:
        return None


def _write_runtime_model_override(model):
    LLM_RUNTIME_MODEL_FILE.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        'w',
        encoding='utf-8',
        dir=LLM_RUNTIME_MODEL_FILE.parent,
        delete=False,
    ) as temp_file:
        temp_file.write(model)
        temp_path = Path(temp_file.name)
    temp_path.replace(LLM_RUNTIME_MODEL_FILE)


def _require_llm_config(provider=None, model=None):
    provider = provider or get_llm_provider()
    env_prefix = 'OPENCODE_GO' if provider == 'opencode_go' else 'OPENROUTER'
    if not _api_key_for_provider(provider):
        raise RuntimeError(f'{env_prefix}_API_KEY is not set')
    if not (model if model is not None else get_llm_model()):
        raise RuntimeError(f'{env_prefix}_MODEL is not set')


def _estimate_tokens(value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return max(1, len(text) // 4) if text else 0


def _retry_delay_seconds(failed_attempt):
    return min(
        LLM_RETRY_BASE_DELAY_SECONDS * (2 ** max(failed_attempt - 1, 0)),
        LLM_RETRY_MAX_DELAY_SECONDS,
    )


def _is_retriable_llm_error(error):
    if isinstance(error, requests.HTTPError):
        response = getattr(error, 'response', None)
        status_code = getattr(response, 'status_code', None)
        return status_code in {404, 408, 409, 425, 429} or (status_code is not None and status_code >= 500)
    return isinstance(error, (requests.ConnectionError, requests.Timeout))


def _enabled(value):
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'on', 'enabled'}


def _deepseek_thinking_enabled(provider, model):
    return (
        provider == 'opencode_go'
        and str(model or '').strip().lower().startswith('deepseek-v4-')
        and _enabled(OPENCODE_GO_THINKING)
    )


def _provider_request_payload_options(provider, model, tools, tool_choice, parallel_tool_calls, allow_thinking=True):
    thinking_enabled = bool(allow_thinking) and _deepseek_thinking_enabled(provider, model)
    options = {
        'thinking_enabled': thinking_enabled,
        'tool_choice': tool_choice,
        'parallel_tool_calls': parallel_tool_calls,
    }
    if thinking_enabled:
        options['thinking'] = {'type': 'enabled'}
        effort = (OPENCODE_GO_REASONING_EFFORT or 'high').strip().lower()
        options['reasoning_effort'] = effort if effort in {'high', 'max'} else 'high'
        if tools:
            # DeepSeek thinking mode rejects tool_choice and does not document parallel_tool_calls.
            options['tool_choice'] = None
            options['parallel_tool_calls'] = None
    elif (
        tools
        and provider == 'opencode_go'
        and str(model or '').strip().lower().startswith('deepseek-v4-')
        and tool_choice == 'required'
    ):
        options['tool_choice'] = None
    return options


def _post_chat_response(
    messages,
    json_mode=False,
    audit_context=None,
    tools=None,
    tool_choice=None,
    parallel_tool_calls=None,
    allow_thinking=True,
    timeout_seconds=60,
    max_attempts=None,
    max_tokens=None,
    provider=None,
    model=None,
):
    audit_context = audit_context or {}
    campaign_id = audit_context.get('campaign_id')
    operation = audit_context.get('operation') or 'chat_completion'
    actor = audit_context.get('actor') or 'dm'
    trace_id = audit_context.get('trace_id') or f'{actor}:{operation}:{uuid4().hex[:10]}'
    parent_trace_id = audit_context.get('parent_trace_id')
    trace_label = audit_context.get('trace_label') or f'{actor}: {operation}'

    provider = provider or get_llm_provider()
    model = model or get_llm_model()
    try:
        _require_llm_config(provider, model)
    except Exception as err:
        if campaign_id:
            log_model_error(
                campaign_id,
                operation,
                actor,
                err,
                commit=True,
                trace_id=trace_id,
                parent_trace_id=parent_trace_id,
                trace_label=trace_label,
                provider=provider,
            )
        raise

    provider_options = _provider_request_payload_options(
        provider,
        model,
        tools,
        tool_choice,
        parallel_tool_calls,
        allow_thinking=allow_thinking,
    )
    payload = {
        'model': model,
        'messages': messages,
    }
    if max_tokens is not None:
        payload['max_tokens'] = max_tokens
    if tools:
        payload['tools'] = tools
    if provider_options.get('tool_choice') is not None:
        payload['tool_choice'] = provider_options['tool_choice']
    if provider_options.get('parallel_tool_calls') is not None:
        payload['parallel_tool_calls'] = provider_options['parallel_tool_calls']
    if provider_options.get('thinking_enabled'):
        payload['thinking'] = provider_options['thinking']
        payload['reasoning_effort'] = provider_options['reasoning_effort']
    if json_mode:
        payload['response_format'] = {'type': 'json_object'}

    if campaign_id:
        log_model_request(
            campaign_id,
            operation,
            actor,
            messages,
            model,
            json_mode=json_mode,
            commit=True,
            trace_id=trace_id,
            parent_trace_id=parent_trace_id,
            trace_label=trace_label,
            provider=provider,
            tools=tools,
            tool_choice=provider_options.get('tool_choice'),
            parallel_tool_calls=provider_options.get('parallel_tool_calls'),
            context_manifest=audit_context.get('context_manifest'),
            token_estimate=audit_context.get('token_estimate') or {
                'estimated_message_tokens': _estimate_tokens(messages),
                'estimated_tool_schema_tokens': _estimate_tokens(tools or []),
                'full_world_graph_included': audit_context.get('full_world_graph_included', None),
            },
            reasoning_requested_by_app=provider_options.get('thinking_enabled', False),
            reasoning_note=(
                'DeepSeek V4 thinking mode is enabled by app configuration.'
                if provider_options.get('thinking_enabled')
                else None
            ),
        )

    attempt_limit = max(1, int(max_attempts or LLM_MAX_ATTEMPTS))
    for attempt in range(1, attempt_limit + 1):
        if attempt > 1:
            tracker = audit_context.get('telemetry_tracker') if isinstance(audit_context, dict) else None
            if isinstance(tracker, dict):
                tracker['provider_retries'] = tracker.get('provider_retries', 0) + 1
        try:
            resp = requests.post(
                _api_url_for_provider(provider),
                headers={
                    'Authorization': f'Bearer {_api_key_for_provider(provider)}',
                    'Content-Type': 'application/json',
                },
                json=payload,
                timeout=timeout_seconds,
            )
            resp.raise_for_status()
            data = resp.json()
            if campaign_id:
                log_model_response(
                    campaign_id,
                    operation,
                    actor,
                    data,
                    commit=True,
                    trace_id=trace_id,
                    parent_trace_id=parent_trace_id,
                    trace_label=trace_label,
                    provider=provider,
                )
            return data
        except Exception as err:
            can_retry = attempt < attempt_limit and _is_retriable_llm_error(err)
            if can_retry:
                delay_seconds = _retry_delay_seconds(attempt)
                if campaign_id:
                    log_audit_event(
                        campaign_id,
                        'model_retry',
                        f'{actor} retrying: {operation}',
                        {
                            'operation': operation,
                            'attempt': attempt,
                            'next_attempt': attempt + 1,
                            'max_attempts': attempt_limit,
                            'delay_seconds': delay_seconds,
                            'error': repr(err),
                        },
                        source=provider,
                        actor=actor,
                        trace_id=trace_id,
                        parent_trace_id=parent_trace_id,
                        trace_label=trace_label,
                        audit_role='tools',
                        commit=True,
                    )
                time.sleep(delay_seconds)
                continue

            if campaign_id:
                log_model_error(
                    campaign_id,
                    operation,
                    actor,
                    err,
                    commit=True,
                    trace_id=trace_id,
                    parent_trace_id=parent_trace_id,
                    trace_label=trace_label,
                    provider=provider,
                )
            raise


def _post_chat(
    messages,
    json_mode=False,
    audit_context=None,
    allow_thinking=True,
    timeout_seconds=60,
    max_attempts=None,
    max_tokens=None,
):
    data = _post_chat_response(
        messages,
        json_mode=json_mode,
        audit_context=audit_context,
        allow_thinking=allow_thinking,
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
        max_tokens=max_tokens,
    )
    return data['choices'][0]['message']['content']


def _post_chat_stream(
    messages,
    json_mode=False,
    audit_context=None,
    allow_thinking=False,
    timeout_seconds=90,
    on_token=None,
):
    """Stream an LLM chat completion, calling on_token(delta_text) for each content chunk.

    Returns the fully accumulated content string when the stream finishes.
    """
    audit_context = audit_context or {}
    campaign_id = audit_context.get('campaign_id')
    operation = audit_context.get('operation') or 'chat_completion_stream'
    actor = audit_context.get('actor') or 'dm'
    trace_id = audit_context.get('trace_id') or f'{actor}:{operation}:{uuid4().hex[:10]}'
    parent_trace_id = audit_context.get('parent_trace_id')
    trace_label = audit_context.get('trace_label') or f'{actor}: {operation}'

    provider = get_llm_provider()
    model = get_llm_model()
    _require_llm_config(provider, model)

    provider_options = _provider_request_payload_options(
        provider, model, None, None, None, allow_thinking=allow_thinking,
    )
    payload = {
        'model': model,
        'messages': messages,
        'stream': True,
    }
    if provider_options.get('thinking_enabled'):
        payload['thinking'] = provider_options['thinking']
        payload['reasoning_effort'] = provider_options['reasoning_effort']
    if json_mode:
        payload['response_format'] = {'type': 'json_object'}

    if campaign_id:
        log_model_request(
            campaign_id, operation, actor, messages, model,
            json_mode=json_mode, commit=True,
            trace_id=trace_id, parent_trace_id=parent_trace_id,
            trace_label=trace_label, provider=provider,
        )

    resp = requests.post(
        _api_url_for_provider(provider),
        headers={
            'Authorization': f'Bearer {_api_key_for_provider(provider)}',
            'Content-Type': 'application/json',
        },
        json=payload,
        timeout=timeout_seconds,
        stream=True,
    )
    resp.raise_for_status()

    accumulated = []
    for line in resp.iter_lines(decode_unicode=True):
        if not line:
            continue
        if line.startswith('data: '):
            data_str = line[6:]
            if data_str.strip() == '[DONE]':
                break
            try:
                chunk = json.loads(data_str)
                delta = (chunk.get('choices') or [{}])[0].get('delta') or {}
                content = delta.get('content') or ''
                if content:
                    accumulated.append(content)
                    if on_token:
                        on_token(content)
            except (json.JSONDecodeError, IndexError, KeyError):
                continue

    full_text = ''.join(accumulated)

    if campaign_id:
        log_model_response(
            campaign_id, operation, actor,
            {'choices': [{'message': {'content': full_text}}]},
            commit=True, trace_id=trace_id,
            parent_trace_id=parent_trace_id,
            trace_label=trace_label, provider=provider,
        )

    return full_text


def _opening_scene_text_from_response(data):
    message, _finish_reason = _choice_message(data)
    content = message.get('content') or ''
    if str(content).strip():
        return str(content).strip()

    return None


def _opening_scene_format_retry_messages(messages):
    return [
        *messages,
        {
            'role': 'user',
            'content': (
                'Your previous response had no visible assistant content. Return the opening scene again, '
                'but put the full player-visible DM message in the assistant content field only. Do not put '
                'the scene in reasoning, thinking, hidden notes, JSON, or a markdown code fence. End with a '
                'clear prompt for player action.'
            ),
        },
    ]


def _json_candidate(text):
    if not isinstance(text, str):
        return text
    start = text.find('{')
    end = text.rfind('}')
    if start >= 0 and end > start:
        return text[start:end + 1]
    return text


def _json_loads_with_error(text):
    if not text:
        return {}, None, text
    try:
        return json.loads(text), None, text
    except (TypeError, ValueError) as raw_error:
        candidate = _json_candidate(text)
        if candidate != text:
            try:
                return json.loads(candidate), None, candidate
            except (TypeError, ValueError) as candidate_error:
                return {}, candidate_error, candidate
        return {}, raw_error, candidate


def _json_loads_or_empty(text):
    data, _error, _candidate = _json_loads_with_error(text)
    return data


def _json_error_excerpt(candidate, error, context_lines=2):
    if not isinstance(candidate, str) or not isinstance(error, json.JSONDecodeError):
        return ''

    lines = candidate.splitlines()
    if not lines:
        return ''

    line_index = max(error.lineno - 1, 0)
    start = max(line_index - context_lines, 0)
    end = min(line_index + context_lines + 1, len(lines))
    excerpt_lines = []
    for index in range(start, end):
        prefix = '>>' if index == line_index else '  '
        excerpt_lines.append(f'{prefix} {index + 1}: {lines[index]}')
        if index == line_index:
            excerpt_lines.append(f'   {" " * (len(str(index + 1)) + 2 + max(error.colno - 1, 0))}^')
    return '\n'.join(excerpt_lines)


def _build_json_repair_messages(candidate, error):
    excerpt = _json_error_excerpt(candidate, error)
    return [
        {
            'role': 'system',
            'content': JSON_REPAIR_SYSTEM_PROMPT,
        },
        {
            'role': 'user',
            'content': (
                'Repair this malformed JSON and return the complete corrected JSON document.\n\n'
                f'Parser error: {error.msg} at line {error.lineno}, column {error.colno} '
                f'(character {error.pos}).\n\n'
                f'Problem area:\n{excerpt}\n\n'
                f'Original malformed JSON:\n{candidate}'
            ),
        },
    ]


def _build_session_format_repair_messages(candidate, format_violation, hot_context=None):
    return _build_session_guard_repair_messages(
        candidate,
        'format',
        format_violation,
        hot_context=hot_context,
    )


def _visible_naming_target(name, hot_context):
    normalized = str(name or '').strip().casefold()
    constraints = (hot_context or {}).get('visible_naming_constraints') or []
    for constraint in constraints:
        avoid_name = str((constraint or {}).get('avoid_visible_name') or '').strip()
        public_reference = str((constraint or {}).get('use_public_reference') or '').strip()
        if avoid_name and public_reference and avoid_name.casefold() == normalized:
            return public_reference
    return str(name or '').strip()


def _latest_player_messages_by_character(hot_context):
    latest_player_messages = []
    for character in (hot_context or {}).get('protected_player_characters') or []:
        latest_player_messages.append({
            'character_name': str(character.get('name') or '').strip(),
            'user_id': character.get('user_id'),
            'latest_player_message': _latest_player_message_for_character(hot_context, character),
        })
    return latest_player_messages


def _leaked_private_items_for_repair(details, hot_context):
    leaked_ids = {
        str(item or '').strip()
        for item in ((details or {}).get('leaked_item_ids') or [])
        if str(item or '').strip()
    }
    if not leaked_ids:
        return []
    return [
        {
            'id': str(item.get('id') or '').strip(),
            'kind': str(item.get('kind') or '').strip(),
            'text': str(item.get('text') or '').strip(),
        }
        for item in ((hot_context or {}).get('private_spoiler_items') or [])
        if isinstance(item, dict) and str(item.get('id') or '').strip() in leaked_ids
    ]


def _session_dm_guard_repair_payload(candidate, guard_name, details, hot_context=None):
    payload = {
        'candidate_visible_dm_reply': candidate,
        'visible_naming_constraints': (hot_context or {}).get('visible_naming_constraints') or [],
        'repair_requirements': [],
    }
    if guard_name == 'format':
        payload['format_violation'] = details or {}
        payload['repair_requirements'] = [
            'Return only the repaired visible reply text.',
            'Preserve the existing wording and narrative meaning unless a change is required to fix formatting.',
            'If clearly attributed NPC speech appears, wrap only the spoken line in <npc target="NPC name">...</npc>.',
            'Leave narration outside any <npc> tag.',
            'If a naming constraint applies, use use_public_reference as the NPC target instead of avoid_visible_name.',
            'Do not use any angle-bracket tag other than <npc target="NPC name">...</npc>.',
        ]
        return payload
    if guard_name == 'pc_control':
        payload['pc_control_violation'] = details or {}
        payload['protected_player_characters'] = (hot_context or {}).get('protected_player_characters') or []
        payload['latest_player_messages_by_character'] = _latest_player_messages_by_character(hot_context)
        payload['repair_requirements'] = [
            'Return only the repaired visible reply text.',
            'Preserve the scene facts, NPC actions, and DM intent unless a change is required to stop controlling a protected player character.',
            'Do not invent dialogue, choices, intent, interior state, or consequential actions for any protected player character.',
            'Convert protected player character dialogue or decisions into non-controlling DM narration, a direct question, or a brief player handoff.',
            'Keep references to protected player characters brief and non-controlling.',
            'Do not use any angle-bracket tag other than <npc target="NPC name">...</npc>.',
        ]
        return payload
    if guard_name == 'canon_discipline':
        payload['canon_discipline_violation'] = details or {}
        payload['latest_player_message'] = _latest_player_message(hot_context)
        payload['established_public_facts'] = (hot_context or {}).get('established_public_facts') or []
        payload['recent_public_world_events'] = (hot_context or {}).get('recent_public_world_events') or []
        payload['open_public_threads'] = (hot_context or {}).get('open_public_threads') or []
        payload['repair_requirements'] = [
            'Return only the repaired visible reply text.',
            'Preserve scene momentum, atmosphere, and any safe established facts unless a change is required to restore evidence discipline.',
            'Do not turn player-supplied specifics into confirmed truth unless the confirmation is grounded in visible evidence already present in the payload.',
            'If a player claim is not corroborated, keep it conditional, skeptical, or merely reactive from the NPC point of view.',
            'Do not replace or sharply reframe an established public lead unless the visible evidence already supports that update.',
            'Prefer uncertainty language over authoritative confirmation when the evidence is incomplete.',
            'Do not use any angle-bracket tag other than <npc target="NPC name">...</npc>.',
        ]
        return payload
    if guard_name == 'spoiler_checker':
        payload['spoiler_violation'] = details or {}
        payload['latest_player_message'] = _latest_player_message(hot_context)
        payload['leaked_private_items'] = _leaked_private_items_for_repair(details, hot_context)
        payload['repair_requirements'] = [
            'Return only the repaired visible reply text.',
            'Preserve the scene intent and any safe public facts unless a change is required to remove the spoiler leak.',
            'Keep only what players could currently observe, hear, or reasonably infer in-world.',
            'Replace hidden motives, hidden leverage, secret causes, private plans, and hidden-tracker language with safe surface-level clues, visible pressure, practical conditions, or evasive reluctance.',
            'Do not reveal or strongly imply unrevealed private items, hidden countdowns, or DM-private causality.',
            'If a witness can safely offer a clue, keep it limited, public-facing, and incomplete rather than explaining the hidden truth.',
            'Do not use any angle-bracket tag other than <npc target="NPC name">...</npc>.',
        ]
        return payload
    raise ValueError(f'Unsupported session DM guard repair type: {guard_name}')


def _session_dm_guard_repair_system_prompt(guard_name):
    if guard_name == 'format':
        return SESSION_FORMAT_REPAIR_SYSTEM_PROMPT
    return SESSION_GUARD_REPAIR_SYSTEM_PROMPT


def _build_session_guard_repair_messages(candidate, guard_name, details, hot_context=None):
    return [
        {
            'role': 'system',
            'content': _session_dm_guard_repair_system_prompt(guard_name),
        },
        {
            'role': 'user',
            'content': json.dumps(
                _session_dm_guard_repair_payload(candidate, guard_name, details, hot_context),
                ensure_ascii=False,
            ),
        },
    ]


def _apply_visible_naming_constraints(text, hot_context):
    repaired = str(text or '')
    constraints = (hot_context or {}).get('visible_naming_constraints') or []
    ordered_constraints = sorted(
        [constraint for constraint in constraints if isinstance(constraint, dict)],
        key=lambda constraint: len(str(constraint.get('avoid_visible_name') or '')),
        reverse=True,
    )
    for constraint in ordered_constraints:
        avoid_name = str(constraint.get('avoid_visible_name') or '').strip()
        public_reference = str(constraint.get('use_public_reference') or '').strip()
        if not avoid_name or not public_reference:
            continue
        repaired = re.sub(
            rf'(?<!\w){re.escape(avoid_name)}(?!\w)',
            public_reference,
            repaired,
            flags=re.IGNORECASE,
        )
    return repaired


def _extract_missing_npc_tag_speaker(format_violation):
    errors = (format_violation or {}).get('errors') or []
    for error in errors:
        if str((error or {}).get('kind') or '').strip().lower() != 'missing_npc_tag':
            continue
        detail = str((error or {}).get('detail') or '')
        match = re.search(r'<npc\s+target="([^"]+)">', detail)
        if match:
            return str(match.group(1) or '').strip()
    return ''


def _wrap_quoted_dialogue_with_npc_tag(text, speaker):
    target = str(speaker or '').strip()
    if not target:
        return str(text or '')

    segments = re.split(r'(<npc\b[^>]*>.*?</npc>)', str(text or ''), flags=re.IGNORECASE | re.DOTALL)
    quote_pattern = re.compile(r'((?:\*\*)?\s*["\u201c][^"\u201c\u201d\n]{2,400}["\u201d](?:\s*\*\*)?)')
    rebuilt = []
    def _wrap_match(match):
        raw_quote = match.group(1)
        stripped_quote = raw_quote.strip()
        if not stripped_quote:
            return raw_quote
        leading_ws = raw_quote[:len(raw_quote) - len(raw_quote.lstrip())]
        trailing_ws = raw_quote[len(raw_quote.rstrip()):]
        return f'{leading_ws}<npc target="{target}">{stripped_quote}</npc>{trailing_ws}'

    for segment in segments:
        if not segment:
            continue
        if re.match(r'<npc\b[^>]*>.*?</npc>$', segment, flags=re.IGNORECASE | re.DOTALL):
            rebuilt.append(segment)
            continue
        rebuilt.append(quote_pattern.sub(_wrap_match, segment))
    return ''.join(rebuilt)


def _local_missing_npc_tag_repair(candidate, format_violation, hot_context=None):
    speaker = _extract_missing_npc_tag_speaker(format_violation)
    if not speaker:
        return ''

    target = _visible_naming_target(speaker, hot_context)
    repaired = _apply_visible_naming_constraints(candidate, hot_context)
    wrapped = _wrap_quoted_dialogue_with_npc_tag(repaired, target)
    if wrapped == str(candidate or ''):
        return ''
    return wrapped.strip()


def _json_loads_with_repair(text, audit_context=None):
    data, error, candidate = _json_loads_with_error(text)
    if error is None or not isinstance(error, json.JSONDecodeError):
        return data

    audit_context = audit_context or {}
    tracker = audit_context.get('telemetry_tracker')
    if isinstance(tracker, dict):
        tracker['parse_repairs'] = tracker.get('parse_repairs', 0) + 1

    operation = audit_context.get('operation') or 'json_response'
    repair_context = {
        **audit_context,
        'operation': f'{operation}_json_repair',
    }
    repaired_text = _post_chat(
        _build_json_repair_messages(candidate, error),
        json_mode=True,
        audit_context=repair_context,
    )
    repaired_data, _repair_error, _repaired_candidate = _json_loads_with_error(repaired_text)
    return repaired_data


def _planning_blank_retry_messages(messages):
    return [
        *messages,
        {
            'role': 'user',
            'content': (
                'Your previous response was blank or invalid. Return only valid JSON now with a non-empty '
                'player-visible message string, an active_page value from identity, scores, combat, '
                'magic_gear, story, or null, and a form_patch object. Do not return whitespace, an empty '
                'JSON object, markdown fences, hidden reasoning, or commentary outside the JSON object.'
            ),
        },
    ]


def _session_memory_retry_messages(messages, failure_kind):
    issue = (
        'blank or invalid'
        if failure_kind == 'blank_response'
        else 'an empty or no-op JSON object'
    )
    minimal_shape = {
        'running_summary': 'non-empty compact summary of the latest visible state',
        'scene_patch': {},
        'scene_reason': 'brief reason or null',
        'upsert_graph_entities': [],
        'upsert_graph_relations': [],
        'upsert_graph_facts': [],
        'create_clocks': [],
        'retire_clocks': [],
        'update_npc_actors': [],
        'record_events': [],
    }
    return [
        *messages,
        {
            'role': 'user',
            'content': (
                f'Your previous response was {issue}. Return exactly one valid JSON object now, matching the '
                'original return shape. If there are no durable graph or clock updates, you must still return a '
                'non-empty running_summary and any current scene_patch you can confirm, while leaving unchanged '
                'sections as empty arrays. Do not return whitespace, null, an empty object, markdown fences, or '
                f'commentary outside the JSON object.\n\nMinimal valid shape:\n{json.dumps(minimal_shape, ensure_ascii=False)}'
            ),
        },
    ]


def _planning_result_from_text(text, audit_context=None):
    if not isinstance(text, str) or not text.strip():
        return None

    data = _json_loads_with_repair(text, audit_context=audit_context)
    if isinstance(data, dict):
        message = data.get('message')
        if isinstance(message, str) and message.strip():
            return {
                'message': message.strip(),
                'active_page': data.get('active_page'),
                'form_patch': data.get('form_patch') if isinstance(data.get('form_patch'), dict) else {},
            }

    if not text.lstrip().startswith('{'):
        return {'message': text.strip(), 'active_page': None, 'form_patch': {}}

    return None


def normalize_session_dm_turn_decision(raw_decision):
    if isinstance(raw_decision, dict):
        data = raw_decision
    elif isinstance(raw_decision, str):
        data = _json_loads_or_empty(raw_decision)
        if not isinstance(data, dict) or not data:
            return {'mode': 'speak', 'content': raw_decision.strip()}
    else:
        return {'mode': 'speak', 'content': ''}

    mode = str(data.get('mode') or data.get('action') or '').strip().lower()
    if mode in {'silent', 'no_response', 'no_dm_response', 'none'}:
        return {
            'mode': 'silent',
            'content': '',
            'reason': str(data.get('reason') or 'The DM intentionally stayed silent.').strip(),
        }

    content = data.get('content')
    if content is None:
        content = data.get('message')
    if content is None:
        content = data.get('visible_message')
    return {
        'mode': 'speak',
        'content': str(content or '').strip(),
        'commit_action_ids': data.get('commit_action_ids') if isinstance(data.get('commit_action_ids'), list) else None,
    }


SESSION_DM_FINALIZER_TOOL_NAMES = {'talk_to_player', 'stay_silent'}
SESSION_DM_FINALIZER_TOOLS = [
    {
        'type': 'function',
        'function': {
            'name': 'talk_to_player',
            'description': 'Finalize the DM turn by sending one player-visible reply. Put only player-facing visible content in content and explicitly select which pending narrative actions should commit.',
            'parameters': {
                'type': 'object',
                'required': ['content', 'commit_action_ids'],
                'properties': {
                    'content': {
                        'type': 'string',
                        'description': 'Visible DM reply for the player, using only valid visible-message syntax.',
                    },
                    'commit_action_ids': {
                        'type': 'array',
                        'items': {'type': 'string'},
                        'description': 'Pending action IDs to commit with this reply. Use [] when no pending action should commit.',
                    },
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'stay_silent',
            'description': 'Finalize the DM turn with no visible reply when the DM should intentionally remain silent.',
            'parameters': {
                'type': 'object',
                'required': ['reason'],
                'properties': {
                    'reason': {
                        'type': 'string',
                        'description': 'Short internal reason for the silence decision.',
                    },
                },
            },
        },
    },
]


def _session_dm_tools_with_finalizers(tools):
    return [*(tools or []), *SESSION_DM_FINALIZER_TOOLS]


def _session_dm_finalizer_decision_from_tool_calls(tool_calls):
    finalizer_calls = []
    for tool_call in tool_calls or []:
        function = tool_call.get('function') or {}
        tool_name = function.get('name')
        if tool_name in SESSION_DM_FINALIZER_TOOL_NAMES:
            finalizer_calls.append(tool_call)

    if not finalizer_calls:
        return None, None

    if len(finalizer_calls) != 1 or len(finalizer_calls) != len(tool_calls or []):
        return None, {
            'kind': 'invalid_finalizer_usage',
            'detail': 'Use exactly one finalization tool call and do not mix it with other tools.',
        }

    function = finalizer_calls[0].get('function') or {}
    tool_name = function.get('name')
    args = _parse_tool_arguments(function.get('arguments'))
    if tool_name == 'talk_to_player':
        if 'commit_action_ids' not in args or not isinstance(args.get('commit_action_ids'), list):
            return None, {
                'kind': 'missing_commit_action_ids',
                'detail': 'talk_to_player must include commit_action_ids, using [] when no pending action should commit.',
            }
        return {
            'mode': 'speak',
            'content': str(args.get('content') or '').strip(),
            'commit_action_ids': args.get('commit_action_ids'),
        }, None
    return {
        'mode': 'silent',
        'reason': str(args.get('reason') or 'The DM intentionally stayed silent.').strip(),
    }, None


def _looks_like_provider_tool_markup(raw_content):
    if not isinstance(raw_content, str):
        return False
    text = raw_content.strip()
    if not text:
        return False
    return (
        '<｜｜DSML｜｜tool_calls>' in text
        or '<｜｜DSML｜｜invoke ' in text
        or '</｜｜DSML｜｜tool_calls>' in text
    )


SESSION_DM_COMBAT_TOOL_NAMES = {
    'move_encounter_actor',
    'get_encounter_overview',
    'get_combatant_state',
    'list_reachable_positions',
    'search_campaign_memory',
    'toggle_encounter_mode',
    'next_combat_turn',
    'set_combat_turn',
    'update_combatant_actions',
    'set_combatant_hp',
    'apply_damage',
    'apply_healing',
    'grant_temp_hp',
    'set_combatant_initiative',
    'update_combatant_conditions',
    'remove_encounter_actor',
}
SESSION_DM_COMBAT_MUTATION_TOOL_NAMES = {
    'move_encounter_actor',
    'toggle_encounter_mode',
    'next_combat_turn',
    'set_combat_turn',
    'update_combatant_actions',
    'set_combatant_hp',
    'apply_damage',
    'apply_healing',
    'grant_temp_hp',
    'set_combatant_initiative',
    'update_combatant_conditions',
    'remove_encounter_actor',
}
SESSION_DM_COMBAT_PROGRESS_TOOL_NAMES = (
    SESSION_DM_COMBAT_MUTATION_TOOL_NAMES - {'next_combat_turn', 'set_combat_turn', 'toggle_encounter_mode'}
) | {'roll_dice'}
SESSION_DM_COMBAT_ACTOR_SCOPED_TOOL_NAMES = {
    'move_encounter_actor',
    'get_combatant_state',
    'list_reachable_positions',
    'update_combatant_actions',
}
SESSION_DM_COMBAT_BLOCKED_TOOL_NAMES = {'set_combat_turn', 'search_campaign_memory'}
SESSION_DM_COMBAT_HANDOFF_PATTERNS = (
    re.compile(r'\bnow let me\b', flags=re.IGNORECASE),
    re.compile(r'\blet me advance\b', flags=re.IGNORECASE),
    re.compile(r'\bnext enemy\b', flags=re.IGNORECASE),
    re.compile(r'\bnext up\b', flags=re.IGNORECASE),
    re.compile(r'\bfinal enemy of the round\b', flags=re.IGNORECASE),
    re.compile(r'\bturn is finished\.\s*now\b', flags=re.IGNORECASE),
)


def _session_dm_combat_tracker(hot_context):
    encounter_map = hot_context.get('current_encounter_map') if isinstance(hot_context, dict) else {}
    encounter_map = encounter_map if isinstance(encounter_map, dict) else {}
    encounter_state = encounter_map.get('encounter_state') if isinstance(encounter_map, dict) else {}
    encounter_state = encounter_state if isinstance(encounter_state, dict) else {}
    turn_order = encounter_state.get('turn_order') if isinstance(encounter_state.get('turn_order'), list) else []
    active_turn_index = encounter_state.get('active_turn_index')
    active_combatant = None
    if isinstance(active_turn_index, int) and 0 <= active_turn_index < len(turn_order):
        item = turn_order[active_turn_index]
        active_combatant = item if isinstance(item, dict) else None
    active_actions = active_combatant.get('actions') if isinstance(active_combatant, dict) else {}
    active_actions = active_actions if isinstance(active_actions, dict) else {}

    return {
        'active': bool(encounter_state.get('active')) and bool(active_combatant),
        'campaign_id': (hot_context.get('campaign') or {}).get('id') if isinstance(hot_context, dict) else None,
        'encounter_map_id': encounter_map.get('id'),
        'encounter_state_active': bool(encounter_state.get('active')),
        'expected_active_turn_index': active_turn_index,
        'expected_active_placement_id': active_combatant.get('placement_id') if active_combatant else None,
        'expected_active_actor_type': active_combatant.get('actor_type') if active_combatant else None,
        'expected_active_actor_id': (
            str(active_combatant.get('actor_id'))
            if active_combatant and active_combatant.get('actor_id') is not None
            else None
        ),
        'expected_active_label': active_combatant.get('label') if active_combatant else None,
        'expected_active_current_hp': active_combatant.get('current_hp') if active_combatant else None,
        'expected_active_actions': json.loads(json.dumps(active_actions)),
        'turn_progress_made': False,
        'mutated': False,
        'snapshot': None,
    }


def _session_dm_combat_target_matches_expected(args, combat_tracker):
    expected_placement_id = combat_tracker.get('expected_active_placement_id')
    expected_actor_type = combat_tracker.get('expected_active_actor_type')
    expected_actor_id = combat_tracker.get('expected_active_actor_id')

    placement_id = args.get('placement_id')
    if placement_id is not None:
        try:
            return int(placement_id) == int(expected_placement_id)
        except (TypeError, ValueError):
            return False

    actor_type = args.get('actor_type')
    actor_id = args.get('actor_id')
    if actor_type is None or actor_id is None:
        return False
    return (
        str(actor_type).strip().lower() == str(expected_actor_type or '').strip().lower()
        and str(actor_id) == str(expected_actor_id)
    )


def _session_dm_refresh_combat_tracker_from_state(combat_tracker, encounter_state):
    if not isinstance(encounter_state, dict):
        return

    old_placement_id = combat_tracker.get('expected_active_placement_id')
    turn_order = encounter_state.get('turn_order') if isinstance(encounter_state.get('turn_order'), list) else []
    active_turn_index = encounter_state.get('active_turn_index')
    active_combatant = None
    if isinstance(active_turn_index, int) and 0 <= active_turn_index < len(turn_order):
        item = turn_order[active_turn_index]
        active_combatant = item if isinstance(item, dict) else None
    active_actions = active_combatant.get('actions') if isinstance(active_combatant, dict) else {}
    active_actions = active_actions if isinstance(active_actions, dict) else {}

    combat_tracker['encounter_state_active'] = bool(encounter_state.get('active'))
    combat_tracker['expected_active_turn_index'] = active_turn_index
    combat_tracker['expected_active_placement_id'] = active_combatant.get('placement_id') if active_combatant else None
    combat_tracker['expected_active_actor_type'] = active_combatant.get('actor_type') if active_combatant else None
    combat_tracker['expected_active_actor_id'] = (
        str(active_combatant.get('actor_id'))
        if active_combatant and active_combatant.get('actor_id') is not None
        else None
    )
    combat_tracker['expected_active_label'] = active_combatant.get('label') if active_combatant else None
    combat_tracker['expected_active_current_hp'] = active_combatant.get('current_hp') if active_combatant else None
    combat_tracker['expected_active_actions'] = json.loads(json.dumps(active_actions))

    if combat_tracker.get('expected_active_placement_id') != old_placement_id:
        combat_tracker['turn_progress_made'] = False


def _session_dm_result_encounter_state(result):
    if not isinstance(result, dict):
        return None
    if isinstance(result.get('encounter_state'), dict):
        return result.get('encounter_state')
    encounter_map = result.get('encounter_map')
    if isinstance(encounter_map, dict) and isinstance(encounter_map.get('encounter_state'), dict):
        return encounter_map.get('encounter_state')
    return None


def _session_dm_combatant_can_still_act(combat_tracker):
    current_hp = combat_tracker.get('expected_active_current_hp')
    try:
        if current_hp is not None and int(current_hp) <= 0:
            return False
    except (TypeError, ValueError):
        pass

    actions = combat_tracker.get('expected_active_actions')
    if not isinstance(actions, dict):
        return True

    if bool(actions.get('action', False)):
        return True
    if bool(actions.get('bonus_action', False)):
        return True
    try:
        if int(actions.get('movement_remaining', 0) or 0) > 0:
            return True
    except (TypeError, ValueError):
        return True
    return False


def _session_dm_combat_tool_violation(tool_name, args, combat_tracker):
    if not combat_tracker.get('active') or tool_name not in SESSION_DM_COMBAT_TOOL_NAMES:
        return None

    label = combat_tracker.get('expected_active_label') or 'the current combatant'
    actor_type = combat_tracker.get('expected_active_actor_type')
    if actor_type == 'player' and tool_name in SESSION_DM_COMBAT_MUTATION_TOOL_NAMES:
        return {
            'kind': 'player_turn_mutation',
            'detail': (
                f'It is currently {label}\'s turn, and player-character combat turns must not be '
                'mutated by the session DM loop.'
            ),
        }

    if tool_name in SESSION_DM_COMBAT_BLOCKED_TOOL_NAMES:
        if tool_name == 'search_campaign_memory':
            return {
                'kind': 'blocked_combat_tool',
                'detail': (
                    'Do not search campaign memory during active combat turns. '
                    'Use the live encounter state and combat tools to finish the current turn.'
                ),
            }
        return {
            'kind': 'blocked_combat_tool',
            'detail': 'set_combat_turn is reserved for explicit corrections and must not be used in session DM combat turns.',
        }

    if tool_name == 'next_combat_turn':
        if (
            not combat_tracker.get('turn_progress_made')
            and combat_tracker.get('expected_active_actor_type') != 'player'
            and _session_dm_combatant_can_still_act(combat_tracker)
        ):
            return {
                'kind': 'combat_turn_skip',
                'detail': (
                    f'Do not skip {label} without first resolving that combatant\'s turn. '
                    'Finish the current non-player turn before advancing.'
                ),
            }
        return None

    if tool_name in SESSION_DM_COMBAT_ACTOR_SCOPED_TOOL_NAMES and not _session_dm_combat_target_matches_expected(args, combat_tracker):
        return {
            'kind': 'wrong_active_combatant',
            'detail': f'This combat step may only act for {label}, the current active combatant.',
        }
    return None


def _session_dm_combat_batch_violation(decision, combat_tracker):
    if not combat_tracker.get('active') or not combat_tracker.get('mutated'):
        return None
    if decision.get('mode') != 'speak':
        return None
    if not combat_tracker.get('encounter_state_active'):
        return None
    if combat_tracker.get('expected_active_actor_type') == 'player':
        return None
    label = combat_tracker.get('expected_active_label') or 'the next non-player combatant'
    return {
        'kind': 'combat_batch_incomplete',
        'detail': (
            f'Combat resolution stopped while it was still {label}\'s turn. '
            'Continue resolving consecutive non-player turns until a player character is active or combat ends.'
        ),
    }


def _session_dm_combat_handoff_violation(response_text, combat_tracker):
    if not combat_tracker.get('active') or not combat_tracker.get('mutated'):
        return None
    if combat_tracker.get('expected_active_actor_type') != 'player':
        return None
    text = str(response_text or '').strip()
    if not text:
        return None
    for pattern in SESSION_DM_COMBAT_HANDOFF_PATTERNS:
        match = pattern.search(text)
        if match:
            return {
                'kind': 'combat_handoff',
                'detail': (
                    'After resolving combat, narrate only the completed actions and hand the turn back cleanly. '
                    'Do not announce that you are about to advance to another combatant.'
                ),
                'matched_phrase': match.group(0),
            }
    return None


def _session_dm_build_combat_snapshot(combat_tracker):
    if not combat_tracker.get('active') or not combat_tracker.get('encounter_map_id'):
        return None
    from models import EncounterMap, EncounterMapPlacement, db

    encounter_map = db.session.get(EncounterMap, combat_tracker.get('encounter_map_id'))
    if not encounter_map:
        return None
    placements = EncounterMapPlacement.query.filter_by(encounter_map_id=encounter_map.id).all()
    return {
        'campaign_id': combat_tracker.get('campaign_id'),
        'encounter_map_id': encounter_map.id,
        'encounter_state_json': encounter_map.encounter_state_json,
        'placements': [placement.to_dict() for placement in placements],
    }


def _session_dm_restore_combat_snapshot(snapshot):
    if not isinstance(snapshot, dict) or not snapshot.get('encounter_map_id'):
        return False
    from models import Campaign, EncounterMap, EncounterMapPlacement, db
    from services.dm_tools import _sync_combatant_storage

    encounter_map = db.session.get(EncounterMap, snapshot.get('encounter_map_id'))
    campaign = db.session.get(Campaign, snapshot.get('campaign_id')) if snapshot.get('campaign_id') else None
    if not encounter_map or not campaign:
        return False

    placements = EncounterMapPlacement.query.filter_by(encounter_map_id=encounter_map.id).all()
    placements_by_id = {placement.id: placement for placement in placements}
    for placement_snapshot in snapshot.get('placements') or []:
        placement = placements_by_id.get(placement_snapshot.get('id'))
        if not placement:
            continue
        placement.label = placement_snapshot.get('label') or placement.label
        try:
            placement.grid_col = int(placement_snapshot.get('col'))
            placement.grid_row = int(placement_snapshot.get('row'))
        except (TypeError, ValueError):
            continue

    encounter_state_json = snapshot.get('encounter_state_json')
    encounter_map.encounter_state_json = encounter_state_json
    encounter_state = _json_loads_or_empty(encounter_state_json) if encounter_state_json else {}
    turn_order = encounter_state.get('turn_order') if isinstance(encounter_state.get('turn_order'), list) else []
    for combatant in turn_order:
        if not isinstance(combatant, dict):
            continue
        placement = placements_by_id.get(combatant.get('placement_id'))
        if placement:
            _sync_combatant_storage(campaign, placement, json.loads(json.dumps(combatant)), sync_conditions=True)

    db.session.commit()
    return True


def _session_dm_finalizer_contract_violation(raw_content):
    if _looks_like_provider_tool_markup(raw_content):
        return {
            'kind': 'provider_tool_markup',
            'detail': 'Session DM final answer contained provider tool-call markup instead of a finalization tool call.',
        }

    return {
        'kind': 'missing_finalizer_tool_call',
        'detail': 'Session DM final answer must use exactly one finalization tool call: talk_to_player or stay_silent.',
    }


def _session_dm_guard_retry_system_prompt(guard_name, details):
    silent_ack = ' Acknowledge these instructions silently and follow them. Do not output an acknowledgement.'
    if guard_name == 'finalizer_contract':
        if isinstance(details, dict) and details.get('kind') == 'provider_tool_markup':
            return (
                'Guard reminder: finalize the turn by calling exactly one of talk_to_player or stay_silent. '
                'Do not output DSML, <｜｜DSML｜｜tool_calls>, invoke tags, XML, or any tool-call markup in visible content. '
                'Do not send the final reply as plain assistant text. If you need to narrate what the player learns, '
                'put only the player-facing visible result inside talk_to_player(content).'
                + silent_ack
            )
        return (
            'Guard reminder: finalize the turn by calling exactly one of talk_to_player or stay_silent. '
            'Do not send the final visible reply as plain assistant text. '
            'Do not output markdown fences, explanations, meta-commentary, or text outside the tool call. '
            'Use stay_silent only when no visible DM response is actually needed.'
            + silent_ack
        )
    if guard_name == 'format':
        return (
            'Guard reminder: use valid visible-message syntax. '
            'Finalize with talk_to_player or stay_silent. '
            'If you speak, talk_to_player content may use Markdown and '
            'plain text. The only allowed angle-bracket tag is <npc target="NPC name">...</npc>. '
            'Do not use <ic>, <ooc>, HTML, XML, invalid closing tags, or stray non-English glyphs. '
            'Write visible replies in English only.'
            + silent_ack
        )
    if guard_name == 'missing_npc_tag':
        return (
            'Guard reminder: if you include clearly attributed NPC speech or performed utterances, use the required '
            '<npc> wrapper. '
            'If you include clearly attributed current NPC spoken lines or performed utterances, wrap them in '
            '<npc target="NPC name">...</npc>, and leave narration outside the tag. '
            'The <npc target="..."> value must be a concrete in-world speaker reference grounded in the transcript '
            'or memory, not a placeholder or meta label. Never use targets like "the speaker", "speaker", "NPC", '
            '"someone", "voice", or vague pronouns. '
            'If a prior guard reminder identified an unrevealed private term, do not use that private term '
            'anywhere, including inside <npc target="...">. Use a public descriptor such as "old dockhand", '
            '"guard captain", or "hooded figure" as the target until the name is revealed through play. '
            'If you cannot identify a concrete speaker reference with high confidence, do not invent one and do not '
            'write quoted NPC dialogue; paraphrase the utterance in narration instead. '
            'Do not use any other angle-bracket tags. Finalize with talk_to_player or stay_silent.'
            + silent_ack
        )
    if guard_name == 'mechanical_resolution':
        return (
            'Guard reminder: do not resolve uncertain combat outcomes before the required D&D mechanics. '
            'Finalize with talk_to_player or stay_silent. If you speak, ask for the required roll or '
            'initiative instead of narrating hit/miss/damage/outcome.'
            + silent_ack
        )
    if guard_name == 'pc_control':
        return (
            'Guard reminder: do not control a protected player character. '
            'Do not invent dialogue, choices, inner state, or consequential behavior for any PC. '
            'Keep any mention of a protected PC brief and non-controlling. '
            'If no DM adjudication is needed (for example PC-to-PC exchange), use stay_silent("PC-to-PC exchange."). '
            'Otherwise use talk_to_player with only safe DM-visible content.'
            + silent_ack
        )
    if guard_name == 'canon_discipline':
        return (
            'Guard reminder: do not promote unsupported claims into objective truth. '
            'Treat player-supplied details, accusations, recollections, and theories as claims unless they are '
            'corroborated by the visible scene, a successful check, or established public facts. '
            'NPCs may react to a claim without validating it. '
            'Do not sharply replace or contradict an established public lead unless the visible evidence clearly earns that change. '
            'If certainty is incomplete, speak conditionally instead of authoritatively. '
            'Finalize with talk_to_player or stay_silent.'
            + silent_ack
        )
    if guard_name == 'private_output':
        terms = ', '.join(details.get('matched_terms') or []) if isinstance(details, dict) else ''
        return (
            'Guard reminder: do not expose DM-private information that has not become visible through '
            f'play. Do not mention these private terms in the visible reply: {terms or "(none listed)"}. '
            'This includes narration, quoted speech, labels, and <npc target="..."> attributes. '
            'Use public descriptors instead of unrevealed names when wrapping NPC speech. '
            'Do not route around this by having another NPC reveal or label the private term. '
            'If the latest player addressed a present NPC by public description, keep that NPC as the responder '
            'using the public descriptor; do not say that NPC vanished or left unless the transcript already established it. '
            'Finalize with talk_to_player or stay_silent using spoiler-safe visible content.'
            + silent_ack
        )
    if guard_name == 'spoiler_checker':
        leaked_ids = set(str(item or '') for item in (details or {}).get('leaked_item_ids') or [])
        if 'deterministic_witness_private_leverage' in leaked_ids:
            return (
                'Guard reminder: the witness may give the factual clue, but must not explain or hint at hidden leverage. '
                'Do not use these words or close metaphors for the witness: debt, debts, owe, owed, owing, favor, '
                'obligation, spiritual, blackmail, ledger, hook, hooked, old ties. '
                'Do not imply the witness has private obligations to the faction. '
                'Show fear using only generic visible pressure: watchers, reprisals, danger, being named, vanishing, '
                'or keeping their head down. Keep the latest addressed witness present unless the transcript says they left. '
                'Use a public descriptor in <npc target="...">. Finalize with talk_to_player or stay_silent.'
                + silent_ack
            )
        return (
            'Guard reminder: keep the visible reply spoiler-safe. '
            'Keep only what players could currently observe or reasonably know in-world. '
            'If a witness is afraid or asks for safety, do not reveal hidden leverage, private debts, '
            'secret motives, or the true reason for their fear. Give practical safety conditions, '
            'evasive reluctance, and a limited actionable clue instead. If the hidden leverage is a debt, '
            'favor, obligation, blackmail, or spiritual bond, do not hint at it with debt/favor/owed/ledger/hook '
            'metaphors; use generic danger, reprisals, watchers, or fear of being named. '
            'Finalize with talk_to_player or stay_silent using spoiler-safe visible content.'
            + silent_ack
        )
    if guard_name == 'combat_batch':
        return (
            'Combat turn reminder: if you are resolving combat for enemies or NPCs, continue through consecutive '
            'non-player turns until control returns to a player character or combat ends. '
            'Do not stop while a monster or NPC turn is still active. '
            'Use tools to finish the remaining non-player turns, then finalize with talk_to_player or stay_silent.'
            + silent_ack
        )
    if guard_name == 'combat_handoff':
        return (
            'Combat turn reminder: narrate only completed actions. '
            'Do not say "now let me advance", "next enemy", or any similar procedural handoff text in visible content. '
            'If the next active turn already belongs to a player character, end cleanly with the resolved combat narration '
            'or a concise "you are up" handoff. Finalize with talk_to_player or stay_silent.'
            + silent_ack
        )
    return (
        'Guard reminder: return exactly one valid JSON object '
        'matching the final contract.'
        + silent_ack
    )


SESSION_DM_GUARD_ONLY_CONTEXT_KEYS = {'private_output_terms', 'private_spoiler_items'}


def _session_dm_prompt_context(hot_context):
    return {
        key: value
        for key, value in (hot_context or {}).items()
        if key not in SESSION_DM_GUARD_ONLY_CONTEXT_KEYS
    }


def build_session_dm_tool_messages(hot_context):
    prompt_context = _session_dm_prompt_context(hot_context)
    messages = [
        {'role': 'system', 'content': SESSION_TOOL_PROMPT},
        {
            'role': 'system',
            'content': 'Compact hot context. Full campaign memory is available through tools, not preloaded:\n'
            + json.dumps(prompt_context, ensure_ascii=False),
        },
    ]
    retrieval_packet = (hot_context or {}).get('retrieval_packet')
    if isinstance(retrieval_packet, dict):
        messages.append({
            'role': 'system',
            'content': (
                'Focused retrieval evidence for this turn. Treat public and party_known items as grounded context. '
                'Items marked internal_only are DM-private constraints: use them to guide consequences and NPC behavior, '
                'but never reveal them unless play has earned the information. Player theories remain unconfirmed unless '
                'corroborated.\n' + json.dumps(retrieval_packet, ensure_ascii=False)
            ),
        })
    naming_constraints = prompt_context.get('visible_naming_constraints') or []
    if naming_constraints:
        messages.append({
            'role': 'system',
            'content': (
                'Visible naming constraints: obey these before drafting any visible reply. '
                'Do not use any avoid_visible_name in narration, quoted speech labels, or <npc target="..."> '
                'unless the latest player message already used that name. Use the listed public reference instead:\n'
                + json.dumps(naming_constraints, ensure_ascii=False)
            ),
        })
    return messages


SAFE_SPOILER_SKIP_PREFLIGHT_MODES = {'silent', 'ooc_only', 'mechanics_only', 'clarification_only'}
THINKING_OFF_PREFLIGHT_MODES = SAFE_SPOILER_SKIP_PREFLIGHT_MODES | {'simple_narrative'}


def _preflight_message_dict(message):
    if isinstance(message, dict):
        role = message.get('role')
        content = message.get('content')
    else:
        role = getattr(message, 'role', None)
        content = getattr(message, 'content', None)
    return {
        'role': role or 'user',
        'content': str(content or '')[:1000],
    }


def build_session_preflight_messages(hot_context, recent_messages, tools):
    prompt_context = _session_dm_prompt_context(hot_context)
    tool_names = [
        tool.get('function', {}).get('name')
        for tool in (tools or [])
        if isinstance(tool, dict) and tool.get('function')
    ]
    payload = {
        'latest_messages': [_preflight_message_dict(message) for message in recent_messages[-4:]],
        'current_character': prompt_context.get('current_player_character'),
        'has_active_combat': bool(prompt_context.get('combat_coordinates')),
        'has_current_encounter_map': bool(prompt_context.get('current_encounter_map')),
        'has_unrevealed_private_items': bool((hot_context or {}).get('private_spoiler_items')),
        'available_tool_names': [name for name in tool_names if name],
        'decision_policy': (
            'skip_spoiler_check may be true only for silent, ooc_only, mechanics_only, or '
            'clarification_only turns with high confidence. Set it false for narrative, unknown, '
            'tool-likely, NPC, lore, object ownership, relationship, location, clue, reveal, or world-state turns. '
            'main_call_thinking may be false only for high-confidence simple turns. Use true for any uncertainty, '
            'combat, complex rules, likely tool use, hidden/private context, lore-heavy reasoning, or major consequences. '
            'Set latest_player_intent_requires_mechanics true when the latest player is trying to attack, harm, '
            'grapple, shove, restrain, flee pursuit, start a fight, or attempt any risky/contested action whose '
            'outcome should not be narrated until a D&D roll or initiative step happens. '
            'For narrative or uncertain turns, provide four focused retrieval queries. For silent, ooc_only, '
            'mechanics_only, or clarification_only turns, use empty retrieval queries.'
        ),
        'return_shape': {
            'dm_reply_mode': 'silent | ooc_only | mechanics_only | clarification_only | simple_narrative | narrative | unknown',
            'skip_spoiler_check': False,
            'main_call_thinking': True,
            'latest_player_intent_requires_mechanics': False,
            'required_mechanic': 'short label such as attack roll, initiative, contested check, saving throw, or empty string',
            'confidence': 'high | medium | low',
            'reason': 'short explanation',
            'retrieval_queries': {
                'entities': 'short focused query for named people, places, objects, or factions',
                'scene_events': 'short focused query for scene state and recent events',
                'clocks_promises': 'short focused query for clocks, promises, and pressure',
                'prior_facts': 'short focused query for prior campaign facts',
            },
        },
    }
    return [
        {'role': 'system', 'content': SESSION_PREFLIGHT_SYSTEM_PROMPT},
        {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)},
    ]


def normalize_session_preflight_decision(raw_decision):
    data = raw_decision if isinstance(raw_decision, dict) else _json_loads_or_empty(raw_decision)
    if not isinstance(data, dict) or not data:
        return {
            'dm_reply_mode': 'unknown',
            'skip_spoiler_check': False,
            'main_call_thinking': True,
            'latest_player_intent_requires_mechanics': False,
            'required_mechanic': '',
            'confidence': 'low',
            'reason': 'Preflight returned no usable decision.',
        }

    mode = str(data.get('dm_reply_mode') or data.get('reply_mode') or 'unknown').strip().lower()
    if mode not in THINKING_OFF_PREFLIGHT_MODES | {'narrative', 'unknown'}:
        mode = 'unknown'
    confidence = str(data.get('confidence') or 'low').strip().lower()
    if confidence not in {'high', 'medium', 'low'}:
        confidence = 'low'
    skip_spoiler_check = (
        data.get('skip_spoiler_check') is True
        and mode in SAFE_SPOILER_SKIP_PREFLIGHT_MODES
        and confidence == 'high'
    )
    main_call_thinking = not (
        data.get('main_call_thinking') is False
        and mode in THINKING_OFF_PREFLIGHT_MODES
        and confidence == 'high'
    )

    normalized = {
        'dm_reply_mode': mode,
        'skip_spoiler_check': skip_spoiler_check,
        'main_call_thinking': main_call_thinking,
        'latest_player_intent_requires_mechanics': data.get('latest_player_intent_requires_mechanics') is True,
        'required_mechanic': str(data.get('required_mechanic') or '').strip()[:80],
        'confidence': confidence,
        'reason': str(data.get('reason') or '').strip(),
    }
    if isinstance(data.get('retrieval_queries'), dict):
        normalized['retrieval_queries'] = {
            key: str(data['retrieval_queries'].get(key) or '').strip()[:240]
            for key in ('entities', 'scene_events', 'clocks_promises', 'prior_facts')
        }
    return normalized


def get_session_preflight_decision(hot_context, recent_messages, tools, audit_context=None):
    base_audit = audit_context or {}
    preflight_audit = _child_audit_context(
        base_audit,
        'session_preflight',
        'session_preflight_router',
        'session_preflight_router: routing',
    )
    try:
        raw_decision = _post_chat(
            build_session_preflight_messages(hot_context, recent_messages, tools),
            json_mode=True,
            audit_context=preflight_audit,
            allow_thinking=False,
            timeout_seconds=SESSION_PREFLIGHT_TIMEOUT_SECONDS,
            max_attempts=1,
            max_tokens=SESSION_PREFLIGHT_MAX_TOKENS,
        )
        return normalize_session_preflight_decision(raw_decision)
    except Exception as err:
        campaign_id = base_audit.get('campaign_id')
        if campaign_id:
            log_audit_event(
                campaign_id,
                'session_preflight_error',
                'Session preflight failed closed.',
                {'error': repr(err)},
                source='session_dm.preflight',
                actor='session_preflight_router',
                trace_id=preflight_audit.get('trace_id'),
                parent_trace_id=preflight_audit.get('parent_trace_id'),
                trace_label=preflight_audit.get('trace_label'),
                audit_role='tools',
                commit=True,
            )
        return {
            'dm_reply_mode': 'unknown',
            'skip_spoiler_check': False,
            'main_call_thinking': True,
            'confidence': 'low',
            'reason': 'Preflight failed closed.',
        }


def _strip_npc_blocks(text):
    import re

    return re.sub(r'<npc\b[^>]*>.*?</npc>', '', text or '', flags=re.IGNORECASE | re.DOTALL)


def _latest_player_message(hot_context):
    recent_messages = (hot_context or {}).get('recent_messages') or []
    for message in reversed(recent_messages):
        if message.get('role') == 'player':
            return str(message.get('content') or '')
    return ''


def _sentence_containing_span(text, start, end):
    text = str(text or '')
    if not text:
        return ''
    left = max(text.rfind('.', 0, start), text.rfind('!', 0, start), text.rfind('?', 0, start), text.rfind('\n', 0, start))
    right_candidates = [idx for idx in (
        text.find('.', end),
        text.find('!', end),
        text.find('?', end),
        text.find('\n', end),
    ) if idx != -1]
    right = min(right_candidates) if right_candidates else len(text)
    return text[left + 1:right].strip()


def _latest_player_message_for_character(hot_context, character):
    recent_messages = (hot_context or {}).get('recent_messages') or []
    user_id = character.get('user_id')
    for message in reversed(recent_messages):
        if message.get('role') != 'player':
            continue
        if user_id is not None and message.get('user_id') != user_id:
            continue
        return str(message.get('content') or '')
    return ''


def _pc_control_staging_echo_exception(sentence, latest_player_message, character):
    import re

    sentence = str(sentence or '').strip()
    latest_player_message = str(latest_player_message or '').strip()
    if not sentence or not latest_player_message:
        return False

    # Allow low-agency scene framing when it is plainly echoing the same player's
    # just-declared positioning toward the same NPC. This avoids blocking harmless
    # paraphrases like "Seraphina draws alongside Miriam Saltwick" after the player
    # already said they sidled closer to Miriam.
    if not re.search(r'\b(?:draws?\s+alongside|draws?\s+closer|steps?\s+closer|moves?\s+closer|approaches?|sidles?\s+closer|drifts?\s+closer)\b', sentence, flags=re.IGNORECASE):
        return False

    protected_names = {
        str(character.get('name') or '').strip().lower(),
        str((character.get('name') or '').split()[0] if character.get('name') else '').strip().lower(),
    }
    mentioned_names = {
        match.group(0).strip()
        for match in re.finditer(r'\b[A-Z][A-Za-z0-9\'._-]*(?:\s+[A-Z][A-Za-z0-9\'._-]*)+\b', sentence)
    }
    npc_names = [
        name for name in mentioned_names
        if name.lower() not in protected_names
    ]
    if not npc_names:
        return False

    player_has_matching_target = any(
        re.search(re.escape(name), latest_player_message, flags=re.IGNORECASE)
        for name in npc_names
    )
    if not player_has_matching_target:
        return False

    return bool(re.search(r'\b(?:sidles?|drifts?|approaches?|steps?|moves?|draws?)\b', latest_player_message, flags=re.IGNORECASE))


def _pc_control_minor_affect_exception(sentence, character):
    import re

    sentence = str(sentence or '').strip()
    name = str((character or {}).get('name') or '').strip()
    if not sentence:
        return False

    lowered = sentence.lower()
    if '"' in sentence or "'" in sentence:
        return False
    if re.search(r'\b(?:decides?|chooses?|plans?|intends?|wants?|hopes?|realizes?|knows?|suspects?|remembers?|trusts?)\b', lowered):
        return False

    affect_words = (
        'sad', 'sadness', 'saddened', 'sorrow', 'sorrowful', 'grief', 'grieving', 'unease', 'uneasy',
        'relief', 'relieved', 'angry', 'anger', 'afraid', 'fear', 'dread', 'frustrated',
        'frustration', 'embarrassed', 'shaken', 'startled', 'wary', 'tense', 'anxious',
        'nervous', 'guilty', 'ashamed', 'hopeful', 'determined',
    )
    subject_patterns = [r'\byou\b']
    if name:
        subject_patterns.append(rf'\b{re.escape(name)}\b')
        subject_patterns.append(rf'\b{re.escape(name.split()[0])}\b')
    subject_pattern = '(?:' + '|'.join(subject_patterns) + ')'
    affect_pattern = (
        rf'{subject_pattern}[^.!?\n]{{0,60}}\b(?:feel|feels|felt|is|are|seems?|seem)\b[^.!?\n]{{0,60}}'
        rf'\b(?:{"|".join(affect_words)})\b'
    )
    if re.search(affect_pattern, sentence, flags=re.IGNORECASE):
        return True
    return bool(re.search(rf'\b(?:pang|flash|wave)\s+of\s+(?:{"|".join(affect_words)})\b', sentence, flags=re.IGNORECASE))


def _pc_control_minor_flair_exception(sentence):
    import re

    sentence = str(sentence or '').strip()
    if not sentence:
        return False
    if '"' in sentence or "'" in sentence:
        return False
    if re.search(r'\b(?:decides?|chooses?|plans?|intends?|wants?|hopes?|asks?|says?|replies?|responds?)\b', sentence, flags=re.IGNORECASE):
        return False
    if re.search(
        r'\b(?:run|rush|sprint|dash|leave|leaves|slip out|slips out|head|heads|walk|walks|move|moves|step away|steps away|follow|follows|grab|grabs|draw|draws|attack|attacks|cast|casts|open|opens|close|closes)\b',
        sentence,
        flags=re.IGNORECASE,
    ):
        return False
    return bool(re.search(
        r'\b(?:nods?|glances?|looks?|smiles?|frowns?|sighs?|shrugs?|straightens?|adjusts?|steadies?|tightens?|loosens?|grips?|swallows|blinks?|hesitates?)\b',
        sentence,
        flags=re.IGNORECASE,
    ))


def _pc_control_declared_followthrough_exception(sentence, latest_player_message, character):
    import re

    sentence = str(sentence or '').strip()
    latest_player_message = str(latest_player_message or '').strip()
    if not sentence or not latest_player_message:
        return False
    if _pc_control_staging_echo_exception(sentence, latest_player_message, character):
        return True

    categories = {
        'movement': r'\b(?:go|goes|going|head|heads|headed|move|moves|moving|step|steps|walk|walks|run|runs|rush|rushes|sprint|sprints|dash|dashes|slip|slips|leave|leaves|follow|follows|climb|climbs|descend|descends|approach|approaches)\b',
        'proximity': r'\b(?:draws?\s+closer|steps?\s+closer|moves?\s+closer|sidles?\s+closer|drifts?\s+closer|alongside)\b',
    }
    matched_category = None
    for category, pattern in categories.items():
        if re.search(pattern, sentence, flags=re.IGNORECASE) and re.search(pattern, latest_player_message, flags=re.IGNORECASE):
            matched_category = category
            break
    if not matched_category:
        return False

    stopwords = {
        'the', 'a', 'an', 'and', 'or', 'but', 'to', 'from', 'into', 'toward', 'towards', 'through',
        'closer', 'alongside', 'with', 'their', 'there', 'here', 'away', 'over', 'under', 'inside',
        'outside', 'forward', 'back', 'lead', 'watch', 'your', 'you', 'i', 'we',
    }
    sentence_tokens = {
        token for token in re.findall(r"[A-Za-z][A-Za-z'-]{2,}", sentence.lower())
        if token not in stopwords
    }
    latest_tokens = {
        token for token in re.findall(r"[A-Za-z][A-Za-z'-]{2,}", latest_player_message.lower())
        if token not in stopwords
    }
    if sentence_tokens & latest_tokens:
        return True

    if matched_category == 'movement' and re.search(r'\b(?:there|here|inside|outside|forward|away|down|up)\b', latest_player_message, flags=re.IGNORECASE):
        return True
    return False


def _protected_character_for_violation(hot_context, character_name):
    protected = (hot_context or {}).get('protected_player_characters') or []
    target = str(character_name or '').strip().lower()
    if not target:
        return None
    for character in protected:
        name = str(character.get('name') or '').strip()
        if not name:
            continue
        lowered = name.lower()
        first = lowered.split()[0]
        if target in {lowered, first}:
            return character
    return None


def _pc_control_filter_allowed_violations(result, hot_context):
    if not isinstance(result, dict):
        return result
    violations = result.get('violations') or []
    if not violations:
        return result

    filtered = []
    for violation in violations:
        character = _protected_character_for_violation(hot_context, violation.get('character'))
        latest_player_message = _latest_player_message_for_character(hot_context, character or {})
        sentence = str(violation.get('sentence') or '').strip()
        kind = str(violation.get('kind') or '').strip().lower()

        if character and kind == 'consequential_action':
            if _pc_control_declared_followthrough_exception(sentence, latest_player_message, character):
                continue
            if _pc_control_minor_flair_exception(sentence):
                continue
        if character and kind == 'interior_state' and _pc_control_minor_affect_exception(sentence, character):
            continue
        if character and kind == 'interior_state' and _pc_control_minor_flair_exception(sentence):
            continue

        filtered.append(violation)

    if not filtered:
        return {
            'safe': True,
            'violations': [],
            'confidence': result.get('confidence') or 'medium',
            'reason': 'Any protected-player references were limited to declared follow-through or minor flavor.',
        }

    return {
        **result,
        'safe': bool(result.get('safe')) and not filtered,
        'violations': filtered,
    }


def _pc_control_violation(response_text, hot_context):
    import re

    visible = _strip_npc_blocks(response_text)
    protected = hot_context.get('protected_player_characters') or []
    consequential_action_verbs = (
        'moves', 'move', 'steps', 'step', 'walks', 'walk', 'runs', 'run', 'rushes', 'rush', 'sprints', 'sprint',
        'dashes', 'dash', 'slips', 'slip', 'leaves', 'leave', 'heads', 'head', 'follows', 'follow',
        'climbs', 'climb', 'descends', 'descend', 'grabs', 'grab', 'draws', 'draw', 'opens', 'open',
        'closes', 'close', 'attacks', 'attack', 'casts', 'cast', 'fires', 'fire', 'strikes', 'strike',
        'falls', 'fall',
    )
    speech_verbs = (
        'says', 'say', 'asks', 'ask', 'replies', 'reply', 'responds', 'respond', 'whispers',
        'whisper', 'mutters', 'mutter', 'shouts', 'shout', 'calls', 'call', 'answers', 'answer',
    )
    choice_verbs = (
        'decides', 'decide', 'chooses', 'choose', 'agrees', 'agree', 'refuses', 'refuse',
        'plans', 'plan', 'intends', 'intend', 'wants', 'want', 'hopes', 'hope',
        'tries', 'try', 'attempts', 'attempt', 'commits', 'commit', 'opts', 'opt',
    )
    action_reason = 'Visible DM reply narrated a protected player character action.'
    speech_reason = 'Visible DM reply put dialogue or speaking behavior in a protected player character\'s mouth.'
    interior_reason = 'Visible DM reply narrated a protected player character\'s expression, voice, or body language.'
    choice_reason = 'Visible DM reply invented a protected player character choice, intent, or strategy.'

    for character in protected:
        name = (character.get('name') or '').strip()
        if not name:
            continue
        escaped = re.escape(name)
        first = re.escape(name.split()[0])
        name_pattern = f'(?:{escaped}|{first})'
        handoff_pattern = rf'\b{name_pattern}\s*,\s+how do you respond\?'
        visible_for_character = re.sub(handoff_pattern, '', visible, flags=re.IGNORECASE)
        latest_player_message = _latest_player_message_for_character(hot_context, character)
        checks = [
            (rf'\*\*{name_pattern}\b[^*]*:\*\*', speech_reason),
            (rf'<npc\s+target=["\']{escaped}["\']', speech_reason),
            (rf'\b{name_pattern}\b[^.!?\n]{{0,80}}\b(?:{"|".join(speech_verbs)})\b', speech_reason),
            (rf'\b{name_pattern}(?:[’\']s)?\b[^.!?\n]{{0,80}}\b(?:{"|".join(consequential_action_verbs)})\b', action_reason),
            (rf'\b{name_pattern}\b[^.!?\n]{{0,80}}\b(?:{"|".join(choice_verbs)})\b', choice_reason),
            (rf'\b{name_pattern}[’\']s\s+(?:eyes|expression|face|voice|smirk|smile|shoulders|hands)\b', interior_reason),
        ]
        for pattern, reason in checks:
            match = re.search(pattern, visible_for_character, flags=re.IGNORECASE)
            if match:
                matched_text = match.group(0).strip()
                sentence = _sentence_containing_span(visible_for_character, match.start(), match.end())
                if reason == action_reason and _pc_control_declared_followthrough_exception(sentence, latest_player_message, character):
                    continue
                if reason == action_reason and _pc_control_minor_flair_exception(sentence):
                    continue
                if reason == interior_reason and _pc_control_minor_affect_exception(sentence, character):
                    continue
                if reason == interior_reason and _pc_control_minor_flair_exception(sentence):
                    continue
                return {
                    'character': character,
                    'pattern': pattern,
                    'matched_text': matched_text[:180],
                    'reason': reason,
                }

    return None


def _private_output_violation(response_text, hot_context):
    import re

    visible = re.sub(r'</?npc\b[^>]*>', '', response_text or '', flags=re.IGNORECASE)
    npc_targets = ' '.join(
        match.group(1)
        for match in re.finditer(r'<npc\b[^>]*\btarget=["\']([^"\']+)["\']', response_text or '', flags=re.IGNORECASE)
    )
    visible_for_private_terms = f'{visible}\n{npc_targets}'
    latest_player_message = _latest_player_message(hot_context)
    matched_terms = []
    for term in hot_context.get('private_output_terms') or []:
        candidate = str(term or '').strip()
        if len(candidate) < 4:
            continue
        if latest_player_message and re.search(re.escape(candidate), latest_player_message, flags=re.IGNORECASE):
            continue
        if re.search(re.escape(candidate), visible_for_private_terms, flags=re.IGNORECASE):
            matched_terms.append(candidate)
    if matched_terms:
        return {'matched_terms': matched_terms}
    return None


def _spoiler_check_allows_earned_clue(response_text, hot_context, spoiler_check):
    import re

    if not spoiler_check or spoiler_check.get('safe', True):
        return False
    leaked_ids = [str(item or '') for item in (spoiler_check.get('leaked_item_ids') or [])]
    if not leaked_ids:
        return False
    hard_private_markers = (
        'dm_private',
        'hidden_faction',
        'true_inciting',
        'villain_plan',
        'mastermind',
    )
    if any(any(marker in leaked_id for marker in hard_private_markers) for leaked_id in leaked_ids):
        return False

    latest_player_message = _latest_player_message(hot_context)
    if not re.search(
        r'\b(?:ask|asks|question|press|listen|what\s+(?:he|she|they|you)\s+saw|what\s+happened|tell\s+me|heard|know)\b',
        latest_player_message,
        flags=re.IGNORECASE,
    ):
        return False
    if not re.search(r'\b(?:saw|seen|heard|witness|know|noticed|dropped|figure|shape|clue)\b', response_text or '', flags=re.IGNORECASE):
        return False
    if not any(('witness' in leaked_id or 'npc_secret' in leaked_id or 'clue' in leaked_id) for leaked_id in leaked_ids):
        return False
    return True


def _witness_private_leverage_spoiler_violation(response_text, hot_context):
    latest_player_message = _latest_player_message(hot_context)
    witness_exchange = re.search(
        r'\b(?:witness|dockhand|old\s+man|elderly\s+dockhand|weathered\s+sailor|what\s+(?:he|she|they|you)\s+saw|'
        r'tell\s+(?:me|us)|listening|safe|safety|protect|protection|name\s+stays?|keep\s+(?:me|him|her|them)\s+safe)\b',
        latest_player_message,
        flags=re.IGNORECASE,
    )
    witness_response = re.search(
        r'\b(?:witness|dockhand|old\s+man|elderly\s+dockhand|weathered\s+sailor)\b',
        response_text or '',
        flags=re.IGNORECASE,
    )
    if not (witness_exchange or witness_response):
        return None

    private_items = (hot_context or {}).get('private_spoiler_items') or []
    has_private_leverage_secret = any(
        isinstance(item, dict)
        and re.search(r'\b(?:debt|spiritual|favor|favour|owe|owed|obligation|blackmail|leverage)\b', str(item.get('text') or ''), flags=re.IGNORECASE)
        and re.search(r'\b(?:witness|dockhand|npc_secret)\b', f"{item.get('id', '')} {item.get('kind', '')} {item.get('text', '')}", flags=re.IGNORECASE)
        for item in private_items
    )
    if not has_private_leverage_secret:
        return None

    leak_patterns = [
        r'\bdebts?\b(?!\s+priests?\b)',
        r'\bfavo[u]?rs?\b',
        r'\bowe[sd]?\b',
        r'\bobligations?\b',
        r'\bspiritual\b',
        r'\bblackmail(?:ed)?\b',
        r'\bledgers?\b',
        r'\bhooks?\s+(?:in|into|on)\b',
        r'\bties?\b[^.!?\n]{0,40}\bold\b',
    ]
    evidence = []
    for pattern in leak_patterns:
        match = re.search(pattern, response_text or '', flags=re.IGNORECASE)
        if match:
            evidence.append(_sentence_containing_span(response_text, match.start(), match.end()) or match.group(0))
    if not evidence:
        return None

    return {
        'safe': False,
        'leaked_item_ids': ['deterministic_witness_private_leverage'],
        'evidence': evidence[:3],
        'reason': 'Visible reply hinted at hidden witness leverage/debt during a witness exchange.',
    }


def _session_dm_format_violation(response_text):
    import re

    text = response_text or ''
    tag_pattern = re.compile(r'</?\s*([A-Za-z][\w:-]*)\b[^>]*>', flags=re.DOTALL)
    attr_pattern = re.compile(
        r'([a-zA-Z_][\w:-]*)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s"\'>/]+))'
    )
    errors = []
    open_npc_tags = []

    def add_error(kind, snippet, detail):
        errors.append({
            'kind': kind,
            'snippet': ' '.join(str(snippet or '').split())[:160],
            'detail': detail,
        })

    for match in re.finditer(r'(?im)^\s*(?:[*_]{1,3}\s*)?(?:ooc|out\s+of\s+character|ic|in\s+character)(?:\s*[*_]{1,3})?\s*:?', text):
        add_error(
            'disallowed_mode_label',
            match.group(0),
            'Visible DM replies should not prefix content with OOC/IC mode labels.',
        )

    for match in re.finditer(r'[\u3400-\u4DBF\u4E00-\u9FFF]', text):
        add_error(
            'non_english_glyph',
            _sentence_containing_span(text, match.start(), match.end()) or match.group(0),
            'Visible DM replies must be written in English and must not contain stray CJK glyphs.',
        )
        break

    for match in tag_pattern.finditer(text):
        raw_tag = match.group(0)
        tag_name = match.group(1).lower()
        is_closing = raw_tag.lstrip().startswith('</')
        is_self_closing = raw_tag.rstrip().endswith('/>')

        if tag_name != 'npc':
            add_error(
                'disallowed_tag',
                raw_tag,
                'Visible DM replies may only use <npc target="NPC name">...</npc> tags.',
            )
            continue

        if is_closing:
            if not open_npc_tags:
                add_error('unmatched_close_tag', raw_tag, 'Found </npc> without a matching open <npc> tag.')
                continue
            open_npc_tags.pop()
            continue

        attrs = {
            attr_match.group(1).lower(): (
                attr_match.group(2)
                if attr_match.group(2) is not None
                else attr_match.group(3)
                if attr_match.group(3) is not None
                else attr_match.group(4)
                or ''
            )
            for attr_match in attr_pattern.finditer(raw_tag)
        }
        target = str(attrs.get('target') or '').strip()
        if not target:
            add_error('missing_npc_target', raw_tag, 'NPC tags must include a non-empty target attribute.')
        if is_self_closing:
            add_error('self_closing_npc_tag', raw_tag, 'NPC speech tags must wrap spoken text and close with </npc>.')
        else:
            open_npc_tags.append(raw_tag)

    for raw_tag in open_npc_tags:
        add_error('unclosed_npc_tag', raw_tag, 'Found an open <npc> tag without a matching </npc> close tag.')

    return {'errors': errors} if errors else None


def _possible_missing_npc_tag_signal(response_text):
    import re

    text = response_text or ''
    if '<npc' in text.lower():
        return None

    def _clean_speaker(raw_name):
        return ' '.join(str(raw_name or '').replace('*', ' ').split()).strip()

    def _build_signal(raw_name, raw_quote, raw_context):
        speaker = _clean_speaker(raw_name)
        if not speaker or speaker in ignored_speakers:
            return None
        return {
            'speaker': speaker,
            'quote': ' '.join(str(raw_quote or '').split())[:240],
            'context': ' '.join(str(raw_context or '').split())[:240],
        }

    quoted_dialogue_pattern = re.compile(
        r'(?P<prefix>(?:^|\n\n?)[^\n<]{0,240}?)'
        r'(?P<quote>(?:\*\*)?\s*["\u201c][^"\u201c\u201d\n]{2,400}["\u201d](?:\s*\*\*)?)',
        flags=re.MULTILINE,
    )
    speaker_cue_pattern = re.compile(
        r'\b(?P<name>[A-Z][A-Za-z0-9\'._-]*(?:\s+[A-Z][A-Za-z0-9\'._-]*){0,3})\b'
        r'(?:(?!<npc|["\u201c]).){0,220}\b'
        r'(?i:(?:says?|said|asks?|asked|repl(?:y|ies|ied)|whispers?|murmurs?|growls?|snaps?|'
        r'shouts?|laughs?|chuckles?|nods?|shrugs?|sighs?|smiles?|grins?|frowns?|smirks?|'
        r'glances?|gestures?|motions?|waves?|points?|leans?|turns?|watches?|scratches?|'
        r'mutters?|answers?|calls?|speaks?|warns?|orders?|adds?|continues?|stops?|spins?|'
        r'squints?|leads?|steps?|stares?|glares?|glowers?|looks?))\b',
        flags=re.DOTALL,
    )
    speaker_label_pattern = re.compile(
        r'(?:^|[\n\r])\s*(?:\*\*)?(?P<name>[A-Z][A-Za-z0-9\'._-]*(?:\s+[A-Z][A-Za-z0-9\'._-]*){0,3})(?:\*\*)?\s*:\s*$',
        flags=re.MULTILINE,
    )
    sentence_lead_pattern = re.compile(
        r'^\s*(?:[*_]{1,3}\s*)?(?P<name>[A-Z][A-Za-z0-9\'._-]*(?:\s+[A-Z][A-Za-z0-9\'._-]*){0,3})\b'
    )
    ignored_speakers = {'I', 'We', 'You', 'He', 'She', 'They', 'It', 'This', 'That', 'The'}

    for match in quoted_dialogue_pattern.finditer(text):
        prefix = match.group('prefix') or ''
        quote = match.group('quote') or ''

        speaker_match = speaker_cue_pattern.search(prefix)
        if speaker_match:
            signal = _build_signal(speaker_match.group('name'), quote, prefix)
            if signal:
                return signal

        label_match = speaker_label_pattern.search(prefix)
        if label_match:
            signal = _build_signal(label_match.group('name'), quote, prefix)
            if signal:
                return signal

        current_clause = re.split(r'(?:\n+|(?<=[.!?])\s+)', prefix.strip())[-1] if prefix.strip() else ''
        lead_match = sentence_lead_pattern.match(current_clause)
        if lead_match:
            signal = _build_signal(lead_match.group('name'), quote, current_clause)
            if signal:
                return signal
    return None


def _should_run_npc_tag_check(response_text, signal=None):
    text = str(response_text or '')
    if not text.strip():
        return False
    if signal:
        return True

    # Only escalate to the LLM checker when there is quoted speech outside existing
    # <npc> tags. This preserves the broader checker coverage for missed heuristics
    # without spending a guard pass on narration-only replies.
    visible = _strip_npc_blocks(text)
    return bool(re.search(r'["\u201c][^"\u201c\u201d\n]{2,400}["\u201d]', visible))


def _normalize_npc_tag_checker_text(text):
    cleaned = re.sub(r'[*_`]+', ' ', str(text or ''))
    return ' '.join(cleaned.split()).strip()


def _npc_wrapped_segments(response_text):
    text = str(response_text or '')
    return [
        _normalize_npc_tag_checker_text(match.group(1))
        for match in re.finditer(r'<npc\b[^>]*>(.*?)</npc>', text, flags=re.IGNORECASE | re.DOTALL)
        if _normalize_npc_tag_checker_text(match.group(1))
    ]


def _npc_tag_check_false_positive(response_text, normalized_check):
    if not normalized_check.get('requires_npc_tag'):
        return False

    evidence = normalized_check.get('evidence') or []
    if not evidence:
        return False

    wrapped_segments = _npc_wrapped_segments(response_text)
    if not wrapped_segments:
        return False

    for snippet in evidence:
        normalized_snippet = _normalize_npc_tag_checker_text(snippet)
        if not normalized_snippet:
            return False
        if not any(normalized_snippet in wrapped for wrapped in wrapped_segments):
            return False
    return True


def normalize_session_spoiler_check(raw_check):
    data = raw_check if isinstance(raw_check, dict) else _json_loads_or_empty(raw_check)
    if not isinstance(data, dict) or not data:
        return {
            'safe': True,
            'leaked_item_ids': [],
            'evidence': [],
            'reason': 'Checker returned no usable decision.',
        }

    leaked_item_ids = data.get('leaked_item_ids')
    if not isinstance(leaked_item_ids, list):
        leaked_item_ids = []
    leaked_item_ids = [str(item).strip() for item in leaked_item_ids if str(item).strip()]

    evidence = data.get('evidence')
    if not isinstance(evidence, list):
        evidence = []
    evidence = [str(item).strip() for item in evidence if str(item).strip()]

    safe = bool(data.get('safe')) and not leaked_item_ids
    return {
        'safe': safe,
        'leaked_item_ids': leaked_item_ids,
        'evidence': evidence,
        'reason': str(data.get('reason') or '').strip(),
    }


def normalize_session_mechanics_check(raw_check):
    data = raw_check if isinstance(raw_check, dict) else _json_loads_or_empty(raw_check)
    if not isinstance(data, dict) or not data:
        return {
            'safe': True,
            'violations': [],
            'required_mechanic': '',
            'reason': 'Checker returned no usable decision.',
        }

    violations = data.get('violations')
    if not isinstance(violations, list):
        violations = []
    violations = [str(item).strip() for item in violations if str(item).strip()]
    safe = bool(data.get('safe')) and not violations
    return {
        'safe': safe,
        'violations': violations,
        'required_mechanic': str(data.get('required_mechanic') or '').strip()[:80],
        'reason': str(data.get('reason') or '').strip(),
    }


def build_session_mechanics_check_messages(response_text, preflight_decision, hot_context):
    return [
        {'role': 'system', 'content': SESSION_MECHANICS_CHECK_SYSTEM_PROMPT},
        {
            'role': 'user',
            'content': json.dumps({
                'candidate_visible_dm_reply': response_text,
                'preflight_decision': preflight_decision or {},
                'has_active_combat': bool((hot_context or {}).get('combat_coordinates')),
                'has_current_encounter_map': bool((hot_context or {}).get('current_encounter_map')),
                'return_shape': {
                    'safe': 'boolean',
                    'violations': ['short exact snippets or paraphrases of resolved outcomes before mechanics'],
                    'required_mechanic': 'attack roll | initiative | contested check | saving throw | other | empty string',
                    'reason': 'one short explanation',
                },
            }, ensure_ascii=False),
        },
    ]


def build_session_spoiler_check_messages(response_text, hot_context):
    return [
        {'role': 'system', 'content': SESSION_SPOILER_CHECK_SYSTEM_PROMPT},
        {
            'role': 'user',
            'content': json.dumps({
                'candidate_visible_dm_reply': response_text,
                'unrevealed_private_items': hot_context.get('private_spoiler_items') or [],
                'recent_visible_messages': (hot_context.get('recent_messages') or [])[-6:],
                'return_shape': {
                    'safe': 'boolean',
                    'leaked_item_ids': ['ids of unrevealed private items leaked or strongly implied'],
                    'evidence': ['short exact snippets from the candidate reply that caused the decision'],
                    'reason': 'one short explanation',
                },
            }, ensure_ascii=False),
        },
    ]


def normalize_session_npc_tag_check(raw_check):
    data = raw_check if isinstance(raw_check, dict) else _json_loads_or_empty(raw_check)
    if not isinstance(data, dict) or not data:
        return {
            'requires_npc_tag': False,
            'speaker': '',
            'evidence': [],
            'reason': 'Checker returned no usable decision.',
        }

    evidence = data.get('evidence')
    if not isinstance(evidence, list):
        evidence = []
    evidence = [str(item).strip() for item in evidence if str(item).strip()]

    return {
        'requires_npc_tag': bool(data.get('requires_npc_tag')),
        'speaker': str(data.get('speaker') or '').strip()[:120],
        'evidence': evidence,
        'reason': str(data.get('reason') or '').strip(),
    }


def normalize_session_pc_control_check(raw_check):
    data = raw_check if isinstance(raw_check, dict) else _json_loads_or_empty(raw_check)
    if not isinstance(data, dict) or not data:
        return {
            'safe': True,
            'violations': [],
            'confidence': 'low',
            'reason': 'Checker returned no usable decision.',
        }

    violations = data.get('violations')
    if not isinstance(violations, list):
        violations = []

    normalized_violations = []
    for item in violations:
        if not isinstance(item, dict):
            continue
        normalized_violations.append({
            'character': str(item.get('character') or '').strip()[:120],
            'sentence': str(item.get('sentence') or '').strip()[:240],
            'kind': str(item.get('kind') or '').strip()[:80],
            'reason': str(item.get('reason') or '').strip()[:240],
        })

    confidence = str(data.get('confidence') or 'medium').strip().lower()
    if confidence not in {'low', 'medium', 'high'}:
        confidence = 'medium'

    safe = bool(data.get('safe')) and not normalized_violations
    return {
        'safe': safe,
        'violations': normalized_violations,
        'confidence': confidence,
        'reason': str(data.get('reason') or '').strip(),
    }


def normalize_session_canon_discipline_check(raw_check):
    data = raw_check if isinstance(raw_check, dict) else _json_loads_or_empty(raw_check)
    if not isinstance(data, dict) or not data:
        return {
            'safe': True,
            'unsupported_confirmations': [],
            'coherence_conflicts': [],
            'confidence': 'low',
            'reason': 'Checker returned no usable decision.',
        }

    def normalize_items(items):
        if not isinstance(items, list):
            return []
        normalized = []
        for item in items:
            if not isinstance(item, dict):
                continue
            normalized.append({
                'sentence': str(item.get('sentence') or '').strip()[:260],
                'claim_source': str(item.get('claim_source') or '').strip()[:80],
                'reason': str(item.get('reason') or '').strip()[:260],
            })
        return [item for item in normalized if item['sentence'] or item['reason']]

    unsupported_confirmations = normalize_items(data.get('unsupported_confirmations'))
    coherence_conflicts = normalize_items(data.get('coherence_conflicts'))
    confidence = str(data.get('confidence') or 'medium').strip().lower()
    if confidence not in {'low', 'medium', 'high'}:
        confidence = 'medium'
    safe = bool(data.get('safe')) and not unsupported_confirmations and not coherence_conflicts
    return {
        'safe': safe,
        'unsupported_confirmations': unsupported_confirmations,
        'coherence_conflicts': coherence_conflicts,
        'confidence': confidence,
        'reason': str(data.get('reason') or '').strip(),
    }


def build_session_npc_tag_check_messages(response_text, signal=None):
    return [
        {'role': 'system', 'content': SESSION_NPC_TAG_CHECK_SYSTEM_PROMPT},
        {
            'role': 'user',
            'content': json.dumps({
                'candidate_visible_dm_reply': response_text,
                'heuristic_signal': signal or {},
                'return_shape': {
                    'requires_npc_tag': 'boolean',
                    'speaker': 'speaker name if confidently identified, else empty string',
                    'evidence': ['short exact snippets from the candidate reply'],
                    'reason': 'one short explanation',
                },
            }, ensure_ascii=False),
        },
    ]


def build_session_pc_control_check_messages(response_text, hot_context):
    protected = hot_context.get('protected_player_characters') or []

    return [
        {'role': 'system', 'content': SESSION_PC_CONTROL_CHECK_SYSTEM_PROMPT},
        {
            'role': 'user',
            'content': json.dumps({
                'candidate_visible_dm_reply': response_text,
                'current_player_character': hot_context.get('current_player_character') or {},
                'protected_player_characters': protected,
                'latest_player_messages_by_character': _latest_player_messages_by_character(hot_context),
                'return_shape': {
                    'safe': 'boolean',
                    'violations': [{
                        'character': 'protected player character name',
                        'sentence': 'short exact sentence from candidate reply',
                        'kind': 'dialogue | interior_state | choice_or_intent | consequential_action',
                        'reason': 'one short explanation',
                    }],
                    'confidence': 'low | medium | high',
                    'reason': 'one short explanation',
                },
            }, ensure_ascii=False),
        },
    ]


def build_session_canon_discipline_check_messages(response_text, hot_context):
    established_public_facts = (hot_context or {}).get('established_public_facts') or []
    recent_public_world_events = (hot_context or {}).get('recent_public_world_events') or []
    open_public_threads = (hot_context or {}).get('open_public_threads') or []
    session_meta = (hot_context or {}).get('session') or {}

    return [
        {'role': 'system', 'content': SESSION_CANON_DISCIPLINE_CHECK_SYSTEM_PROMPT},
        {
            'role': 'user',
            'content': json.dumps({
                'candidate_visible_dm_reply': response_text,
                'latest_player_message': _latest_player_message(hot_context),
                'recent_visible_messages': (hot_context.get('recent_messages') or [])[-6:],
                'current_scene': (hot_context.get('current_scene') or {}),
                'session_running_summary': str(session_meta.get('running_summary') or '')[:1800],
                'established_public_facts': established_public_facts[:8],
                'recent_public_world_events': recent_public_world_events[:6],
                'open_public_threads': open_public_threads[:6],
                'return_shape': {
                    'safe': 'boolean',
                    'unsupported_confirmations': [{
                        'sentence': 'short exact sentence from candidate reply',
                        'claim_source': 'player_claim | guess | accusation | bluff | unsupported_lore | other',
                        'reason': 'one short explanation',
                    }],
                    'coherence_conflicts': [{
                        'sentence': 'short exact sentence from candidate reply',
                        'claim_source': 'contradicted_lead | replaced_fact | unsupported_reframe | other',
                        'reason': 'one short explanation',
                    }],
                    'confidence': 'low | medium | high',
                    'reason': 'one short explanation',
                },
            }, ensure_ascii=False),
        },
    ]


def _pc_control_check_needed(response_text, hot_context):
    import re

    visible = _strip_npc_blocks(response_text)
    protected = hot_context.get('protected_player_characters') or []
    if not protected:
        return False

    for character in protected:
        name = str(character.get('name') or '').strip()
        if not name:
            continue
        first = name.split()[0].strip()
        visible_without_handoffs = re.sub(
            rf'\b(?:{re.escape(name)}|{re.escape(first)})\s*,\s+(?:you are up|how do you respond\?)\.?',
            '',
            visible,
            flags=re.IGNORECASE,
        )
        if re.search(rf'\b(?:{re.escape(name)}|{re.escape(first)})\b', visible_without_handoffs, flags=re.IGNORECASE):
            return True

    return bool(re.search(r"\b(?:you|your|you're|you've|you’d|you'll)\b", visible, flags=re.IGNORECASE))


def _canon_discipline_check_needed(response_text, hot_context):
    if not (response_text or '').strip():
        return False
    latest_player_message = _latest_player_message(hot_context)
    if not latest_player_message:
        return False
    return bool(
        (hot_context or {}).get('established_public_facts')
        or (hot_context or {}).get('recent_public_world_events')
        or (hot_context or {}).get('open_public_threads')
    )


def _child_audit_context(base_audit, operation, actor, trace_label):
    parent_trace_id = base_audit.get('trace_id')
    return {
        **base_audit,
        'operation': operation,
        'actor': actor,
        'trace_id': f'{parent_trace_id or actor}:{operation}:{uuid4().hex[:10]}',
        'parent_trace_id': parent_trace_id,
        'trace_label': trace_label,
    }


def _session_dm_content_format_violation(content, audit_context=None):
    format_violation = _session_dm_format_violation(content)
    missing_npc_signal = (
        _possible_missing_npc_tag_signal(content)
        if str(content or '').strip() and not format_violation
        else None
    )
    npc_tag_check = (
        check_session_missing_npc_tags_with_llm(
            content,
            audit_context,
            missing_npc_signal,
        )
        if str(content or '').strip()
        and not format_violation
        and _should_run_npc_tag_check(content, missing_npc_signal)
        else {'requires_npc_tag': False, 'speaker': '', 'evidence': [], 'reason': ''}
    )
    if not format_violation and npc_tag_check.get('requires_npc_tag'):
        speaker = str(npc_tag_check.get('speaker') or (missing_npc_signal or {}).get('speaker') or '').strip() or 'the speaker'
        evidence = npc_tag_check.get('evidence') or []
        snippet = str(evidence[0] if evidence else (missing_npc_signal or {}).get('quote') or '').strip()
        reason = str(npc_tag_check.get('reason') or '').strip()
        detail = f'Quoted dialogue attributed to {speaker} should be wrapped in <npc target="{speaker}">...</npc>.'
        if reason:
            detail = f'{detail} Checker reason: {reason}'
        format_violation = {
            'errors': [{
                'kind': 'missing_npc_tag',
                'snippet': snippet[:160],
                'detail': detail,
            }],
        }
    return format_violation


def _repair_session_dm_visible_reply(content, guard_name, details, hot_context, audit_context=None):
    if not str(content or '').strip():
        return ''

    guard_operation = f'session_dm_{guard_name}_repair'
    base_repair_audit = _child_audit_context(
        audit_context or {},
        guard_operation,
        guard_operation,
        f'{guard_operation}: visible reply repair',
    )

    estimated_tokens = max(1, _estimate_tokens(content))
    repair_token_budgets = []
    next_budget = max(512, estimated_tokens + 256)
    while len(repair_token_budgets) < 3:
        repair_token_budgets.append(next_budget)
        if next_budget >= 16384:
            break
        next_budget = min(16384, max(next_budget * 2, next_budget + 1024))

    for attempt_index, max_tokens in enumerate(repair_token_budgets, start=1):
        repair_audit = base_repair_audit
        if attempt_index > 1:
            repair_audit = _child_audit_context(
                base_repair_audit,
                f'{guard_operation}_retry_{attempt_index}',
                guard_operation,
                f'{guard_operation}: visible reply repair retry {attempt_index}',
            )
        data = _post_chat_response(
            _build_session_guard_repair_messages(content, guard_name, details, hot_context),
            json_mode=False,
            audit_context=repair_audit,
            tools=None,
            allow_thinking=False,
            max_tokens=max_tokens,
        )
        message, finish_reason = _choice_message(data)
        finish_reason = str(finish_reason or '').strip().lower()
        if finish_reason == 'length':
            continue
        repaired_content = str(message.get('content') or '').strip()
        if repaired_content:
            return repaired_content
        finalizer_decision, _violation = _session_dm_finalizer_decision_from_tool_calls(message.get('tool_calls') or [])
        if isinstance(finalizer_decision, dict) and finalizer_decision.get('mode') == 'speak':
            return str(finalizer_decision.get('content') or '').strip()

    if guard_name == 'format':
        local_repair = _local_missing_npc_tag_repair(content, details, hot_context)
        if local_repair:
            return local_repair
    return ''


def check_session_mechanics_with_llm(response_text, preflight_decision, hot_context, audit_context=None):
    if not (response_text or '').strip():
        return {'safe': True, 'violations': [], 'required_mechanic': '', 'reason': ''}
    if not (preflight_decision or {}).get('latest_player_intent_requires_mechanics'):
        return {'safe': True, 'violations': [], 'required_mechanic': '', 'reason': ''}

    base_audit = audit_context or {}
    checker_audit = _child_audit_context(
        base_audit,
        'session_mechanics_check',
        'session_mechanics_checker',
        'session_mechanics_checker: mechanics check',
    )
    try:
        raw_check = _post_chat(
            build_session_mechanics_check_messages(response_text, preflight_decision, hot_context),
            json_mode=True,
            audit_context=checker_audit,
            allow_thinking=False,
        )
        return normalize_session_mechanics_check(raw_check)
    except Exception as err:
        campaign_id = base_audit.get('campaign_id')
        if campaign_id:
            log_audit_event(
                campaign_id,
                'mechanics_checker_error',
                'Session mechanics checker failed open.',
                {'error': repr(err)},
                source='session_dm.guard',
                actor='server',
                trace_id=base_audit.get('trace_id'),
                trace_label=base_audit.get('trace_label'),
                audit_role='guard',
                commit=True,
            )
        return {
            'safe': True,
            'violations': [],
            'required_mechanic': '',
            'reason': 'Checker failed open.',
        }


def check_session_missing_npc_tags_with_llm(response_text, audit_context=None, signal=None):
    if not (response_text or '').strip():
        return {'requires_npc_tag': False, 'speaker': '', 'evidence': [], 'reason': ''}

    base_audit = audit_context or {}
    checker_audit = _child_audit_context(
        base_audit,
        'session_npc_tag_check',
        'session_npc_tag_checker',
        'session_npc_tag_checker: npc tag check',
    )
    try:
        raw_check = _post_chat(
            build_session_npc_tag_check_messages(response_text, signal),
            json_mode=True,
            audit_context=checker_audit,
            allow_thinking=False,
        )
        normalized = normalize_session_npc_tag_check(raw_check)
        if _npc_tag_check_false_positive(response_text, normalized):
            return {
                'requires_npc_tag': False,
                'speaker': '',
                'evidence': [],
                'reason': 'Checker evidence was already wrapped in <npc> tags.',
            }
        return normalized
    except Exception as err:
        campaign_id = base_audit.get('campaign_id')
        if campaign_id:
            log_audit_event(
                campaign_id,
                'npc_tag_checker_error',
                'Session NPC tag checker failed open.',
                {'error': repr(err)},
                source='session_dm.guard',
                actor='server',
                trace_id=base_audit.get('trace_id'),
                trace_label=base_audit.get('trace_label'),
                audit_role='guard',
                commit=True,
            )
        return {
            'requires_npc_tag': False,
            'speaker': '',
            'evidence': [],
            'reason': 'Checker failed open.',
        }


def check_session_pc_control_with_llm(response_text, hot_context, audit_context=None):
    if not (response_text or '').strip():
        return {'safe': True, 'violations': [], 'confidence': 'high', 'reason': ''}
    if not _pc_control_check_needed(response_text, hot_context):
        return {'safe': True, 'violations': [], 'confidence': 'high', 'reason': 'No protected-player reference or second-person consequence phrasing detected.'}

    base_audit = audit_context or {}
    checker_audit = _child_audit_context(
        base_audit,
        'session_pc_control_check',
        'session_pc_control_checker',
        'session_pc_control_checker: pc control check',
    )
    try:
        raw_check = _post_chat(
            build_session_pc_control_check_messages(response_text, hot_context),
            json_mode=True,
            audit_context=checker_audit,
            allow_thinking=False,
        )
        return _pc_control_filter_allowed_violations(
            normalize_session_pc_control_check(raw_check),
            hot_context,
        )
    except Exception as err:
        campaign_id = base_audit.get('campaign_id')
        if campaign_id:
            log_audit_event(
                campaign_id,
                'pc_control_checker_error',
                'Session PC-control checker failed open.',
                {'error': repr(err)},
                source='session_dm.guard',
                actor='server',
                trace_id=base_audit.get('trace_id'),
                trace_label=base_audit.get('trace_label'),
                audit_role='guard',
                commit=True,
            )
        return {
            'safe': True,
            'violations': [],
            'confidence': 'low',
            'reason': 'Checker failed open.',
        }


def check_session_canon_discipline_with_llm(response_text, hot_context, audit_context=None):
    if not _canon_discipline_check_needed(response_text, hot_context):
        return {
            'safe': True,
            'unsupported_confirmations': [],
            'coherence_conflicts': [],
            'confidence': 'high',
            'reason': '',
        }

    base_audit = audit_context or {}
    checker_audit = _child_audit_context(
        base_audit,
        'session_canon_discipline_check',
        'session_canon_discipline_checker',
        'session_canon_discipline_checker: canon discipline check',
    )
    try:
        raw_check = _post_chat(
            build_session_canon_discipline_check_messages(response_text, hot_context),
            json_mode=True,
            audit_context=checker_audit,
            allow_thinking=False,
        )
        return normalize_session_canon_discipline_check(raw_check)
    except Exception as err:
        campaign_id = base_audit.get('campaign_id')
        if campaign_id:
            log_audit_event(
                campaign_id,
                'canon_discipline_checker_error',
                'Session canon-discipline checker failed open.',
                {'error': repr(err)},
                source='session_dm.guard',
                actor='server',
                trace_id=base_audit.get('trace_id'),
                trace_label=base_audit.get('trace_label'),
                audit_role='guard',
                commit=True,
            )
        return {
            'safe': True,
            'unsupported_confirmations': [],
            'coherence_conflicts': [],
            'confidence': 'low',
            'reason': 'Checker failed open.',
        }


def check_session_spoilers_with_llm(response_text, hot_context, audit_context=None, skip_spoiler_check=False):
    if not (response_text or '').strip() or not hot_context.get('private_spoiler_items'):
        return {'safe': True, 'leaked_item_ids': [], 'evidence': [], 'reason': ''}
    if skip_spoiler_check is True:
        return {
            'safe': True,
            'leaked_item_ids': [],
            'evidence': [],
            'reason': 'Conservative preflight classified this turn as safe to skip spoiler checking.',
        }

    base_audit = audit_context or {}
    checker_audit = _child_audit_context(
        base_audit,
        'session_spoiler_check',
        'session_spoiler_checker',
        'session_spoiler_checker: spoiler check',
    )
    try:
        raw_check = _post_chat(
            build_session_spoiler_check_messages(response_text, hot_context),
            json_mode=True,
            audit_context=checker_audit,
            allow_thinking=False,
        )
        spoiler_check = normalize_session_spoiler_check(raw_check)
        if _spoiler_check_allows_earned_clue(response_text, hot_context, spoiler_check):
            return {
                'safe': True,
                'leaked_item_ids': [],
                'evidence': [],
                'reason': 'Allowed limited clue reveal prompted by the latest visible player action.',
            }
        return spoiler_check
    except Exception as err:
        campaign_id = base_audit.get('campaign_id')
        if campaign_id:
            log_audit_event(
                campaign_id,
                'spoiler_checker_error',
                'Session spoiler checker failed open.',
                {'error': repr(err)},
                source='session_dm.guard',
                actor='server',
                trace_id=base_audit.get('trace_id'),
                trace_label=base_audit.get('trace_label'),
                audit_role='guard',
                commit=True,
            )
        return {
            'safe': True,
            'leaked_item_ids': [],
            'evidence': [],
            'reason': 'Checker failed open.',
        }


def build_opening_scene_messages(context, world_context):
    return [
        {'role': 'system', 'content': SYSTEM_PROMPT},
        {
            'role': 'system',
            'content': (
                'Use the private campaign world memory to write the first visible DM message. '
                'Do not reveal DM-private facts, hidden clocks, secret motives, true villains, or NPC secrets. '
                'Open at the public starting location with immediate sensory detail, establish the public party hook, '
                'and end with a clear prompt for player action.'
            ),
        },
        {
            'role': 'user',
            'content': json.dumps({
                'planning_context': context,
                'world_context': world_context,
            }, ensure_ascii=False),
        },
    ]


def build_planning_dm_messages(context, current_user_messages, draft_character=None, active_page=None):
    return [
        {'role': 'system', 'content': PLANNING_SYSTEM_PROMPT},
        {
            'role': 'system',
            'content': 'Planning context:\n' + json.dumps({
                **context,
                'draft_character': draft_character or {},
                'active_page': active_page,
                'valid_pages': ['identity', 'scores', 'combat', 'magic_gear', 'story'],
            }, ensure_ascii=False),
        },
        *[
            {
                'role': 'assistant' if msg.role == 'dm' else 'user',
                'content': msg.content,
            }
            for msg in current_user_messages
        ],
    ]


def build_planning_summary_messages(context, latest_player_message, latest_dm_message):
    return [
        {'role': 'system', 'content': SUMMARY_SYSTEM_PROMPT},
        {
            'role': 'user',
            'content': json.dumps({
                'context': context,
                'latest_player_message': latest_player_message,
                'latest_dm_message': latest_dm_message,
                'return_shape': {
                    'summary_update': {
                        'party_balance': 'short paragraph',
                        'confirmed_public_facts': ['facts visible to party'],
                        'dm_private_secrets': {'user_id': ['private facts or secrets']},
                        'explicit_player_points': {'user_id': ['complete canonical durable points for this user']},
                        'unresolved_gaps': ['party or story gaps still open'],
                        'accepted_hooks': ['confirmed hooks and approved bonds'],
                    },
                    'bond_suggestions': [
                        {
                            'title': 'short title',
                            'description': 'proposed connection requiring approval',
                            'involved_user_ids': [1, 2],
                        }
                    ],
                },
            }, ensure_ascii=False),
        },
    ]


def build_world_genesis_messages(context):
    return [
        {'role': 'system', 'content': WORLD_GENESIS_SYSTEM_PROMPT},
        {
            'role': 'user',
            'content': json.dumps({
                'context': context,
                'return_shape': {
                    'public_intro': {
                        'title': 'short campaign-facing title',
                        'elevator_pitch': '2-3 spoiler-free sentences for players',
                        'starting_location': 'public starting place name',
                        'campaign_tone': ['3-5 spoiler-free tone tags'],
                        'party_hook': 'why the party is together at the opening moment without revealing secrets',
                    },
                    'knowledge_graph': {
                        'entities': [
                            {
                                'id': 'stable_snake_case_id',
                                'type': 'npc | location | faction | item | event | threat | concept',
                                'name': 'display name',
                                'summary': 'durable fact summary',
                                'visibility': 'public | party_known | dm_private',
                                'tags': ['short tags'],
                            },
                        ],
                        'relations': [
                            {
                                'id': 'stable_snake_case_id',
                                'source_id': 'entity id',
                                'target_id': 'entity id',
                                'type': 'relationship type',
                                'summary': 'relationship summary',
                                'visibility': 'public | party_known | dm_private',
                            },
                        ],
                        'facts': [
                            {
                                'id': 'stable_snake_case_id',
                                'entity_ids': ['entity ids'],
                                'text': 'durable fact text',
                                'certainty': 'confirmed | suspected | false_rumor',
                                'visibility': 'public | party_known | dm_private',
                            },
                        ],
                    },
                    'world_state': {
                        'current_arc': 'opening arc name',
                        'current_scene': {
                            'location_id': 'entity id for starting location',
                            'location_name': 'display name',
                            'time_of_day': 'opening scene timing',
                            'active_npc_ids': ['npc actor ids present or relevant'],
                            'immediate_tension': 'what is visibly pressing right now',
                        },
                        'party': {
                            'known_location_id': 'entity id',
                            'public_reputation': 'how locals currently read the party',
                        },
                        'open_threads': ['non-spoiler and private thread summaries as appropriate'],
                    },
                    'dm_private': {
                        'true_inciting_incident': 'private truth behind the opening problem',
                        'villain_plan': 'private antagonist or pressure plan if applicable',
                        'hidden_factions': ['private faction notes'],
                        'npc_secrets': ['private NPC secrets'],
                        'opening_scene_private_notes': 'DM-only guidance for the first exchange',
                    },
                    'npc_actors': [
                        {
                            'id': 'npc_stable_id',
                            'name': 'NPC name',
                            'role': 'story role',
                            'public_summary': 'safe public-facing summary',
                            'voice': 'how they speak',
                            'background': 'private background',
                            'wants': ['motivations'],
                            'fears': ['fears'],
                            'secrets': ['private secrets'],
                            'relationships': {'party': 'initial relationship'},
                            'recent_offscreen_activity': ['what they did before the opening'],
                        },
                    ],
                    'clocks': [
                        {
                            'id': 'clock_stable_id',
                            'name': 'Clock name',
                            'segments': 4,
                            'filled': 0,
                            'pressure_type': 'faction | danger | mystery | environment | personal',
                            'visibility': 'public | party_known | dm_private',
                            'summary': 'what this pressure represents',
                            'trigger': 'when it advances',
                            'on_complete': 'what happens when filled',
                            'status': 'active',
                        },
                    ],
                },
            }, ensure_ascii=False),
        },
    ]


WORLD_GENESIS_SECTION_SPECS = (
    (
        'public_intro',
        {
            'public_intro': {
                'title': 'short campaign-facing title',
                'elevator_pitch': '2-3 spoiler-free sentences for players',
                'starting_location': 'public starting place name',
                'campaign_tone': ['3-5 spoiler-free tone tags'],
                'party_hook': 'why the party is together at the opening moment without revealing secrets',
            },
        },
    ),
    (
        'knowledge_graph',
        {
            'knowledge_graph': {
                'entities': [
                    {
                        'id': 'stable_snake_case_id',
                        'type': 'npc | location | faction | item | event | threat | concept',
                        'name': 'display name',
                        'summary': 'durable fact summary',
                        'visibility': 'public | party_known | dm_private',
                        'tags': ['short tags'],
                    },
                ],
                'relations': [
                    {
                        'id': 'stable_snake_case_id',
                        'source_id': 'entity id',
                        'target_id': 'entity id',
                        'type': 'relationship type',
                        'summary': 'relationship summary',
                        'visibility': 'public | party_known | dm_private',
                    },
                ],
                'facts': [
                    {
                        'id': 'stable_snake_case_id',
                        'entity_ids': ['entity ids'],
                        'text': 'durable fact text',
                        'certainty': 'confirmed | suspected | false_rumor',
                        'visibility': 'public | party_known | dm_private',
                    },
                ],
            },
        },
    ),
    (
        'world_state',
        {
            'world_state': {
                'current_arc': 'opening arc name',
                'current_scene': {
                    'location_id': 'entity id for starting location',
                    'location_name': 'display name',
                    'time_of_day': 'opening scene timing',
                    'active_npc_ids': ['npc actor ids present or relevant'],
                    'immediate_tension': 'what is visibly pressing right now',
                },
                'party': {
                    'known_location_id': 'entity id',
                    'public_reputation': 'how locals currently read the party',
                },
                'open_threads': ['non-spoiler and private thread summaries as appropriate'],
            },
        },
    ),
    (
        'dm_private',
        {
            'dm_private': {
                'true_inciting_incident': 'private truth behind the opening problem',
                'villain_plan': 'private antagonist or pressure plan if applicable',
                'hidden_factions': ['private faction notes'],
                'npc_secrets': ['private NPC secrets'],
                'opening_scene_private_notes': 'DM-only guidance for the first exchange',
            },
        },
    ),
    (
        'npc_actors',
        {
            'npc_actors': [
                {
                    'id': 'npc_stable_id',
                    'name': 'NPC name',
                    'role': 'story role',
                    'public_summary': 'safe public-facing summary',
                    'voice': 'how they speak',
                    'background': 'private background',
                    'wants': ['motivations'],
                    'fears': ['fears'],
                    'secrets': ['private secrets'],
                    'relationships': {'party': 'initial relationship'},
                    'recent_offscreen_activity': ['what they did before the opening'],
                },
            ],
        },
    ),
    (
        'clocks',
        {
            'clocks': [
                {
                    'id': 'clock_stable_id',
                    'name': 'Clock name',
                    'segments': 4,
                    'filled': 0,
                    'pressure_type': 'faction | danger | mystery | environment | personal',
                    'visibility': 'public | party_known | dm_private',
                    'summary': 'what this pressure represents',
                    'trigger': 'when it advances',
                    'on_complete': 'what happens when filled',
                    'status': 'active',
                },
            ],
        },
    ),
)


def build_world_genesis_section_seed_messages(context):
    return [
        {'role': 'system', 'content': WORLD_GENESIS_SYSTEM_PROMPT},
        {
            'role': 'user',
            'content': json.dumps({
                'context': context,
                'task': (
                    'Build the campaign world package in sections. I will ask for one JSON section at a time. '
                    'Keep each new section consistent with the campaign context and with any prior sections in '
                    'this conversation. Return only the requested JSON object for each section.'
                ),
                'final_package_keys': [
                    section_name
                    for section_name, _shape in WORLD_GENESIS_SECTION_SPECS
                ],
            }, ensure_ascii=False),
        },
    ]


def build_world_genesis_section_prompt(section_name, return_shape):
    return {
        'role': 'user',
        'content': json.dumps({
            'section': section_name,
            'instructions': (
                f'Generate only the {section_name} section now. Return valid JSON with exactly one top-level '
                f'key named "{section_name}". Do not repeat previous sections. Keep spoiler-sensitive facts '
                'out of public-facing fields and mark hidden facts dm_private where the schema supports visibility.'
            ),
            'return_shape': return_shape,
        }, ensure_ascii=False),
    }


def _coerce_world_genesis_section(section_name, data):
    if isinstance(data, dict) and section_name in data:
        return {section_name: data[section_name]}
    if section_name in {'npc_actors', 'clocks'} and isinstance(data, list):
        return {section_name: data}
    if isinstance(data, dict):
        return {section_name: data}
    raise ValueError(f'World genesis section {section_name} returned invalid JSON')


def build_session_memory_messages(memory_context):
    return [
        {'role': 'system', 'content': SESSION_MEMORY_SYSTEM_PROMPT},
        {
            'role': 'user',
            'content': json.dumps({
                'context': memory_context,
                'return_shape': {
                    'running_summary': 'compact updated session summary, replacing prior summary',
                    'scene_patch': {
                        'location_id': 'optional current scene location id; use for temporary location state',
                        'location_name': 'optional current scene location name',
                        'time_of_day': 'must match current_scene.time_of_day unless the exchange explicitly describes a time transition',
                        'active_npc_ids': ['npc ids currently present or active in the scene; use this instead of graph facts for occupants. Listing an NPC is optional; NPCs already on stage are retained unless you also list them in departed_npc_ids.'],
                        'departed_npc_ids': ['npc ids who actually left the scene this turn (walked out, fled, were escorted away, or otherwise exited). Do not infer departure from omission.'],
                        'immediate_tension': 'optional current scene tension; use for transient pressure visible right now',
                    },
                    'scene_reason': 'why scene changed',
                    'upsert_graph_entities': [
                        {
                            'id': 'stable_id',
                            'type': 'npc | location | faction | item | event | threat | concept',
                            'name': 'display name',
                            'summary': 'durable summary',
                            'visibility': 'public | party_known | dm_private',
                            'tags': [],
                            'certainty': 'confirmed | suspected | inferred | false | retconned',
                            'importance': 3,
                            'expires_or_retire_condition': 'optional description or null',
                            'reason': 'why created/updated',
                            'memory_type': 'npc | location | fact'
                        }
                    ],
                    'upsert_graph_relations': [
                        {
                            'id': 'stable_id',
                            'source_id': 'entity id',
                            'target_id': 'entity id',
                            'type': 'relationship type',
                            'summary': 'durable summary',
                            'visibility': 'public | party_known | dm_private',
                            'certainty': 'confirmed | suspected | inferred | false | retconned',
                            'importance': 3,
                            'expires_or_retire_condition': 'optional description or null',
                            'reason': 'why created/updated',
                            'memory_type': 'relation'
                        }
                    ],
                    'upsert_graph_facts': [
                        {
                            'id': 'stable_id',
                            'entity_ids': ['all directly relevant entity ids'],
                            'text': 'durable fact',
                            'certainty': 'confirmed | suspected | inferred | false | retconned',
                            'visibility': 'public | party_known | dm_private',
                            'importance': 3,
                            'expires_or_retire_condition': 'optional description or null',
                            'reason': 'why created/updated',
                            'memory_type': 'fact | quest | inventory | money'
                        }
                    ],
                    'create_clocks': [
                        {
                            'id': 'stable_clock_id',
                            'name': 'Clock name',
                            'segments': 4,
                            'filled': 0,
                            'pressure_type': 'faction | danger | mystery | environment | personal | story',
                            'visibility': 'public | party_known | dm_private',
                            'summary': 'pressure summary',
                            'trigger': 'when it advances',
                            'on_complete': 'what happens',
                            'status': 'active',
                            'reason': 'why this clock now exists',
                            'certainty': 'confirmed | suspected | inferred | false | retconned',
                            'importance': 3,
                            'expires_or_retire_condition': 'optional description or null',
                            'memory_type': 'clock'
                        }
                    ],
                    'retire_clocks': [
                        {
                            'clock_id': 'existing_clock_id',
                            'status': 'completed | resolved | inactive',
                            'reason': 'why retired',
                            'certainty': 'confirmed | suspected | inferred | false | retconned',
                            'importance': 3,
                            'expires_or_retire_condition': 'optional description or null',
                            'memory_type': 'clock'
                        }
                    ],
                    'update_npc_actors': [
                        {
                            'id': 'npc_stable_id',
                            'name': 'NPC name',
                            'role': 'story role',
                            'public_summary': 'public summary',
                            'wants': [],
                            'fears': [],
                            'secrets': [],
                            'relationships': {},
                            'recent_offscreen_activity': [],
                            'reason': 'why updated',
                            'certainty': 'confirmed | suspected | inferred | false | retconned',
                            'importance': 3,
                            'expires_or_retire_condition': 'optional description or null',
                            'memory_type': 'npc'
                        }
                    ],
                    'record_events': [
                        {
                            'event_type': 'short_type',
                            'summary': 'durable event summary',
                            'payload': {},
                            'visibility': 'public | party_known | dm_private',
                            'certainty': 'confirmed | suspected | inferred | false | retconned',
                            'importance': 3,
                            'expires_or_retire_condition': 'optional description or null',
                            'reason': 'why created',
                            'memory_type': 'fact'
                        }
                    ],
                },
            }, ensure_ascii=False),
        },
    ]


def build_session_memory_summary_scene_messages(memory_context):
    compact = _session_memory_compact_context(memory_context)
    return [
        {'role': 'system', 'content': SESSION_MEMORY_SUMMARY_SCENE_SYSTEM_PROMPT},
        {
            'role': 'user',
            'content': json.dumps({
                'current_scene': compact.get('current_scene'),
                'latest_player_message': compact.get('latest_player_message'),
                'latest_dm_message': compact.get('latest_dm_message'),
            }, ensure_ascii=False),
        },
    ]


def build_session_memory_facts_messages(memory_context):
    compact = _session_memory_compact_context(memory_context)
    return [
        {'role': 'system', 'content': SESSION_MEMORY_FACTS_SYSTEM_PROMPT},
        {
            'role': 'user',
            'content': json.dumps({
                'current_scene': compact.get('current_scene'),
                'relevant_memory': compact.get('relevant_memory'),
                'identity_rules': [
                    'Reuse a relevant_memory match item_id when the latest exchange updates the same fact.',
                    'Use relevant_memory entity item_id values in entity_ids for known entities.',
                    'Create a new fact id only for a distinct durable truth not represented by relevant_memory.',
                ],
                'visibility_policy': {
                    'party_known': 'Use for facts established in latest_player_message or latest_dm_message.',
                    'public': 'Use only for facts generally known outside the party.',
                    'dm_private': 'Use only for unrevealed secrets, hidden causes, off-screen actions, or DM-only pressure.',
                },
                'latest_player_message': compact.get('latest_player_message'),
                'latest_dm_message': compact.get('latest_dm_message'),
            }, ensure_ascii=False),
        },
    ]


def build_session_memory_clocks_messages(memory_context):
    compact = _session_memory_compact_context(memory_context)
    return [
        {'role': 'system', 'content': SESSION_MEMORY_CLOCKS_SYSTEM_PROMPT},
        {
            'role': 'user',
            'content': json.dumps({
                'current_scene': compact.get('current_scene'),
                'active_clocks': compact.get('active_clocks'),
                'latest_player_message': compact.get('latest_player_message'),
                'latest_dm_message': compact.get('latest_dm_message'),
            }, ensure_ascii=False),
        },
    ]


def build_session_memory_extractor_messages(memory_context):
    compact = _session_memory_compact_context(memory_context)
    return [
        {'role': 'system', 'content': SESSION_MEMORY_EXTRACTOR_SYSTEM_PROMPT},
        {
            'role': 'user',
            'content': json.dumps({
                'prior_running_summary': memory_context.get('prior_running_summary'),
                'prior_memory_anchors': memory_context.get('prior_memory_anchors'),
                'current_scene': compact.get('current_scene'),
                'relevant_memory': compact.get('relevant_memory'),
                'latest_player_message': compact.get('latest_player_message'),
                'latest_dm_message': compact.get('latest_dm_message'),
                'return_shape': {
                    'running_summary': 'fresh compact replacement summary for the session after this turn',
                    'memory_anchors': {
                        'current_goal': 'string goal, or null',
                        'current_scene': 'string scene description, or null',
                        'open_clues': ['list of open clues'],
                        'unresolved_questions': ['list of open questions'],
                        'npc_observations': ['list of observations about NPCs'],
                        'recent_offers_promises': ['list of recent offers or promises made']
                    },
                    'scene_patch': {
                        'location_id': 'optional existing location id only if already known in prompt context',
                        'location_name': 'optional location name',
                        'time_of_day': 'optional scene time only if visibly changed',
                        'active_npc_ids': ['optional known ids already present in prompt context'],
                        'departed_npc_ids': ['optional known ids already present in prompt context'],
                        'immediate_tension': 'optional compact visible tension',
                    },
                    'scene_reason': 'why scene state should change',
                    'fact_claims': [
                        {
                            'text': 'durable fact text',
                            'entity_refs': ['raw names or ids that need resolution'],
                            'source_surface': 'visible_transcript',
                            'intended_visibility': 'party_known',
                            'certainty': 'confirmed',
                            'importance': 3,
                            'reason': 'why this fact matters',
                            'expires_or_retire_condition': None,
                            'memory_type': 'fact',
                        }
                    ],
                    'entity_claims': [
                        {
                            'id': 'optional canonical entity id if known',
                            'name': 'entity name',
                            'type': 'entity type (e.g. location, npc, item, faction, organization, other)',
                            'summary': 'entity description/summary',
                            'tags': ['list of tags'],
                            'source_surface': 'visible_transcript',
                            'intended_visibility': 'party_known',
                            'certainty': 'confirmed',
                            'importance': 3,
                            'reason': 'why this entity exists/changes',
                            'expires_or_retire_condition': None,
                            'memory_type': 'entity',
                        }
                    ],
                    'relation_claims': [
                        {
                            'id': 'optional canonical relation id if known',
                            'type': 'relation type/verb',
                            'source_ref': 'source entity name or id',
                            'target_ref': 'target entity name or id',
                            'summary': 'relationship description',
                            'source_surface': 'visible_transcript',
                            'intended_visibility': 'party_known',
                            'certainty': 'confirmed',
                            'importance': 3,
                            'reason': 'why this relation exists/changes',
                            'expires_or_retire_condition': None,
                            'memory_type': 'relation',
                        }
                    ],
                    'npc_claims': [
                        {
                            'actor_ref': 'NPC actor ID or name',
                            'name': 'optional name',
                            'role': 'optional role',
                            'public_summary': 'optional public summary',
                            'voice': 'optional voice description',
                            'background': 'optional background story',
                            'wants': ['optional list of wants'],
                            'fears': ['optional list of fears'],
                            'secrets': ['optional list of secrets'],
                            'certainty': 'confirmed',
                            'importance': 3,
                            'reason': 'why this NPC is updated',
                            'expires_or_retire_condition': None,
                        }
                    ],
                    'event_claims': [
                        {
                            'event_type': 'event type',
                            'summary': 'event summary',
                            'payload': {'key': 'value'},
                            'intended_visibility': 'dm_private',
                            'certainty': 'confirmed',
                            'importance': 3,
                            'reason': 'why this event is recorded',
                            'expires_or_retire_condition': None,
                        }
                    ],
                },
            }, ensure_ascii=False),
        },
    ]


def build_session_memory_resolver_messages(memory_context, extracted):
    compact = _session_memory_compact_context(memory_context)
    return [
        {'role': 'system', 'content': SESSION_MEMORY_RESOLVER_SYSTEM_PROMPT},
        {
            'role': 'user',
            'content': json.dumps({
                'prior_memory_anchors': memory_context.get('prior_memory_anchors'),
                'current_scene': compact.get('current_scene'),
                'latest_player_message': compact.get('latest_player_message'),
                'latest_dm_message': compact.get('latest_dm_message'),
                'extracted_memory_candidates': extracted,
                'available_tools': [
                    'get_running_summary',
                    'get_transcript_window',
                    'search_campaign_memory',
                    'get_world_state',
                    'get_clocks',
                    'get_npcs',
                    'get_recent_world_events',
                    'get_scene_candidates',
                    'get_entity_candidates',
                    'get_fact_candidates',
                ],
            }, ensure_ascii=False),
        },
    ]


def build_session_clock_adjudication_messages(clock_context):
    return [
        {'role': 'system', 'content': SESSION_CLOCK_ADJUDICATOR_SYSTEM_PROMPT},
        {
            'role': 'user',
            'content': json.dumps({
                'current_scene_before': clock_context.get('current_scene_before') or {},
                'current_scene_after': clock_context.get('current_scene_after') or {},
                'active_clocks': clock_context.get('active_clocks') or [],
                'latest_player_message': clock_context.get('latest_player_message'),
                'latest_dm_message': clock_context.get('latest_dm_message'),
                'recent_events': clock_context.get('recent_events') or [],
            }, ensure_ascii=False),
        },
    ]


def _session_memory_summary_scene_retry_messages(messages):
    return [
        *messages,
        {
            'role': 'user',
            'content': (
                'Your previous response was blank or invalid. Return exactly one valid JSON object now with a '
                'non-empty turn_summary string and a scene_patch object. Do not return whitespace, null, an '
                'empty object, markdown fences, or commentary outside the JSON object.'
            ),
        },
    ]


def build_character_sheet_agent_messages(question, scope, character_sheets):
    return [
        {'role': 'system', 'content': CHARACTER_SHEET_SYSTEM_PROMPT},
        {
            'role': 'user',
            'content': json.dumps({
                'question': question,
                'scope': scope,
                'character_sheets': character_sheets,
                'return_shape': {
                    'answer': 'concise answer for the DM',
                    'character_ids': [1],
                    'missing': False,
                },
            }, ensure_ascii=False),
        },
    ]


def _choice_message(data):
    choices = data.get('choices') if isinstance(data, dict) else []
    if not choices or not isinstance(choices[0], dict):
        return {}, None
    message = choices[0].get('message') or {}
    return message if isinstance(message, dict) else {}, choices[0].get('finish_reason')


def _json_object_from_text(text):
    raw = str(text or '').strip()
    if not raw:
        raise ValueError('LLM response was empty.')
    start = raw.find('{')
    end = raw.rfind('}')
    if start == -1 or end == -1 or end < start:
        raise ValueError('LLM response did not contain a JSON object.')
    return json.loads(raw[start:end + 1])


def _parse_tool_arguments(raw_arguments):
    if isinstance(raw_arguments, dict):
        return raw_arguments
    if not raw_arguments:
        return {}
    try:
        return json.loads(raw_arguments)
    except (TypeError, ValueError):
        return {}


def _assistant_tool_message(message):
    assistant_message = {
        'role': 'assistant',
        'content': message.get('content') or '',
        'tool_calls': message.get('tool_calls') or [],
    }
    for field in ('reasoning_content', 'reasoning', 'reasoning_details'):
        if message.get(field) is not None:
            assistant_message[field] = message.get(field)
    return assistant_message


def _session_dm_tool_result_for_prompt(result, hot_context):
    constraints = (hot_context or {}).get('visible_naming_constraints') or []
    if not constraints:
        return result
    if isinstance(result, dict):
        payload = dict(result)
    else:
        payload = {'tool_result': result}
    payload['_visible_naming_constraints'] = constraints
    payload['_visibility_policy'] = (
        'These constraints still apply after reading this tool result. '
        'Do not use any avoid_visible_name in visible narration, quoted labels, or <npc target="..."> '
        'unless the latest player message already used that name; use use_public_reference instead.'
    )
    return payload


def get_session_dm_response_with_tools(
    hot_context,
    recent_messages,
    tools,
    execute_tool,
    audit_context=None,
    max_tool_rounds=30,
    on_status_change=None,
    build_retrieval_packet=None,
):
    base_audit = audit_context or {}
    trace_id = base_audit.get('trace_id') or f"session_dm:session_dm_response:{uuid4().hex[:10]}"
    trace_label = base_audit.get('trace_label') or 'session_dm: session_dm_response'
    all_tools = _session_dm_tools_with_finalizers(tools)
    finalizer_tools = SESSION_DM_FINALIZER_TOOLS
    action_buffer = {'actions': []}
    preflight_decision = {
        'dm_reply_mode': 'unknown',
        'skip_spoiler_check': False,
        'main_call_thinking': True,
        'confidence': 'low',
        'reason': 'Preflight not run.',
    }
    if base_audit.get('operation') == 'session_dm_response':
        preflight_decision = get_session_preflight_decision(
            hot_context,
            recent_messages,
            all_tools,
            audit_context={
                **base_audit,
                'trace_id': trace_id,
                'trace_label': trace_label,
                'operation': 'session_preflight',
                'actor': 'session_preflight_router',
            },
        )

    if callable(build_retrieval_packet):
        try:
            packet = build_retrieval_packet(preflight_decision)
            if isinstance(packet, dict):
                hot_context = {**(hot_context or {}), 'retrieval_packet': packet}
        except Exception as err:
            if base_audit.get('campaign_id'):
                log_audit_event(
                    base_audit['campaign_id'],
                    'session_retrieval_packet_error',
                    'Focused pre-generation retrieval failed open.',
                    {'error': repr(err)},
                    source='session_context',
                    actor='session_retrieval',
                    trace_id=trace_id,
                    parent_trace_id=base_audit.get('parent_trace_id'),
                    trace_label=trace_label,
                    audit_role='tools',
                    commit=True,
                )

    messages = build_session_dm_tool_messages(hot_context)
    for msg in recent_messages:
        if msg.role == 'dm':
            role = 'assistant'
        elif msg.role == 'player':
            role = 'user'
        else:
            role = msg.role
        messages.append({'role': role, 'content': msg.content})

    if on_status_change:
        on_status_change({"step": "preflight"})

    tool_round = 0
    finalizer_contract_retry_count = 0
    combat_batch_retry_count = 0
    combat_batch_force_tools = False
    format_retry_count = 0
    mechanical_retried = False
    pc_control_retried = False
    pc_control_repair_attempted = False
    canon_checker_retry_count = 0
    canon_checker_repair_attempted = False
    private_output_retry_count = 0
    spoiler_checker_retry_count = 0
    spoiler_checker_repair_attempted = False
    combat_handoff_retried = False
    guard_audits = {}
    combat_tracker = _session_dm_combat_tracker(hot_context)
    if not combat_tracker.get('campaign_id'):
        combat_tracker['campaign_id'] = base_audit.get('campaign_id')
    combat_tracker['snapshot'] = _session_dm_build_combat_snapshot(combat_tracker)

    def guard_audit(guard_name):
        if guard_name not in guard_audits:
            guard_audits[guard_name] = _child_audit_context(
                base_audit,
                guard_name,
                'session_dm_guard',
                f'session_dm_guard: {guard_name}',
            )
        return guard_audits[guard_name]

    def rollback_combat_if_needed(reason, details=None):
        if not combat_tracker.get('mutated') or not combat_tracker.get('snapshot'):
            return False
        restored = _session_dm_restore_combat_snapshot(combat_tracker.get('snapshot'))
        if restored and base_audit.get('campaign_id'):
            log_audit_event(
                base_audit.get('campaign_id'),
                'combat_turn_rollback',
                'Rolled back combat state after a session DM turn failed to finish coherently.',
                {
                    'operation': 'combat_turn_rollback',
                    'reason': reason,
                    'details': details or {},
                    'encounter_map_id': combat_tracker.get('encounter_map_id'),
                },
                source='session_dm.guard',
                actor='session_dm_guard',
                trace_id=trace_id,
                parent_trace_id=base_audit.get('parent_trace_id'),
                trace_label=trace_label,
                audit_role='guard',
                commit=True,
            )
        combat_tracker['mutated'] = False
        return restored

    while True:
        if on_status_change:
            on_status_change({"step": "thinking", "reasoning": "Determining best actions or phrasing narration"})

        retrying_visible_answer = any((
            finalizer_contract_retry_count > 0,
            format_retry_count > 0,
            mechanical_retried,
            pc_control_retried,
            canon_checker_retry_count > 0,
            private_output_retry_count > 0,
            spoiler_checker_retry_count > 0,
            combat_handoff_retried,
        )) and not combat_batch_force_tools
        if retrying_visible_answer and finalizer_tools:
            active_tools = finalizer_tools
        elif tool_round < max_tool_rounds and all_tools:
            active_tools = all_tools
        elif finalizer_tools:
            active_tools = finalizer_tools
        else:
            active_tools = None
        finalizer_only_tools = (
            bool(active_tools)
            and {
                str(((tool or {}).get('function') or {}).get('name') or '').strip()
                for tool in (active_tools or [])
            } == SESSION_DM_FINALIZER_TOOL_NAMES
        )
        active_tool_choice = (
            'required'
            if finalizer_only_tools
            else 'auto'
            if active_tools
            else None
        )

        loop_audit = {
            **base_audit,
            'trace_id': trace_id,
            'trace_label': trace_label,
            'operation': base_audit.get('operation') or 'session_dm_response',
            'context_manifest': base_audit.get('context_manifest'),
            'token_estimate': base_audit.get('token_estimate'),
            'full_world_graph_included': False,
        }
        data = _post_chat_response(
            messages,
            # Keep session DM finalization in plain chat mode and enforce the finalizer-tool
            # contract in the guard loop instead of relying on provider JSON mode.
            json_mode=False,
            audit_context=loop_audit,
            tools=active_tools,
            tool_choice=active_tool_choice,
            parallel_tool_calls=False if active_tools else None,
            allow_thinking=(
                False
                if retrying_visible_answer
                else preflight_decision.get('main_call_thinking') is not False or tool_round > 0
            ),
        )
        message, finish_reason = _choice_message(data)

        # If the LLM returned a reasoning field, let's extract it and feed it to status
        reasoning_val = message.get("reasoning_content") or message.get("reasoning")
        if on_status_change and reasoning_val:
            on_status_change({"step": "thinking", "reasoning": reasoning_val})

        tool_calls = message.get('tool_calls') or []
        finalizer_decision, finalizer_violation = _session_dm_finalizer_decision_from_tool_calls(tool_calls)
        if finalizer_violation:
            tool_calls = []
        if finalizer_decision is not None or not tool_calls or tool_round >= max_tool_rounds:
            if on_status_change:
                on_status_change({"step": "guard_check"})
            raw_content = message.get('content') or ''
            if finalizer_decision is not None:
                decision = normalize_session_dm_turn_decision(finalizer_decision)
                raw_content = json.dumps(finalizer_decision, ensure_ascii=False)
                finalizer_contract_violation = None
            elif finalizer_violation:
                finalizer_contract_violation = finalizer_violation
            else:
                finalizer_contract_violation = _session_dm_finalizer_contract_violation(raw_content)
                if (
                    finalizer_contract_violation
                    and finalizer_contract_violation.get('kind') == 'missing_finalizer_tool_call'
                    and str(raw_content or '').strip()
                    and not action_buffer['actions']
                ):
                    decision = {
                        'mode': 'speak',
                        'content': str(raw_content or '').strip(),
                    }
                    finalizer_contract_violation = None
            if finalizer_contract_violation and finalizer_contract_retry_count < 2:
                if on_status_change:
                    on_status_change({"step": "revising", "violations": {"type": "finalizer_contract", "details": finalizer_contract_violation}})
                if base_audit.get('campaign_id'):
                    audit = guard_audit('finalizer_contract_guard')
                    log_audit_event(
                        base_audit.get('campaign_id'),
                        'finalizer_contract_guard_retry',
                        'Session DM response did not finish with a valid finalization tool call; discarded candidate and reran with guard reminder.',
                        {
                            'operation': 'finalizer_contract_guard',
                            'violation': finalizer_contract_violation,
                            'draft_response': raw_content,
                        },
                        source='session_dm.guard',
                        actor=audit.get('actor'),
                        trace_id=audit.get('trace_id'),
                        parent_trace_id=audit.get('parent_trace_id'),
                        trace_label=audit.get('trace_label'),
                        audit_role='guard',
                        commit=True,
                    )
                messages.append({
                    'role': 'system',
                    'content': _session_dm_guard_retry_system_prompt('finalizer_contract', finalizer_contract_violation),
                })
                finalizer_contract_retry_count += 1
                continue
            if finalizer_contract_violation:
                rollback_combat_if_needed('invalid_final_output', {'finalizer_contract_violation': finalizer_contract_violation})
                return {
                    'mode': 'silent',
                    'reason': 'The DM response did not produce a valid finalizer tool call.',
                }
            combat_batch_violation = _session_dm_combat_batch_violation(decision, combat_tracker)
            if combat_batch_violation and combat_batch_retry_count < 2 and tool_round < max_tool_rounds:
                if on_status_change:
                    on_status_change({"step": "revising", "violations": {"type": "combat_batch", "details": combat_batch_violation}})
                if base_audit.get('campaign_id'):
                    audit = guard_audit('combat_batch_guard')
                    log_audit_event(
                        base_audit.get('campaign_id'),
                        'combat_batch_guard_retry',
                        'Session DM stopped before resolving consecutive non-player combat turns; discarded candidate and continued with a combat-turn reminder.',
                        {
                            'operation': 'combat_batch_guard',
                            'violation': combat_batch_violation,
                            'draft_response': raw_content,
                        },
                        source='session_dm.guard',
                        actor=audit.get('actor'),
                        trace_id=audit.get('trace_id'),
                        parent_trace_id=audit.get('parent_trace_id'),
                        trace_label=audit.get('trace_label'),
                        audit_role='guard',
                        commit=True,
                    )
                messages.append({
                    'role': 'system',
                    'content': _session_dm_guard_retry_system_prompt('combat_batch', combat_batch_violation),
                })
                combat_batch_force_tools = True
                combat_batch_retry_count += 1
                continue
            content = decision.get('content') or ''
            format_violation = None
            mechanical_violation = None
            violation = None
            canon_violation = None
            private_violation = None
            spoiler_check = {'safe': True, 'leaked_item_ids': [], 'evidence': [], 'reason': ''}
            combat_handoff_violation = None
            while True:
                format_violation = (
                    _session_dm_content_format_violation(content, loop_audit)
                    if decision.get('mode') == 'speak'
                    else None
                )
                repaired_from_format_guard = False
                while format_violation and format_retry_count < 2:
                    if on_status_change:
                        on_status_change({"step": "revising", "violations": {"type": "format", "details": format_violation}})
                    if base_audit.get('campaign_id'):
                        audit = guard_audit('format_guard')
                        log_audit_event(
                            base_audit.get('campaign_id'),
                            'format_guard_retry',
                            'Session DM response used malformed visible-message syntax; sent candidate to the format repair pass.',
                            {
                                'operation': 'format_guard',
                                'violation': format_violation,
                                'draft_response': content,
                                'repair_strategy': 'separate_repair_pass',
                            },
                            source='session_dm.guard',
                            actor=audit.get('actor'),
                            trace_id=audit.get('trace_id'),
                            parent_trace_id=audit.get('parent_trace_id'),
                            trace_label=audit.get('trace_label'),
                            audit_role='guard',
                            commit=True,
                        )
                    repaired_content = _repair_session_dm_visible_reply(
                        content,
                        'format',
                        format_violation,
                        hot_context,
                        audit_context=loop_audit,
                    )
                    format_retry_count += 1
                    if not repaired_content:
                        break
                    repaired_from_format_guard = True
                    content = repaired_content
                    raw_content = repaired_content
                    decision = {
                        **decision,
                        'content': repaired_content,
                    }
                    format_violation = _session_dm_content_format_violation(content, loop_audit)
                mechanics_check = (
                    check_session_mechanics_with_llm(content, preflight_decision, hot_context, loop_audit)
                    if decision.get('mode') == 'speak' and not format_violation
                    else {'safe': True, 'violations': [], 'required_mechanic': '', 'reason': ''}
                )
                mechanical_violation = (
                    {
                        'kind': 'mechanics_resolved_without_required_step',
                        **mechanics_check,
                    }
                    if not mechanics_check.get('safe', True)
                    else None
                )
                pc_control_check = (
                    check_session_pc_control_with_llm(content, hot_context, loop_audit)
                    if decision.get('mode') == 'speak' and not format_violation
                    else {'safe': True, 'violations': [], 'confidence': 'high', 'reason': ''}
                )
                violation = (
                    {
                        'kind': 'pc_control_classifier',
                        **pc_control_check,
                    } if not pc_control_check.get('safe', True) else None
                )
                canon_check = (
                    check_session_canon_discipline_with_llm(content, hot_context, loop_audit)
                    if decision.get('mode') == 'speak' and not format_violation and not mechanical_violation
                    else {
                        'safe': True,
                        'unsupported_confirmations': [],
                        'coherence_conflicts': [],
                        'confidence': 'high',
                        'reason': '',
                    }
                )
                canon_violation = (
                    {
                        'kind': 'canon_discipline',
                        **canon_check,
                    } if not canon_check.get('safe', True) else None
                )
                private_violation = (
                    _private_output_violation(content, hot_context)
                    if decision.get('mode') == 'speak' and not format_violation and not canon_violation
                    else None
                )
                deterministic_spoiler_violation = (
                    _witness_private_leverage_spoiler_violation(content, hot_context)
                    if decision.get('mode') == 'speak' and not format_violation and not private_violation
                    and not mechanical_violation and not canon_violation
                    else None
                )
                spoiler_check = (
                    deterministic_spoiler_violation
                    or check_session_spoilers_with_llm(
                        content,
                        hot_context,
                        loop_audit,
                        skip_spoiler_check=preflight_decision.get('skip_spoiler_check') is True,
                    )
                    if decision.get('mode') == 'speak' and not format_violation and not private_violation
                    and not mechanical_violation and not canon_violation
                    else {'safe': True, 'leaked_item_ids': [], 'evidence': [], 'reason': ''}
                )
                combat_handoff_violation = (
                    _session_dm_combat_handoff_violation(content, combat_tracker)
                    if decision.get('mode') == 'speak' and not format_violation and not private_violation
                    and not mechanical_violation and not canon_violation
                    else None
                )
                if format_violation and repaired_from_format_guard and format_retry_count < 2:
                    continue
                if violation and not pc_control_repair_attempted and not pc_control_retried:
                    if on_status_change:
                        on_status_change({"step": "revising", "violations": {"type": "pc_control", "details": violation}})
                    if base_audit.get('campaign_id'):
                        audit = guard_audit('pc_control_guard')
                        log_audit_event(
                            base_audit.get('campaign_id'),
                            'pc_control_guard_retry',
                            'Session DM response controlled a protected player character; sent candidate to a repair pass and will rerun with a guard reminder if needed.',
                            {
                                'operation': 'pc_control_guard',
                                'violation': violation,
                                'draft_response': raw_content,
                                'repair_strategy': 'repair_pass_then_rerun',
                            },
                            source='session_dm.guard',
                            actor=audit.get('actor'),
                            trace_id=audit.get('trace_id'),
                            parent_trace_id=audit.get('parent_trace_id'),
                            trace_label=audit.get('trace_label'),
                            audit_role='guard',
                            commit=True,
                        )
                    pc_control_repair_attempted = True
                    repaired_content = _repair_session_dm_visible_reply(
                        content,
                        'pc_control',
                        violation,
                        hot_context,
                        audit_context=loop_audit,
                    )
                    if repaired_content and repaired_content != content:
                        content = repaired_content
                        raw_content = repaired_content
                        decision = {
                            **decision,
                            'content': repaired_content,
                        }
                        continue
                if (
                    canon_violation
                    and canon_checker_retry_count < 3
                    and not canon_checker_repair_attempted
                ):
                    if on_status_change:
                        on_status_change({"step": "revising", "violations": {"type": "canon_discipline", "details": canon_violation}})
                    if base_audit.get('campaign_id'):
                        audit = guard_audit('canon_discipline_guard')
                        log_audit_event(
                            base_audit.get('campaign_id'),
                            'canon_discipline_guard_retry',
                            'Session DM response promoted unsupported claims or conflicted with established public facts; sent candidate to a repair pass and will rerun with a guard reminder if needed.',
                            {
                                'operation': 'canon_discipline_guard',
                                'violation': canon_violation,
                                'draft_response': raw_content,
                                'repair_strategy': 'repair_pass_then_rerun',
                            },
                            source='session_dm.guard',
                            actor=audit.get('actor'),
                            trace_id=audit.get('trace_id'),
                            parent_trace_id=audit.get('parent_trace_id'),
                            trace_label=audit.get('trace_label'),
                            audit_role='guard',
                            commit=True,
                        )
                    canon_checker_repair_attempted = True
                    repaired_content = _repair_session_dm_visible_reply(
                        content,
                        'canon_discipline',
                        canon_violation,
                        hot_context,
                        audit_context=loop_audit,
                    )
                    if repaired_content and repaired_content != content:
                        content = repaired_content
                        raw_content = repaired_content
                        decision = {
                            **decision,
                            'content': repaired_content,
                        }
                        continue
                if (
                    not spoiler_check.get('safe', True)
                    and spoiler_checker_retry_count < 3
                    and not spoiler_checker_repair_attempted
                ):
                    if on_status_change:
                        on_status_change({"step": "revising", "violations": {"type": "spoiler", "details": spoiler_check}})
                    if base_audit.get('campaign_id'):
                        audit = guard_audit('spoiler_checker_guard')
                        log_audit_event(
                            base_audit.get('campaign_id'),
                            'spoiler_checker_guard_retry',
                            'Session spoiler checker flagged a semantic leak; sent candidate to a repair pass and will rerun with a guard reminder if needed.',
                            {
                                'operation': 'spoiler_checker_guard',
                                'checker_result': spoiler_check,
                                'draft_response': raw_content,
                                'repair_strategy': 'repair_pass_then_rerun',
                            },
                            source='session_dm.guard',
                            actor=audit.get('actor'),
                            trace_id=audit.get('trace_id'),
                            parent_trace_id=audit.get('parent_trace_id'),
                            trace_label=audit.get('trace_label'),
                            audit_role='guard',
                            commit=True,
                        )
                    spoiler_checker_repair_attempted = True
                    repaired_content = _repair_session_dm_visible_reply(
                        content,
                        'spoiler_checker',
                        spoiler_check,
                        hot_context,
                        audit_context=loop_audit,
                    )
                    if repaired_content and repaired_content != content:
                        content = repaired_content
                        raw_content = repaired_content
                        decision = {
                            **decision,
                            'content': repaired_content,
                        }
                        continue
                break
            if mechanical_violation and not mechanical_retried:
                if on_status_change:
                    on_status_change({"step": "revising", "violations": {"type": "mechanical_resolution", "details": mechanical_violation}})
                if base_audit.get('campaign_id'):
                    audit = guard_audit('mechanical_resolution_guard')
                    log_audit_event(
                        base_audit.get('campaign_id'),
                        'mechanical_resolution_guard_retry',
                        'Session DM response resolved combat without required D&D mechanics; discarded candidate and reran with guard reminder.',
                        {
                            'operation': 'mechanical_resolution_guard',
                            'violation': mechanical_violation,
                            'draft_response': raw_content,
                        },
                        source='session_dm.guard',
                        actor=audit.get('actor'),
                        trace_id=audit.get('trace_id'),
                        parent_trace_id=audit.get('parent_trace_id'),
                        trace_label=audit.get('trace_label'),
                        audit_role='guard',
                        commit=True,
                    )
                messages.append({
                    'role': 'system',
                    'content': _session_dm_guard_retry_system_prompt('mechanical_resolution', mechanical_violation),
                })
                mechanical_retried = True
                continue
            if violation and not pc_control_retried:
                if on_status_change and not pc_control_repair_attempted:
                    on_status_change({"step": "revising", "violations": {"type": "pc_control", "details": violation}})
                if base_audit.get('campaign_id') and not pc_control_repair_attempted:
                    audit = guard_audit('pc_control_guard')
                    log_audit_event(
                        base_audit.get('campaign_id'),
                        'pc_control_guard_retry',
                        'Session DM response controlled a protected player character; discarded candidate and reran with guard reminder.',
                        {
                            'operation': 'pc_control_guard',
                            'violation': violation,
                            'draft_response': raw_content,
                            'repair_strategy': 'guard_reminder_rerun',
                        },
                        source='session_dm.guard',
                        actor=audit.get('actor'),
                        trace_id=audit.get('trace_id'),
                        parent_trace_id=audit.get('parent_trace_id'),
                        trace_label=audit.get('trace_label'),
                        audit_role='guard',
                        commit=True,
                    )
                messages.append({
                    'role': 'system',
                    'content': _session_dm_guard_retry_system_prompt('pc_control', violation),
                })
                pc_control_retried = True
                continue
            if canon_violation and canon_checker_retry_count < 3:
                if on_status_change and (canon_checker_retry_count > 0 or not canon_checker_repair_attempted):
                    on_status_change({"step": "revising", "violations": {"type": "canon_discipline", "details": canon_violation}})
                if base_audit.get('campaign_id') and (canon_checker_retry_count > 0 or not canon_checker_repair_attempted):
                    audit = guard_audit('canon_discipline_guard')
                    log_audit_event(
                        base_audit.get('campaign_id'),
                        'canon_discipline_guard_retry',
                        'Session DM response promoted unsupported claims or conflicted with established public facts; discarded candidate and reran with a guard reminder.',
                        {
                            'operation': 'canon_discipline_guard',
                            'violation': canon_violation,
                            'draft_response': raw_content,
                            'repair_strategy': 'guard_reminder_rerun',
                        },
                        source='session_dm.guard',
                        actor=audit.get('actor'),
                        trace_id=audit.get('trace_id'),
                        parent_trace_id=audit.get('parent_trace_id'),
                        trace_label=audit.get('trace_label'),
                        audit_role='guard',
                        commit=True,
                    )
                messages.append({
                    'role': 'system',
                    'content': _session_dm_guard_retry_system_prompt('canon_discipline', canon_violation),
                })
                canon_checker_retry_count += 1
                continue
            if private_violation and private_output_retry_count < 2:
                if on_status_change:
                    on_status_change({"step": "revising", "violations": {"type": "private_output", "details": private_violation}})
                if base_audit.get('campaign_id'):
                    audit = guard_audit('private_output_guard')
                    log_audit_event(
                        base_audit.get('campaign_id'),
                        'private_output_guard_retry',
                        'Session DM response exposed DM-private output terms; discarded candidate and reran with guard reminder.',
                        {
                            'operation': 'private_output_guard',
                            'violation': private_violation,
                            'draft_response': raw_content,
                        },
                        source='session_dm.guard',
                        actor=audit.get('actor'),
                        trace_id=audit.get('trace_id'),
                        parent_trace_id=audit.get('parent_trace_id'),
                        trace_label=audit.get('trace_label'),
                        audit_role='guard',
                        commit=True,
                    )
                messages.append({
                    'role': 'system',
                    'content': _session_dm_guard_retry_system_prompt('private_output', private_violation),
                })
                private_output_retry_count += 1
                continue
            if not spoiler_check.get('safe', True) and spoiler_checker_retry_count < 3:
                if on_status_change and (spoiler_checker_retry_count > 0 or not spoiler_checker_repair_attempted):
                    on_status_change({"step": "revising", "violations": {"type": "spoiler", "details": spoiler_check}})
                if base_audit.get('campaign_id') and (spoiler_checker_retry_count > 0 or not spoiler_checker_repair_attempted):
                    audit = guard_audit('spoiler_checker_guard')
                    log_audit_event(
                        base_audit.get('campaign_id'),
                        'spoiler_checker_guard_retry',
                        'Session spoiler checker flagged a semantic leak; discarded candidate and reran with guard reminder.',
                        {
                            'operation': 'spoiler_checker_guard',
                            'checker_result': spoiler_check,
                            'draft_response': raw_content,
                            'repair_strategy': 'guard_reminder_rerun',
                        },
                        source='session_dm.guard',
                        actor=audit.get('actor'),
                        trace_id=audit.get('trace_id'),
                        parent_trace_id=audit.get('parent_trace_id'),
                        trace_label=audit.get('trace_label'),
                        audit_role='guard',
                        commit=True,
                    )
                messages.append({
                    'role': 'system',
                    'content': _session_dm_guard_retry_system_prompt('spoiler_checker', spoiler_check),
                })
                spoiler_checker_retry_count += 1
                continue
            if combat_handoff_violation and not combat_handoff_retried:
                if on_status_change:
                    on_status_change({"step": "revising", "violations": {"type": "combat_handoff", "details": combat_handoff_violation}})
                if base_audit.get('campaign_id'):
                    audit = guard_audit('combat_handoff_guard')
                    log_audit_event(
                        base_audit.get('campaign_id'),
                        'combat_handoff_guard_retry',
                        'Session DM response used procedural next-turn handoff text after combat resolution; discarded candidate and reran with a combat handoff reminder.',
                        {
                            'operation': 'combat_handoff_guard',
                            'violation': combat_handoff_violation,
                            'draft_response': raw_content,
                        },
                        source='session_dm.guard',
                        actor=audit.get('actor'),
                        trace_id=audit.get('trace_id'),
                        parent_trace_id=audit.get('parent_trace_id'),
                        trace_label=audit.get('trace_label'),
                        audit_role='guard',
                        commit=True,
                    )
                messages.append({
                    'role': 'system',
                    'content': _session_dm_guard_retry_system_prompt('combat_handoff', combat_handoff_violation),
                })
                combat_handoff_retried = True
                continue
            if combat_batch_violation:
                if base_audit.get('campaign_id'):
                    audit = guard_audit('combat_batch_guard')
                    log_audit_event(
                        base_audit.get('campaign_id'),
                        'combat_batch_guard_blocked',
                        'Session DM still stopped while a non-player combat turn was active.',
                        {
                            'operation': 'combat_batch_guard',
                            'violation': combat_batch_violation,
                            'draft_response': raw_content,
                        },
                        source='session_dm.guard',
                        actor=audit.get('actor'),
                        trace_id=audit.get('trace_id'),
                        parent_trace_id=audit.get('parent_trace_id'),
                        trace_label=audit.get('trace_label'),
                        audit_role='guard',
                        commit=True,
                    )
                rollback_combat_if_needed('combat_batch_incomplete', combat_batch_violation)
                return {
                    'mode': 'silent',
                    'reason': 'The DM response stopped before combat returned to a player character.',
                }
            if violation:
                if base_audit.get('campaign_id'):
                    audit = guard_audit('pc_control_guard')
                    log_audit_event(
                        base_audit.get('campaign_id'),
                        'pc_control_guard_blocked',
                        'Session DM response still controlled a protected player character after retry.',
                        {
                            'operation': 'pc_control_guard',
                            'violation': violation,
                            'draft_response': raw_content,
                        },
                        source='session_dm.guard',
                        actor=audit.get('actor'),
                        trace_id=audit.get('trace_id'),
                        parent_trace_id=audit.get('parent_trace_id'),
                        trace_label=audit.get('trace_label'),
                        audit_role='guard',
                        commit=True,
                    )
                rollback_combat_if_needed('pc_control_violation', violation)
                return {
                    'mode': 'silent',
                    'reason': 'The DM response would have controlled a protected player character.',
                }
            if canon_violation:
                if base_audit.get('campaign_id'):
                    audit = guard_audit('canon_discipline_guard')
                    log_audit_event(
                        base_audit.get('campaign_id'),
                        'canon_discipline_guard_blocked',
                        'Session DM response still promoted unsupported claims or conflicted with established public facts after retry.',
                        {
                            'operation': 'canon_discipline_guard',
                            'violation': canon_violation,
                            'draft_response': raw_content,
                        },
                        source='session_dm.guard',
                        actor=audit.get('actor'),
                        trace_id=audit.get('trace_id'),
                        parent_trace_id=audit.get('parent_trace_id'),
                        trace_label=audit.get('trace_label'),
                        audit_role='guard',
                        commit=True,
                    )
                rollback_combat_if_needed('canon_discipline_violation', canon_violation)
                return {
                    'mode': 'silent',
                    'reason': 'The DM response would have promoted unsupported claims or contradicted established public facts.',
                }
            if mechanical_violation:
                if base_audit.get('campaign_id'):
                    audit = guard_audit('mechanical_resolution_guard')
                    log_audit_event(
                        base_audit.get('campaign_id'),
                        'mechanical_resolution_guard_blocked',
                        'Session DM response still resolved combat without required D&D mechanics after retry.',
                        {
                            'operation': 'mechanical_resolution_guard',
                            'violation': mechanical_violation,
                            'draft_response': raw_content,
                        },
                        source='session_dm.guard',
                        actor=audit.get('actor'),
                        trace_id=audit.get('trace_id'),
                        parent_trace_id=audit.get('parent_trace_id'),
                        trace_label=audit.get('trace_label'),
                        audit_role='guard',
                        commit=True,
                    )
                rollback_combat_if_needed('mechanical_resolution_violation', mechanical_violation)
                return {
                    'mode': 'silent',
                    'reason': 'The DM response would have resolved combat without required D&D mechanics.',
                }
            if private_violation:
                if base_audit.get('campaign_id'):
                    audit = guard_audit('private_output_guard')
                    log_audit_event(
                        base_audit.get('campaign_id'),
                        'private_output_guard_blocked',
                        'Session DM response still exposed DM-private output terms after retry.',
                        {
                            'operation': 'private_output_guard',
                            'violation': private_violation,
                            'draft_response': raw_content,
                        },
                        source='session_dm.guard',
                        actor=audit.get('actor'),
                        trace_id=audit.get('trace_id'),
                        parent_trace_id=audit.get('parent_trace_id'),
                        trace_label=audit.get('trace_label'),
                        audit_role='guard',
                        commit=True,
                    )
                rollback_combat_if_needed('private_output_violation', private_violation)
                return {
                    'mode': 'silent',
                    'reason': 'The DM response would have exposed DM-private information.',
                }
            if format_violation:
                if base_audit.get('campaign_id'):
                    audit = guard_audit('format_guard')
                    log_audit_event(
                        base_audit.get('campaign_id'),
                        'format_guard_blocked',
                        'Session DM response still used malformed visible-message syntax after retry.',
                        {
                            'operation': 'format_guard',
                            'violation': format_violation,
                            'draft_response': raw_content,
                        },
                        source='session_dm.guard',
                        actor=audit.get('actor'),
                        trace_id=audit.get('trace_id'),
                        parent_trace_id=audit.get('parent_trace_id'),
                        trace_label=audit.get('trace_label'),
                        audit_role='guard',
                        commit=True,
                    )
                rollback_combat_if_needed('format_violation', format_violation)
                return {
                    'mode': 'silent',
                    'reason': 'The DM response used malformed visible-message syntax.',
                }
            if combat_handoff_violation:
                if base_audit.get('campaign_id'):
                    audit = guard_audit('combat_handoff_guard')
                    log_audit_event(
                        base_audit.get('campaign_id'),
                        'combat_handoff_guard_blocked',
                        'Session DM response still used procedural combat handoff text after retry.',
                        {
                            'operation': 'combat_handoff_guard',
                            'violation': combat_handoff_violation,
                            'draft_response': raw_content,
                        },
                        source='session_dm.guard',
                        actor=audit.get('actor'),
                        trace_id=audit.get('trace_id'),
                        parent_trace_id=audit.get('parent_trace_id'),
                        trace_label=audit.get('trace_label'),
                        audit_role='guard',
                        commit=True,
                    )
                rollback_combat_if_needed('combat_handoff_violation', combat_handoff_violation)
                return {
                    'mode': 'silent',
                    'reason': 'The DM response used procedural combat handoff text.',
                }
            if not spoiler_check.get('safe', True):
                if base_audit.get('campaign_id'):
                    audit = guard_audit('spoiler_checker_guard')
                    log_audit_event(
                        base_audit.get('campaign_id'),
                        'spoiler_checker_guard_blocked',
                        'Session spoiler checker still flagged a semantic leak after retry.',
                        {
                            'operation': 'spoiler_checker_guard',
                            'checker_result': spoiler_check,
                            'draft_response': raw_content,
                        },
                        source='session_dm.guard',
                        actor=audit.get('actor'),
                        trace_id=audit.get('trace_id'),
                        parent_trace_id=audit.get('parent_trace_id'),
                        trace_label=audit.get('trace_label'),
                        audit_role='guard',
                        commit=True,
                    )
                rollback_combat_if_needed('spoiler_checker_violation', spoiler_check)
                return {
                    'mode': 'silent',
                    'reason': 'The DM response would have semantically exposed DM-private information.',
                }
            if base_audit.get('operation') == 'session_dm_response':
                return {**decision, '_pending_actions': list(action_buffer['actions'])}
            # Preserve the direct helper's historical public return shape; production callers
            # receive the action IDs through the session-DM operation result above.
            return {key: value for key, value in decision.items() if key != 'commit_action_ids'}

        messages.append(_assistant_tool_message(message))
        combat_batch_force_tools = False
        for tool_call in tool_calls:
            function = tool_call.get('function') or {}
            tool_name = function.get('name')
            args = _parse_tool_arguments(function.get('arguments'))
            if on_status_change:
                on_status_change({
                    "step": "tool_call",
                    "tool_name": tool_name,
                    "arguments": args
                })
            combat_tool_violation = _session_dm_combat_tool_violation(tool_name, args, combat_tracker)
            if combat_tool_violation:
                if base_audit.get('campaign_id'):
                    audit = guard_audit('combat_turn_scope_guard')
                    log_audit_event(
                        base_audit.get('campaign_id'),
                        'combat_turn_scope_guard_blocked',
                        'Blocked a session DM combat tool call that drifted outside the current allowed combat scope.',
                        {
                            'operation': 'combat_turn_scope_guard',
                            'tool_name': tool_name,
                            'arguments': args,
                            'violation': combat_tool_violation,
                        },
                        source='session_dm.guard',
                        actor=audit.get('actor'),
                        trace_id=audit.get('trace_id'),
                        parent_trace_id=audit.get('parent_trace_id'),
                        trace_label=audit.get('trace_label'),
                        audit_role='guard',
                        commit=True,
                    )
                result = {'error': combat_tool_violation.get('detail') or 'Blocked combat tool call.'}
            else:
                result = execute_tool(
                    tool_name,
                    args,
                    {
                        **loop_audit,
                        'parent_trace_id': trace_id,
                        'trace_id': trace_id,
                        'pending_action_buffer': action_buffer,
                    },
                )
                if tool_name in SESSION_DM_COMBAT_PROGRESS_TOOL_NAMES and isinstance(result, dict) and not result.get('error'):
                    combat_tracker['turn_progress_made'] = True
                if tool_name in SESSION_DM_COMBAT_MUTATION_TOOL_NAMES and isinstance(result, dict) and not result.get('error'):
                    combat_tracker['mutated'] = True
                updated_state = _session_dm_result_encounter_state(result)
                if updated_state:
                    _session_dm_refresh_combat_tracker_from_state(combat_tracker, updated_state)
            messages.append({
                'role': 'tool',
                'tool_call_id': tool_call.get('id'),
                'name': tool_name,
                'content': json.dumps(_session_dm_tool_result_for_prompt(result, hot_context), ensure_ascii=False),
            })
        tool_round += 1


def get_opening_scene_response(context, world_context, audit_context=None):
    messages = build_opening_scene_messages(context, world_context)

    try:
        data = _post_chat_response(messages, audit_context=audit_context)
        opening_text = _opening_scene_text_from_response(data)
        if opening_text:
            return opening_text

        retry_audit_context = {
            **(audit_context or {}),
            'operation': 'opening_scene_format_retry',
        }
        data = _post_chat_response(
            _opening_scene_format_retry_messages(messages),
            audit_context=retry_audit_context,
        )
        return _opening_scene_text_from_response(data)
    except Exception as e:
        print(f'[openrouter] Opening scene error: {e}')
        return None


def _memory_fallback_text(value, limit=900):
    text = re.sub(r'</?npc\b[^>]*>', '', str(value or ''), flags=re.IGNORECASE)
    text = re.sub(r'\*\*|__|`', '', text)
    text = ' '.join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + '...'


def _memory_context_lookup(memory_context, *keys):
    if not isinstance(memory_context, dict):
        return None
    for key in keys:
        value = memory_context.get(key)
        if value:
            return value
    return None


def _fallback_scene_npc_ids(latest_dm_message):
    if not latest_dm_message:
        return []
    seen = set()
    npc_ids = []
    for raw_target in re.findall(r'<npc\b[^>]*\btarget="([^"]+)"', str(latest_dm_message), flags=re.IGNORECASE):
        normalized = re.sub(r'[^a-z0-9]+', '_', raw_target.strip().lower()).strip('_')
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        npc_ids.append(normalized)
    return npc_ids



def _fallback_scene_immediate_tension(latest_dm_message, current_scene):
    cleaned = _memory_fallback_text(latest_dm_message, 600)
    paragraphs = [
        ' '.join(chunk.split())
        for chunk in re.split(r'\n\s*\n', str(cleaned or ''))
        if chunk and chunk.strip()
    ]
    for paragraph in reversed(paragraphs):
        plain = paragraph.strip()
        if not plain:
            continue
        if plain.lower().startswith('what do you do'):
            continue
        if plain.lower().startswith('make a '):
            continue
        return _memory_fallback_text(plain, 260)
    if isinstance(current_scene, dict):
        return current_scene.get('immediate_tension')
    return None


def _fallback_scene_patch(memory_context):
    hot_context = memory_context.get('hot_context') if isinstance(memory_context.get('hot_context'), dict) else {}
    current_scene = hot_context.get('current_scene') if isinstance(hot_context.get('current_scene'), dict) else {}
    latest_dm_message = _memory_context_lookup(memory_context, 'latest_dm_message', 'latest_dm_response') or ''

    scene_patch = dict(current_scene) if isinstance(current_scene, dict) else {}

    spoken_ids = _fallback_scene_npc_ids(latest_dm_message)
    if spoken_ids:
        existing_ids = scene_patch.get('active_npc_ids') if isinstance(scene_patch.get('active_npc_ids'), list) else []
        merged = list(existing_ids)
        merged_set = set(merged)
        for actor_id in spoken_ids:
            if actor_id not in merged_set:
                merged_set.add(actor_id)
                merged.append(actor_id)
        if merged != list(existing_ids):
            scene_patch['active_npc_ids'] = merged

    immediate_tension = _fallback_scene_immediate_tension(latest_dm_message, current_scene)
    if immediate_tension:
        scene_patch['immediate_tension'] = immediate_tension

    return scene_patch


def _protected_pc_scene_tokens(memory_context):
    hot_context = memory_context.get('hot_context') if isinstance(memory_context.get('hot_context'), dict) else {}
    protected = hot_context.get('protected_player_characters') if isinstance(hot_context.get('protected_player_characters'), list) else []
    blocked = set()
    for character in protected:
        if not isinstance(character, dict):
            continue
        value = str(character.get('name') or '').strip().lower()
        if not value:
            continue
        token = re.sub(r'[^a-z0-9]+', '_', value).strip('_')
        if token:
            blocked.add(token)
    return blocked


def _sanitize_scene_patch(scene_patch, memory_context):
    if not isinstance(scene_patch, dict):
        return {}
    sanitized = dict(scene_patch)
    blocked_tokens = _protected_pc_scene_tokens(memory_context)
    active_npc_ids = sanitized.get('active_npc_ids')
    if isinstance(active_npc_ids, list) and blocked_tokens:
        filtered_ids = []
        seen = set()
        for raw_value in active_npc_ids:
            normalized = re.sub(r'[^a-z0-9]+', '_', str(raw_value or '').strip().lower()).strip('_')
            if not normalized or normalized in blocked_tokens or normalized in seen:
                continue
            seen.add(normalized)
            filtered_ids.append(normalized)
        sanitized['active_npc_ids'] = filtered_ids
    return sanitized


def _session_memory_compact_context(memory_context):
    memory_context = memory_context if isinstance(memory_context, dict) else {}
    hot_context = memory_context.get('hot_context') if isinstance(memory_context.get('hot_context'), dict) else {}
    current_scene = hot_context.get('current_scene') if isinstance(hot_context.get('current_scene'), dict) else {}
    active_clocks = hot_context.get('active_clocks') if isinstance(hot_context.get('active_clocks'), list) else []
    relevant_memory = memory_context.get('relevant_memory') if isinstance(memory_context.get('relevant_memory'), dict) else {}
    scene_hint = _fallback_scene_patch(memory_context)
    active_npc_ids = (
        _sanitize_scene_patch({'active_npc_ids': scene_hint.get('active_npc_ids')}, memory_context).get('active_npc_ids')
        or _sanitize_scene_patch({'active_npc_ids': current_scene.get('active_npc_ids')}, memory_context).get('active_npc_ids')
        or []
    )
    compact_scene = {
        'location_id': scene_hint.get('location_id') or current_scene.get('location_id'),
        'location_name': scene_hint.get('location_name') or current_scene.get('location_name'),
        'time_of_day': scene_hint.get('time_of_day') or current_scene.get('time_of_day'),
        'active_npc_ids': active_npc_ids[:6] if isinstance(active_npc_ids, list) else [],
        'immediate_tension': _memory_fallback_text(
            scene_hint.get('immediate_tension') or current_scene.get('immediate_tension'),
            220,
        ) or None,
    }
    compact_clocks = []
    for clock in active_clocks[:6]:
        if not isinstance(clock, dict):
            continue
        compact_clocks.append({
            'clock_id': clock.get('clock_id') or clock.get('id'),
            'name': clock.get('name'),
            'filled': clock.get('filled'),
            'segments': clock.get('segments'),
            'status': clock.get('status'),
            'summary': _memory_fallback_text(clock.get('summary'), 160),
        })
    return {
        'current_scene': compact_scene,
        'active_clocks': compact_clocks,
        'active_clock_count': memory_context.get('active_clock_count'),
        'all_active_clocks_completed': bool(memory_context.get('all_active_clocks_completed')),
        'latest_player_message': memory_context.get('latest_player_message'),
        'latest_dm_message': _memory_context_lookup(memory_context, 'latest_dm_message', 'latest_dm_response'),
        'relevant_memory': relevant_memory,
    }


def _merge_session_running_summary(prior_summary, turn_summary, limit=1800):
    prior_text = _memory_fallback_text(prior_summary, 1000)
    turn_text = _memory_fallback_text(turn_summary, 900)
    running_summary = ' '.join(part for part in [prior_text, turn_text] if part).strip()
    if len(running_summary) > limit:
        running_summary = running_summary[-limit:].lstrip()
    return running_summary


def _should_use_staged_session_memory(memory_context):
    if SESSION_MEMORY_MODE != 'staged':
        return False
    if not isinstance(memory_context, dict):
        return False
    return bool(memory_context.get('campaign_id') and memory_context.get('session_id'))


def _memory_tool_result_message(tool_call, tool_name, result):
    return {
        'role': 'tool',
        'tool_call_id': tool_call.get('id'),
        'name': tool_name,
        'content': json.dumps(result, ensure_ascii=False),
    }


def _get_session_memory_patch_staged(memory_context, audit_context, telemetry):
    from services.session_memory_agent import (
        SESSION_MEMORY_TOOL_DEFINITIONS,
        compile_staged_memory_patch,
        execute_memory_tool,
    )

    campaign_id = audit_context.get('campaign_id')
    trace_id = audit_context.get('trace_id')
    trace_label = audit_context.get('trace_label')

    extractor_messages = build_session_memory_extractor_messages(memory_context)
    try:
        extracted, extractor_chars = _request_session_memory_json(
            extractor_messages,
            audit_context,
            'session_memory_extract',
            max_tokens=SESSION_MEMORY_MAX_TOKENS,
            timeout_seconds=SESSION_MEMORY_TIMEOUT_SECONDS,
        )
    except Exception as err:
        telemetry['staged_extractor_error'] = repr(err)
        return None
    if not isinstance(extracted, dict) or not str(extracted.get('running_summary') or '').strip():
        telemetry['staged_extractor_error'] = 'blank_or_invalid_extractor'
        telemetry['staged_extractor_response_chars'] = extractor_chars
        return None
    telemetry['staged_extractor_response_chars'] = extractor_chars
    if campaign_id:
        log_audit_event(
            campaign_id,
            'memory_writer_extracted',
            'Built staged memory extraction candidates.',
            {'extracted': extracted},
            source=get_llm_provider(),
            actor='session_memory_writer',
            trace_id=trace_id,
            parent_trace_id=audit_context.get('parent_trace_id'),
            trace_label=trace_label,
            audit_role='agent',
            commit=True,
        )

    messages = build_session_memory_resolver_messages(memory_context, extracted)
    tool_trace = []
    response_chain = []
    max_tool_rounds = 6
    final_payload = None
    for tool_round in range(max_tool_rounds + 1):
        data = _post_chat_response(
            messages,
            json_mode=False,
            audit_context={
                **audit_context,
                'operation': 'session_memory_resolve',
                'full_world_graph_included': False,
            },
            tools=SESSION_MEMORY_TOOL_DEFINITIONS,
            tool_choice='auto',
            parallel_tool_calls=False,
            allow_thinking=False,
        )
        response_chain.append(data)
        message, _finish_reason = _choice_message(data)
        tool_calls = message.get('tool_calls') or []
        if tool_calls and tool_round < max_tool_rounds:
            messages.append(_assistant_tool_message(message))
            for tool_call in tool_calls:
                function = tool_call.get('function') if isinstance(tool_call, dict) else {}
                tool_name = function.get('name') if isinstance(function, dict) else None
                tool_args = _parse_tool_arguments(function.get('arguments') if isinstance(function, dict) else None)
                result = execute_memory_tool(memory_context, tool_name, tool_args)
                tool_trace.append({
                    'tool_name': tool_name,
                    'args': tool_args,
                    'result': result,
                })
                if campaign_id:
                    log_audit_event(
                        campaign_id,
                        'memory_writer_tool_call',
                        f'Staged memory resolver called {tool_name}.',
                        {'tool_name': tool_name, 'args': tool_args, 'result': result},
                        source='session_memory_writer.tool',
                        actor='session_memory_writer',
                        trace_id=trace_id,
                        parent_trace_id=audit_context.get('parent_trace_id'),
                        trace_label=trace_label,
                        audit_role='tools',
                        commit=True,
                    )
                messages.append(_memory_tool_result_message(tool_call, tool_name, result))
            continue
        content = message.get('content') or ''
        try:
            final_payload = _json_object_from_text(content)
        except Exception as err:
            telemetry['staged_resolver_error'] = repr(err)
            return None
        break
    if final_payload is None:
        telemetry['staged_resolver_error'] = 'max_tool_rounds_exceeded'
        return None

    compiled = compile_staged_memory_patch(memory_context, extracted, final_payload)
    telemetry['mode'] = 'staged_memory_writer'
    telemetry['staged_tool_call_count'] = len(tool_trace)
    telemetry['staged_resolver_response_count'] = len(response_chain)
    telemetry['staged_compile_summary'] = compiled.get('compile_summary')
    if campaign_id:
        log_audit_event(
            campaign_id,
            'memory_writer_resolved',
            'Compiled staged memory patch after tool-backed resolution.',
            {
                'resolved': final_payload,
                'compile_summary': compiled.get('compile_summary'),
                'unresolved_items': compiled.get('unresolved_items') or [],
            },
            source=get_llm_provider(),
            actor='session_memory_writer',
            trace_id=trace_id,
            parent_trace_id=audit_context.get('parent_trace_id'),
            trace_label=trace_label,
            audit_role='agent',
            commit=True,
        )

    try:
        clocks_data, clocks_chars = _request_session_memory_json(
            build_session_memory_clocks_messages(memory_context),
            audit_context,
            'session_memory_update_clocks',
            max_tokens=SESSION_MEMORY_MAX_TOKENS,
            timeout_seconds=SESSION_MEMORY_TIMEOUT_SECONDS,
        )
    except Exception as err:
        clocks_data = None
        clocks_chars = 0
        telemetry['clocks_error'] = repr(err)
    telemetry['clocks_response_chars'] = clocks_chars
    if isinstance(clocks_data, dict):
        compiled['create_clocks'] = clocks_data.get('create_clocks') if isinstance(clocks_data.get('create_clocks'), list) else []
        compiled['retire_clocks'] = clocks_data.get('retire_clocks') if isinstance(clocks_data.get('retire_clocks'), list) else []
    return compiled


def _fallback_session_memory_patch(memory_context, telemetry):
    memory_context = memory_context or {}
    prior_summary = _memory_fallback_text(memory_context.get('prior_running_summary'), 1000)
    latest_player = _memory_fallback_text(
        _memory_context_lookup(memory_context, 'latest_player_message'),
        350,
    )
    latest_dm = _memory_fallback_text(
        _memory_context_lookup(memory_context, 'latest_dm_message', 'latest_dm_response'),
        900,
    )
    latest_parts = []
    if latest_player:
        latest_parts.append(f'Player: {latest_player}')
    if latest_dm:
        latest_parts.append(f'DM: {latest_dm}')
    latest_summary = ' '.join(latest_parts)
    running_summary = ' '.join(part for part in [prior_summary, latest_summary] if part).strip()
    if len(running_summary) > 1800:
        running_summary = running_summary[-1800:].lstrip()

    prior_anchors = memory_context.get('prior_memory_anchors') if isinstance(memory_context.get('prior_memory_anchors'), dict) else {}
    normalized_anchors = {
        "current_goal": prior_anchors.get("current_goal"),
        "current_scene": prior_anchors.get("current_scene"),
        "open_clues": prior_anchors.get("open_clues") if isinstance(prior_anchors.get("open_clues"), list) else [],
        "unresolved_questions": prior_anchors.get("unresolved_questions") if isinstance(prior_anchors.get("unresolved_questions"), list) else [],
        "npc_observations": prior_anchors.get("npc_observations") if isinstance(prior_anchors.get("npc_observations"), list) else [],
        "recent_offers_promises": prior_anchors.get("recent_offers_promises") if isinstance(prior_anchors.get("recent_offers_promises"), list) else []
    }

    return {
        'running_summary': running_summary,
        'memory_anchors': normalized_anchors,
        'scene_patch': _fallback_scene_patch(memory_context),
        'scene_reason': 'Fallback summary applied because the memory writer returned no visible JSON.',
        'upsert_graph_entities': [],
        'upsert_graph_relations': [],
        'upsert_graph_facts': [],
        'create_clocks': [],
        'retire_clocks': [],
        'update_npc_actors': [],
        'record_events': [],
        '_fallback': {
            'reason': 'empty_memory_writer_response',
            'mode': 'summary_and_scene_only',
        },
        '_telemetry': telemetry,
    }


def _session_memory_patch_has_substance(data):
    if not isinstance(data, dict):
        return False
    if str(data.get('running_summary') or '').strip():
        return True
    scene_patch = data.get('scene_patch')
    if isinstance(scene_patch, dict) and any(value not in (None, '', [], {}) for value in scene_patch.values()):
        return True
    anchors = data.get("memory_anchors")
    if isinstance(anchors, dict) and any(
        value not in (None, "", [], {}) for value in anchors.values()
    ):
        return True
    for key in (
        'upsert_graph_entities',
        'upsert_graph_relations',
        'upsert_graph_facts',
        'create_clocks',
        'retire_clocks',
        'update_npc_actors',
        'record_events',
    ):
        value = data.get(key)
        if isinstance(value, list) and value:
            return True
    return False


def _request_session_memory_json(messages, audit_context, operation, max_tokens, timeout_seconds):
    text = _post_chat(
        messages,
        json_mode=True,
        audit_context={
            **(audit_context or {}),
            'operation': operation,
        },
        allow_thinking=False,
        timeout_seconds=timeout_seconds,
        max_attempts=1,
        max_tokens=max_tokens,
    )
    if not isinstance(text, str) or not text.strip():
        return None, 0
    data = _json_loads_with_repair(
        text,
        audit_context={
            **(audit_context or {}),
            'operation': operation,
        },
    )
    if not isinstance(data, dict):
        return None, len(text)
    return data, len(text)


def _mark_memory_fallback_active(telemetry, audit_context):
    tracker = audit_context.get("telemetry_tracker") if isinstance(audit_context, dict) else None
    if isinstance(tracker, dict):
        tracker["fallback_active"] = True
    if isinstance(telemetry, dict):
        telemetry["fallback_active"] = True


def _get_session_memory_patch_opencode_go(memory_context, audit_context, telemetry):
    timeout_seconds = SESSION_MEMORY_TIMEOUT_SECONDS
    max_tokens = SESSION_MEMORY_MAX_TOKENS
    summary_messages = build_session_memory_summary_scene_messages(memory_context)

    try:
        summary_data, summary_chars = _request_session_memory_json(
            summary_messages,
            audit_context,
            'session_memory_update_summary_scene',
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
        )
    except Exception as err:
        telemetry['summary_scene_error'] = repr(err)
        _mark_memory_fallback_active(telemetry, audit_context)
        fallback_patch = _fallback_session_memory_patch(memory_context, telemetry)
        fallback_patch['_telemetry'] = {**telemetry, **(fallback_patch.get('_telemetry') or {})}
        _compile_telemetry_summary(fallback_patch['_telemetry'], audit_context)
        return fallback_patch

    if not isinstance(summary_data, dict) or not str(summary_data.get('turn_summary') or '').strip():
        try:
            summary_data, summary_chars = _request_session_memory_json(
                _session_memory_summary_scene_retry_messages(summary_messages),
                audit_context,
                'session_memory_update_summary_scene_retry',
                max_tokens=max_tokens,
                timeout_seconds=timeout_seconds,
            )
            telemetry['summary_scene_retry'] = True
        except Exception as err:
            telemetry['summary_scene_retry_error'] = repr(err)

    if not isinstance(summary_data, dict) or not str(summary_data.get('turn_summary') or '').strip():
        telemetry['summary_scene_error'] = 'blank_or_invalid_summary_scene'
        telemetry['summary_scene_response_chars'] = summary_chars
        _mark_memory_fallback_active(telemetry, audit_context)
        fallback_patch = _fallback_session_memory_patch(memory_context, telemetry)
        fallback_patch['_telemetry'] = {**telemetry, **(fallback_patch.get('_telemetry') or {})}
        _compile_telemetry_summary(fallback_patch['_telemetry'], audit_context)
        return fallback_patch

    telemetry['summary_scene_response_chars'] = summary_chars
    scene_patch = _sanitize_scene_patch(
        summary_data.get('scene_patch') if isinstance(summary_data.get('scene_patch'), dict) else {},
        memory_context,
    )

    try:
        facts_data, facts_chars = _request_session_memory_json(
            build_session_memory_facts_messages(memory_context),
            audit_context,
            'session_memory_update_facts',
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
        )
    except Exception as err:
        facts_data = None
        facts_chars = 0
        telemetry['facts_error'] = repr(err)

    try:
        clocks_data, clocks_chars = _request_session_memory_json(
            build_session_memory_clocks_messages(memory_context),
            audit_context,
            'session_memory_update_clocks',
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
        )
    except Exception as err:
        clocks_data = None
        clocks_chars = 0
        telemetry['clocks_error'] = repr(err)

    telemetry['facts_response_chars'] = facts_chars
    telemetry['clocks_response_chars'] = clocks_chars

    facts = facts_data.get('upsert_graph_facts') if isinstance(facts_data, dict) and isinstance(facts_data.get('upsert_graph_facts'), list) else []
    create_clocks = clocks_data.get('create_clocks') if isinstance(clocks_data, dict) and isinstance(clocks_data.get('create_clocks'), list) else []
    retire_clocks = clocks_data.get('retire_clocks') if isinstance(clocks_data, dict) and isinstance(clocks_data.get('retire_clocks'), list) else []

    return {
        'running_summary': _merge_session_running_summary(
            memory_context.get('prior_running_summary'),
            summary_data.get('turn_summary'),
        ),
        'scene_patch': scene_patch,
        'scene_reason': 'Session memory summary/scene pass updated the active scene state.',
        'upsert_graph_entities': [],
        'upsert_graph_relations': [],
        'upsert_graph_facts': facts,
        'create_clocks': create_clocks,
        'retire_clocks': retire_clocks,
        'update_npc_actors': [],
        'record_events': [],
        '_telemetry': telemetry,
    }


def _compile_telemetry_summary(telemetry, audit_context):
    tracker = audit_context.get('telemetry_tracker') if isinstance(audit_context, dict) else None
    if not isinstance(tracker, dict):
        tracker = {
            'status': 'success',
            'provider_retries': 0,
            'parse_repairs': 0,
            'guard_retries': 0,
            'fallback_active': False,
            'failure_category': None,
            'warnings': {
                'scene_mutation_rejected': 0,
                'unresolved_scene_references': 0
            }
        }

    # If telemetry has any errors, override status/failure_category
    err_str = ""
    for k in ('error', 'staged_extractor_error', 'staged_resolver_error', 'summary_scene_error', 'clocks_error'):
        if telemetry.get(k):
            err_str = str(telemetry.get(k))
            break

    if err_str:
        if 'JSONDecodeError' in err_str or 'blank_or_invalid' in err_str:
            tracker['status'] = 'parser_failure'
            tracker['failure_category'] = 'parser'
        elif 'empty_response' in err_str or 'empty_patch' in err_str:
            tracker['status'] = 'model_output_failure'
            tracker['failure_category'] = 'model'
        else:
            tracker['status'] = 'provider_failure'
            tracker['failure_category'] = 'provider'

    if telemetry.get('fallback_active') or tracker.get('fallback_active'):
        tracker['fallback_active'] = True
        if tracker.get('status', 'success') == 'success':
            tracker['status'] = 'partial_fallback'

    telemetry_summary = {
        'status': tracker.get('status', 'success'),
        'provider_retries': tracker.get('provider_retries', 0),
        'parse_repairs': tracker.get('parse_repairs', 0),
        'guard_retries': tracker.get('guard_retries', 0),
        'fallback_active': tracker.get('fallback_active', False),
        'failure_category': tracker.get('failure_category'),
        'warnings': tracker.get('warnings', {
            'scene_mutation_rejected': 0,
            'unresolved_scene_references': 0
        })
    }
    telemetry['telemetry_summary'] = telemetry_summary
    return telemetry_summary


def get_session_memory_patch(memory_context, audit_context=None):
    messages = build_session_memory_messages(memory_context)
    if not isinstance(audit_context, dict):
        audit_context = {}
    tracker = {
        'status': 'success',
        'provider_retries': 0,
        'parse_repairs': 0,
        'guard_retries': 0,
        'fallback_active': False,
        'failure_category': None,
        'warnings': {
            'scene_mutation_rejected': 0,
            'unresolved_scene_references': 0
        }
    }
    audit_context['telemetry_tracker'] = tracker

    provider = get_llm_provider()
    campaign_id = audit_context.get('campaign_id')
    trace_id = audit_context.get('trace_id') or f"session_memory_writer:memory_update:{uuid4().hex[:10]}"
    trace_label = audit_context.get('trace_label') or 'session_memory_writer: memory_update'

    prompt_str = json.dumps(messages, ensure_ascii=False)
    prompt_chars = len(prompt_str)
    prompt_tokens_estimate = prompt_chars // 4

    context_breakdown = {}
    if isinstance(memory_context, dict):
        for k, v in memory_context.items():
            try:
                context_breakdown[k] = len(json.dumps(v, ensure_ascii=False))
            except Exception:
                context_breakdown[k] = 0

    if campaign_id:
        log_audit_event(
            campaign_id,
            'memory_writer_request',
            'Requested post-turn session memory update.',
            {'context': memory_context, 'messages': messages},
            source=provider,
            actor='session_memory_writer',
            trace_id=trace_id,
            parent_trace_id=audit_context.get('parent_trace_id'),
            trace_label=trace_label,
            audit_role='tools',
            commit=True,
        )
    if _should_use_staged_session_memory(memory_context):
        telemetry = {
            'prompt_chars': prompt_chars,
            'prompt_tokens_estimate': prompt_tokens_estimate,
            'context_breakdown': context_breakdown,
        }
        patch = _get_session_memory_patch_staged(
            memory_context,
            {
                **audit_context,
                'trace_id': trace_id,
                'trace_label': trace_label,
                'actor': 'session_memory_writer',
                'full_world_graph_included': False,
            },
            telemetry,
        )
        if patch and campaign_id:
            log_audit_event(
                campaign_id,
                'memory_writer_response',
                'Received staged post-turn session memory patch.',
                {'patch': patch},
                source=provider,
                actor='session_memory_writer',
                trace_id=trace_id,
                parent_trace_id=audit_context.get('parent_trace_id'),
                trace_label=trace_label,
                audit_role='agent',
                commit=True,
            )
        if patch:
            patch['_telemetry'] = {**telemetry, **(patch.get('_telemetry') or {})}
            _compile_telemetry_summary(patch['_telemetry'], audit_context)
            return patch
    if provider == 'opencode_go':
        telemetry = {
            'prompt_chars': prompt_chars,
            'prompt_tokens_estimate': prompt_tokens_estimate,
            'context_breakdown': context_breakdown,
            'mode': 'split_opencode_go_memory_writer',
        }
        patch = _get_session_memory_patch_opencode_go(
            memory_context,
            {
                **audit_context,
                'trace_id': trace_id,
                'trace_label': trace_label,
                'actor': 'session_memory_writer',
                'full_world_graph_included': False,
            },
            telemetry,
        )
        if campaign_id:
            log_audit_event(
                campaign_id,
                'memory_writer_response',
                'Received post-turn session memory patch.',
                {'patch': patch},
                source=provider,
                actor='session_memory_writer',
                trace_id=trace_id,
                parent_trace_id=audit_context.get('parent_trace_id'),
                trace_label=trace_label,
                audit_role='agent',
                commit=True,
            )
        if patch:
            patch['_telemetry'] = {**telemetry, **(patch.get('_telemetry') or {})}
            _compile_telemetry_summary(patch['_telemetry'], audit_context)
        return patch
    try:
        base_operation = audit_context.get('operation') or 'session_memory_update'
        request_audit_context = {
            **audit_context,
            'trace_id': trace_id,
            'trace_label': trace_label,
            'operation': base_operation,
            'actor': 'session_memory_writer',
            'full_world_graph_included': False,
        }

        def request_patch_text(request_messages, operation_suffix=None):
            request_context = dict(request_audit_context)
            if operation_suffix:
                request_context['operation'] = f'{base_operation}_{operation_suffix}'
            response_text = _post_chat(
                request_messages,
                json_mode=True,
                audit_context=request_context,
                allow_thinking=False,
                timeout_seconds=SESSION_MEMORY_TIMEOUT_SECONDS,
                max_attempts=SESSION_MEMORY_MAX_ATTEMPTS,
                max_tokens=SESSION_MEMORY_MAX_TOKENS,
            )
            return response_text, len(response_text) if response_text else 0

        retry_suffix = None
        text, response_chars = request_patch_text(messages)
        if not text or not text.strip():
            retry_suffix = 'blank_retry'
            text, response_chars = request_patch_text(
                _session_memory_retry_messages(messages, 'blank_response'),
                operation_suffix=retry_suffix,
            )
        if not text or not text.strip():
            telemetry = {
                'prompt_chars': prompt_chars,
                'prompt_tokens_estimate': prompt_tokens_estimate,
                'response_chars': response_chars,
                'context_breakdown': context_breakdown,
                'error': 'empty_response',
                'retry_suffix': retry_suffix,
            }
            if campaign_id:
                log_audit_event(
                    campaign_id,
                    'memory_writer_empty',
                    'Post-turn session memory writer returned an empty patch.',
                    {'_telemetry': telemetry},
                    source=provider,
                    actor='session_memory_writer',
                    trace_id=trace_id,
                    parent_trace_id=audit_context.get('parent_trace_id'),
                    trace_label=trace_label,
                    audit_role='tools',
                    commit=True,
                )
            fallback_patch = _fallback_session_memory_patch(memory_context, telemetry)
            if campaign_id:
                log_audit_event(
                    campaign_id,
                    'memory_writer_fallback',
                    'Applied deterministic summary-only fallback after empty memory writer response.',
                    {'patch': fallback_patch},
                    source=provider,
                    actor='session_memory_writer',
                    trace_id=trace_id,
                    parent_trace_id=audit_context.get('parent_trace_id'),
                    trace_label=trace_label,
                    audit_role='tools',
                    commit=True,
                )
            tracker = audit_context.get("telemetry_tracker")
            if isinstance(tracker, dict):
                tracker["fallback_active"] = True
            telemetry["fallback_active"] = True
            fallback_patch['_telemetry'] = {**telemetry, **(fallback_patch.get('_telemetry') or {})}
            _compile_telemetry_summary(fallback_patch['_telemetry'], audit_context)
            return fallback_patch
        data = _json_loads_with_repair(text, audit_context=request_audit_context)
        if not isinstance(data, dict):
            data = {}
        if isinstance(data.get('scene_patch'), dict):
            data['scene_patch'] = _sanitize_scene_patch(data['scene_patch'], memory_context)

        if not _session_memory_patch_has_substance(data):
            retry_suffix = 'empty_patch_retry'
            tracker = audit_context.get('telemetry_tracker')
            if isinstance(tracker, dict):
                tracker['guard_retries'] = tracker.get('guard_retries', 0) + 1
            retry_text, retry_response_chars = request_patch_text(
                _session_memory_retry_messages(messages, 'empty_patch'),
                operation_suffix=retry_suffix,
            )
            if retry_text and retry_text.strip():
                text = retry_text
                response_chars = retry_response_chars
                data = _json_loads_with_repair(
                    text,
                    audit_context={
                        **request_audit_context,
                        'operation': f'{base_operation}_{retry_suffix}',
                    },
                )
                if not isinstance(data, dict):
                    data = {}
                if isinstance(data.get('scene_patch'), dict):
                    data['scene_patch'] = _sanitize_scene_patch(data['scene_patch'], memory_context)

        if not _session_memory_patch_has_substance(data):
            telemetry = {
                'prompt_chars': prompt_chars,
                'prompt_tokens_estimate': prompt_tokens_estimate,
                'response_chars': response_chars,
                'context_breakdown': context_breakdown,
                'error': 'empty_patch',
                'retry_suffix': retry_suffix,
                'patch_preview': data,
            }
            if campaign_id:
                log_audit_event(
                    campaign_id,
                    'memory_writer_empty_patch',
                    'Post-turn session memory writer returned an empty or no-op patch.',
                    {'patch': data, '_telemetry': telemetry},
                    source=provider,
                    actor='session_memory_writer',
                    trace_id=trace_id,
                    parent_trace_id=audit_context.get('parent_trace_id'),
                    trace_label=trace_label,
                    audit_role='tools',
                    commit=True,
                )
            fallback_patch = _fallback_session_memory_patch(memory_context, telemetry)
            if campaign_id:
                log_audit_event(
                    campaign_id,
                    'memory_writer_fallback',
                    'Applied deterministic summary-only fallback after empty or no-op memory writer patch.',
                    {'patch': fallback_patch},
                    source=provider,
                    actor='session_memory_writer',
                    trace_id=trace_id,
                    parent_trace_id=audit_context.get('parent_trace_id'),
                    trace_label=trace_label,
                    audit_role='tools',
                    commit=True,
                )
            tracker = audit_context.get("telemetry_tracker")
            if isinstance(tracker, dict):
                tracker["fallback_active"] = True
            telemetry["fallback_active"] = True
            fallback_patch['_telemetry'] = {**telemetry, **(fallback_patch.get('_telemetry') or {})}
            _compile_telemetry_summary(fallback_patch['_telemetry'], audit_context)
            return fallback_patch

        data['_telemetry'] = {
            'prompt_chars': prompt_chars,
            'prompt_tokens_estimate': prompt_tokens_estimate,
            'response_chars': response_chars,
            'context_breakdown': context_breakdown,
            'retry_suffix': retry_suffix,
        }

        if campaign_id:
            log_audit_event(
                campaign_id,
                'memory_writer_response',
                'Received post-turn session memory patch.',
                {'patch': data},
                source=provider,
                actor='session_memory_writer',
                trace_id=trace_id,
                parent_trace_id=audit_context.get('parent_trace_id'),
                trace_label=trace_label,
                audit_role='agent',
                commit=True,
            )
        _compile_telemetry_summary(data['_telemetry'], audit_context)
        return data
    except Exception as e:
        print(f'[openrouter] Session memory writer error: {e}')
        telemetry = {
            'prompt_chars': prompt_chars,
            'prompt_tokens_estimate': prompt_tokens_estimate,
            'response_chars': 0,
            'context_breakdown': context_breakdown,
            'error': str(e),
        }
        fallback_patch = _fallback_session_memory_patch(memory_context, telemetry)
        fallback_patch['_fallback']['reason'] = 'memory_writer_model_error'
        tracker = audit_context.get("telemetry_tracker")
        if isinstance(tracker, dict):
            tracker["fallback_active"] = True
        telemetry["fallback_active"] = True
        fallback_patch['_telemetry'] = {**telemetry, **(fallback_patch.get('_telemetry') or {})}
        _compile_telemetry_summary(fallback_patch['_telemetry'], audit_context)
        return fallback_patch


def get_session_clock_updates(clock_context, audit_context=None):
    messages = build_session_clock_adjudication_messages(clock_context or {})
    audit_context = audit_context or {}
    provider = get_llm_provider()
    campaign_id = audit_context.get('campaign_id')
    trace_id = audit_context.get('trace_id') or f"session_clock_adjudicator:clock_update:{uuid4().hex[:10]}"
    trace_label = audit_context.get('trace_label') or 'session_clock_adjudicator: clock_update'

    if campaign_id:
        log_audit_event(
            campaign_id,
            'clock_adjudicator_request',
            'Requested post-turn clock adjudication.',
            {'context': clock_context, 'messages': messages},
            source=provider,
            actor='session_clock_adjudicator',
            trace_id=trace_id,
            parent_trace_id=audit_context.get('parent_trace_id'),
            trace_label=trace_label,
            audit_role='tools',
            commit=True,
        )

    request_audit = {
        **audit_context,
        'trace_id': trace_id,
        'trace_label': trace_label,
        'actor': 'session_clock_adjudicator',
        'full_world_graph_included': False,
    }

    try:
        if provider == 'opencode_go':
            data, _response_chars = _request_session_memory_json(
                messages,
                request_audit,
                'session_clock_adjudication',
                max_tokens=SESSION_MEMORY_MAX_TOKENS,
                timeout_seconds=SESSION_MEMORY_TIMEOUT_SECONDS,
            )
        else:
            text = _post_chat(
                messages,
                json_mode=True,
                audit_context={
                    **request_audit,
                    'operation': 'session_clock_adjudication',
                },
                allow_thinking=False,
                timeout_seconds=SESSION_MEMORY_TIMEOUT_SECONDS,
                max_attempts=1,
                max_tokens=SESSION_MEMORY_MAX_TOKENS,
            )
            data = _json_loads_with_repair(
                text,
                audit_context={
                    **request_audit,
                    'operation': 'session_clock_adjudication',
                },
            ) if text else None
    except Exception as err:
        if campaign_id:
            log_audit_event(
                campaign_id,
                'clock_adjudicator_error',
                'Post-turn clock adjudication failed.',
                {'error': repr(err)},
                source=provider,
                actor='session_clock_adjudicator',
                trace_id=trace_id,
                parent_trace_id=audit_context.get('parent_trace_id'),
                trace_label=trace_label,
                audit_role='tools',
                commit=True,
            )
        data = None

    result = {
        'create_clocks': data.get('create_clocks') if isinstance(data, dict) and isinstance(data.get('create_clocks'), list) else [],
        'advance_clocks': data.get('advance_clocks') if isinstance(data, dict) and isinstance(data.get('advance_clocks'), list) else [],
        'retire_clocks': data.get('retire_clocks') if isinstance(data, dict) and isinstance(data.get('retire_clocks'), list) else [],
        'no_change_explanations': (
            data.get('no_change_explanations')
            if isinstance(data, dict) and isinstance(data.get('no_change_explanations'), list)
            else []
        ),
    }

    if campaign_id:
        log_audit_event(
            campaign_id,
            'clock_adjudicator_response',
            'Received post-turn clock adjudication.',
            {'updates': result},
            source=provider,
            actor='session_clock_adjudicator',
            trace_id=trace_id,
            parent_trace_id=audit_context.get('parent_trace_id'),
            trace_label=trace_label,
            audit_role='agent',
            commit=True,
        )
    return result


def get_character_sheet_answer(question, scope, character_sheets, audit_context=None):
    if not character_sheets:
        return {
            'answer': 'No matching character sheet was found.',
            'character_ids': [],
            'missing': True,
        }

    base_audit = audit_context or {}
    agent_audit = _child_audit_context(
        base_audit,
        'character_sheet_answer',
        'character_sheet_agent',
        'character_sheet_agent: sheet answer',
    )
    messages = build_character_sheet_agent_messages(question, scope, character_sheets)

    try:
        data = _json_loads_with_repair(
            _post_chat(messages, json_mode=True, audit_context=agent_audit),
            audit_context=agent_audit,
        )
    except Exception as err:
        print(f'[openrouter] Character sheet agent error: {err}')
        return {
            'answer': 'Character sheet lookup failed.',
            'character_ids': [],
            'missing': True,
        }

    if not isinstance(data, dict):
        return {
            'answer': 'Character sheet lookup returned an invalid response.',
            'character_ids': [],
            'missing': True,
        }

    ids = data.get('character_ids')
    if not isinstance(ids, list):
        ids = []
    return {
        'answer': str(data.get('answer') or '').strip() or 'No answer was returned from the character sheet.',
        'character_ids': [item for item in ids if isinstance(item, int)],
        'missing': bool(data.get('missing')),
    }


def get_loot_generation_response(generation_context):
    from services.lootbox_service import _build_loot_generation_messages

    messages = _build_loot_generation_messages(generation_context)
    try:
        data = _json_loads_with_repair(_post_chat(messages, json_mode=True))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def get_shop_menu_generation_response(generation_context, audit_context=None):
    shop = generation_context.get('shop') or {}
    scene = generation_context.get('current_scene') or {}
    campaign = generation_context.get('campaign') or {}
    item_count = shop.get('item_count') or 6
    messages = [
        {
            'role': 'system',
            'content': SHOP_MENU_GENERATION_SYSTEM_PROMPT,
        },
        {
            'role': 'user',
            'content': (
                'Create the item list for this single shop.\n\n'
                f'Campaign: {json.dumps(campaign, ensure_ascii=False)}\n'
                f'Current scene: {json.dumps(scene, ensure_ascii=False)}\n'
                f'Shop summary: {json.dumps(shop, ensure_ascii=False)}\n\n'
                f'Return exactly {item_count} items unless the shop concept clearly needs fewer.\n'
                'Return only JSON matching this shape:\n'
                '{\n'
                '  "items": [\n'
                '    {"name": "Item Name", "description": "1 concise sentence", "cost_gp": 5, "quantity": 3}\n'
                '  ]\n'
                '}\n'
                'Use quantity null for common unlimited goods. Use integer quantities for limited stock.'
            ),
        },
    ]
    try:
        data = _json_loads_with_repair(_post_chat(messages, json_mode=True, audit_context=audit_context))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def get_planning_dm_response(context, current_user_messages, draft_character=None, active_page=None, audit_context=None):
    messages = build_planning_dm_messages(context, current_user_messages, draft_character, active_page)

    try:
        text = _post_chat(messages, json_mode=True, audit_context=audit_context)
        result = _planning_result_from_text(text, audit_context=audit_context)
        if result:
            return result

        retry_text = _post_chat(
            _planning_blank_retry_messages(messages),
            json_mode=True,
            audit_context={
                **(audit_context or {}),
                'operation': f"{(audit_context or {}).get('operation') or 'planning_dm_response'}_blank_retry",
            },
            allow_thinking=False,
        )
        return _planning_result_from_text(retry_text, audit_context=audit_context)
    except Exception as e:
        print(f'[openrouter] Planning DM error: {e}')
        return None


def get_planning_dm_response_streaming(context, current_user_messages, draft_character=None, active_page=None, audit_context=None, on_token=None):
    """Streaming variant of get_planning_dm_response.

    Calls on_token(delta_text) for each text chunk from the LLM.
    The stream contains raw JSON text including the 'message' value —
    the caller is responsible for extracting visible content from the
    streaming tokens (the planning_stream worker handles this).

    Returns the final parsed result dict (same shape as get_planning_dm_response).
    """
    messages = build_planning_dm_messages(context, current_user_messages, draft_character, active_page)

    try:
        text = _post_chat_stream(
            messages,
            json_mode=True,
            audit_context=audit_context,
            on_token=on_token,
        )
        result = _planning_result_from_text(text, audit_context=audit_context)
        if result:
            return result

        retry_text = _post_chat(
            _planning_blank_retry_messages(messages),
            json_mode=True,
            audit_context={
                **(audit_context or {}),
                'operation': f"{(audit_context or {}).get('operation') or 'planning_dm_response'}_blank_retry",
            },
            allow_thinking=False,
        )
        return _planning_result_from_text(retry_text, audit_context=audit_context)
    except Exception as e:
        print(f'[openrouter] Planning DM streaming error: {e}')
        return None


def get_planning_summary_update(context, latest_player_message, latest_dm_message, audit_context=None):
    messages = build_planning_summary_messages(context, latest_player_message, latest_dm_message)

    try:
        data = _json_loads_with_repair(
            _post_chat(messages, json_mode=True, audit_context=audit_context),
            audit_context=audit_context,
        )
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f'[openrouter] Planning summary error: {e}')
        return {}


def get_world_genesis_package(context, audit_context=None):
    base_audit = audit_context or {}
    trace_id = base_audit.get('trace_id') or f"world_architect:world_genesis:{uuid4().hex[:10]}"
    trace_label = base_audit.get('trace_label') or 'world_architect: world_genesis'
    messages = build_world_genesis_section_seed_messages(context)
    package = {}

    try:
        for section_name, return_shape in WORLD_GENESIS_SECTION_SPECS:
            section_audit_context = {
                **base_audit,
                'operation': f'world_genesis_{section_name}',
                'actor': base_audit.get('actor') or 'world_architect',
                'trace_id': f'{trace_id}:{section_name}',
                'parent_trace_id': trace_id,
                'trace_label': f'{trace_label}: {section_name}',
            }
            messages.append(build_world_genesis_section_prompt(section_name, return_shape))
            text = _post_chat(
                list(messages),
                json_mode=True,
                audit_context=section_audit_context,
            )
            data = _json_loads_with_repair(text, audit_context=section_audit_context)
            section_payload = _coerce_world_genesis_section(section_name, data)
            package.update(section_payload)
            messages.append({
                'role': 'assistant',
                'content': json.dumps(section_payload, ensure_ascii=False),
            })

        return package
    except Exception as e:
        print(f'[openrouter] World genesis error: {e}')
        return {}

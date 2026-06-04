import os
import json
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
    int(os.environ.get('SESSION_PREFLIGHT_MAX_TOKENS', '160')),
)

PC_CONTROL_POLICY = (
    "Player-character control policy: NPCs may speak and act. Player characters are protected. "
    "Do not write exact dialogue for any player character unless that player supplied the exact words. "
    "Do not narrate another player's PC actions, gestures, thoughts, emotions, decisions, or answers. "
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
    + PC_CONTROL_POLICY + " "
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
    "protected_player_characters and current_player_character; obey those boundaries exactly. "
    "For the final turn decision, return only valid JSON. Use {\"mode\":\"speak\",\"content\":\"...\"} "
    "when the DM should send a visible reply. Use {\"mode\":\"silent\",\"reason\":\"...\"} when the latest "
    "message is only PC-to-PC conversation or waiting on another PC and the DM should not send anything. "
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
    "Extract durable changes from the latest player message and visible DM reply. Create new clocks "
    "when new pressure, deadlines, mysteries, faction moves, or consequences emerge, especially if all "
    "existing active clocks are completed. Retire completed or resolved clocks instead of deleting them. "
    "Update knowledge graph entities, relations, and facts without duplicating existing ids. Preserve "
    "visibility as dm_private, party_known, or public. Do not invent large new lore unless it follows "
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

SESSION_SPOILER_CHECK_SYSTEM_PROMPT = (
    "You are a spoiler-safety checker for visible Dungeon Master replies. "
    "Return only valid JSON. Decide whether the candidate visible DM reply directly reveals or strongly implies "
    "any unrevealed private item. A reply is unsafe when a reasonable player could learn the hidden truth from "
    "the reply itself. Ordinary foreshadowing, mood, uncertainty, and clues that do not effectively answer the "
    "hidden truth are safe. Do not mark a reply unsafe merely because it is thematically related to a private item. "
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
    "to a specific NPC or named non-player speaker without the required <npc> wrapper."
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
):
    audit_context = audit_context or {}
    campaign_id = audit_context.get('campaign_id')
    operation = audit_context.get('operation') or 'chat_completion'
    actor = audit_context.get('actor') or 'dm'
    trace_id = audit_context.get('trace_id') or f'{actor}:{operation}:{uuid4().hex[:10]}'
    parent_trace_id = audit_context.get('parent_trace_id')
    trace_label = audit_context.get('trace_label') or f'{actor}: {operation}'

    provider = get_llm_provider()
    model = get_llm_model()
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


def _json_loads_with_repair(text, audit_context=None):
    data, error, candidate = _json_loads_with_error(text)
    if error is None or not isinstance(error, json.JSONDecodeError):
        return data

    audit_context = audit_context or {}
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
    }


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


def _session_dm_json_contract_violation(raw_content):
    if _looks_like_provider_tool_markup(raw_content):
        return {
            'kind': 'provider_tool_markup',
            'detail': 'Session DM final answer contained provider tool-call markup instead of a visible JSON reply.',
        }

    data = _json_loads_or_empty(raw_content)
    if not isinstance(data, dict) or not data:
        return {
            'kind': 'non_json_response',
            'detail': 'Session DM final answer must be a JSON object with mode/content or mode/reason.',
        }

    mode = str(data.get('mode') or data.get('action') or '').strip().lower()
    if mode in {'silent', 'no_response', 'no_dm_response', 'none'}:
        return None
    if mode and mode != 'speak':
        return {
            'kind': 'invalid_mode',
            'detail': 'Session DM mode must be "speak" or "silent".',
            'mode': mode,
        }
    content_candidate = data.get('content')
    if content_candidate is None:
        content_candidate = data.get('message')
    if content_candidate is None:
        content_candidate = data.get('visible_message')
    if content_candidate is None:
        return {
            'kind': 'missing_content',
            'detail': 'Session DM speak decisions must include content, message, or visible_message.',
        }
    if mode != 'silent' and not str(content_candidate).strip():
        return {
            'kind': 'empty_content',
            'detail': 'Session DM speak decisions must include non-empty visible content.',
        }
    return None


def _session_dm_guard_retry_system_prompt(guard_name, details):
    silent_ack = ' Acknowledge these instructions silently and follow them. Do not output an acknowledgement.'
    if guard_name == 'json_contract':
        if isinstance(details, dict) and details.get('kind') == 'provider_tool_markup':
            return (
                'Guard reminder: generate a player-visible DM reply only. '
                'Do not output DSML, <｜｜DSML｜｜tool_calls>, invoke tags, XML, or any tool-call markup in visible content. '
                'The final answer must be exactly one JSON object only, with either '
                '{"mode":"speak","content":"..."} or {"mode":"silent","reason":"..."}. '
                'If you need to narrate what the player learns, write only the player-facing result in content.'
                + silent_ack
            )
        return (
            'Guard reminder: return exactly one JSON object only, with either '
            '{"mode":"speak","content":"..."} or {"mode":"silent","reason":"..."}. '
            'Do not output markdown fences, explanations, meta-commentary, or text outside the JSON object. '
            'Use mode="silent" only when no visible DM response is actually needed.'
            + silent_ack
        )
    if guard_name == 'format':
        return (
            'Guard reminder: use valid visible-message syntax. '
            'Return exactly one JSON object in the final contract. If mode="speak", content may use Markdown and '
            'plain text. The only allowed angle-bracket tag is <npc target="NPC name">...</npc>. '
            'Do not use <ic>, <ooc>, HTML, XML, or invalid closing tags.'
            + silent_ack
        )
    if guard_name == 'missing_npc_tag':
        return (
            'Guard reminder: if you include clearly attributed NPC speech or performed utterances, use the required '
            '<npc> wrapper. '
            'If you include clearly attributed current NPC spoken lines or performed utterances, wrap them in '
            '<npc target="NPC name">...</npc>, and leave narration outside the tag. '
            'Do not use any other angle-bracket tags. Return exactly one JSON object in the final contract.'
            + silent_ack
        )
    if guard_name == 'mechanical_resolution':
        return (
            'Guard reminder: do not resolve uncertain combat outcomes before the required D&D mechanics. '
            'Return exactly one JSON object in the final contract. If mode="speak", ask for the required roll or '
            'initiative instead of narrating hit/miss/damage/outcome.'
            + silent_ack
        )
    if guard_name == 'pc_control':
        return (
            'Guard reminder: do not control a protected player character. '
            'Do not write dialogue, actions, gestures, thoughts, emotions, or decisions for any PC. '
            'If no DM adjudication is needed (for example PC-to-PC exchange), return '
            '{"mode":"silent","reason":"PC-to-PC exchange."}. Otherwise return mode="speak" with only safe DM-visible '
            'content. Return exactly one JSON object in the final contract.'
            + silent_ack
        )
    if guard_name == 'private_output':
        terms = ', '.join(details.get('matched_terms') or []) if isinstance(details, dict) else ''
        return (
            'Guard reminder: do not expose DM-private information that has not become visible through '
            f'play. Do not mention these private terms in the visible reply: {terms or "(none listed)"}. '
            'Return exactly one JSON object in the final contract with spoiler-safe visible content.'
            + silent_ack
        )
    if guard_name == 'spoiler_checker':
        return (
            'Guard reminder: keep the visible reply spoiler-safe. '
            'Keep only what players could currently observe or reasonably know in-world. '
            'Return exactly one JSON object in the final contract with spoiler-safe visible content.'
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
    return [
        {'role': 'system', 'content': SESSION_TOOL_PROMPT},
        {
            'role': 'system',
            'content': 'Compact hot context. Full campaign memory is available through tools, not preloaded:\n'
            + json.dumps(prompt_context, ensure_ascii=False),
        },
    ]


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
            'outcome should not be narrated until a D&D roll or initiative step happens.'
        ),
        'return_shape': {
            'dm_reply_mode': 'silent | ooc_only | mechanics_only | clarification_only | simple_narrative | narrative | unknown',
            'skip_spoiler_check': False,
            'main_call_thinking': True,
            'latest_player_intent_requires_mechanics': False,
            'required_mechanic': 'short label such as attack roll, initiative, contested check, saving throw, or empty string',
            'confidence': 'high | medium | low',
            'reason': 'short explanation',
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

    return {
        'dm_reply_mode': mode,
        'skip_spoiler_check': skip_spoiler_check,
        'main_call_thinking': main_call_thinking,
        'latest_player_intent_requires_mechanics': data.get('latest_player_intent_requires_mechanics') is True,
        'required_mechanic': str(data.get('required_mechanic') or '').strip()[:80],
        'confidence': confidence,
        'reason': str(data.get('reason') or '').strip(),
    }


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


def _pc_control_violation(response_text, hot_context):
    import re

    visible = _strip_npc_blocks(response_text)
    protected = hot_context.get('protected_player_characters') or []
    action_verbs = (
        'nods', 'nod', 'glances', 'glance', 'looks', 'look', 'turns', 'turn', 'steps', 'step',
        'moves', 'move', 'smiles', 'smile', 'frowns', 'frown', 'laughs', 'laugh', 'sighs',
        'sigh', 'shrugs', 'shrug', 'reaches', 'reach', 'draws', 'draw', 'takes', 'take',
        'gives', 'give', 'straightens', 'straighten', 'waits', 'wait',
    )
    speech_verbs = (
        'says', 'say', 'asks', 'ask', 'replies', 'reply', 'responds', 'respond', 'whispers',
        'whisper', 'mutters', 'mutter', 'shouts', 'shout', 'calls', 'call', 'answers', 'answer',
    )

    for character in protected:
        name = (character.get('name') or '').strip()
        if not name:
            continue
        escaped = re.escape(name)
        first = re.escape(name.split()[0])
        name_pattern = f'(?:{escaped}|{first})'
        handoff_pattern = rf'\b{name_pattern}\s*,\s+how do you respond\?'
        visible_for_character = re.sub(handoff_pattern, '', visible, flags=re.IGNORECASE)
        checks = [
            rf'\*\*{name_pattern}\b[^*]*:\*\*',
            rf'<npc\s+target=["\']{escaped}["\']',
            rf'\b{name_pattern}\b[^.!?\n]{{0,80}}\b(?:{"|".join(speech_verbs)})\b',
            rf'\b{name_pattern}(?:[’\']s)?\b[^.!?\n]{{0,80}}\b(?:{"|".join(action_verbs)})\b',
            rf'\b{name_pattern}[’\']s\s+(?:eyes|expression|face|voice|smirk|smile|shoulders|hands)\b',
        ]
        for pattern in checks:
            if re.search(pattern, visible_for_character, flags=re.IGNORECASE):
                return {
                    'character': character,
                    'pattern': pattern,
                }

    return None


def _private_output_violation(response_text, hot_context):
    import re

    visible = _strip_npc_blocks(response_text)
    matched_terms = []
    for term in hot_context.get('private_output_terms') or []:
        candidate = str(term or '').strip()
        if len(candidate) < 4:
            continue
        if re.search(re.escape(candidate), visible, flags=re.IGNORECASE):
            matched_terms.append(candidate)
    if matched_terms:
        return {'matched_terms': matched_terms}
    return None


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


def _session_dm_format_feedback(format_violation):
    errors = format_violation.get('errors') if isinstance(format_violation, dict) else []
    lines = []
    for error in errors[:6]:
        snippet = str(error.get('snippet') or '').strip()
        detail = str(error.get('detail') or '').strip()
        if snippet and detail:
            lines.append(f'- {detail} Problem tag: {snippet}')
        elif detail:
            lines.append(f'- {detail}')
    if not lines:
        lines.append('- The visible reply contains malformed or disallowed visible-message syntax.')
    return '\n'.join(lines)


def _possible_missing_npc_tag_signal(response_text):
    import re

    text = response_text or ''
    if '<npc' in text.lower():
        return None

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
        r'mutters?|answers?|calls?))\b',
        flags=re.DOTALL,
    )
    ignored_speakers = {'I', 'We', 'You', 'It', 'This', 'That', 'The'}

    for match in quoted_dialogue_pattern.finditer(text):
        prefix = match.group('prefix') or ''
        speaker_match = speaker_cue_pattern.search(prefix)
        if not speaker_match:
            continue
        speaker = str(speaker_match.group('name') or '').strip()
        if not speaker or speaker in ignored_speakers:
            continue
        return {
            'speaker': speaker,
            'quote': ' '.join(str(match.group('quote') or '').split())[:240],
            'context': ' '.join(prefix.split())[:240],
        }
    return None


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


def build_session_npc_tag_check_messages(response_text, signal):
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




def _spoiler_rewrite_feedback(spoiler_check):
    evidence = spoiler_check.get('evidence') if isinstance(spoiler_check, dict) else []
    snippets = [str(item).strip() for item in evidence or [] if str(item).strip()]
    if snippets:
        quoted = '\n'.join(f'- "{snippet}"' for snippet in snippets)
        reason = str(spoiler_check.get('reason') or '').strip()
        reason_line = f'\nChecker reason: {reason}' if reason else ''
        return (
            'The checker flagged these visible spoiler-bearing snippets:\n'
            f'{quoted}'
            f'{reason_line}\n'
            'Remove or generalize those claims while preserving the rest of the safe answer where possible. '
            'Do not restate or paraphrase the hidden explanation behind them.'
        )
    return (
        'Remove or generalize any claims that directly reveal or strongly imply unrevealed DM-private '
        'information while preserving the rest of the safe answer where possible.'
    )


def _mechanics_rewrite_feedback(mechanics_check):
    violations = mechanics_check.get('violations') if isinstance(mechanics_check, dict) else []
    snippets = [str(item).strip() for item in violations or [] if str(item).strip()]
    required = str((mechanics_check or {}).get('required_mechanic') or '').strip()
    reason = str((mechanics_check or {}).get('reason') or '').strip()
    lines = []
    if snippets:
        lines.append('The mechanics checker flagged these premature outcome decisions:')
        lines.extend(f'- "{snippet}"' for snippet in snippets[:6])
    if required:
        lines.append(f'Required mechanic: {required}.')
    if reason:
        lines.append(f'Checker reason: {reason}')
    if not lines:
        lines.append('The response resolved an uncertain mechanical outcome before the required D&D step.')
    return '\n'.join(lines)


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


def check_session_missing_npc_tags_with_llm(response_text, signal, audit_context=None):
    if not (response_text or '').strip() or not signal:
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
        return normalize_session_npc_tag_check(raw_check)
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
        return normalize_session_spoiler_check(raw_check)
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
                        'time_of_day': 'optional current scene timing',
                        'active_npc_ids': ['npc ids currently present or active in the scene; use this instead of graph facts for occupants'],
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


def get_session_dm_response_with_tools(
    hot_context,
    recent_messages,
    tools,
    execute_tool,
    audit_context=None,
    max_tool_rounds=12,
    on_status_change=None,
):
    base_audit = audit_context or {}
    trace_id = base_audit.get('trace_id') or f"session_dm:session_dm_response:{uuid4().hex[:10]}"
    trace_label = base_audit.get('trace_label') or 'session_dm: session_dm_response'
    messages = build_session_dm_tool_messages(hot_context)
    for msg in recent_messages:
        if msg.role == 'dm':
            role = 'assistant'
        elif msg.role == 'player':
            role = 'user'
        else:
            role = msg.role
        messages.append({'role': role, 'content': msg.content})
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
            tools,
            audit_context={
                **base_audit,
                'trace_id': trace_id,
                'trace_label': trace_label,
                'operation': 'session_preflight',
                'actor': 'session_preflight_router',
            },
        )

    if on_status_change:
        on_status_change({"step": "preflight"})

    tool_round = 0
    json_contract_retry_count = 0
    format_retried = False
    mechanical_retried = False
    pc_control_retried = False
    private_output_retried = False
    spoiler_checker_retried = False
    json_contract_fallback_draft = None
    guard_audits = {}

    def guard_audit(guard_name):
        if guard_name not in guard_audits:
            guard_audits[guard_name] = _child_audit_context(
                base_audit,
                guard_name,
                'session_dm_guard',
                f'session_dm_guard: {guard_name}',
            )
        return guard_audits[guard_name]

    while True:
        if on_status_change:
            on_status_change({"step": "thinking", "reasoning": "Determining best actions or phrasing narration"})

        retrying_visible_answer = any((
            json_contract_retry_count > 0,
            format_retried,
            mechanical_retried,
            pc_control_retried,
            private_output_retried,
            spoiler_checker_retried,
        ))
        active_tools = None if retrying_visible_answer else tools
        active_tool_choice = (
            'auto'
            if active_tools and tool_round < max_tool_rounds
            else 'none'
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
            # Some configured providers/models do not reliably support response_format JSON mode.
            # Keep retries in plain chat mode and enforce JSON via guard prompts + contract checks.
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
        if not tool_calls or tool_round >= max_tool_rounds:
            if on_status_change:
                on_status_change({"step": "guard_check"})
            raw_content = message.get('content') or ''
            json_contract_violation = _session_dm_json_contract_violation(raw_content)
            if (
                json_contract_violation
                and json_contract_violation.get('kind') == 'non_json_response'
                and str(raw_content).strip()
            ):
                json_contract_fallback_draft = str(raw_content).strip()
            if json_contract_violation and json_contract_retry_count < 2:
                if on_status_change:
                    on_status_change({"step": "revising", "violations": {"type": "json_contract", "details": json_contract_violation}})
                if base_audit.get('campaign_id'):
                    audit = guard_audit('json_contract_guard')
                    log_audit_event(
                        base_audit.get('campaign_id'),
                        'json_contract_guard_retry',
                        'Session DM response did not follow the final JSON contract; discarded candidate and reran with guard reminder.',
                        {
                            'operation': 'json_contract_guard',
                            'violation': json_contract_violation,
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
                    'content': _session_dm_guard_retry_system_prompt('json_contract', json_contract_violation),
                })
                json_contract_retry_count += 1
                continue
            if json_contract_violation:
                if json_contract_fallback_draft:
                    decision = normalize_session_dm_turn_decision(json_contract_fallback_draft)
                else:
                    return {
                        'mode': 'silent',
                        'reason': 'The DM response did not produce a valid visible reply.',
                    }
            else:
                decision = normalize_session_dm_turn_decision(raw_content)
            content = decision.get('content') or ''
            format_violation = (
                _session_dm_format_violation(content)
                if decision.get('mode') == 'speak'
                else None
            )
            missing_npc_signal = (
                _possible_missing_npc_tag_signal(content)
                if decision.get('mode') == 'speak' and not format_violation
                else None
            )
            npc_tag_check = (
                check_session_missing_npc_tags_with_llm(content, missing_npc_signal, loop_audit)
                if missing_npc_signal
                else {'requires_npc_tag': False, 'speaker': '', 'evidence': [], 'reason': ''}
            )
            if not format_violation and npc_tag_check.get('requires_npc_tag'):
                speaker = str(npc_tag_check.get('speaker') or missing_npc_signal.get('speaker') or '').strip() or 'the speaker'
                evidence = npc_tag_check.get('evidence') or []
                snippet = str(evidence[0] if evidence else missing_npc_signal.get('quote') or '').strip()
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
            violation = (
                _pc_control_violation(content, hot_context)
                if decision.get('mode') == 'speak' and not format_violation
                else None
            )
            private_violation = (
                _private_output_violation(content, hot_context)
                if decision.get('mode') == 'speak' and not format_violation
                else None
            )
            spoiler_check = (
                check_session_spoilers_with_llm(
                    content,
                    hot_context,
                    loop_audit,
                    skip_spoiler_check=preflight_decision.get('skip_spoiler_check') is True,
                )
                if decision.get('mode') == 'speak' and not format_violation and not private_violation
                and not mechanical_violation
                else {'safe': True, 'leaked_item_ids': [], 'evidence': [], 'reason': ''}
            )
            if format_violation and not format_retried:
                if on_status_change:
                    on_status_change({"step": "revising", "violations": {"type": "format", "details": format_violation}})
                if base_audit.get('campaign_id'):
                    audit = guard_audit('format_guard')
                    log_audit_event(
                        base_audit.get('campaign_id'),
                        'format_guard_retry',
                        'Session DM response used malformed visible-message syntax; discarded candidate and reran with guard reminder.',
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
                messages.append({
                    'role': 'system',
                    'content': _session_dm_guard_retry_system_prompt(
                        'missing_npc_tag'
                        if any(err.get('kind') == 'missing_npc_tag' for err in (format_violation.get('errors') or []))
                        else 'format',
                        format_violation,
                    ),
                })
                format_retried = True
                continue
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
                if on_status_change:
                    on_status_change({"step": "revising", "violations": {"type": "pc_control", "details": violation}})
                if base_audit.get('campaign_id'):
                    audit = guard_audit('pc_control_guard')
                    log_audit_event(
                        base_audit.get('campaign_id'),
                        'pc_control_guard_retry',
                        'Session DM response controlled a protected player character; discarded candidate and reran with guard reminder.',
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
                messages.append({
                    'role': 'system',
                    'content': _session_dm_guard_retry_system_prompt('pc_control', violation),
                })
                pc_control_retried = True
                continue
            if private_violation and not private_output_retried:
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
                private_output_retried = True
                continue
            if not spoiler_check.get('safe', True) and not spoiler_checker_retried:
                if on_status_change:
                    on_status_change({"step": "revising", "violations": {"type": "spoiler", "details": spoiler_check}})
                if base_audit.get('campaign_id'):
                    audit = guard_audit('spoiler_checker_guard')
                    log_audit_event(
                        base_audit.get('campaign_id'),
                        'spoiler_checker_guard_retry',
                        'Session spoiler checker flagged a semantic leak; discarded candidate and reran with guard reminder.',
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
                messages.append({
                    'role': 'system',
                    'content': _session_dm_guard_retry_system_prompt('spoiler_checker', spoiler_check),
                })
                spoiler_checker_retried = True
                continue
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
                return {
                    'mode': 'silent',
                    'reason': 'The DM response would have controlled a protected player character.',
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
                return {
                    'mode': 'silent',
                    'reason': 'The DM response used malformed visible-message syntax.',
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
                return {
                    'mode': 'silent',
                    'reason': 'The DM response would have semantically exposed DM-private information.',
                }
            return decision

        messages.append(_assistant_tool_message(message))
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
            result = execute_tool(
                tool_name,
                args,
                {
                    **loop_audit,
                    'parent_trace_id': trace_id,
                    'trace_id': trace_id,
                },
            )
            messages.append({
                'role': 'tool',
                'tool_call_id': tool_call.get('id'),
                'name': tool_name,
                'content': json.dumps(result, ensure_ascii=False),
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


def get_session_memory_patch(memory_context, audit_context=None):
    messages = build_session_memory_messages(memory_context)
    audit_context = audit_context or {}
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
    try:
        request_audit_context = {
            **audit_context,
            'trace_id': trace_id,
            'trace_label': trace_label,
            'operation': audit_context.get('operation') or 'session_memory_update',
            'actor': 'session_memory_writer',
            'full_world_graph_included': False,
        }
        text = _post_chat(
            messages,
            json_mode=True,
            audit_context=request_audit_context,
        )
        response_chars = len(text) if text else 0
        data = _json_loads_with_repair(text, audit_context=request_audit_context)
        if not isinstance(data, dict):
            data = {}

        data['_telemetry'] = {
            'prompt_chars': prompt_chars,
            'prompt_tokens_estimate': prompt_tokens_estimate,
            'response_chars': response_chars,
            'context_breakdown': context_breakdown,
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
        return data
    except Exception as e:
        print(f'[openrouter] Session memory writer error: {e}')
        return {
            '_telemetry': {
                'prompt_chars': prompt_chars,
                'prompt_tokens_estimate': prompt_tokens_estimate,
                'response_chars': 0,
                'context_breakdown': context_breakdown,
                'error': str(e)
            }
        }


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

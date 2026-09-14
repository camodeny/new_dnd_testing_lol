"""Provider-backed forward-DM adjudication — issue #354.

Production ``adjudicate(packet) -> DmTurnContractV1`` implementation that calls
a single configured real provider/model through the existing
``app.providers`` adapter/transport surface (no parallel test-only chat
endpoint). Consumed by ``app.dm.execution``; tests inject fakes instead.
"""
from __future__ import annotations

import json
import logging
import uuid

logger = logging.getLogger(__name__)

FORWARD_DM_SYSTEM = """\
You are the Dungeon Master adjudicating a D&D 5e table turn. You receive the
authoritative forward-DM context packet (player inputs, protected PCs, scene,
history). Respond with EXACTLY ONE JSON object matching the dm_turn_contract_v1
schema — no markdown fences, no commentary.

Modes:
- respond: resolve the turn now with 1-8 beats of atomic true claims.
- await_roll: uncertain outcome needs a player die roll; include roll_request
  with a hidden dc_private, and no staged_effects.
- need_evidence: you lack a required fact; 1-3 evidence_requests plus a short
  safe_prelude progress update (<=240 chars), no beats.
- clarify: player intent is ambiguous; ask via clarify_question or
  open_player_choice (at most 2 setup beats).
- table_chat: pure out-of-character chat; table_chat_intent only, no beats.
- silent: nothing to narrate; no beats.

HARD RULES:
1. Never invent voluntary player-character speech, thought, or action.
2. Never leak dm_private truth, hidden DCs, or internal IDs into public claims.
3. Established facts cite packet evidence. New fictional developments are your
   adjudication: use origin=dm_adjudication and trigger_refs identifying the
   player input or scene that prompted them. Never label an invention as
   established_state or resolver_evidence.
4. New entities: at most 2 proposals, structurally distinct from references.
5. Staged effects: at most 4 typed effects, none before rolls resolve.

PLAY:
Resolve the player's intent with a concrete response, discovery, consequence,
or necessary roll. Do not merely repeat their action and ask what they do.
Preserve established facts and player agency, but advance the world in response
to the action. Missing prewritten story detail is not a reason to freeze play.
Unmarked narration in player inputs can declare actions even when its segment
is ooc; table_chat is for actual discussion of the game, not action declarations.

REFERENCES:
EntityRef.id is the exact durable character/entity UUID in the packet, never
a record_id, display name, submission ID, or temporary proposal ID. The current
scene's location_entity_id is a location reference. Source refs instead use
record_id or source_id from the relevant context record. Player declarations
must cite their originating submission in evidence_refs or trigger_refs.
Do not retell player declarations unless necessary; focus on the world's reply.
Introduce new NPCs through new_entities and a narrated introduction, without
using their temp_id as an EntityRef. They can speak as canonical NPCs on later
turns once their durable identity is in context.

ROLLS:
await_roll requires a public roll_instruction beat and roll_request. On that
instruction actor_ref is null and the PC may be a target_ref; it is not an
action already performed by the PC. roll_request_id on a CLAIM must be null
unless claim_kind is roll_outcome. Put the requested roll's handle in
roll_request.request_id. Never decide the outcome or invent the player's dice.
After fulfillment, use the supplied roll evidence to resolve the original
intent; do not request the same roll again.
"""

def resolve_dm_provider():
    """Resolve (adapter, model, provider_name) for forward-DM execution.

    Thin wrapper over ``app.providers.resolve_area("dm")`` kept for
    backwards compatibility. Provider + model are pinned in code
    (see ``app.providers.areas``); only the API key comes from env.
    """
    from app.providers.areas import resolve_area

    return resolve_area("dm")


def build_forward_dm_messages(packet) -> list[dict]:
    """System + serialized-packet messages for the adjudication call."""
    try:
        context_json = packet.serialize_for_adjudication()
    except AttributeError:
        context_json = json.dumps(
            packet.model_dump(mode="json") if hasattr(packet, "model_dump") else packet,
            ensure_ascii=False,
            sort_keys=True,
        )
    schema_hint = (
        "Return dm_turn_contract_v1 JSON with keys: contract_version "
        "('dm_turn_contract_v1'), mode, reason, beats[], open_player_choice, "
        "narration_hints, adjudication_input, new_entities[], staged_effects[], "
        "evidence_requests[], roll_request, table_chat_intent, safe_prelude, "
        "clarify_question. beats is REQUIRED except in need_evidence, "
        "table_chat, and silent modes: respond needs 1-8 beats, clarify at "
        "most 2 setup beats. Every beat needs id, type, and 1+ claims; beat "
        "type is ONLY narration or npc_dialogue, never anything else; never "
        "emit an empty beats array with mode respond. Every claim is an "
        "OBJECT, never a bare string: "
        '{"text": "...", "claim_kind": "observation|world_fact|npc_utterance|'
        'player_declaration|roll_instruction|roll_outcome", "origin": '
        '"player_transcript|established_state|resolver_evidence|'
        'dm_adjudication|roll_adjudication"}. Scene description claims use '
        'claim_kind=observation with origin=established_state. '
        'player_declaration claims REQUIRE actor_ref {"type": "character", '
        '"id": "<speaking PC id from the packet>"} with origin '
        'player_transcript and evidence_refs containing its source submission. '
        'If the speaker id is unknown, do not assert a PC action. '
        'open_player_choice is a plain STRING question like "What do you '
        'do?", never an object. On narration beats, speaker_ref, '
        'speaker_public_name, truth_status, and dm_private_context must all '
        'be null (they belong to npc_dialogue beats only).'
    )
    return [
        {"role": "system", "content": FORWARD_DM_SYSTEM + "\n" + schema_hint},
        {"role": "user", "content": context_json},
    ]


def _strip_fences(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        lines = t.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t


def parse_contract_json(text: str) -> dict:
    """Parse model output to a raw contract dict (strict, no fabrication)."""
    from app.providers.contracts import ProviderError

    cleaned = _strip_fences(text or "")
    if not cleaned:
        raise ProviderError("DM provider returned empty adjudication output", kind="malformed")
    try:
        raw = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ProviderError(
            f"DM provider returned non-JSON adjudication output: {exc}",
            kind="malformed",
        ) from exc
    if not isinstance(raw, dict):
        raise ProviderError("DM adjudication output must be a JSON object", kind="malformed")
    return raw


def adjudicate_with_provider(
    packet,
    *,
    adapter=None,
    model: str | None = None,
    timeout_seconds: float = 90,
    trace_id: str | None = None,
):
    """Call the configured provider and normalize to DmTurnContractV1.

    Raises the provider/validation error unchanged so the execution
    orchestrator can mark a visible failure (never fabricate a turn).
    """
    from app.dm.contract import contract_json_schema_strict, normalize_contract
    from app.providers import ProviderRequest, execute_chat
    from app.observability.tracing import structured_log

    if adapter is None or model is None:
        resolved_adapter, resolved_model, _ = resolve_dm_provider()
        adapter = adapter or resolved_adapter
        model = model or resolved_model
    tid = trace_id or str(uuid.uuid4())
    messages = build_forward_dm_messages(packet)
    request = ProviderRequest(
        messages=messages,
        model=model,
        json_schema=contract_json_schema_strict(),
        json_schema_name="dm_turn_contract_v1",
        timeout_seconds=timeout_seconds,
        # Deterministic adjudication: structured contracts need exact schema
        # adherence, not sampling variance.
        temperature=0,
    )
    structured_log(
        logger, logging.INFO, "forward_dm_provider_start",
        provider=adapter.name, model=model, trace_id=tid,
    )
    response = execute_chat(adapter, request)
    raw = parse_contract_json(response.content)
    contract = normalize_contract(raw)
    structured_log(
        logger, logging.INFO, "forward_dm_provider_contract",
        provider=adapter.name, model=model, mode=contract.mode, trace_id=tid,
    )
    return contract


def adjudicate_with_failover(
    packet,
    *,
    db=None,
    role: str = "forward_dm",
    timeout_seconds: float = 90,
    trace_id: str | None = None,
    adapter=None,
    model: str | None = None,
    is_retry: bool = False,
):
    """Adjudicate through the role policy path with bounded failover.

    Primary first, then same-model alternate providers, then only
    explicitly approved different-model fallbacks. Every candidate is
    gated by ``policy.is_model_approved`` — unapproved substitution
    raises rather than executes. Recovery attempts (failover index > 0,
    or any call with ``is_retry=True`` for an explicit-Retry attempt) are
    recorded as non-billable AI runs when ``db`` is given; only a first
    try's primary call is ``primary``/billable. Runs are finished
    (succeeded/failed) instead of left running.

    Returns ``(contract, path_info)`` where path_info holds
    ``provider``/``model``/``attempt_index``/``failover_reasons``.
    """
    import time

    from app.providers import ProviderRequest, execute_chat
    from app.providers import policy as role_policy
    from app.observability.tracing import structured_log

    tid = trace_id or str(uuid.uuid4())
    t_start = time.monotonic()
    policy = role_policy.get_role_policy(role)

    if adapter is not None and model is not None:
        # Injected seam (tests): single pinned attempt, no failover chain.
        if not role_policy.is_model_approved(role, adapter.name, model):
            raise RuntimeError(
                f"Unapproved model substitution blocked for role {role!r}: "
                f"{adapter.name}/{model}"
            )
        contract = adjudicate_with_provider(
            packet, adapter=adapter, model=model,
            timeout_seconds=timeout_seconds, trace_id=tid,
        )
        return contract, {
            "provider": adapter.name, "model": model,
            "attempt_index": 0, "failover_reasons": [],
            "ttft_added_ms": 0.0,
        }

    path = role_policy.execution_path(role)
    failover_reasons: list[str] = []
    last_exc: BaseException | None = None
    for index, (provider_name, candidate_model) in enumerate(path):
        if not role_policy.is_model_approved(role, provider_name, candidate_model):
            # Defense in depth: execution_path should never yield these.
            raise RuntimeError(
                f"Unapproved model substitution blocked for role {role!r}: "
                f"{provider_name}/{candidate_model}"
            )
        try:
            if index == 0:
                # Primary resolves through the canonical seam so existing
                # config gates and test hooks on resolve_dm_provider hold.
                cand_adapter, cand_model, _ = resolve_dm_provider()
            else:
                from app.providers.registry import provider_registry

                cand_adapter = provider_registry.get(provider_name)
                cand_adapter.require_config(candidate_model)
                cand_model = candidate_model
        except Exception as exc:
            last_exc = exc
            reason = f"config_unavailable:{provider_name}"
            failover_reasons.append(reason)
            role_policy.record_failover_attempt(reason, provider_name, candidate_model)
            continue
        classification = "recovery" if (index > 0 or is_retry) else "primary"
        ai_run = None
        if db is not None:
            try:
                from app.observability.service import (
                    finish_ai_run_inline,
                    record_ai_run_inline,
                )

                ai_run = record_ai_run_inline(
                    db, logical_operation="forward_dm_adjudicate",
                    role=role, provider=cand_adapter.name, model=cand_model,
                    attempt=index + 1, classification=classification,
                    billable=(classification == "primary"),
                    trace_id=tid,
                )
                if classification == "recovery":
                    role_policy.record_recovery_run(billable=False)
            except Exception:
                ai_run = None
        try:
            from app.dm.contract import contract_json_schema_strict, normalize_contract

            messages = build_forward_dm_messages(packet)
            request = ProviderRequest(
                messages=messages, model=cand_model,
                json_schema=contract_json_schema_strict(),
                json_schema_name="dm_turn_contract_v1",
                timeout_seconds=timeout_seconds, temperature=0,
            )
            structured_log(
                logger, logging.INFO, "forward_dm_provider_start",
                provider=cand_adapter.name, model=cand_model,
                trace_id=tid, attempt=index + 1, classification=classification,
            )
            response = execute_chat(cand_adapter, request)
            raw = parse_contract_json(response.content)
            contract = normalize_contract(raw)
            ttft_added = (time.monotonic() - t_start) * 1000 if index > 0 else 0.0
            if ai_run is not None:
                try:
                    from app.observability.service import finish_ai_run_inline

                    finish_ai_run_inline(db, ai_run.id, status="succeeded",
                                         result_code="contract_ok")
                except Exception:
                    pass
            structured_log(
                logger, logging.INFO, "forward_dm_provider_contract",
                provider=cand_adapter.name, model=cand_model,
                mode=contract.mode, trace_id=tid,
                failover_reasons=failover_reasons,
            )
            return contract, {
                "provider": cand_adapter.name, "model": cand_model,
                "attempt_index": index, "failover_reasons": list(failover_reasons),
                "ttft_added_ms": ttft_added,
            }
        except Exception as exc:
            last_exc = exc
            if ai_run is not None:
                try:
                    from app.observability.service import finish_ai_run_inline

                    finish_ai_run_inline(db, ai_run.id, status="failed",
                                         error_type=type(exc).__name__[:128])
                except Exception:
                    pass
            cls, reason = role_policy.classify_execution_failure(exc)
            failover_reasons.append(reason)
            role_policy.record_failover_attempt(
                reason, cand_adapter.name, cand_model
            )
            if cls == "terminal" or index >= len(path) - 1:
                break
            continue
    assert last_exc is not None
    role_policy.record_exhausted()
    raise last_exc


def build_provider_narrator(*, adapter=None, model: str | None = None, timeout_seconds: float = 90):
    """Streaming narrator backed by the configured provider.

    Resolves the ``narrator`` call area (pinned in code) when
    adapter/model are not injected. Returns a ``StreamingNarratorFn`` taking a contract-bound
    ``NarratorRequest`` and yielding text deltas via ``stream_chat``.
    """
    from app.providers import ProviderRequest, stream_chat
    from app.providers.areas import resolve_area

    if adapter is None or model is None:
        resolved_adapter, resolved_model, _ = resolve_area("narrator")
        adapter = adapter or resolved_adapter
        model = model or resolved_model

    def _narrate(narrator_request) -> object:
        prompt = getattr(narrator_request, "prompt", "")
        pr = ProviderRequest(
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": "Expand the structured turn above into table narration."},
            ],
            model=model,
            timeout_seconds=timeout_seconds,
            stream=True,
        )
        deltas: list[str] = []

        def _gen():
            for event in stream_chat(adapter, pr):
                if event.kind == "token" and event.text:
                    deltas.append(event.text)
                    yield event.text

        # stream_chat is a generator; return the iterable for the delta loop.
        return _gen()

    return _narrate

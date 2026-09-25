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

#: Reasoning effort for forward-DM adjudication. Muse Spark always reasons
#: server-side; the default burns ~3k hidden thinking tokens (~30-40s) on a
#: schema-constrained contract call. "low" holds ~14s with first-try valid
#: contracts (measured 2026-09-25); "none" is rejected (400) for Muse models.
FORWARD_DM_REASONING_EFFORT = "low"

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
MIXED DISCUSSION + ACTION: when one input asks an OOC question and declares an
IC action, answer first via clarify_question (or table_chat_intent) AND emit
beats for the action in the same contract — the answer must be able to stand
before the resolution. If the honest answer makes the declared action
impossible, moot, or uninformed in a way the player would reconsider, do NOT
emit beats for an action that never happens: use clarify mode with the answer
and let the player redeclare.

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
    # Minimal semantic hint: strict JSON schema enforces structure, but not
    # these conditional rules. (Full prose hint removed — it duplicated the
    # schema at ~1.4k chars; unhinted output violates the narration-beat
    # null-field rule, so this stump stays.)
    schema_hint = (
        "Mode/beat rules: respond needs 1-8 beats (never empty); "
        "need_evidence, table_chat, and silent need none. "
        "player_declaration claims REQUIRE actor_ref {\"type\": \"character\", "
        "\"id\": \"<speaking PC id from the packet>\"} with origin "
        "player_transcript, or do not assert a PC action. On narration "
        "beats, speaker_ref, speaker_public_name, truth_status, and "
        "dm_private_context must all be null (npc_dialogue beats only)."
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
        reasoning_effort=FORWARD_DM_REASONING_EFFORT,
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
    campaign_id=None,
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
    # Independent telemetry transaction: AI-run rows must survive gameplay
    # rollback so failed attempts keep their recovery/billing attribution.
    telemetry = None
    if db is not None:
        try:
            from app.observability.service import telemetry_factory_for

            telemetry = telemetry_factory_for(db)
        except Exception:
            telemetry = None

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
        if telemetry is not None:
            try:
                from app.observability.service import start_ai_run

                ai_run = start_ai_run(
                    telemetry, logical_operation="forward_dm_adjudicate",
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
            if ai_run is not None and telemetry is not None:
                try:
                    from app.billing.config import cost_usd_for, tokens_from_usage
                    from app.observability.service import finish_ai_run

                    usage = getattr(response, "usage", None)
                    in_tokens, out_tokens = tokens_from_usage(usage)
                    finish_ai_run(telemetry, ai_run.id, status="succeeded",
                                  result_code="contract_ok",
                                  input_tokens=in_tokens, output_tokens=out_tokens,
                                  cost_usd=cost_usd_for(cand_adapter.name, cand_model, usage),
                                  campaign_id=campaign_id)
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
            if ai_run is not None and telemetry is not None:
                try:
                    from app.observability.service import finish_ai_run

                    finish_ai_run(telemetry, ai_run.id, status="failed",
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


def build_provider_narrator(
    *,
    adapter=None,
    model: str | None = None,
    timeout_seconds: float = 90,
    db=None,
    trace_id: str | None = None,
    role: str = "narration",
    is_retry: bool = False,
    campaign_id=None,
):
    """Streaming narrator backed by the role-policy provider path.

    Resolves candidates through the ``narration`` role policy: primary
    first, then same-model alternate providers, then only explicitly
    approved different-model fallbacks (unapproved substitution raises and
    is never executed). Returns a ``StreamingNarratorFn`` taking a
    contract-bound ``NarratorRequest`` and yielding text deltas.

    Failover preserves the visible-prefix invariant: provider switching
    happens ONLY before anything is durably player-visible (checked via the
    request's durability probe, falling back to a no-yield rule without one).
    On a switch the narrator yields a ``NarratorFailoverMarker`` so the
    streaming service drops the failed provider's unpersisted prefix — the
    next provider's text starts clean. Once output is durable, the provider
    is pinned; later failures propagate to the normal post-visibility
    handling (fidelity-gated continuation, never silent mid-stream switch).

    Candidate lineage follows the policy-path index (not the resolved
    order): if the primary cannot even be configured, the first alternate
    is still index 1 — ``recovery``/non-billable, never promoted to
    billable primary work. Failover attempts are recorded as non-billable
    recovery AI runs when ``db`` is given (as is narration on an
    explicit-retry attempt).
    """
    from app.providers import ProviderRequest, stream_chat
    from app.providers import policy as role_policy
    from app.observability.tracing import structured_log

    tid = trace_id or str(uuid.uuid4())
    # Independent telemetry transaction (see adjudication path): narration
    # runs survive gameplay rollback.
    telemetry = None
    if db is not None:
        try:
            from app.observability.service import telemetry_factory_for

            telemetry = telemetry_factory_for(db)
        except Exception:
            telemetry = None
    if adapter is not None and model is not None:
        if not role_policy.is_model_approved(role, adapter.name, model):
            raise RuntimeError(
                f"Unapproved model substitution blocked for role {role!r}: "
                f"{adapter.name}/{model}"
            )
        pinned = [(0, adapter, model)]
    else:
        if (adapter is not None) != (model is not None):
            raise RuntimeError(
                "Provide both adapter and model, or neither (policy path)"
            )
        pinned = None
    policy_path = None if pinned is not None else role_policy.execution_path(role)

    def _resolve(path_index: int, provider_name: str, candidate_model: str):
        """Resolve one candidate, preserving its policy-path index."""
        if not role_policy.is_model_approved(role, provider_name, candidate_model):
            raise RuntimeError(
                f"Unapproved model substitution blocked for role {role!r}: "
                f"{provider_name}/{candidate_model}"
            )
        if path_index == 0:
            # Primary resolves through the canonical seam so existing
            # config gates hold.
            from app.providers.areas import resolve_area

            area = role_policy.ROLE_AREA.get(role, role)
            cand_adapter, cand_model, _ = resolve_area(area)
            return cand_adapter, cand_model
        from app.providers.registry import provider_registry

        cand_adapter = provider_registry.get(provider_name)
        cand_adapter.require_config(candidate_model)
        return cand_adapter, candidate_model

    def _narrate(narrator_request) -> object:
        prompt = getattr(narrator_request, "prompt", "")
        probe = getattr(narrator_request, "durable_prefix_fn", None)

        def _durable_visible() -> bool:
            try:
                return bool(probe() if callable(probe) else False)
            except Exception:
                return False

        def _gen():
            # (path_index, provider_ref, model, pre_resolved): provider_ref
            # is an adapter when pinned, else a registry name.
            if pinned is not None:
                entries = [(0, adapter, model, True)]
            else:
                assert policy_path is not None
                entries = [(i, p, m, False) for i, (p, m) in enumerate(policy_path)]
            resolved: list[tuple[int, object, str]] = []
            first_error: BaseException | None = None
            for path_index, provider_ref, candidate_model, pre_resolved in entries:
                try:
                    if pre_resolved:
                        cand_adapter, cand_model = provider_ref, candidate_model
                    else:
                        cand_adapter, cand_model = _resolve(
                            path_index, provider_ref, candidate_model)
                except Exception as exc:
                    if first_error is None:
                        first_error = exc
                    role_policy.record_failover_attempt(
                        f"config_unavailable:{provider_ref}", str(provider_ref),
                        candidate_model,
                    )
                    continue
                resolved.append((path_index, cand_adapter, cand_model))
            if not resolved:
                raise first_error if first_error is not None else RuntimeError(
                    f"No narration provider available for role {role!r}"
                )
            failover_reasons: list[str] = []
            for position, (path_index, cand_adapter, cand_model) in enumerate(resolved):
                # Lineage follows the POLICY-PATH index: a skipped primary
                # never promotes an alternate to billable primary work.
                classification = (
                    "recovery" if (path_index > 0 or is_retry) else "primary"
                )
                ai_run = None
                if telemetry is not None:
                    try:
                        from app.observability.service import start_ai_run

                        ai_run = start_ai_run(
                            telemetry, logical_operation="narration_stream",
                            role=role, provider=cand_adapter.name,
                            model=cand_model, attempt=path_index + 1,
                            classification=classification,
                            billable=(classification == "primary"),
                            trace_id=tid,
                        )
                    except Exception:
                        ai_run = None
                pr = ProviderRequest(
                    messages=[
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": "Expand the structured turn above into table narration."},
                    ],
                    model=cand_model,
                    timeout_seconds=timeout_seconds,
                    stream=True,
                )
                structured_log(
                    logger, logging.INFO, "narration_provider_start",
                    provider=cand_adapter.name, model=cand_model,
                    trace_id=tid, attempt=path_index + 1,
                    classification=classification,
                )
                yielded_downstream = False
                stream_usage = None
                try:
                    for event in stream_chat(cand_adapter, pr):
                        if event.kind == "token" and event.text:
                            yielded_downstream = True
                            yield event.text
                        elif event.kind == "done" and getattr(event, "usage", None):
                            stream_usage = event.usage
                    if ai_run is not None and telemetry is not None:
                        try:
                            from app.billing.config import cost_usd_for, tokens_from_usage
                            from app.observability.service import finish_ai_run

                            in_tokens, out_tokens = tokens_from_usage(stream_usage)
                            finish_ai_run(telemetry, ai_run.id, status="succeeded",
                                          result_code="stream_ok",
                                          input_tokens=in_tokens, output_tokens=out_tokens,
                                          cost_usd=cost_usd_for(cand_adapter.name, cand_model,
                                                                stream_usage),
                                          campaign_id=campaign_id)
                        except Exception:
                            pass
                    return
                except Exception as exc:
                    if ai_run is not None and telemetry is not None:
                        try:
                            from app.observability.service import finish_ai_run

                            finish_ai_run(telemetry, ai_run.id, status="failed",
                                          error_type=type(exc).__name__[:128])
                        except Exception:
                            pass
                    cls, reason = role_policy.classify_execution_failure(exc)
                    failover_reasons.append(reason)
                    role_policy.record_failover_attempt(
                        reason, cand_adapter.name, cand_model
                    )
                    # Switching is keyed on DURABLE visibility, not raw token
                    # emission: a retryable failure with nothing durably
                    # visible moves to the next approved candidate (the
                    # marker tells the service to drop the failed
                    # provider's unpersisted prefix). Once output is
                    # durable, the provider is pinned — propagate for
                    # fidelity-gated continuation handling. Without a
                    # durability probe (direct narrator use), fall back to
                    # the conservative no-yield rule.
                    if probe is None:
                        pre_visible = not yielded_downstream
                    else:
                        pre_visible = not _durable_visible()
                    can_switch = (
                        cls == "retriable"
                        and position < len(resolved) - 1
                        and pre_visible
                    )
                    if can_switch:
                        if db is not None:
                            role_policy.record_recovery_run(billable=False)
                        if yielded_downstream:
                            # The failed provider's prefix reached the
                            # service but was never durable: tell it to
                            # drop the prefix so the next provider starts
                            # clean. With nothing yielded, there is nothing
                            # to retract (and probe-less consumers never see
                            # a marker).
                            from app.dm.narration import NarratorFailoverMarker

                            yield NarratorFailoverMarker(
                                provider=cand_adapter.name, reason=reason,
                            )
                        continue
                    raise
            # Unreachable: loop either returns or raises.
            raise RuntimeError("Narration provider path exhausted without result")

        # stream_chat is a generator; return the iterable for the delta loop.
        return _gen()

    return _narrate

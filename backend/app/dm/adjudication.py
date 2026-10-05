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

from app.observability.tracing import structured_log
from app.providers import ProviderRequest, execute_chat, policy as role_policy, stream_chat
from app.providers.areas import resolve_area
from app.providers.contracts import ProviderError
from app.providers.runner import AiRunLedger, classification_for, require_approved, resolve_candidate

logger = logging.getLogger(__name__)

#: Reasoning effort for forward-DM adjudication. On GPT-6 Luna "low" settles
#: ~94% of replayed turns in one call at ~8s median; "none" is ~3s faster but
#: fails twice as many turns (16-turn replay, 2026-10-05).
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

RULES REFERENCES:
The evidence_results lane may include rules_guidance: SRD passages retrieved
for this turn, with canonical rule IDs and citations. Use relevant passages
when proposing mechanical rulings. Retrieval order and semantic relevance
are advisory, not proof of legality or complete coverage. If a needed rule or
exception is missing, request search_rules/lookup_rule evidence; never treat
no relevant rules as permission to invent a rule. Creative rulings beyond
SRD coverage remain explicit DM adjudication. Code owns dice arithmetic,
resource availability, ownership, and all supported deterministic checks.

PLAY:
Resolve the player's intent with a concrete response, discovery, consequence,
or necessary roll. Do not merely repeat their action and ask what they do.
Preserve established facts and player agency, but advance the world in response
to the action. Missing prewritten story detail is not a reason to freeze play.
A broad or vague action is still an action: resolve its most plausible reading
with a concrete lead or consequence. Clarify only when readings conflict in a
way that changes the outcome, not to ask for specifics.
Unmarked narration in player inputs can declare actions even when its segment
is ooc; table_chat is for actual discussion of the game, not action declarations.
MIXED DISCUSSION + ACTION: when one input asks an OOC question and declares an
IC action, answer first via clarify_question (or table_chat_intent) AND emit
beats for the action in the same contract — the answer must be able to stand
before the resolution. If the honest answer makes the declared action
impossible, moot, or uninformed in a way the player would reconsider, do NOT
emit beats for an action that never happens: use clarify mode with the answer
and let the player redeclare.

PRESSURES:
The pressures lane lists campaign clocks: threats and schemes that advance
between turns whether or not the players engage them. Let them shape the
world: NPCs pursue them, scenes show their progress. You never advance a
clock yourself; code does that from what happens in play. When a pressure
carries a directive, the clock just reached a stage or ended: show it in this
turn's beats (an NPC acts, a threat arrives, the scene visibly changes),
woven into the response to the players rather than replacing it.

ADVENTURE COMPLETION:
The active_adventure lane names the current adventure. When the played fiction
has resolved its arc (victory, failure, retreat, capture, or any other
conclusive outcome), stage complete_adventure with the outcome and reason;
omit adventure_id to close the active one. Do not stage it while the arc is
still open.

REFERENCES:
EntityRef.id is the exact durable character/entity UUID in the packet, never
a record_id, display name, submission ID, or temporary proposal ID. The current
scene's location_entity_id is a location reference. Source refs instead use
record_id or source_id from the relevant context record. Player declarations
must cite their originating submission in evidence_refs or trigger_refs.
Do not retell player declarations unless necessary; focus on the world's reply.
Introduce new NPCs through new_entities and a narrated introduction, without
using their temp_id as an EntityRef. When a known NPC reveals a true name
("they call me Pell"), stage reveal_entity_name so the registry carries it from
then on. Each entity id is one person: never voice a different person (a new
Ledger agent, a second guard) through an existing NPC's id; introduce them as
new_entities. They can speak as canonical NPCs on later
turns once their durable identity is in context.
REGISTRY FIRST: the packet carries a complete entity registry (id, name, kind,
one-line summary for every known NPC/location). Before proposing a new entity,
check the registry: if the figure is already there under any name or guise,
reference its exact ID instead. Propose new_entities only for genuinely new
faces, described with distinguishing detail so they cannot be confused with
registry entries.

MECHANICS:
When the resolved fiction changes hit points, conditions, or resources, add
respond-mode mechanics entries stating WHAT happens; code rolls the dice, checks
legality against character_state, applies it, and appends the outcome to your
beats. Never write damage totals, remaining HP, or slot counts yourself, and do
not narrate those numbers in your beats.
- damage: harm that has already landed (failed save, trap, fall, hazard) with
  damage_dice like "2d6" and a damage_type. Not for attacks that still need a
  hit roll; request the roll first.
- heal: heal_dice restored to the target (Healing Word 2d4+mod, Second Wind
  1d10+level, a potion 2d4+2); code rolls it and caps at maximum HP.
- condition: condition_op add/remove a named condition with its source.
- spend: the target spends a tracked resource (by its character_state name)
  or one spell_slot_level slot, e.g. when a PC casts a leveled spell.
If code refuses a mechanic (no slot left, untracked resource or HP), the
feedback says why: adjust the fiction or drop the mechanic and narrate it.
STAT BLOCKS: NPCs have no combat stats until you give them one. Before an NPC
fights or takes damage, stage assign_stat_block with the SRD 5.2.1 monster_id
that fits the fiction and its creature_type (what the creature is: humanoid,
ooze, elemental, ...). Ordinary people use the SRD's people stat blocks
(commoner, guard, bandit, noble, priest, knight). For anything else, first
request search_stat_blocks evidence describing its nature ("silt water ooze",
"fire spirit") and pick a returned block whose defenses and attacks match the
fiction; a fire creature's block does not fit a water monster. Code refuses a
block too strong for this party or of the wrong type; an assigned block is
permanent for that NPC.

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

    The forward-DM primary seam over ``app.providers.resolve_area("dm")``
    (test fakes patch it). Provider + model are pinned in code (see
    ``app.providers.areas``); only the API key comes from env.
    """
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
        "player_transcript, or do not assert a PC action. NPC actor_ref, "
        "speaker_ref, and target/topic refs use the exact entity id from the "
        "packet (subject_entity_id / registry id), never a name or alias. "
        "npc_dialogue beats are one NPC speaking: speaker_ref is that NPC, "
        "speaker_public_name is set, truth_status is REQUIRED (truthful, "
        "mistaken, deceptive, incomplete, or unknown), every claim is an "
        "npc_utterance whose actor_ref equals speaker_ref, and any "
        "truth_status other than truthful needs dm_private_context stating "
        "what is actually true. Narration beats hold no NPC speech: "
        "speaker_ref, speaker_public_name, truth_status, and "
        "dm_private_context are null and no claim is an npc_utterance. "
        "roll_request_id is null except on roll_outcome claims."
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


def _adjudicate_once(packet, *, adapter, model: str, timeout_seconds: float,
                     trace_id: str, attempt: int = 1, classification: str = "primary"):
    """One schema-constrained adjudication call; returns (contract, response)."""
    from app.dm.contract import contract_json_schema_strict, normalize_contract
    request = ProviderRequest(
        messages=build_forward_dm_messages(packet),
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
        provider=adapter.name, model=model,
        trace_id=trace_id, attempt=attempt, classification=classification,
    )
    response = execute_chat(adapter, request)
    contract = normalize_contract(parse_contract_json(response.content))
    return contract, response


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

    tid = trace_id or str(uuid.uuid4())
    t_start = time.monotonic()
    role_policy.get_role_policy(role)

    if adapter is not None and model is not None:
        # Injected seam (tests): single pinned attempt, no failover chain.
        require_approved(role, adapter.name, model)
        contract, _ = _adjudicate_once(
            packet, adapter=adapter, model=model,
            timeout_seconds=timeout_seconds, trace_id=tid,
        )
        return contract, {
            "provider": adapter.name, "model": model,
            "attempt_index": 0, "failover_reasons": [],
            "ttft_added_ms": 0.0,
        }

    ledger = AiRunLedger(
        db, role=role, logical_operation="forward_dm_adjudicate",
        trace_id=tid, campaign_id=campaign_id,
    )
    path = role_policy.execution_path(role)
    failover_reasons: list[str] = []
    last_exc: BaseException | None = None
    for index, (provider_name, candidate_model) in enumerate(path):
        # Defense in depth: execution_path should never yield unapproved
        # candidates; the gate raises before any config resolution.
        require_approved(role, provider_name, candidate_model)
        try:
            cand_adapter, cand_model = resolve_candidate(
                role, index, provider_name, candidate_model,
                # Primary resolves through the canonical seam so existing
                # config gates and test hooks on resolve_dm_provider hold.
                resolve_primary=resolve_dm_provider,
            )
        except Exception as exc:
            last_exc = exc
            reason = f"config_unavailable:{provider_name}"
            failover_reasons.append(reason)
            continue
        classification = classification_for(index, is_retry)
        run = ledger.start(
            adapter=cand_adapter, model=cand_model, path_index=index,
            classification=classification,
        )
        try:
            contract, response = _adjudicate_once(
                packet, adapter=cand_adapter, model=cand_model,
                timeout_seconds=timeout_seconds, trace_id=tid,
                attempt=index + 1, classification=classification,
            )
        except Exception as exc:
            last_exc = exc
            run.failed(exc)
            cls, reason = role_policy.classify_execution_failure(exc)
            failover_reasons.append(reason)
            if cls == "terminal" or index >= len(path) - 1:
                break
            continue
        ttft_added = (time.monotonic() - t_start) * 1000 if index > 0 else 0.0
        run.succeeded(usage=getattr(response, "usage", None), result_code="contract_ok")
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
    assert last_exc is not None
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
    tid = trace_id or str(uuid.uuid4())
    ledger = AiRunLedger(
        db, role=role, logical_operation="narration_stream",
        trace_id=tid, campaign_id=campaign_id,
    )
    if adapter is not None and model is not None:
        require_approved(role, adapter.name, model)
        policy_path = None
    else:
        if (adapter is not None) != (model is not None):
            raise RuntimeError(
                "Provide both adapter and model, or neither (policy path)"
            )
        policy_path = role_policy.execution_path(role)

    def _resolved_candidates() -> list[tuple[int, object, str]]:
        """(path_index, adapter, model) for every configurable candidate."""
        if policy_path is None:
            return [(0, adapter, model)]
        resolved: list[tuple[int, object, str]] = []
        first_error: BaseException | None = None
        for path_index, (provider_name, candidate_model) in enumerate(policy_path):
            try:
                cand_adapter, cand_model = resolve_candidate(
                    role, path_index, provider_name, candidate_model)
            except Exception as exc:
                if first_error is None:
                    first_error = exc
                continue
            resolved.append((path_index, cand_adapter, cand_model))
        if not resolved:
            raise first_error if first_error is not None else RuntimeError(
                f"No narration provider available for role {role!r}"
            )
        return resolved

    def _narrate(narrator_request) -> object:
        prompt = getattr(narrator_request, "prompt", "")
        probe = getattr(narrator_request, "durable_prefix_fn", None)

        def _durable_visible() -> bool:
            try:
                return bool(probe() if callable(probe) else False)
            except Exception:
                return False

        def _gen():
            resolved = _resolved_candidates()
            for position, (path_index, cand_adapter, cand_model) in enumerate(resolved):
                classification = classification_for(path_index, is_retry)
                run = ledger.start(
                    adapter=cand_adapter, model=cand_model, path_index=path_index,
                    classification=classification,
                )
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
                    run.succeeded(usage=stream_usage, result_code="stream_ok")
                    return
                except Exception as exc:
                    run.failed(exc)
                    cls, reason = role_policy.classify_execution_failure(exc)
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

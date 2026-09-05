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
3. Every claim needs provenance from the packet; no unsupported facts.
4. New entities: at most 2 proposals, structurally distinct from references.
5. Staged effects: at most 4 typed effects, none before rolls resolve.
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
        "clarify_question."
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
    from app.dm.contract import contract_json_schema, normalize_contract
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
        json_schema=contract_json_schema(),
        json_schema_name="dm_turn_contract_v1",
        timeout_seconds=timeout_seconds,
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

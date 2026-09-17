"""Deterministic fake-provider mode — issue #373.

Test-scoped stand-in for the external model at the provider boundary. The
fake answers ``execute_chat`` calls with fixture responses and resolves
through the same ``resolve_dm_provider`` seam the production
``adjudicate_with_failover`` path uses, so context assembly, contract
parsing/normalization, validation, narration/stream persistence, turn
commit, and projection all stay production code.

Selection is explicit and test-scoped: scenarios call
:meth:`FakeDMProvider.install` with a pytest ``monkeypatch`` fixture, which
patches only the provider-boundary functions for the life of one test.
Real providers are never registered, replaced, or affected outside an
installed test. The AI is the only DM; this module contains no gameplay,
adjudication, or narration logic of its own.

Fixtures are keyed by logical test step/input and, where needed,
authoritative packet state (lane record presence, e.g. roll evidence),
not call order: each fixture declares the player-input substrings it
answers (or that it answers the input-free opening) plus optional
lane-state conditions, and the fake matches the incoming provider
request's ``player_inputs`` lane and lane record counts. An unmatched request raises
:exc:`FakeProviderUsageError` identifying the logical request instead of
inventing output.

Extensibility: new scenarios register more fixtures on the same provider
(rolls, memory, combat, completion) without a separate fake runtime.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Callable

from app.providers.contracts import ProviderError

FAKE_PROVIDER_NAME = "fake-dm-test"
FAKE_MODEL_NAME = "fake-dm-test-model-v1"

# Request shape marker for the forward-DM structured contract call.
FORWARD_DM_SCHEMA_NAME = "dm_turn_contract_v1"


class FakeProviderUsageError(ProviderError):
    """An unmatched or misconfigured fake-provider call.

    Raised at the provider boundary when no fixture answers the logical
    request, so scenario failures point at the missing fixture/step
    instead of surfacing as downstream validation noise.

    A ``ProviderError`` with terminal kind (not a plain ``RuntimeError``)
    so the production failover loop classifies it terminal and surfaces
    it immediately: an unmatched fixture must never dissolve into
    alternate-provider resolution or provider-config errors.
    """

    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("kind", "unsupported_feature")
        kwargs.setdefault("provider", FAKE_PROVIDER_NAME)
        super().__init__(message, **kwargs)


@dataclass
class FakeDMFixture:
    """One deterministic response, keyed by logical step/input/state.

    ``input_substrings`` must ALL appear in the request's player-input
    texts (case-sensitive). ``match_when_no_inputs`` answers the
    input-free opening turn. ``lane_requires_records`` names lanes that
    must carry at least one record (e.g. ``evidence_results`` once a
    roll is fulfilled); ``lane_requires_empty`` names lanes that must
    carry zero records. Together they distinguish same-input requests
    in different authoritative states — pre-roll vs post-roll resume —
    deterministically, without call counters. ``contract`` is either a
    raw contract dict (returned verbatim as the model JSON payload) or
    a factory taking the extracted request summary and returning one —
    factories stay deterministic (no randomness, wall-clock, or call
    counters). ``roles`` scopes which AI role call the fixture may
    satisfy; later #267 scenarios add roles here without a new fake
    runtime.
    """

    step: str
    contract: dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any]]
    input_substrings: tuple[str, ...] = ()
    match_when_no_inputs: bool = False
    roles: tuple[str, ...] = ("forward_dm",)
    lane_requires_records: tuple[str, ...] = ()
    lane_requires_empty: tuple[str, ...] = ()

    def render(self, summary: dict[str, Any]) -> dict[str, Any]:
        if callable(self.contract):
            rendered = self.contract(summary)
        else:
            rendered = self.contract
        if not isinstance(rendered, dict):
            raise FakeProviderUsageError(
                f"fake-provider fixture {self.step!r} did not render a contract dict"
            )
        return rendered


def _extract_forward_dm_inputs(messages: Any) -> tuple[list[str], dict[str, int]]:
    """Strict player-input read for ``forward_dm`` provider requests.

    The production ``ForwardDmContextPacket`` carries every named lane
    exactly once in canonical order, and each ``player_inputs`` record
    has the ``record_id`` / ``value.segments[].text`` shape. Any
    deviation raises :exc:`FakeProviderUsageError` so a serialization
    regression can never collapse into an ``opening=True`` fixture
    match.

    Returns ``(input_texts, lane_record_counts)``: texts is [] only
    for a structurally valid lane that genuinely carries no input
    text, and counts maps each named lane to its record count so
    fixtures can match on authoritative packet state (pre-roll vs
    post-roll resume) without call counters.
    """
    if not isinstance(messages, (list, tuple)) or not messages:
        raise FakeProviderUsageError("fake-provider forward_dm request has no messages")
    last = messages[-1]
    content = last.get("content") if isinstance(last, dict) else None
    if not isinstance(content, str):
        raise FakeProviderUsageError(
            "fake-provider forward_dm message content is not text "
            f"(got {type(content).__name__})"
        )
    if not content.strip().startswith("{"):
        raise FakeProviderUsageError(
            "fake-provider forward_dm request is not canonical packet JSON "
            f"(content excerpt: {content[:200]!r})"
        )
    try:
        packet = json.loads(content)
    except json.JSONDecodeError as exc:
        raise FakeProviderUsageError(
            "fake-provider could not parse the canonical request packet: "
            f"{exc} (content excerpt: {content[:200]!r})"
        ) from exc
    if not isinstance(packet, dict):
        raise FakeProviderUsageError(
            "fake-provider request packet is not a JSON object "
            f"(got {type(packet).__name__})"
        )
    lanes = packet.get("lanes")
    if not isinstance(lanes, list):
        raise FakeProviderUsageError(
            "fake-provider request is missing the canonical lanes list"
        )
    player_lanes = [
        lane
        for lane in lanes
        if isinstance(lane, dict) and lane.get("name") == "player_inputs"
    ]
    if len(player_lanes) != 1:
        raise FakeProviderUsageError(
            "fake-provider request must carry exactly one player_inputs lane "
            f"(found {len(player_lanes)})"
        )
    lane_counts: dict[str, int] = {}
    for lane in lanes:
        if isinstance(lane, dict) and isinstance(lane.get("name"), str):
            lane_records = lane.get("records")
            lane_counts[lane["name"]] = (
                len(lane_records) if isinstance(lane_records, list) else 0
            )
    records = player_lanes[0].get("records")
    if not isinstance(records, list):
        raise FakeProviderUsageError(
            "fake-provider player_inputs lane has malformed records "
            f"(got {type(records).__name__})"
        )
    texts: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            raise FakeProviderUsageError(
                "fake-provider player_inputs record is not an object "
                f"(got {type(record).__name__})"
            )
        record_id = record.get("record_id")
        if not isinstance(record_id, str) or not record_id:
            raise FakeProviderUsageError(
                "fake-provider player_inputs record is missing record_id"
            )
        value = record.get("value")
        if not isinstance(value, dict):
            raise FakeProviderUsageError(
                f"fake-provider player_inputs record {record_id!r} "
                "has a malformed value"
            )
        segments = value.get("segments")
        if not isinstance(segments, list):
            raise FakeProviderUsageError(
                f"fake-provider player_inputs record {record_id!r} "
                "has malformed segments"
            )
        for segment in segments:
            if not isinstance(segment, dict) or not isinstance(
                segment.get("text"), str
            ):
                raise FakeProviderUsageError(
                    f"fake-provider player_inputs record {record_id!r} "
                    "has a malformed segment"
                )
            if segment["text"]:
                texts.append(segment["text"])
    return texts, lane_counts


def extract_player_input_texts(messages: Any, *, role: str = "unknown") -> list[str]:
    """Player-input texts from a provider request's serialized packet.

    ``forward_dm`` requests go through the strict canonical reader:
    [] is returned only for a structurally valid ``player_inputs``
    lane that genuinely carries no input text (the valid input-free
    opening); any shape deviation raises :exc:`FakeProviderUsageError`.

    Other roles keep the lenient read (unparseable content yields [])
    and fail loudly downstream as unmatched roles instead.
    """
    if role == "forward_dm":
        return _extract_forward_dm_inputs(messages)[0]
    try:
        if not isinstance(messages, (list, tuple)) or not messages:
            return []
        content = (
            messages[-1].get("content") if isinstance(messages[-1], dict) else None
        )
        if not isinstance(content, str) or not content.strip().startswith("{"):
            return []
        packet = json.loads(content)
        texts: list[str] = []
        lanes = packet.get("lanes")
        if not isinstance(lanes, list):
            return []
        for lane in lanes:
            if not isinstance(lane, dict) or lane.get("name") != "player_inputs":
                continue
            for record in lane.get("records") or []:
                value = record.get("value") if isinstance(record, dict) else None
                segments = value.get("segments") if isinstance(value, dict) else None
                if not isinstance(segments, list):
                    continue
                for segment in segments:
                    text = segment.get("text") if isinstance(segment, dict) else None
                    if text:
                        texts.append(str(text))
        return texts
    except Exception:
        return []


def infer_role(request: Any) -> str:
    """AI role for a provider request, from request shape alone."""
    schema_name = getattr(request, "json_schema_name", None)
    if schema_name == FORWARD_DM_SCHEMA_NAME:
        return "forward_dm"
    if getattr(request, "stream", False):
        return "narration"
    return "unknown"


class FakeDMProvider:
    """Deterministic fixture-backed provider-boundary double."""

    def __init__(self) -> None:
        self._fixtures: list[FakeDMFixture] = []
        # One entry per satisfied AI role call: fixture step, role,
        # provider/model, and an input excerpt for failure tracing.
        self.calls: list[dict[str, Any]] = []

    def register(self, fixture: FakeDMFixture) -> FakeDMFixture:
        self._fixtures.append(fixture)
        return fixture

    def register_step(
        self,
        step: str,
        contract: dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any]],
        *,
        inputs: tuple[str, ...] = (),
        opening: bool = False,
        roles: tuple[str, ...] = ("forward_dm",),
        requires_records: tuple[str, ...] = (),
        requires_empty: tuple[str, ...] = (),
    ) -> FakeDMFixture:
        return self.register(
            FakeDMFixture(
                step=step,
                contract=contract,
                input_substrings=tuple(inputs),
                match_when_no_inputs=opening,
                roles=roles,
                lane_requires_records=tuple(requires_records),
                lane_requires_empty=tuple(requires_empty),
            )
        )

    @property
    def steps(self) -> list[str]:
        return [fixture.step for fixture in self._fixtures]

    def calls_for_step(self, step: str) -> list[dict[str, Any]]:
        return [call for call in self.calls if call["fixture_step"] == step]

    def _match(
        self,
        role: str,
        input_texts: list[str],
        lane_counts: dict[str, int] | None = None,
    ) -> FakeDMFixture | None:
        counts = lane_counts or {}
        candidates: list[tuple[int, int, FakeDMFixture]] = []
        for order, fixture in enumerate(self._fixtures):
            if role not in fixture.roles:
                continue
            if any(counts.get(lane, 0) < 1 for lane in fixture.lane_requires_records):
                continue
            if any(counts.get(lane, 0) != 0 for lane in fixture.lane_requires_empty):
                continue
            if fixture.match_when_no_inputs and not input_texts:
                candidates.append((0, order, fixture))
                continue
            if fixture.input_substrings and all(
                substring in " ".join(input_texts)
                for substring in fixture.input_substrings
            ):
                specificity = sum(len(s) for s in fixture.input_substrings)
                candidates.append((specificity, order, fixture))
        if not candidates:
            return None
        # Most specific match wins; registration order breaks ties so
        # repeated runs resolve identically.
        candidates.sort(key=lambda item: (-item[0], item[1]))
        return candidates[0][2]

    def execute_chat(self, adapter: Any, request: Any) -> Any:
        """Drop-in for ``app.providers.execute_chat`` (test-installed only)."""
        from app.providers.contracts import NormalizedChatResponse

        role = infer_role(request)
        messages = getattr(request, "messages", None)
        if role == "forward_dm":
            input_texts, lane_counts = _extract_forward_dm_inputs(messages)
        else:
            input_texts, lane_counts = (
                extract_player_input_texts(messages, role=role),
                {},
            )
        fixture = self._match(role, input_texts, lane_counts)
        if fixture is None:
            excerpt = " ".join(input_texts)[:300] or "<no player inputs>"
            raise FakeProviderUsageError(
                "fake-provider has no fixture for this logical request: "
                f"role={role} inputs={excerpt!r} lanes={lane_counts} "
                f"(registered steps: {self.steps or '<none>'})"
            )
        summary = {
            "step": fixture.step,
            "role": role,
            "provider": getattr(adapter, "name", "?"),
            "model": getattr(request, "model", "?"),
            "input_texts": input_texts,
        }
        raw_contract = fixture.render(summary)
        content = json.dumps(raw_contract, ensure_ascii=False, sort_keys=True)
        self.calls.append(
            {
                "fixture_step": fixture.step,
                "role": role,
                "provider": getattr(adapter, "name", "?"),
                "model": getattr(request, "model", "?"),
                "input_excerpt": " ".join(input_texts)[:300],
            }
        )
        return NormalizedChatResponse(
            provider=getattr(adapter, "name", FAKE_PROVIDER_NAME),
            model=getattr(request, "model", FAKE_MODEL_NAME),
            content=content,
            tool_calls=[],
            finish_reason="stop",
            usage={},
            reasoning=None,
            reasoning_details=None,
            raw={"fake_fixture_step": fixture.step},
        )

    def install(self, monkeypatch: Any) -> "FakeDMProvider":
        """Install the fake at the provider boundary for one test.

        Patches ``resolve_dm_provider`` (so no API keys are needed) and
        ``app.providers.execute_chat`` (so the production failover path
        consumes fixtures). ``monkeypatch`` scoping keeps this strictly
        test-local. Refuses production environments outright, honoring
        both the generic ``APP_ENV`` signal and this repository's
        deployment convention ``VERCEL_ENV=production``.
        """
        for env_var in ("APP_ENV", "VERCEL_ENV"):
            if (os.getenv(env_var) or "").strip().lower() == "production":
                raise FakeProviderUsageError(
                    "fake-provider mode is test-scoped and refused in production "
                    f"({env_var}=production)"
                )

        provider = self

        class _FakeAdapter:
            name = FAKE_PROVIDER_NAME

            def require_config(self, model=None):
                return None

        def _fake_resolve():
            return _FakeAdapter(), FAKE_MODEL_NAME, FAKE_PROVIDER_NAME

        monkeypatch.setattr(
            "app.dm.adjudication.resolve_dm_provider", _fake_resolve, raising=True
        )
        monkeypatch.setattr(
            "app.providers.execute_chat", provider.execute_chat, raising=True
        )
        return provider


def phase0_respond_contract(*, marker: str, reason: str) -> dict[str, Any]:
    """Valid production ``respond`` contract with a deterministic marker."""
    from app.dm.contract import CONTRACT_VERSION

    return {
        "contract_version": CONTRACT_VERSION,
        "mode": "respond",
        "reason": reason,
        "beats": [
            {
                "id": "beat_1",
                "type": "narration",
                "claims": [
                    {
                        "text": (
                            f"Phase0 deterministic DM reply: embers shift in "
                            f"the tavern hearth. ({marker})"
                        ),
                        "claim_kind": "observation",
                        "origin": "dm_adjudication",
                        "visibility": "public",
                    }
                ],
            }
        ],
        "open_player_choice": "What do you do?",
    }


def build_phase0_provider(
    *,
    freeform_turns: tuple[str, ...],
    post_reconnect_turn: str,
    opening_inputs: tuple[str, ...] = (),
) -> FakeDMProvider:
    """Phase 0 fixture set: opening plus one step per player turn.

    Markers are fixed per logical step (not call order), so repeated runs
    from clean state produce identical DM outputs. The opening matches an
    input-free packet or the given synthetic opening inputs (the #355
    solo bootstrap seeds an OOC opening line).
    """
    provider = FakeDMProvider()
    provider.register_step(
        "opening",
        phase0_respond_contract(
            marker="phase0-reply-opening", reason="phase0 deterministic opening"
        ),
        inputs=opening_inputs,
        opening=True,
    )
    for index, text in enumerate(freeform_turns):
        provider.register_step(
            f"play-{index + 1}",
            phase0_respond_contract(
                marker=f"phase0-reply-play-{index + 1}",
                reason=f"phase0 deterministic reply play-{index + 1}",
            ),
            inputs=(text,),
        )
    provider.register_step(
        "post-reconnect",
        phase0_respond_contract(
            marker="phase0-reply-post",
            reason="phase0 deterministic post-reconnect reply",
        ),
        inputs=(post_reconnect_turn,),
    )
    return provider

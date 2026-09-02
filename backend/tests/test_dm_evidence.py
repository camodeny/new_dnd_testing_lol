"""Issue #203 — bounded evidence-request loop and tool mediation."""

import time
import uuid

import pytest

from app.dm.context import (
    AuthorizationScope,
    ContextAudience,
    ContextRecord,
    LaneName,
    LANE_ORDER,
    SourceRef,
    assemble_context_packet,
)
from app.dm.contract import CONTRACT_VERSION, normalize_contract
from app.dm.evidence import (
    EvidenceLoopLimitError,
    EvidenceResult,
    EvidenceValidationError,
    execute_evidence_round,
    evidence_results_to_records,
    run_bounded_evidence_loop,
    validate_evidence_requests,
)


def _audience(campaign_id=None, thread_id=None, audience="campaign", user_ids=None):
    cid = campaign_id or str(uuid.uuid4())
    tid = thread_id or str(uuid.uuid4())
    uids = user_ids or [str(uuid.uuid4())]
    return ContextAudience(campaign_id=cid, thread_id=tid, audience=audience, user_ids=uids)


def _packet(audience=None):
    aud = audience or _audience()
    recs = {ln: [] for ln in LANE_ORDER}
    # Mark optional lanes not_applicable so required check passes on empty stub
    return assemble_context_packet(
        audience=aud,
        records=recs,
        lane_status={
            ln.value: "not_applicable"
            for ln in [
                LaneName.CURRENT_SCENE,
                LaneName.KNOWLEDGE_VISIBILITY,
                LaneName.CLOCKS_PRESSURES,
                LaneName.COMBAT_HOOKS,
                LaneName.RELEVANT_CANON,
                LaneName.REPAIR_DIRECTIVES,
            ]
        },
    )


def _sheet_ok(req, audience, db=None):
    return EvidenceResult(
        request_id=req.id,
        tool=req.tool,
        status="ok",
        sources=[SourceRef(source_type="dnd5e_character_sheet", source_id="sheet:1", source_version="v1")],
        visibility="campaign",
        authorization=AuthorizationScope(campaign_id=audience.campaign_id, thread_ids=[audience.thread_id]),
        payload={"ac": 15},
        result_count=1,
    )


def test_one_pass_evidence():
    packet = _packet()
    calls = [0]

    def adjudicate(pkt):
        calls[0] += 1
        if calls[0] == 1:
            return normalize_contract(
                {
                    "contract_version": CONTRACT_VERSION,
                    "mode": "need_evidence",
                    "reason": "need sheet",
                    "beats": [],
                    "evidence_requests": [
                        {"id": "evidence_1", "tool": "ask_character_sheet", "question": "what is AC?", "scope": "current_player"}
                    ],
                    "safe_prelude": "Checking sheet...",
                }
            )
        return normalize_contract(
            {
                "contract_version": CONTRACT_VERSION,
                "mode": "respond",
                "reason": "done",
                "beats": [
                    {
                        "id": "beat_1",
                        "type": "narration",
                        "claims": [
                            {
                                "text": "AC is 15.",
                                "claim_kind": "observation",
                                "origin": "resolver_evidence",
                                "evidence_refs": ["evidence:evidence_1"],
                            }
                        ],
                    }
                ],
            }
        )

    final, bundle = run_bounded_evidence_loop(initial_packet=packet, adjudicate=adjudicate, tool_handlers={"ask_character_sheet": _sheet_ok})
    assert final.mode == "respond"
    assert bundle.rounds == 1
    assert bundle.results[0].status == "ok"
    assert bundle.results[0].sources[0].source_id == "sheet:1"
    # evidence lane carries provenance and is visible to adjudication
    assert any(r.request_id == "evidence_1" for r in bundle.results)


def test_two_pass_evidence():
    packet = _packet()
    calls = [0]

    def adjudicate(pkt):
        calls[0] += 1
        if calls[0] == 1:
            return normalize_contract(
                {
                    "contract_version": CONTRACT_VERSION,
                    "mode": "need_evidence",
                    "reason": "first",
                    "beats": [],
                    "evidence_requests": [{"id": "evidence_1", "tool": "search_campaign_memory", "query": "seal"}],
                    "safe_prelude": "Searching...",
                }
            )
        if calls[0] == 2:
            return normalize_contract(
                {
                    "contract_version": CONTRACT_VERSION,
                    "mode": "need_evidence",
                    "reason": "second",
                    "beats": [],
                    "evidence_requests": [{"id": "evidence_2", "tool": "get_current_scene"}],
                    "safe_prelude": "Checking scene...",
                }
            )
        return normalize_contract(
            {
                "contract_version": CONTRACT_VERSION,
                "mode": "respond",
                "reason": "done",
                "beats": [
                    {
                        "id": "beat_1",
                        "type": "narration",
                        "claims": [{"text": "Seal found.", "claim_kind": "observation", "origin": "resolver_evidence", "evidence_refs": ["evidence:evidence_1"]}],
                    }
                ],
            }
        )

    def mem_handler(req, audience, db=None):
        return EvidenceResult(
            request_id=req.id,
            tool=req.tool,
            status="ok",
            sources=[SourceRef(source_type="campaign_memory", source_id="mem:1", source_version="1")],
            visibility="campaign",
            authorization=AuthorizationScope(campaign_id=audience.campaign_id, thread_ids=[audience.thread_id]),
            payload={"found": True},
            result_count=1,
        )

    def scene_handler(req, audience, db=None):
        return EvidenceResult(
            request_id=req.id,
            tool=req.tool,
            status="ok",
            sources=[SourceRef(source_type="scene", source_id="scene:1", source_version="1")],
            visibility="campaign",
            authorization=AuthorizationScope(campaign_id=audience.campaign_id, thread_ids=[audience.thread_id]),
            payload={"scene": "tavern"},
            result_count=1,
        )

    final, bundle = run_bounded_evidence_loop(
        initial_packet=packet, adjudicate=adjudicate, tool_handlers={"search_campaign_memory": mem_handler, "get_current_scene": scene_handler}
    )
    assert bundle.rounds == 2
    assert final.mode == "respond"
    assert len(bundle.results) == 2


def test_invalid_request_rejected_before_execution():
    with pytest.raises(EvidenceValidationError):
        validate_evidence_requests([{"id": "evidence_1", "tool": "roll_dice", "question": "hi", "scope": "current_player"}])
    # also via loop
    packet = _packet()

    def adjudicate_bad(pkt):
        return normalize_contract(
            {
                "contract_version": CONTRACT_VERSION,
                "mode": "need_evidence",
                "reason": "bad",
                "beats": [],
                "evidence_requests": [{"id": "evidence_1", "tool": "search_campaign_memory", "query": "x"}],
                "safe_prelude": "Checking...",
            }
        )

    # monkeypatch validate to simulate bad tool after contract validation (contract would already reject roll_dice)
    # So we test per-round limit
    with pytest.raises(EvidenceValidationError):
        validate_evidence_requests(
            [
                {"id": "e1", "tool": "search_campaign_memory", "query": "a"},
                {"id": "e2", "tool": "search_campaign_memory", "query": "b"},
                {"id": "e3", "tool": "search_campaign_memory", "query": "c"},
                {"id": "e4", "tool": "search_campaign_memory", "query": "d"},
            ]
        )


def test_unavailable_source_distinguished_as_missing():
    packet = _packet()
    calls = [0]

    def adjudicate(pkt):
        calls[0] += 1
        if calls[0] == 1:
            return normalize_contract(
                {
                    "contract_version": CONTRACT_VERSION,
                    "mode": "need_evidence",
                    "reason": "need",
                    "beats": [],
                    "evidence_requests": [{"id": "evidence_1", "tool": "search_campaign_memory", "query": "nonexistent"}],
                    "safe_prelude": "Searching...",
                }
            )
        return normalize_contract(
            {
                "contract_version": CONTRACT_VERSION,
                "mode": "respond",
                "reason": "uncertain",
                "beats": [{"id": "beat_1", "type": "narration", "claims": [{"text": "No record.", "claim_kind": "observation", "origin": "dm_adjudication"}]}],
            }
        )

    def missing_handler(req, audience, db=None):
        return EvidenceResult(
            request_id=req.id,
            tool=req.tool,
            status="missing",
            sources=[SourceRef(source_type="campaign_memory", source_id=req.id, source_version="v1")],
            visibility="campaign",
            authorization=AuthorizationScope(campaign_id=audience.campaign_id, thread_ids=[audience.thread_id]),
            payload=None,
            result_count=0,
        )

    _, bundle = run_bounded_evidence_loop(initial_packet=packet, adjudicate=adjudicate, tool_handlers={"search_campaign_memory": missing_handler})
    assert bundle.results[0].status == "missing"
    assert bundle.results[0].sources[0].source_version == "v1"


def test_private_source_does_not_leak_to_campaign_audience():
    aud = _audience(audience="campaign", user_ids=[str(uuid.uuid4()), str(uuid.uuid4())])
    packet = _packet(audience=aud)

    def private_handler(req, audience, db=None):
        return EvidenceResult(
            request_id=req.id,
            tool=req.tool,
            status="ok",
            sources=[SourceRef(source_type="dnd5e_character_sheet", source_id="sheet:priv", source_version="1")],
            visibility="private",
            authorization=AuthorizationScope(campaign_id=audience.campaign_id, thread_ids=[audience.thread_id], user_ids=[audience.user_ids[0]]),
            payload={"secret": "__PRIVATE_SECRET_7f3a9c__"},
            result_count=1,
        )

    calls = [0]

    def adjudicate(pkt):
        calls[0] += 1
        if calls[0] == 1:
            return normalize_contract(
                {
                    "contract_version": CONTRACT_VERSION,
                    "mode": "need_evidence",
                    "reason": "need",
                    "beats": [],
                    "evidence_requests": [{"id": "evidence_1", "tool": "ask_character_sheet", "question": "private?", "scope": "current_player"}],
                    "safe_prelude": "Checking...",
                }
            )
        ev_lane = next(l for l in pkt.lanes if l.name == LaneName.EVIDENCE_RESULTS)
        assert len(ev_lane.records) == 0, "private evidence leaked to campaign audience"
        assert "__PRIVATE_SECRET_7f3a9c__" not in pkt.serialize_for_adjudication()
        assert "__PRIVATE_SECRET_7f3a9c__" not in pkt.serialize_for_narration()
        return normalize_contract(
            {
                "contract_version": CONTRACT_VERSION,
                "mode": "respond",
                "reason": "done",
                "beats": [{"id": "beat_1", "type": "narration", "claims": [{"text": "Unknown.", "claim_kind": "observation", "origin": "dm_adjudication"}]}],
            }
        )

    final, bundle = run_bounded_evidence_loop(initial_packet=packet, adjudicate=adjudicate, tool_handlers={"ask_character_sheet": private_handler})
    assert final.mode == "respond"
    # result exists but record was filtered
    assert bundle.results[0].status == "ok"
    assert bundle.results[0].visibility == "private"


def test_tool_timeout_retry_with_deadline():
    packet = _packet()
    attempts = [0]

    def flaky_handler(req, audience, db=None):
        attempts[0] += 1
        if attempts[0] == 1:
            raise TimeoutError("temporarily unavailable")
        return EvidenceResult(
            request_id=req.id,
            tool=req.tool,
            status="ok",
            sources=[SourceRef(source_type="scene", source_id="scene:2", source_version="1")],
            visibility="campaign",
            authorization=AuthorizationScope(campaign_id=audience.campaign_id, thread_ids=[audience.thread_id]),
            payload={"ok": True},
            result_count=1,
        )

    calls = [0]

    def adjudicate(pkt):
        calls[0] += 1
        if calls[0] == 1:
            return normalize_contract(
                {
                    "contract_version": CONTRACT_VERSION,
                    "mode": "need_evidence",
                    "reason": "need",
                    "beats": [],
                    "evidence_requests": [{"id": "evidence_1", "tool": "get_current_scene"}],
                    "safe_prelude": "Checking...",
                }
            )
        return normalize_contract(
            {
                "contract_version": CONTRACT_VERSION,
                "mode": "respond",
                "reason": "done",
                "beats": [{"id": "beat_1", "type": "narration", "claims": [{"text": "Scene ok.", "claim_kind": "observation", "origin": "resolver_evidence", "evidence_refs": ["evidence:evidence_1"]}]}],
            }
        )

    _, bundle = run_bounded_evidence_loop(initial_packet=packet, adjudicate=adjudicate, tool_handlers={"get_current_scene": flaky_handler})
    assert bundle.results[0].retries == 1
    assert bundle.results[0].status == "ok"

    # actual deadline enforcement — handler sleeps longer than timeout; caller must return at deadline, not after handler finishes
    def slow_handler(req, audience, db=None):
        time.sleep(0.5)
        return EvidenceResult(
            request_id=req.id,
            tool=req.tool,
            status="ok",
            sources=[SourceRef(source_type="scene", source_id="scene:slow", source_version="1")],
            visibility="campaign",
            authorization=AuthorizationScope(campaign_id=audience.campaign_id, thread_ids=[audience.thread_id]),
            payload={"ok": True},
            result_count=1,
        )

    aud2 = _audience()
    req = normalize_contract(
        {
            "contract_version": CONTRACT_VERSION,
            "mode": "need_evidence",
            "reason": "need",
            "beats": [],
            "evidence_requests": [{"id": "evidence_1", "tool": "get_current_scene"}],
            "safe_prelude": "Checking...",
        }
    ).evidence_requests
    t0 = time.monotonic()
    results, trace = execute_evidence_round(req, aud2, tool_handlers={"get_current_scene": slow_handler}, timeout_s=0.1, max_retries=1)
    elapsed = time.monotonic() - t0
    assert results[0].status == "tool_failure"
    assert "timed out" in (results[0].error or "").lower()
    assert results[0].retries == 1  # retried once then failed
    # Must return at deadline (~0.25s = 0.1*2 + backoff 0.05), not after handler sleep (~1.0s)
    assert elapsed < 0.4, f"timeout did not bound wall time; elapsed={elapsed:.3f}s"


def test_loop_limit():
    packet = _packet()

    def adjudicate_loop(pkt):
        return normalize_contract(
            {
                "contract_version": CONTRACT_VERSION,
                "mode": "need_evidence",
                "reason": "loop",
                "beats": [],
                "evidence_requests": [{"id": f"evidence_{uuid.uuid4().hex[:6]}", "tool": "search_campaign_memory", "query": "x"}],
                "safe_prelude": "Searching...",
            }
        )

    with pytest.raises(EvidenceLoopLimitError):
        run_bounded_evidence_loop(initial_packet=packet, adjudicate=adjudicate_loop, max_rounds=2, timeout_s=1.0)


def test_unknown_result_allows_uncertainty():
    packet = _packet()
    calls = [0]

    def adjudicate(pkt):
        calls[0] += 1
        if calls[0] == 1:
            return normalize_contract(
                {
                    "contract_version": CONTRACT_VERSION,
                    "mode": "need_evidence",
                    "reason": "need",
                    "beats": [],
                    "evidence_requests": [{"id": "evidence_1", "tool": "search_campaign_memory", "query": "mystery"}],
                    "safe_prelude": "Searching...",
                }
            )
        return normalize_contract(
            {
                "contract_version": CONTRACT_VERSION,
                "mode": "respond",
                "reason": "unknown",
                "beats": [{"id": "beat_1", "type": "narration", "claims": [{"text": "Its origin is unknown.", "claim_kind": "observation", "origin": "dm_adjudication"}]}],
            }
        )

    def unknown_handler(req, audience, db=None):
        return EvidenceResult(
            request_id=req.id,
            tool=req.tool,
            status="unknown",
            sources=[SourceRef(source_type="campaign_memory", source_id="mem:unknown", source_version="1")],
            visibility="campaign",
            authorization=AuthorizationScope(campaign_id=audience.campaign_id, thread_ids=[audience.thread_id]),
            payload={"note": "no canon"},
            result_count=0,
        )

    _, bundle = run_bounded_evidence_loop(initial_packet=packet, adjudicate=adjudicate, tool_handlers={"search_campaign_memory": unknown_handler})
    assert bundle.results[0].status == "unknown"


def test_dict_return_missing_visibility_fails_closed():
    aud = _audience()
    req = normalize_contract(
        {
            "contract_version": CONTRACT_VERSION,
            "mode": "need_evidence",
            "reason": "need",
            "beats": [],
            "evidence_requests": [{"id": "evidence_1", "tool": "search_campaign_memory", "query": "x"}],
            "safe_prelude": "Searching...",
        }
    ).evidence_requests

    def bad_dict_handler(req, audience, db=None):
        # Missing visibility
        return {"status": "ok", "payload": {"x": 1}, "sources": []}

    with pytest.raises(EvidenceValidationError):
        execute_evidence_round(req, aud, tool_handlers={"search_campaign_memory": bad_dict_handler}, timeout_s=1.0)

    def private_dict_missing_auth(req, audience, db=None):
        return {"status": "ok", "visibility": "private", "payload": {"secret": 1}, "sources": []}

    with pytest.raises(EvidenceValidationError):
        execute_evidence_round(req, aud, tool_handlers={"search_campaign_memory": private_dict_missing_auth}, timeout_s=1.0)


def test_evidence_tool_failure_triggers_retry_then_failure_handling():
    aud = _audience()
    req = normalize_contract(
        {
            "contract_version": CONTRACT_VERSION,
            "mode": "need_evidence",
            "reason": "need",
            "beats": [],
            "evidence_requests": [{"id": "evidence_1", "tool": "get_current_scene"}],
            "safe_prelude": "Checking...",
        }
    ).evidence_requests

    def always_fail(req, audience, db=None):
        raise RuntimeError("terminal boom")

    results, _ = execute_evidence_round(req, aud, tool_handlers={"get_current_scene": always_fail}, timeout_s=1.0, max_retries=1)
    assert results[0].status == "tool_failure"
    assert results[0].retries == 0  # terminal not retried


def test_unexpected_handler_result_type_fails_closed():
    aud = _audience()
    req = normalize_contract(
        {
            "contract_version": CONTRACT_VERSION,
            "mode": "need_evidence",
            "reason": "need",
            "beats": [],
            "evidence_requests": [{"id": "evidence_1", "tool": "get_current_scene"}],
            "safe_prelude": "Checking...",
        }
    ).evidence_requests

    def string_handler(req, audience, db=None):
        return "just a string, not typed"

    def int_handler(req, audience, db=None):
        return 42

    for handler in (string_handler, int_handler):
        with pytest.raises(EvidenceValidationError):
            execute_evidence_round(req, aud, tool_handlers={"get_current_scene": handler}, timeout_s=1.0)


def test_evidence_results_carry_stable_source_ids():
    aud = _audience()
    r = EvidenceResult(
        request_id="evidence_1",
        tool="search_campaign_memory",
        status="ok",
        sources=[SourceRef(source_type="campaign_memory", source_id="mem:123", source_version="v5", campaign_revision=10)],
        visibility="campaign",
        authorization=AuthorizationScope(campaign_id=aud.campaign_id, thread_ids=[aud.thread_id]),
        payload={"x": 1},
        result_count=1,
    )
    records = evidence_results_to_records([r], aud)
    assert records[0].sources[0].source_id == "mem:123"
    assert records[0].sources[0].source_version == "v5"
    assert records[0].record_id == "evidence:evidence_1"

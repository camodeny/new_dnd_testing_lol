"""Issue #375 — manually opt-in real generative Phase 0 observation.

Run only with both explicit acknowledgements (and the configured provider
credential) from ``backend``::

    E2E_REAL_AI=1 E2E_REAL_AI_CONFIRM=paid python -m pytest -m real_ai \\
        tests/test_alpha_e2e_real_ai_375.py -v

To additionally exercise the experimental real decision-first route, configure
TypeSafe/Jev and add ``E2E_REAL_DECISION_AI=experimental``.  This label is
deliberately distinct from the approved generative route: decision routing is
pre-alpha dogfood and is not an invite-alpha approval.

The double opt-in is intentionally test-local.  Normal pytest and CI runs
skip this module before the scenario fixture or any provider code is invoked.
The base mode exercises the approved configured forward-DM generative route;
the additional experimental opt-in exercises #382 decision-first selection.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

import pytest
from sqlalchemy import select

from app.decisions.contracts import ChoiceResult, DecisionResponse
from app.decisions.frames import OPEN_ENDED_DM_CANDIDATE_ID
from app.decisions.runtime import DecisionService
from app.dm import execution as dm_execution
from app.dm.decision_routing import ROUTE_QUESTION_ID, ROUTE_SILENT_ID
from models.dm import DmTurnAttempt
from models.reliability import AIRun, DecisionTelemetry
from test_alpha_e2e_solo_dogfood_372 import (
    phase0_provider,
    run_phase0_solo_scenario,
    scn,
)

logger = logging.getLogger(__name__)

REAL_AI_ENV = "E2E_REAL_AI"
REAL_AI_CONFIRM_ENV = "E2E_REAL_AI_CONFIRM"
REAL_AI_CONFIRM_VALUE = "paid"
REAL_DECISION_AI_ENV = "E2E_REAL_DECISION_AI"
REAL_DECISION_AI_VALUE = "experimental"


class _OpenEndedDecisionService:
    """Test-only route seam: choose the ordinary generative escape.

    The real-AI observation must not contact a decision provider.  Production
    execution still receives a decision service so the route is exercised,
    but this deterministic response makes the OPEN_ENDED_DM escape explicit.
    """

    def __init__(self) -> None:
        self.decision_calls = 0
        self.adapter_calls = 0

    @property
    def adapter(self):
        self.adapter_calls += 1
        raise AssertionError("real scenario must not access a decision adapter")

    def decide(self, request) -> DecisionResponse:
        self.decision_calls += 1
        return DecisionResponse(
            results={
                request.questions[0].question_id: ChoiceResult(
                    question_id=request.questions[0].question_id,
                    selected_id=OPEN_ENDED_DM_CANDIDATE_ID,
                )
            },
            provider="test-open-ended-route",
            model="test-open-ended-route",
            latency_ms=0,
            trace_id="test-open-ended-route",
        )


class _OpeningThenSilentDecisionService:
    """Keep one visible reply, then exercise deterministic direct silence."""

    def __init__(self) -> None:
        self.calls = 0

    def decide(self, request) -> DecisionResponse:
        self.calls += 1
        selected_id = (
            OPEN_ENDED_DM_CANDIDATE_ID if self.calls == 1 else ROUTE_SILENT_ID
        )
        candidates = request.questions[0].candidates
        probabilities = {
            candidate.id: 1.0 if candidate.id == selected_id else 0.0
            for candidate in candidates
        }
        return DecisionResponse(
            results={
                ROUTE_QUESTION_ID: ChoiceResult(
                    question_id=ROUTE_QUESTION_ID,
                    selected_id=selected_id,
                    probabilities=probabilities,
                    confidence=1.0,
                )
            },
            provider="fake-decision",
            model="direct-silent-regression",
            latency_ms=0,
            trace_id=f"direct-silent-{self.calls}",
        )


def real_ai_enabled() -> bool:
    """Require two exact, deliberately unambiguous paid-call opt-ins."""
    return (
        os.getenv(REAL_AI_ENV) == "1"
        and os.getenv(REAL_AI_CONFIRM_ENV) == REAL_AI_CONFIRM_VALUE
    )


def real_decision_ai_enabled() -> bool:
    """Require paid-call opt-in plus an explicit experimental-route label."""
    return (
        real_ai_enabled()
        and os.getenv(REAL_DECISION_AI_ENV) == REAL_DECISION_AI_VALUE
    )


def test_real_decision_ai_requires_explicit_experimental_opt_in(monkeypatch):
    """Normal/approved real-AI mode alone must never spend on Jev."""
    monkeypatch.setenv(REAL_AI_ENV, "1")
    monkeypatch.setenv(REAL_AI_CONFIRM_ENV, REAL_AI_CONFIRM_VALUE)
    monkeypatch.delenv(REAL_DECISION_AI_ENV, raising=False)
    assert not real_decision_ai_enabled()
    monkeypatch.setenv(REAL_DECISION_AI_ENV, REAL_DECISION_AI_VALUE)
    assert real_decision_ai_enabled()


def test_shared_phase0_harness_accepts_authorized_direct_silent_results(
    scn, phase0_provider, monkeypatch
):
    """A valid #382 silent commit has no stream but remains reconnect-safe."""
    decision_service = _OpeningThenSilentDecisionService()
    production_execute = dm_execution.execute_dm_attempt

    def execute_with_direct_silence(db, attempt_id, **kwargs):
        kwargs["decision_service"] = decision_service
        return production_execute(db, attempt_id, **kwargs)

    monkeypatch.setattr(dm_execution, "execute_dm_attempt", execute_with_direct_silence)
    run_phase0_solo_scenario(
        scn,
        expected_reply_marker="phase0-reply-",
        allow_silent=True,
    )

    with scn.factory() as db:
        attempts = list(
            db.execute(select(DmTurnAttempt).order_by(DmTurnAttempt.created_at))
            .scalars()
            .all()
        )
    silent = [
        attempt
        for attempt in attempts
        if (attempt.contract_snapshot or {}).get("mode") == "silent"
        and attempt.stream_id is None
    ]
    assert silent
    assert len(scn.ids["stream_ids"]) < len(scn.ids["attempt_ids"])


def _milliseconds(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds() * 1000))


def _run_metadata(
    scenario,
    *,
    ai_mode: str = "real_generative_pre_alpha",
    decision_route: str = "not_exercised",
) -> dict:
    """Extract only safe real-route identity and latency observations."""
    with scenario.factory() as db:
        runs = list(
            db.execute(select(AIRun).order_by(AIRun.started_at, AIRun.attempt))
            .scalars()
            .all()
        )
        decisions = list(
            db.execute(
                select(DecisionTelemetry).order_by(DecisionTelemetry.created_at)
            )
            .scalars()
            .all()
        )
    return {
        "ai_mode": ai_mode,
        "decision_route": decision_route,
        "runs": [
            {
                "role": run.role,
                "logical_operation": run.logical_operation,
                "provider": run.provider,
                "adapter": run.provider,
                "model": run.model,
                "classification": run.classification,
                "attempt": run.attempt,
                "status": run.status,
                "ttft_ms": _milliseconds(run.started_at, run.first_token_at),
                "turn_duration_ms": _milliseconds(run.started_at, run.completed_at),
            }
            for run in runs
        ],
        "decisions": [
            {
                "decision_class": item.decision_class,
                "provider": item.provider,
                "model": item.model,
                "selected_id": item.selected_id,
                "mode": item.mode,
                "policy_directive": item.policy_directive,
                "candidate_schema_version": item.candidate_schema_version,
                "policy_schema_version": item.policy_schema_version,
                "latency_ms": item.latency_ms,
                "verified": item.verified,
            }
            for item in decisions
        ],
    }


@pytest.mark.real_ai
@pytest.mark.skipif(
    not real_ai_enabled(),
    reason=(
        "real AI is disabled; set E2E_REAL_AI=1 and "
        "E2E_REAL_AI_CONFIRM=paid for a deliberate manual paid run"
    ),
)
def test_phase0_solo_dogfood_with_configured_real_generative_route(scn, monkeypatch):
    """Same #372 flow, with prose-only assertions relaxed for real output."""
    decision_service = _OpenEndedDecisionService()
    production_execute = dm_execution.execute_dm_attempt

    def execute_open_ended(db, attempt_id, **kwargs):
        kwargs["decision_service"] = decision_service
        return production_execute(db, attempt_id, **kwargs)

    # run_dm_execute_sweep resolves execute_dm_attempt from its production
    # module, so this remains a test-local injection with no production change.
    monkeypatch.setattr(dm_execution, "execute_dm_attempt", execute_open_ended)
    try:
        run_phase0_solo_scenario(scn, expected_reply_marker=None)
    finally:
        # Failure artifacts from #374 include this metadata because the
        # shared scenario fixture saves diagnostics after this function exits.
        metadata = _run_metadata(scn)
        scn.diag.record_metadata(real_ai=metadata)

    runs = metadata["runs"]
    scn.check(runs, "diagnostics", "real route produced no AI run telemetry")
    scn.check(
        all(run["role"] == "forward_dm" for run in runs),
        "diagnostics",
        "unexpected AI role in generative-only real scenario",
    )
    scn.check(
        all(run["provider"] and run["model"] for run in runs),
        "diagnostics",
        "real route telemetry lacks provider or model identity",
    )
    scn.check(
        decision_service.decision_calls >= 1,
        "diagnostics",
        "real scenario did not exercise the OPEN_ENDED_DM route",
    )
    scn.check(
        decision_service.adapter_calls == 0,
        "diagnostics",
        "real scenario unexpectedly called a decision adapter",
    )
    # Success needs an operator-readable record too; this uses the same
    # redacted #374 artifact format as failures and contains no credentials
    # or prompt/output content.
    artifact = scn.diag.save_artifact()
    logger.info("phase0-375 real-ai metadata=%s artifact=%s", metadata, artifact)


@pytest.mark.real_ai
@pytest.mark.skipif(
    not real_decision_ai_enabled(),
    reason=(
        "real decision AI is disabled; set E2E_REAL_AI=1, "
        "E2E_REAL_AI_CONFIRM=paid, and "
        "E2E_REAL_DECISION_AI=experimental for deliberate pre-alpha dogfood"
    ),
)
def test_phase0_solo_dogfood_with_experimental_real_decision_route(
    scn, monkeypatch
):
    """Run the shared Phase 0 flow through real Jev routing and generation."""
    decision_service = DecisionService(session_factory=scn.factory)
    production_execute = dm_execution.execute_dm_attempt

    def execute_decision_first(db, attempt_id, **kwargs):
        kwargs["decision_service"] = decision_service
        return production_execute(db, attempt_id, **kwargs)

    monkeypatch.setattr(dm_execution, "execute_dm_attempt", execute_decision_first)
    try:
        run_phase0_solo_scenario(
            scn, expected_reply_marker=None, allow_silent=True
        )
    finally:
        metadata = _run_metadata(
            scn,
            ai_mode="real_generative_with_experimental_decision",
            decision_route="experimental_pre_alpha",
        )
        scn.diag.record_metadata(real_ai=metadata)

    runs = metadata["runs"]
    decisions = metadata["decisions"]
    scn.check(
        any(run["role"] == "decision" for run in runs),
        "diagnostics",
        "real decision route produced no decision AI run telemetry",
    )
    scn.check(
        decisions,
        "diagnostics",
        "real decision route produced no policy telemetry",
    )
    scn.check(
        all(item["provider"] and item["model"] for item in decisions),
        "diagnostics",
        "decision telemetry lacks provider or model identity",
    )
    scn.check(
        all(
            item["candidate_schema_version"] is not None
            and item["policy_schema_version"] is not None
            and item["policy_directive"]
            for item in decisions
        ),
        "diagnostics",
        "decision telemetry lacks candidate/policy/result metadata",
    )
    scn.check(
        metadata["decision_route"] == "experimental_pre_alpha",
        "diagnostics",
        "experimental decision route is not visibly distinguished",
    )
    artifact = scn.diag.save_artifact()
    logger.info(
        "phase0-375 experimental-decision metadata=%s artifact=%s",
        metadata,
        artifact,
    )

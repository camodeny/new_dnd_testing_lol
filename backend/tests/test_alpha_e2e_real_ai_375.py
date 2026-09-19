"""Issue #375 — manually opt-in real generative Phase 0 observation.

Run only with both explicit acknowledgements (and the configured provider
credential) from ``backend``::

    E2E_REAL_AI=1 E2E_REAL_AI_CONFIRM=paid python -m pytest -m real_ai \\
        tests/test_alpha_e2e_real_ai_375.py -v

The double opt-in is intentionally test-local.  Normal pytest and CI runs
skip this module before the scenario fixture or any provider code is invoked.
This currently exercises only the configured forward-DM generative route.
Decision-first route selection remains owned by #382/#258/#269.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

import pytest
from sqlalchemy import select

from app.decisions.contracts import ChoiceResult, DecisionResponse
from app.decisions.frames import OPEN_ENDED_DM_CANDIDATE_ID
from app.dm import execution as dm_execution
from models.reliability import AIRun
from test_alpha_e2e_solo_dogfood_372 import run_phase0_solo_scenario, scn

logger = logging.getLogger(__name__)

REAL_AI_ENV = "E2E_REAL_AI"
REAL_AI_CONFIRM_ENV = "E2E_REAL_AI_CONFIRM"
REAL_AI_CONFIRM_VALUE = "paid"


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


def real_ai_enabled() -> bool:
    """Require two exact, deliberately unambiguous paid-call opt-ins."""
    return (
        os.getenv(REAL_AI_ENV) == "1"
        and os.getenv(REAL_AI_CONFIRM_ENV) == REAL_AI_CONFIRM_VALUE
    )


def _milliseconds(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds() * 1000))


def _run_metadata(scenario) -> dict:
    """Extract only safe real-route identity and latency observations."""
    with scenario.factory() as db:
        runs = list(
            db.execute(select(AIRun).order_by(AIRun.started_at, AIRun.attempt))
            .scalars()
            .all()
        )
    return {
        "ai_mode": "real_generative_pre_alpha",
        "decision_route": "not_exercised",
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

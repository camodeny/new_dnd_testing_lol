"""Issue #375 — manually opt-in real generative Phase 0 observation.

Run only with both explicit acknowledgements (and the configured provider
credential) from ``backend``::

    E2E_REAL_AI=1 E2E_REAL_AI_CONFIRM=paid python -m pytest -m real_ai \\
        tests/test_alpha_e2e_real_ai_375.py -v

The double opt-in is intentionally test-local.  Normal pytest and CI runs
skip this module before the scenario fixture or any provider code is invoked.
It exercises the approved configured forward-DM generative route.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

import pytest
from sqlalchemy import select

from app.dm import execution as dm_execution
from app.dm.contract import CONTRACT_VERSION, normalize_contract
from models.dm import DmTurnAttempt
from models.reliability import AIRun
from test_alpha_e2e_solo_dogfood_372 import (
    phase0_provider,
    run_phase0_solo_scenario,
    scn,
)

logger = logging.getLogger(__name__)

REAL_AI_ENV = "E2E_REAL_AI"
REAL_AI_CONFIRM_ENV = "E2E_REAL_AI_CONFIRM"
REAL_AI_CONFIRM_VALUE = "paid"


def real_ai_enabled() -> bool:
    """Require two exact, deliberately unambiguous paid-call opt-ins."""
    return (
        os.getenv(REAL_AI_ENV) == "1"
        and os.getenv(REAL_AI_CONFIRM_ENV) == REAL_AI_CONFIRM_VALUE
    )


def _silent_adjudicate(packet, feedback=None):
    return normalize_contract({
        "contract_version": CONTRACT_VERSION,
        "mode": "silent",
        "reason": "no player-visible response required",
    })


def test_shared_phase0_harness_accepts_authorized_silent_results(
    scn, phase0_provider, monkeypatch
):
    """A valid silent commit has no stream but remains reconnect-safe."""
    production_execute = dm_execution.execute_dm_attempt
    calls = {"n": 0}

    def execute_then_silent(db, attempt_id, **kwargs):
        # Keep the first visible reply, then complete later turns silently.
        calls["n"] += 1
        if calls["n"] > 1:
            kwargs["adjudicate"] = _silent_adjudicate
        return production_execute(db, attempt_id, **kwargs)

    monkeypatch.setattr(dm_execution, "execute_dm_attempt", execute_then_silent)
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
def test_phase0_solo_dogfood_with_configured_real_generative_route(scn):
    """Same #372 flow, with prose-only assertions relaxed for real output."""
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
    # Success needs an operator-readable record too; this uses the same
    # redacted #374 artifact format as failures and contains no credentials
    # or prompt/output content.
    artifact = scn.diag.save_artifact()
    logger.info("phase0-375 real-ai metadata=%s artifact=%s", metadata, artifact)

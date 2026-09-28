"""Provider-neutral bounded decision service (issue #380).

Gameplay code calls :meth:`DecisionService.decide` with typed questions and
an already-authorized state payload. The service validates the request,
executes it through the configured adapter with bounded retry, re-validates
the response against the caller-supplied candidate sets, and accounts the
call as a ``decision`` AI run — distinct from generative provider calls.
"""

from __future__ import annotations

import json
import math
import time
import uuid
from typing import Any

from app.decisions.adapters.base import DecisionAdapter
from app.decisions.adapters.fake import FakeDecisionAdapter
from app.decisions.adapters.jev import JevAdapter
from app.decisions.config import (
    MAX_DECISION_ATTEMPTS,
    default_max_attempts,
    default_timeout_seconds,
    retry_delay_seconds,
)
from app.decisions.contracts import (
    ChoiceQuestion,
    DecisionRequest,
    DecisionResponse,
    NoulQuestion,
    ScoreQuestion,
)
from app.decisions.errors import DecisionError

DECISION_ROLE = "decision"
DECISION_LOGICAL_OPERATION = "bounded_decision"


def create_adapter(name: str | None = None, **kwargs: Any) -> DecisionAdapter:
    """Build an adapter by name. Branching lives here, not in gameplay code."""
    resolved = (name or "jev").strip().lower()
    if resolved == "jev":
        return JevAdapter()
    if resolved in {"fake", "fake-decision"}:
        return FakeDecisionAdapter(**kwargs)
    raise DecisionError(
        f"unknown decision adapter: {name!r}", kind="unsupported_feature"
    )


def validate_request(request: DecisionRequest) -> None:
    """Reject malformed requests before any adapter/network contact."""
    if not isinstance(request.questions, (list, tuple)) or not request.questions:
        raise DecisionError("decision request has no questions", kind="malformed")
    if request.model is not None and (
        not isinstance(request.model, str) or not request.model.strip()
    ):
        raise DecisionError(
            f"model must be a non-empty string: {request.model!r}",
            kind="malformed",
        )
    # System One state is required and typed (string | object | array); a
    # missing state would fail only after a billable provider attempt.
    if not isinstance(request.state, (str, dict, list, tuple)):
        raise DecisionError(
            "decision request state must be a string, object, or array",
            kind="malformed",
        )
    # The state is the only caller-controlled part of the outbound payload,
    # so its JSON-serializability (including non-finite numbers, which the
    # stdlib encoder would otherwise emit as invalid JSON) is preflighted
    # here, before any AI run starts.
    try:
        json.dumps(request.state, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise DecisionError(
            "decision request state is not JSON-serializable",
            kind="malformed",
        ) from error
    timeout = request.timeout_seconds
    if timeout is not None and (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise DecisionError(
            f"timeout_seconds must be a positive number: {timeout!r}",
            kind="malformed",
        )
    max_attempts = request.max_attempts
    if max_attempts is not None and (
        isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or not 1 <= max_attempts <= MAX_DECISION_ATTEMPTS
    ):
        raise DecisionError(
            f"max_attempts must be an integer 1-{MAX_DECISION_ATTEMPTS}: "
            f"{max_attempts!r}",
            kind="malformed",
        )
    seen: set[str] = set()
    for question in request.questions:
        question_id = getattr(question, "question_id", None)
        if not isinstance(question_id, str) or not question_id:
            raise DecisionError(
                "decision question is missing a question_id", kind="malformed"
            )
        if question_id in seen:
            raise DecisionError(
                f"duplicate question_id: {question_id!r}", kind="malformed"
            )
        seen.add(question_id)
        instructions = getattr(question, "instructions", None)
        if not isinstance(instructions, str) or not instructions.strip():
            raise DecisionError(
                f"question {question_id!r} is missing instructions", kind="malformed"
            )
        if isinstance(question, ChoiceQuestion):
            raw_candidates = getattr(question, "candidates", None)
            if not isinstance(raw_candidates, (list, tuple)):
                raise DecisionError(
                    f"choice question {question_id!r} has malformed candidates",
                    kind="malformed",
                )
            candidates = list(raw_candidates)
            if len(candidates) < 2:
                raise DecisionError(
                    f"choice question {question_id!r} needs at least 2 candidates",
                    kind="malformed",
                )
            if len(candidates) > 255:
                raise DecisionError(
                    f"choice question {question_id!r} exceeds 255 candidates",
                    kind="unsupported_feature",
                )
            candidate_ids = [getattr(c, "id", None) for c in candidates]
            if any(not isinstance(cid, str) or not cid for cid in candidate_ids):
                raise DecisionError(
                    f"choice question {question_id!r} has an empty candidate id",
                    kind="malformed",
                )
            for candidate, candidate_id in zip(candidates, candidate_ids):
                description = getattr(candidate, "description", None)
                if description is not None and not isinstance(description, str):
                    raise DecisionError(
                        f"choice question {question_id!r} has a non-string "
                        f"description for candidate {candidate_id!r}",
                        kind="malformed",
                    )
            if len(set(candidate_ids)) != len(candidate_ids):
                raise DecisionError(
                    f"choice question {question_id!r} has duplicate candidate ids",
                    kind="malformed",
                )
        elif isinstance(question, NoulQuestion):
            continue
        elif isinstance(question, ScoreQuestion):
            raw_levels = getattr(question, "levels", None)
            if not isinstance(raw_levels, (list, tuple)):
                raise DecisionError(
                    f"score question {question_id!r} has malformed levels",
                    kind="malformed",
                )
            levels = list(raw_levels)
            if len(levels) < 2 or len(levels) > 10:
                raise DecisionError(
                    f"score question {question_id!r} needs 2-10 ordered levels",
                    kind="malformed",
                )
            if any(not isinstance(level, str) or not level.strip() for level in levels):
                raise DecisionError(
                    f"score question {question_id!r} has an empty level",
                    kind="malformed",
                )
        else:
            raise DecisionError(
                f"unsupported question type: {type(question).__name__}",
                kind="unsupported_feature",
            )


class DecisionService:
    """Executes bounded decision requests through one adapter."""

    def __init__(
        self,
        adapter: DecisionAdapter | None = None,
        *,
        session_factory: Any = None,
        logical_operation: str = DECISION_LOGICAL_OPERATION,
    ) -> None:
        self._adapter = adapter or JevAdapter()
        self._session_factory = session_factory
        self._logical_operation = logical_operation

    @property
    def adapter(self) -> DecisionAdapter:
        return self._adapter

    def decide(
        self,
        request: DecisionRequest,
        *,
        byok=None,
        decision_role: str | None = None,
        mode: str = "primer",
    ) -> DecisionResponse:
        """Execute one bounded decision request.

        Issue #257 — ``byok`` (``app.byok.accounting.ByokExecution``) runs
        the request through the user's credential when its adapter/model is
        approved for ``decision_role`` in ``mode`` (see
        ``app.byok.routing.resolve_decision_route``). Production callers
        must build ``byok`` via
        ``app.byok.service.resolve_decision_execution`` (the campaign
        authorization boundary); caller-constructed contexts are test-only.
        ``decision_role`` is
        required with ``byok``; unapproved provider/model/role/mode
        combinations raise clearly instead of executing, and possession of
        a decision-provider key is never treated as approval. BYOK runs
        are non-billable with a credential-ID trace plus a zero-amount
        ``byok_marker``; an invalid/expired credential escalates through
        the normal decision error path (the caller falls back to the
        generative DM) without exposing secret details.
        """
        from app.observability import tracing as tracing_module

        validate_request(request)
        adapter = self._adapter
        credential_id = None
        byok_campaign_id = None
        if byok is not None:
            from app.byok.adapters import wrap_decision_adapter
            from app.byok.routing import MODE_PRIMER, resolve_decision_route

            if not decision_role:
                from app.byok.errors import ByokError

                raise ByokError(
                    "decision_role is required for decision-role BYOK execution",
                    kind="malformed",
                )
            candidate_model = request.model or adapter.default_model()
            route = resolve_decision_route(
                decision_role,
                provider=byok.provider,
                model=candidate_model,
                mode=mode,
            )
            adapter = wrap_decision_adapter(route.provider, byok.secret)
            credential_id = byok.credential_id
            byok_campaign_id = byok.campaign_id
            # Approval resolved the effective model; keep telemetry honest.
            request_model_override = route.model
        else:
            request_model_override = None
        capabilities = adapter.capabilities() or {}
        unsupported = sorted(
            {question.kind for question in request.questions}
            - {kind for kind, supported in capabilities.items() if supported}
        )
        if unsupported:
            raise DecisionError(
                f"Adapter {adapter.name} does not support: {', '.join(unsupported)}",
                provider=adapter.name,
                kind="unsupported_feature",
            )
        model = request_model_override or request.model or adapter.default_model()
        timeout = request.timeout_seconds or default_timeout_seconds()
        attempt_limit = max(
            1, int(request.max_attempts or default_max_attempts())
        )

        trace_id = tracing_module.current_trace_id() or uuid.uuid4().hex
        operation_id = tracing_module.current_operation_id() or trace_id
        # Configuration is validated before the first AIRun, so a missing
        # key or bad env never creates a billable run without a real attempt.
        # Adapters keep the defensive check inside execute() as well.
        adapter.require_config(model)
        started = time.monotonic()
        parent_run_id: Any = None
        last_error: DecisionError | None = None
        for attempt in range(1, attempt_limit + 1):
            # One AI run per real provider attempt, linked by parent_run_id,
            # so retries keep failed/succeeded attempt history like the
            # forward-DM failover path instead of collapsing into one row.
            # BYOK runs are user-funded: non-billable with a credential-ID
            # trace, never platform cost.
            run_id = self._start_run(
                adapter, model, trace_id, operation_id,
                attempt=attempt, parent_run_id=parent_run_id,
                billable=False if credential_id is not None else None,
                credential_id=credential_id,
            )
            if parent_run_id is None:
                parent_run_id = self._run_uuid(run_id)
            try:
                data = adapter.execute(request, model=model, timeout=timeout)
                results, answered_model, usage = adapter.parse_response(data, request)
                latency_ms = max(0, int((time.monotonic() - started) * 1000))
                self._finish_run(run_id, status="succeeded", usage=usage)
                if credential_id is not None:
                    from app.byok.accounting import mark_byok_run_fail_soft

                    mark_byok_run_fail_soft(
                        self._session_factory, campaign_id=byok_campaign_id,
                        ai_run_id=self._run_uuid(run_id),
                        credential_id=credential_id,
                        execution_class="decision",
                        role=decision_role or DECISION_ROLE,
                        provider=adapter.name, model=answered_model or model,
                    )
                return DecisionResponse(
                    results=results,
                    provider=adapter.name,
                    model=answered_model or model,
                    latency_ms=latency_ms,
                    trace_id=trace_id,
                    operation_id=operation_id,
                    usage=usage,
                )
            except Exception as error:
                classified = (
                    error
                    if isinstance(error, DecisionError)
                    else adapter.classify_error(error)
                )
                last_error = classified
                self._finish_run(
                    run_id, status="failed", error_type=classified.kind
                )
                if credential_id is not None:
                    # An invalid/expired BYOK credential escalates through
                    # the normal decision error path (callers fall back to
                    # the generative DM) instead of burning bounded retries
                    # against a dead key. Secret details never surface.
                    from app.byok.accounting import is_auth_failure

                    if is_auth_failure(classified) or is_auth_failure(error):
                        from app.byok.service import flag_invalid_credential

                        try:
                            flag_invalid_credential(
                                self._session_factory, credential_id
                            )
                        except Exception:
                            pass
                        raise classified from error
                if attempt < attempt_limit and classified.retryable:
                    time.sleep(retry_delay_seconds(attempt))
                    continue
                raise classified from error
        raise last_error  # pragma: no cover - loop always raises first

    @staticmethod
    def _run_uuid(run_id: Any) -> Any:
        run = getattr(run_id, "id", run_id)
        return run

    def _start_run(
        self, adapter: DecisionAdapter, model: str, trace_id: str, operation_id: str,
        *, attempt: int = 1, parent_run_id: Any = None,
        billable: bool | None = None, credential_id: Any = None,
    ) -> Any:
        if self._session_factory is None:
            return None
        try:
            from app.observability.service import fail_soft, start_ai_run

            def _start() -> Any:
                return start_ai_run(
                    self._session_factory,
                    logical_operation=self._logical_operation,
                    role=DECISION_ROLE,
                    provider=adapter.name,
                    model=model,
                    attempt=attempt,
                    # First attempt is the billable primary; retries are
                    # recovery, matching the forward-DM failover convention.
                    # BYOK runs are user-funded: explicitly non-billable.
                    classification="primary" if attempt == 1 else "recovery",
                    **({"billable": billable} if billable is not None else {}),
                    credential_id=credential_id,
                    parent_run_id=parent_run_id,
                    trace_id=trace_id,
                    operation_id=operation_id,
                )

            # Note: fail_soft consumes a ``trace_id`` kwarg itself, so the
            # run parameters travel inside the closure instead.
            return fail_soft(_start, trace_id=trace_id)
        except Exception:
            return None

    def _finish_run(
        self, run_id: Any, *, status: str, usage: dict | None = None,
        error_type: str | None = None,
    ) -> None:
        if self._session_factory is None or run_id is None:
            return
        try:
            from app.observability.service import fail_soft, finish_ai_run

            run = getattr(run_id, "id", run_id)
            usage = usage or {}
            fail_soft(
                finish_ai_run,
                self._session_factory,
                run,
                status=status,
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
                result_code="decision" if status == "succeeded" else None,
                error_type=error_type,
            )
        except Exception:
            return

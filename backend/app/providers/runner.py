"""Role-policy candidate resolution and AI-run accounting for provider calls.

Shared by every role-policy execution path (forward-DM adjudication,
narration streaming) so the policy-path walk and the AI-run ledger
bookkeeping exist once:

- :func:`resolve_candidate` — approval gate + adapter resolution for one
  ``execution_path`` entry. Index 0 resolves through the role's canonical
  area seam; later entries resolve from the registry.
- :class:`AiRunLedger` — opens an independent telemetry transaction (AI-run
  rows survive gameplay rollback), starts one run per provider call with
  primary/recovery lineage taken from the policy-path index, and finishes it
  with usage, cost, and the campaign usage charge. All accounting is
  best-effort: a ledger failure never fails the provider call.
"""
from __future__ import annotations

from typing import Any, Callable

from app.billing.config import cost_usd_for, tokens_from_usage
from app.billing.ledger import charge_finished_run
from app.observability.service import finish_ai_run, start_ai_run, telemetry_factory_for
from app.providers import policy as role_policy


def require_approved(role: str, provider: str, model: str) -> None:
    """Refuse an unapproved provider/model substitution for ``role``."""
    if not role_policy.is_model_approved(role, provider, model):
        raise RuntimeError(
            f"Unapproved model substitution blocked for role {role!r}: "
            f"{provider}/{model}"
        )


def resolve_candidate(
    role: str,
    path_index: int,
    provider_name: str,
    model: str,
    *,
    resolve_primary: Callable[[], tuple[Any, str, str]] | None = None,
):
    """Resolve one policy-path candidate to ``(adapter, model)``.

    The primary (index 0) resolves through ``resolve_primary`` (defaulting
    to the role's configured area) so existing config gates hold.
    """
    require_approved(role, provider_name, model)
    if path_index == 0:
        if resolve_primary is None:
            from app.providers.areas import resolve_area

            def resolve_primary():
                return resolve_area(role_policy.ROLE_AREA.get(role, role))
        adapter, resolved_model, _ = resolve_primary()
        return adapter, resolved_model
    from app.providers.registry import provider_registry

    adapter = provider_registry.get(provider_name)
    adapter.require_config(model)
    return adapter, model


def classification_for(path_index: int, is_retry: bool) -> str:
    """Lineage follows the POLICY-PATH index: only a first try's primary bills."""
    return "recovery" if (path_index > 0 or is_retry) else "primary"


class _AiRun:
    def __init__(self, ledger: "AiRunLedger", run_id, provider: str, model: str):
        self._ledger = ledger
        self._run_id = run_id
        self._provider = provider
        self._model = model

    @property
    def tracked(self) -> bool:
        return self._run_id is not None

    def succeeded(self, *, usage, result_code: str) -> None:
        if self._run_id is None:
            return
        try:
            in_tokens, out_tokens = tokens_from_usage(usage)
            finish_ai_run(
                self._ledger.telemetry, self._run_id, status="succeeded",
                result_code=result_code,
                input_tokens=in_tokens, output_tokens=out_tokens,
                cost_usd=cost_usd_for(self._provider, self._model, usage),
            )
            if self._ledger.campaign_id is not None:
                charge_finished_run(
                    self._ledger.telemetry, run_id=self._run_id,
                    campaign_id=self._ledger.campaign_id,
                )
        except Exception:
            pass

    def failed(self, exc: BaseException) -> None:
        if self._run_id is None:
            return
        try:
            finish_ai_run(
                self._ledger.telemetry, self._run_id, status="failed",
                error_type=type(exc).__name__[:128],
            )
        except Exception:
            pass


class AiRunLedger:
    """AI-run accounting for one logical operation's provider calls."""

    def __init__(self, db, *, role: str, logical_operation: str,
                 trace_id: str, campaign_id=None):
        self.role = role
        self.logical_operation = logical_operation
        self.trace_id = trace_id
        self.campaign_id = campaign_id
        self.telemetry = None
        if db is not None:
            try:
                self.telemetry = telemetry_factory_for(db)
            except Exception:
                self.telemetry = None

    def start(self, *, adapter, model: str, path_index: int,
              classification: str) -> _AiRun:
        """Start one run; returns a handle whose finish calls are no-ops when untracked."""
        run_id = None
        if self.telemetry is not None:
            try:
                run_id = start_ai_run(
                    self.telemetry, logical_operation=self.logical_operation,
                    role=self.role, provider=adapter.name, model=model,
                    attempt=path_index + 1, classification=classification,
                    billable=(classification == "primary"),
                    trace_id=self.trace_id,
                ).id
            except Exception:
                run_id = None
        return _AiRun(self, run_id, adapter.name, model)

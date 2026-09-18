"""Scenario failure diagnostics for Alpha E2E runs — issue #374.

When an end-to-end scenario fails, the failure must name the exact logical
pipeline stage that broke and preserve the IDs/artifacts needed to
investigate — including bounded decision candidate/policy failures — rather
than collapsing into an opaque assertion or HTTP error.

What this module owns:

- Canonical logical scenario stages (setup → continuation) plus a compact
  completed-stage timeline with the first failed boundary.
- Privacy-safe decision-failure classification: candidate construction,
  model response, execution-policy choice, stale revalidation, and domain
  execution are distinguished using only IDs/versions/counts/directives —
  never raw state, candidate text, or private evidence. This is the
  #383-telemetry shape in miniature: when full decision telemetry lands,
  its records plug into the same ``metadata`` slot.
- Structural expected-vs-actual formatters for revision/event-order and
  reconnect snapshot mismatches.
- Best-effort CI artifact preservation: diagnostic collection never raises,
  never mutates gameplay state, and never masks the original failure.

Deliberately dependency-light (stdlib + decision traces only) so every
later #267 scenario — decision, memory, roll, combat, completion,
multiplayer — can reuse it without dragging in domain services.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Any

from app.decisions.errors import DecisionError
from app.decisions.frames import CANDIDATE_SCHEMA_VERSION, FRAME_SCHEMA_VERSION, frame_trace
from app.decisions.policy import POLICY_SCHEMA_VERSION, policy_trace

logger = logging.getLogger(__name__)

ISSUE_NUMBER = 374

# Canonical logical scenario stages, in pipeline order. Extension stages
# beyond this tuple are permitted in timelines (later scenarios add their
# own), but failure reports for the #374 verification boundaries always use
# one of these.
STAGE_SETUP = "setup"
STAGE_START = "start"
STAGE_OPENING = "opening"
STAGE_SUBMISSION = "submission"
STAGE_CANDIDATE_FRAME = "candidate_frame"
STAGE_DECISION_POLICY = "decision_policy"
STAGE_GENERATIVE_EXECUTION = "generative_execution"
STAGE_COMMIT = "commit"
STAGE_REFRESH_RECONNECT = "refresh_reconnect"
STAGE_CONTINUATION = "continuation"

STAGES = (
    STAGE_SETUP,
    STAGE_START,
    STAGE_OPENING,
    STAGE_SUBMISSION,
    STAGE_CANDIDATE_FRAME,
    STAGE_DECISION_POLICY,
    STAGE_GENERATIVE_EXECUTION,
    STAGE_COMMIT,
    STAGE_REFRESH_RECONNECT,
    STAGE_CONTINUATION,
)

# Fine-grained decision-first failure categories. Every category maps to one
# canonical pipeline stage; the category is what distinguishes e.g. a stale
# frame from a bad model response inside the decision boundary.
CATEGORY_SUBMISSION_EXECUTION = "submission_execution"
CATEGORY_CANDIDATE_CONSTRUCTION = "candidate_construction"
CATEGORY_MODEL_RESPONSE = "model_response"
CATEGORY_EXECUTION_POLICY = "execution_policy"
CATEGORY_STALE_REVALIDATION = "stale_revalidation"
CATEGORY_DOMAIN_EXECUTION = "domain_execution"
CATEGORY_GENERATIVE_EXECUTION = "generative_execution"
CATEGORY_STREAM_PERSISTENCE = "stream_persistence"
CATEGORY_RECONNECT_RECONSTRUCTION = "reconnect_reconstruction"
CATEGORY_DUPLICATE_COMMIT = "duplicate_commit"
CATEGORY_REVISION_ORDERING = "revision_ordering"

CATEGORY_STAGE = {
    CATEGORY_SUBMISSION_EXECUTION: STAGE_SUBMISSION,
    CATEGORY_CANDIDATE_CONSTRUCTION: STAGE_CANDIDATE_FRAME,
    CATEGORY_MODEL_RESPONSE: STAGE_DECISION_POLICY,
    CATEGORY_EXECUTION_POLICY: STAGE_DECISION_POLICY,
    CATEGORY_STALE_REVALIDATION: STAGE_DECISION_POLICY,
    CATEGORY_DOMAIN_EXECUTION: STAGE_COMMIT,
    CATEGORY_GENERATIVE_EXECUTION: STAGE_GENERATIVE_EXECUTION,
    CATEGORY_STREAM_PERSISTENCE: STAGE_COMMIT,
    CATEGORY_RECONNECT_RECONSTRUCTION: STAGE_REFRESH_RECONNECT,
    CATEGORY_DUPLICATE_COMMIT: STAGE_SUBMISSION,
    CATEGORY_REVISION_ORDERING: STAGE_COMMIT,
}

# Dict keys that must never reach CI artifacts (case-insensitive substring).
SECRET_KEY_HINTS = (
    "token",
    "secret",
    "credential",
    "password",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "private_key",
    "access_key",
    "session_key",
)

REDACTED = "[redacted]"
TRUNCATED_SUFFIX = "…[truncated]"
_MAX_STRING = 500
_MAX_DEPTH = 10


def redact(value: Any, _depth: int = 0) -> Any:
    """Return a privacy-safe copy of ``value`` for shared CI artifacts.

    Secret-looking mapping keys are replaced, long strings are truncated,
    and anything not JSON-shaped is replaced by its type name. Never raises.
    """
    try:
        return _redact(value, _depth)
    except Exception:
        return f"<unserializable {type(value).__name__}>"


def _redact(value: Any, depth: int) -> Any:
    if depth > _MAX_DEPTH:
        return f"<max-depth {type(value).__name__}>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) > _MAX_STRING:
            return value[:_MAX_STRING] + TRUNCATED_SUFFIX
        return value
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            name = key if isinstance(key, str) else str(key)
            lowered = name.lower()
            if any(hint in lowered for hint in SECRET_KEY_HINTS):
                out[name] = REDACTED
            else:
                out[name] = _redact(item, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact(item, depth + 1) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_redact(item, depth + 1) for item in value), key=repr)
    return f"<{type(value).__name__}>"


def _jsonable(value: Any) -> Any:
    """Best-effort conversion to JSON-serializable, privacy-safe data."""
    try:
        json.dumps(value)
        return redact(value)
    except Exception:
        return redact(repr(value))


# ── decision-failure metadata (privacy-safe by construction) ───────────────


def frame_metadata(
    frame: Any,
    *,
    role: str | None = None,
    adapter: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    policy_version: int | None = None,
) -> dict[str, Any]:
    """IDs/versions/counts for a decision frame — never state or text.

    Built on :func:`frame_trace` (candidate IDs only) plus caller-supplied
    routing metadata. Raw frame ``state``, candidate labels/hints, provenance
    refs, and payload refs are never included.
    """
    try:
        trace = dict(frame_trace(frame, policy_version=policy_version))
    except Exception:
        trace = {
            "candidate_schema_version": CANDIDATE_SCHEMA_VERSION,
            "frame_schema_version": FRAME_SCHEMA_VERSION,
        }
    trace["candidate_count"] = len(trace.get("candidate_ids", []))
    if role is not None:
        trace["decision_role"] = role
    if adapter is not None:
        trace["adapter"] = adapter
    if model is not None:
        trace["model"] = model
    if provider is not None:
        trace["provider"] = provider
    return trace


def verdict_metadata(frame: Any, verdict: Any) -> dict[str, Any]:
    """Policy outcome for a calibrated selection (direct/primer/escalate)."""
    try:
        return dict(policy_trace(frame, verdict))
    except Exception:
        return {
            "directive": getattr(verdict, "directive", None),
            "policy_schema_version": POLICY_SCHEMA_VERSION,
        }


def _infer_category(error: BaseException) -> str:
    """Map an error to a decision-failure category without model output."""
    if isinstance(error, DecisionError):
        if error.kind == "stale":
            return CATEGORY_STALE_REVALIDATION
        if error.kind in ("timeout", "connection", "http"):
            return CATEGORY_MODEL_RESPONSE
        message = str(error).lower()
        if "no execution policy" in message or message.startswith("policy input"):
            return CATEGORY_EXECUTION_POLICY
        if (
            "unknown candidate" in message
            or "distribution" in message
            or "contradicts" in message
        ):
            return CATEGORY_MODEL_RESPONSE
        if (
            "revalidation" in message
            or "no longer legal" in message
            or "legality" in message
        ):
            return CATEGORY_STALE_REVALIDATION
        return CATEGORY_CANDIDATE_CONSTRUCTION
    return CATEGORY_DOMAIN_EXECUTION


def classify_decision_failure(
    error: BaseException,
    *,
    phase: str | None = None,
    frame: Any = None,
    role: str | None = None,
    adapter: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    selected_id: str | None = None,
    verdict: Any = None,
    current_revision: Any = None,
) -> dict[str, Any]:
    """Classify a decision-first failure into stage + privacy-safe metadata.

    ``phase`` (one of the ``CATEGORY_*`` constants) is trusted when supplied
    by the caller that owns the failing boundary; otherwise the category is
    inferred from the deterministic error taxonomy only — never from model
    output. ``metadata`` carries decision role, adapter/model/provider,
    candidate/question/schema/policy versions, candidate IDs/count, the
    selected result, the direct/primer/escalate outcome, and the
    deterministic revalidation failure. Raw state and candidate text are
    excluded even when the frame is supplied.
    """
    category = phase or _infer_category(error)
    stage = CATEGORY_STAGE.get(category, STAGE_DECISION_POLICY)
    metadata: dict[str, Any] = {}
    if frame is not None:
        metadata.update(
            frame_metadata(
                frame, role=role, adapter=adapter, model=model, provider=provider
            )
        )
    else:
        if role is not None:
            metadata["decision_role"] = role
        if adapter is not None:
            metadata["adapter"] = adapter
        if model is not None:
            metadata["model"] = model
        if provider is not None:
            metadata["provider"] = provider
    if selected_id is not None:
        metadata["selected_id"] = selected_id
    if verdict is not None and frame is not None:
        metadata.update(verdict_metadata(frame, verdict))
    elif verdict is not None:
        metadata["directive"] = getattr(verdict, "directive", None)
    if current_revision is not None:
        metadata["current_revision"] = current_revision
    if isinstance(error, DecisionError):
        metadata["error_kind"] = error.kind
        metadata["retryable"] = error.retryable
    return {
        "category": category,
        "stage": stage,
        "detail": redact(str(error))[:_MAX_STRING],
        "metadata": redact(metadata),
    }


# ── pipeline-boundary formatters ───────────────────────────────────────────


def format_sweep_failure(outcome: dict[str, Any]) -> dict[str, Any]:
    """First failed entry of a ``run_dm_execute_sweep`` outcome shape."""
    failed = outcome.get("failed") or []
    first = dict(failed[0]) if failed else {}
    return {
        "category": CATEGORY_SUBMISSION_EXECUTION,
        "stage": STAGE_SUBMISSION,
        "detail": redact(str(first.get("error", "unknown sweep failure"))),
        "metadata": redact(
            {
                "attempt_id": first.get("attempt_id"),
                "failed_count": len(failed),
                "executed_count": len(outcome.get("executed") or []),
                "skipped_count": len(outcome.get("skipped") or []),
            }
        ),
    }


def format_provider_failure(
    error: BaseException, *, role: str, step: str | None = None
) -> dict[str, Any]:
    """Generative-execution failure naming the logical request, not bytes."""
    return {
        "category": CATEGORY_GENERATIVE_EXECUTION,
        "stage": STAGE_GENERATIVE_EXECUTION,
        "detail": redact(str(error))[:_MAX_STRING],
        "metadata": redact({"decision_role": role, "fixture_step": step}),
    }


def format_commit_failure(
    *, turn_id: str | None, attempt_id: str | None, stream_id: str | None, detail: str
) -> dict[str, Any]:
    """Stream-persistence/commit failure with the durable IDs involved."""
    return {
        "category": CATEGORY_STREAM_PERSISTENCE,
        "stage": STAGE_COMMIT,
        "detail": redact(detail)[:_MAX_STRING],
        "metadata": redact(
            {"turn_id": turn_id, "attempt_id": attempt_id, "stream_id": stream_id}
        ),
    }


def format_revision_mismatch(
    *, expected: list[int], actual: list[int], revision: int
) -> dict[str, Any]:
    """Expected-vs-actual event-sequence comparison (IDs only, no payloads)."""
    expected_set, actual_set = set(expected), set(actual)
    return {
        "category": CATEGORY_REVISION_ORDERING,
        "stage": STAGE_COMMIT,
        "detail": (
            f"revision {revision} != event count {len(actual)} "
            "(revision==sequence invariant broken)"
            if revision != len(actual)
            else f"event sequences not strictly increasing/contiguous: {actual}"
        ),
        "metadata": {
            "revision": revision,
            "expected_sequences": list(expected),
            "actual_sequences": list(actual),
            "missing_sequences": sorted(expected_set - actual_set),
            "extra_sequences": sorted(actual_set - expected_set),
        },
    }


def _structural_ref(value: Any) -> dict[str, Any]:
    """Compact structural fingerprint: type + length + scalar/ID summary."""
    if isinstance(value, list):
        ids = [item.get("id") for item in value if isinstance(item, dict) and "id" in item]
        return {"type": "list", "length": len(value), "ids": ids[:50]}
    if isinstance(value, dict):
        return {
            "type": "dict",
            "length": len(value),
            "keys": sorted(str(k) for k in value)[:50],
        }
    return {"type": type(value).__name__, "length": len(value) if hasattr(value, "__len__") else None}


def format_snapshot_mismatch(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    keys: tuple[str, ...],
    include_content: bool = False,
) -> dict[str, Any]:
    """Privacy-safe reconnect comparison: which keys diverged and how.

    Structural refs (lengths, message IDs) are always included; full values
    only when ``include_content`` is set, still passed through redaction.
    """
    diverged: dict[str, Any] = {}
    for key in keys:
        if before.get(key) != after.get(key):
            entry: dict[str, Any] = {
                "expected_ref": _structural_ref(before.get(key)),
                "actual_ref": _structural_ref(after.get(key)),
            }
            if include_content:
                entry["expected"] = redact(before.get(key))
                entry["actual"] = redact(after.get(key))
            diverged[key] = entry
    return {
        "category": CATEGORY_RECONNECT_RECONSTRUCTION,
        "stage": STAGE_REFRESH_RECONNECT,
        "detail": (
            f"reconnect divergence in snapshot[{', '.join(sorted(diverged))}]"
            if diverged
            else "snapshots match"
        ),
        "metadata": {"diverged_keys": sorted(diverged), "compared_keys": list(keys), "diverged": diverged},
    }


def format_duplicate_commit(*, submission_id: str, turn_ids: list[str]) -> dict[str, Any]:
    """One submission committed by multiple turns (or replayed as new)."""
    return {
        "category": CATEGORY_DUPLICATE_COMMIT,
        "stage": STAGE_SUBMISSION,
        "detail": (
            f"submission {submission_id} committed {len(turn_ids)} times "
            f"(turns={turn_ids})"
        ),
        "metadata": {"submission_id": submission_id, "turn_ids": list(turn_ids)},
    }


# ── scenario collector + CI artifacts ──────────────────────────────────────


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ScenarioDiagnostics:
    """Per-run collector: stage timeline, IDs, and the first failure.

    Purely observational: in-memory bookkeeping plus an optional JSON
    artifact write. Never touches gameplay state, never raises, and never
    masks the caller's original failure — every method degrades to a
    best-effort record.
    """

    def __init__(self, scenario: str = "phase0") -> None:
        self.scenario = scenario
        self._timeline: list[dict[str, Any]] = []
        self._ids: dict[str, Any] = {}
        self._first_failure: dict[str, Any] | None = None

    def begin_stage(self, stage: str) -> None:
        try:
            self._timeline.append({"stage": stage, "status": "started"})
            logger.info("e2e-374 scenario=%s stage=%s started", self.scenario, stage)
        except Exception:
            pass

    def end_stage(self, stage: str) -> None:
        try:
            self._timeline.append({"stage": stage, "status": "completed"})
        except Exception:
            pass

    def record_ids(self, **fields: Any) -> None:
        """Merge identifying fields; list values are extended, scalars set."""
        try:
            for key, value in fields.items():
                if isinstance(value, list) and isinstance(self._ids.get(key), list):
                    self._ids[key].extend(value)
                elif isinstance(value, list):
                    self._ids[key] = list(value)
                else:
                    self._ids[key] = value
        except Exception:
            pass

    def fail(
        self,
        stage: str,
        message: str,
        *,
        category: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record the first failure and return its report (never raises)."""
        try:
            report = {
                "scenario": self.scenario,
                "stage": stage,
                "category": category or "assertion",
                "message": message,
                "detail": _jsonable(detail) if detail is not None else None,
                "ids": _jsonable(self._ids),
            }
            self._timeline.append({"stage": stage, "status": "failed"})
            if self._first_failure is None:
                self._first_failure = report
            logger.error("e2e-374 failure %s", format_failure_line(report))
            return report
        except Exception:
            return {
                "scenario": self.scenario,
                "stage": stage,
                "category": category or "assertion",
                "message": redact(message),
                "detail": None,
                "ids": {},
            }

    def timeline(self) -> list[dict[str, Any]]:
        return list(self._timeline)

    @property
    def first_failure(self) -> dict[str, Any] | None:
        return self._first_failure

    def to_artifact(self) -> dict[str, Any]:
        return {
            "issue": ISSUE_NUMBER,
            "scenario": self.scenario,
            "stages": list(STAGES),
            "timeline": self.timeline(),
            "first_failure": _jsonable(self._first_failure),
            "ids": _jsonable(self._ids),
            "generated_at": _utcnow_iso(),
        }

    def save_artifact(self, directory: str | None = None) -> str | None:
        """Write the JSON artifact for CI; return the path or None.

        Missing diagnostic data never masks the original scenario failure:
        any I/O or serialization problem logs and returns None.
        """
        try:
            target = directory or os.environ.get("E2E_DIAGNOSTICS_DIR") or os.path.join(
                tempfile.gettempdir(), "e2e-diagnostics"
            )
            os.makedirs(target, exist_ok=True)
            stamp = _utcnow_iso().replace(":", "").replace("+", "Z")
            path = os.path.join(target, f"e2e-374-{self.scenario}-{stamp}.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(self.to_artifact(), handle, indent=2, default=str)
            logger.info("e2e-374 artifact saved to %s", path)
            return path
        except Exception as exc:
            logger.warning("e2e-374 artifact save failed (non-fatal): %s", exc)
            return None


def format_failure_line(report: dict[str, Any]) -> str:
    """Single-line CI-greppable summary: ``[374:{stage}] … | ids={…}``."""
    try:
        stage = report.get("stage", "?")
        category = report.get("category", "?")
        message = str(report.get("message", ""))[:300]
        ids = report.get("ids") or {}
        names = ",".join(sorted(str(k) for k in ids)) if isinstance(ids, dict) else "?"
        return f"[374:{stage}] ({category}) {message} | ids={names}"
    except Exception:
        return "[374:?] failure report unformattable (non-fatal)"

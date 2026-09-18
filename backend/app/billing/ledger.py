"""Campaign monetary capacity ledger service — issue #253.

Deterministic accounting authority (code-owned, never delegated to a model):

- Capacity derives from ledger entries + actual primary AI-run cost
  (``AIRun.cost_usd`` → cents), never message/token counts.
- Recovery / non-billable runs are excluded: attempting to charge one
  raises :class:`NonBillableRunError` and writes nothing.
- Each primary billable run maps to exactly one ``ai_spend`` entry
  (``ai_run_id`` unique). Reprocessing returns the existing row.
- Spend writes go only through :func:`record_ai_spend_for_run`, which
  verifies the run is primary/billable/succeeded AND that the run's
  operation trace attributes it to the charged campaign. Raw
  :func:`record_entry` rejects ``ai_spend`` so callers cannot bypass
  those invariants.
- Retries with the same ``(campaign_id, idempotency_key)`` return the
  existing row only when the full accounting payload (type, amount, run,
  contributor) matches — else :class:`LedgerConflictError`.
- Failed writes are recoverable by retrying idempotently: inserts run
  inside a savepoint, so a uniqueness conflict rolls back to the
  savepoint and re-reads the winner instead of poisoning the caller's
  session.
- Ambiguous cost (a succeeded primary run with ``cost_usd is None``)
  raises :class:`AmbiguousCostError`: surfaced, never silently zero,
  never double-charged.
- Corrections are compensating entries (refund / recredit / signed admin
  adjustment). There is deliberately no update/delete API: append-only.
- These functions only ``flush``; the caller owns commit/rollback so an
  accounting failure can never rewrite completed gameplay. Gameplay code
  must treat ledger errors as fail-soft telemetry-style failures.
- Production charging enters through :func:`charge_completed_run`, called
  fail-soft from the authoritative AI-run finalization path after the run
  commits; it reuses the same exactly-once invariants above.

Billing state must never enter DM narrative / rules inputs: this module
imports only ledger/config/models. ``app/dm/context.py`` has no billing
lane (covered by test).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models.reliability import AIRun, OperationTrace
from models.usage import (
    ENTRY_TYPES,
    FUNDED_CREDIT_TYPES,
    ENTRY_TYPE_ADMIN_ADJUSTMENT,
    ENTRY_TYPE_AI_SPEND,
    ENTRY_TYPE_BYOK_MARKER,
    CampaignUsageEntry,
)


class AccountingError(RuntimeError):
    """Base class for deterministic ledger failures."""


class NonBillableRunError(AccountingError):
    """A recovery / non-billable run was offered for charging. Excluded."""


class AmbiguousCostError(AccountingError):
    """A billable run has no usable cost. Surfaced, never guessed."""


class LedgerConflictError(AccountingError):
    """Idempotency-key reuse with conflicting payload, or entry collision."""


# Entry types that must carry a strictly positive amount.
_POSITIVE_TYPES = FUNDED_CREDIT_TYPES
# ai_spend must be strictly negative; byok_marker exactly zero; admin any nonzero? allow zero? require nonzero to keep audit clean.
_AMOUNT_RULES = "positive-funding / negative-spend / zero-byok / nonzero-admin"


def usd_to_cents(cost_usd: float | Decimal | None) -> int:
    """Convert an AI-run USD cost to integer cents (half-up)."""
    if cost_usd is None:
        raise AmbiguousCostError("primary billable run has no cost_usd; refusing to guess zero")
    try:
        cents = int((Decimal(str(cost_usd)) * 100).to_integral_value(rounding=ROUND_HALF_UP))
    except Exception as exc:
        raise AmbiguousCostError(f"unusable cost_usd {cost_usd!r}: {exc}") from exc
    if cents < 0:
        raise AmbiguousCostError(f"negative cost_usd {cost_usd!r} is ambiguous")
    return cents


def _validate_amount(entry_type: str, amount_cents: int) -> None:
    if entry_type not in ENTRY_TYPES:
        raise AccountingError(f"unknown entry_type: {entry_type}")
    if entry_type in _POSITIVE_TYPES:
        if amount_cents <= 0:
            raise AccountingError(f"{entry_type} requires amount_cents > 0, got {amount_cents}")
    elif entry_type == ENTRY_TYPE_AI_SPEND:
        if amount_cents >= 0:
            raise AccountingError(f"ai_spend requires amount_cents < 0, got {amount_cents}")
    elif entry_type == ENTRY_TYPE_BYOK_MARKER:
        if amount_cents != 0:
            raise AccountingError(f"byok_marker requires amount_cents == 0, got {amount_cents}")
    elif entry_type == ENTRY_TYPE_ADMIN_ADJUSTMENT:
        if amount_cents == 0:
            raise AccountingError("admin_adjustment requires nonzero amount_cents")
    else:  # pragma: no cover — guarded by entry-type check above
        raise AccountingError(f"unhandled entry_type: {entry_type}")


def _idempotent_get(db: Session, campaign_id, idempotency_key: str) -> CampaignUsageEntry | None:
    return db.scalar(
        select(CampaignUsageEntry).where(
            CampaignUsageEntry.campaign_id == campaign_id,
            CampaignUsageEntry.idempotency_key == idempotency_key,
        )
    )


def _payload_matches(
    existing: CampaignUsageEntry, *, entry_type: str, amount_cents: int,
    ai_run_id=None, contributor_user_id=None,
) -> bool:
    """One strict matcher for idempotency replay: every persisted field that
    defines a retry-equivalent accounting event must agree. Attribution or
    amount differences are conflicts, never silent replays."""
    existing_run = str(existing.ai_run_id) if existing.ai_run_id else None
    wanted_run = str(ai_run_id) if ai_run_id else None
    existing_contrib = str(existing.contributor_user_id) if existing.contributor_user_id else None
    wanted_contrib = str(contributor_user_id) if contributor_user_id else None
    return (
        existing.entry_type == entry_type
        and existing.amount_cents == amount_cents
        and existing_run == wanted_run
        and existing_contrib == wanted_contrib
    )


def _insert_entry(
    db: Session,
    *,
    campaign_id,
    entry_type: str,
    amount_cents: int,
    idempotency_key: str,
    contributor_user_id=None,
    ai_run_id=None,
    note: str | None = None,
    entry_metadata: dict | None = None,
) -> CampaignUsageEntry:
    """Insert one ledger line inside a savepoint (flush, no commit).

    On a uniqueness conflict the savepoint rolls back — leaving the
    caller-owned session usable — then the winner is re-read and returned
    only when its full payload matches; otherwise
    :class:`LedgerConflictError`.
    """
    entry = CampaignUsageEntry(
        campaign_id=campaign_id,
        entry_type=entry_type,
        amount_cents=amount_cents,
        ai_run_id=ai_run_id,
        contributor_user_id=contributor_user_id,
        idempotency_key=idempotency_key,
        note=note,
        entry_metadata=entry_metadata,
    )
    try:
        with db.begin_nested():
            db.add(entry)
            db.flush()
    except IntegrityError as exc:
        # Savepoint rolled back; the session is usable for the re-read.
        winner = _idempotent_get(db, campaign_id, idempotency_key)
        if winner is not None:
            if not _payload_matches(
                winner, entry_type=entry_type, amount_cents=amount_cents,
                ai_run_id=ai_run_id, contributor_user_id=contributor_user_id,
            ):
                raise LedgerConflictError(
                    f"idempotency_key {idempotency_key!r} already used with different payload"
                ) from exc
            return winner
        if ai_run_id is not None:
            # Key lookup missed but the run was charged under another key
            # (exactly-once race): return it only on full payload agreement.
            by_run = db.scalar(
                select(CampaignUsageEntry).where(CampaignUsageEntry.ai_run_id == ai_run_id)
            )
            if by_run is not None:
                if not _payload_matches(
                    by_run, entry_type=entry_type, amount_cents=amount_cents,
                    ai_run_id=ai_run_id, contributor_user_id=contributor_user_id,
                ):
                    raise LedgerConflictError(
                        f"run {ai_run_id} already charged with different payload"
                    ) from exc
                return by_run
        raise LedgerConflictError(f"ledger write conflict: {exc}") from exc
    return entry


def record_entry(
    db: Session,
    *,
    campaign_id,
    entry_type: str,
    amount_cents: int,
    idempotency_key: str,
    contributor_user_id=None,
    ai_run_id=None,
    note: str | None = None,
    entry_metadata: dict | None = None,
) -> CampaignUsageEntry:
    """Append one non-spend ledger line (flush, no commit). Idempotent on retry.

    A retry with the same ``(campaign_id, idempotency_key)`` returns the
    existing row when the full payload (type/amount/run/contributor)
    matches, else raises :class:`LedgerConflictError` so ambiguity surfaces
    instead of double-posting.

    ``ai_spend`` is rejected here: spend writes must go through
    :func:`record_ai_spend_for_run`, which enforces the
    primary/billable/succeeded + operation-trace invariants.
    """
    if not idempotency_key or not str(idempotency_key).strip():
        raise AccountingError("idempotency_key is required")
    if entry_type == ENTRY_TYPE_AI_SPEND:
        raise AccountingError("ai_spend must go through record_ai_spend_for_run")
    _validate_amount(entry_type, amount_cents)
    if ai_run_id is not None:
        raise AccountingError(f"{entry_type} must not carry ai_run_id")

    existing = _idempotent_get(db, campaign_id, idempotency_key)
    if existing is not None:
        if not _payload_matches(
            existing, entry_type=entry_type, amount_cents=amount_cents,
            ai_run_id=None, contributor_user_id=contributor_user_id,
        ):
            raise LedgerConflictError(
                f"idempotency_key {idempotency_key!r} already used with different payload"
            )
        return existing

    return _insert_entry(
        db,
        campaign_id=campaign_id,
        entry_type=entry_type,
        amount_cents=amount_cents,
        idempotency_key=idempotency_key,
        contributor_user_id=contributor_user_id,
        note=note,
        entry_metadata=entry_metadata,
    )


def record_ai_spend_for_run(
    db: Session,
    *,
    campaign_id,
    ai_run: AIRun,
    idempotency_key: str | None = None,
    note: str | None = None,
) -> CampaignUsageEntry | None:
    """Charge exactly one ``ai_spend`` entry for a primary billable run.

    Returns the existing entry when this run was already charged (no
    double-charge). Raises :class:`NonBillableRunError` for recovery /
    non-billable runs (excluded, writes nothing),
    :class:`AmbiguousCostError` when the run's cost is missing, and
    :class:`LedgerConflictError` when the run's operation trace does not
    attribute it to ``campaign_id`` — a run can never be charged to a
    different campaign than its trace (fail closed, writes nothing).
    """
    if ai_run.classification != "primary" or not ai_run.billable:
        raise NonBillableRunError(
            f"run {ai_run.id} is {ai_run.classification}/billable={ai_run.billable}: excluded from capacity"
        )
    if ai_run.status not in {"succeeded"}:
        raise AmbiguousCostError(
            f"run {ai_run.id} status={ai_run.status!r}: only succeeded primary runs are chargeable"
        )
    trace = db.get(OperationTrace, ai_run.trace_id) if ai_run.trace_id else None
    if trace is None or trace.campaign_id is None or str(trace.campaign_id) != str(campaign_id):
        raise LedgerConflictError(
            f"run {ai_run.id} is not attributable to campaign {campaign_id}: refusing to charge"
        )
    # Exactly-once: a run already charged returns its entry.
    charged = db.scalar(select(CampaignUsageEntry).where(CampaignUsageEntry.ai_run_id == ai_run.id))
    if charged is not None:
        return charged
    cents = usd_to_cents(ai_run.cost_usd)
    if cents == 0:
        # A $0.00 primary run still gets its exactly-one marker entry so
        # reprocessing stays idempotent without moving capacity.
        cents = 0
    # Zero-cost marker: ai_spend normally negative; allow 0 only here.
    key = idempotency_key or f"ai_spend:{ai_run.id}"
    metadata = {"cost_usd": ai_run.cost_usd}
    if cents == 0:
        existing = _idempotent_get(db, campaign_id, key)
        if existing is not None:
            if not _payload_matches(
                existing, entry_type=ENTRY_TYPE_AI_SPEND, amount_cents=0,
                ai_run_id=ai_run.id, contributor_user_id=None,
            ):
                raise LedgerConflictError(
                    f"idempotency_key {key!r} already used with different payload"
                )
            return existing
        return _insert_entry(
            db,
            campaign_id=campaign_id,
            entry_type=ENTRY_TYPE_AI_SPEND,
            amount_cents=0,
            idempotency_key=key,
            ai_run_id=ai_run.id,
            note=note,
            entry_metadata=metadata,
        )
    return _insert_entry(
        db,
        campaign_id=campaign_id,
        entry_type=ENTRY_TYPE_AI_SPEND,
        amount_cents=-cents,
        idempotency_key=key,
        ai_run_id=ai_run.id,
        note=note,
        entry_metadata=metadata,
    )


def charge_completed_run(db: Session, *, run_id, campaign_id) -> CampaignUsageEntry | None:
    """Charge one finalized AI run from the authoritative completion path.

    Flush-only (no commit): call from an independent, fail-soft accounting
    transaction *after* the run row commits, so accounting failure can never
    rewrite gameplay. Returns ``None`` for runs that are not chargeable by
    policy (not succeeded / recovery / non-billable). Raises
    :class:`AmbiguousCostError` for a chargeable run with no usable cost and
    :class:`LedgerConflictError` when the trace does not attribute the run
    to ``campaign_id`` — both surfaced, never silent. Exactly-once per run
    via the ``ai_run_id`` unique constraint (re-finalization replays).
    """
    run = db.get(AIRun, run_id)
    if run is None:
        raise AccountingError(f"AI run {run_id} not found")
    if run.status != "succeeded" or run.classification != "primary" or not run.billable:
        return None
    trace = db.get(OperationTrace, run.trace_id) if run.trace_id else None
    if trace is None:
        trace = OperationTrace(
            trace_id=run.trace_id,
            operation_id=run.operation_id,
            campaign_id=campaign_id,
            submitted_at=datetime.now(timezone.utc),
        )
        db.add(trace)
        db.flush()
    elif trace.campaign_id is None:
        trace.campaign_id = campaign_id
        db.flush()
    return record_ai_spend_for_run(db, campaign_id=campaign_id, ai_run=run)


def _campaign_trace_ids(db: Session, campaign_id) -> list[str]:
    rows = db.scalars(select(OperationTrace.trace_id).where(OperationTrace.campaign_id == campaign_id)).all()
    return list(rows)


def recovery_cost_usd(db: Session, campaign_id) -> float:
    """Sum of non-billable (recovery) run costs attributable to a campaign.

    Tracked separately from capacity: recovery compute is always free.
    Attribution joins through operation traces; runs without a campaign
    trace are not counted (never guessed).
    """
    trace_ids = _campaign_trace_ids(db, campaign_id)
    if not trace_ids:
        return 0.0
    total = db.scalar(
        select(func.coalesce(func.sum(AIRun.cost_usd), 0.0)).where(
            AIRun.trace_id.in_(trace_ids),
            AIRun.billable == False,  # noqa: E712 — portable across PG/SQLite
            AIRun.cost_usd.is_not(None),
        )
    )
    return float(total or 0.0)


def get_capacity_summary(db: Session, campaign_id) -> dict:
    """Derive funded / consumed / remaining / display percentage from ledger.

    - ``funded_cents``: sum of credit types + signed admin adjustments.
    - ``consumed_cents``: abs sum of ``ai_spend`` (billable actuals only).
    - ``remaining_cents``: funded − consumed.
    - ``percent_used``: consumed/funded·100 clamped to [0, 100]; 0.0 when
      nothing is funded and nothing spent, 100.0 when spent with no funding.
    - ``byok_run_markers``: count of zero-amount BYOK markers (no capacity effect).
    - ``recovery_cost_usd``: separately tracked non-billable cost (free).
    - ``contributors``: per-user funded totals (aggregates only).
    """
    entries = db.scalars(select(CampaignUsageEntry).where(CampaignUsageEntry.campaign_id == campaign_id)).all()

    funded = 0
    consumed = 0
    spend_count = 0
    byok_markers = 0
    contributors: dict[str, int] = {}
    for e in entries:
        if e.entry_type in FUNDED_CREDIT_TYPES:
            funded += e.amount_cents
            if e.contributor_user_id is not None:
                key = str(e.contributor_user_id)
                contributors[key] = contributors.get(key, 0) + e.amount_cents
        elif e.entry_type == ENTRY_TYPE_ADMIN_ADJUSTMENT:
            funded += e.amount_cents  # signed operator correction
        elif e.entry_type == ENTRY_TYPE_AI_SPEND:
            consumed += abs(e.amount_cents)
            spend_count += 1
        elif e.entry_type == ENTRY_TYPE_BYOK_MARKER:
            byok_markers += 1

    remaining = funded - consumed
    if funded <= 0:
        percent = 0.0 if consumed <= 0 else 100.0
    else:
        percent = round(consumed / funded * 100.0, 1)
        percent = max(0.0, min(100.0, percent))

    return {
        "campaign_id": str(campaign_id),
        "funded_cents": funded,
        "consumed_cents": consumed,
        "remaining_cents": remaining,
        "percent_used": percent,
        "entry_count": len(entries),
        "spend_entry_count": spend_count,
        "byok_run_markers": byok_markers,
        "recovery_cost_usd": recovery_cost_usd(db, campaign_id),
        "contributors": contributors,
    }


def public_capacity(db: Session, campaign_id) -> dict:
    """Participant-safe aggregate projection (no secrets, no keys)."""
    summary = get_capacity_summary(db, campaign_id)
    return {
        "campaign_id": summary["campaign_id"],
        "funded_cents": summary["funded_cents"],
        "consumed_cents": summary["consumed_cents"],
        "remaining_cents": summary["remaining_cents"],
        "percent_used": summary["percent_used"],
        "contributor_count": len(summary["contributors"]),
    }


def reconcile(db: Session, campaign_id) -> list[str]:
    """Detect ledger/AI-run inconsistencies. Returns human-readable errors.

    Checks: ai_spend entries whose run is missing / non-billable / cost
    mismatch, and succeeded primary billable runs with no entry (uncharged).
    Empty list means reconciled.
    """
    errors: list[str] = []
    entries = db.scalars(
        select(CampaignUsageEntry).where(
            CampaignUsageEntry.campaign_id == campaign_id,
            CampaignUsageEntry.entry_type == ENTRY_TYPE_AI_SPEND,
        )
    ).all()
    for e in entries:
        if e.ai_run_id is None:
            errors.append(f"spend entry {e.id} has no ai_run_id")
            continue
        run = db.get(AIRun, e.ai_run_id)
        if run is None:
            errors.append(f"spend entry {e.id} references missing run {e.ai_run_id}")
            continue
        if run.classification != "primary" or not run.billable:
            errors.append(f"spend entry {e.id} references non-billable run {run.id}")
            continue
        try:
            expected = usd_to_cents(run.cost_usd)
        except AmbiguousCostError:
            # None is always ambiguous for a persisted spend entry: a genuine
            # zero-cost run carries cost_usd=0.0, which converts cleanly, so a
            # zero amount here cannot validate an unknown cost.
            errors.append(
                f"spend entry {e.id} references run {run.id} with ambiguous cost"
            )
            continue
        if abs(e.amount_cents) != expected:
            errors.append(
                f"spend entry {e.id} amount {e.amount_cents} != run cost {expected}c"
            )

    # Uncharged billable runs attributable to this campaign (via traces).
    trace_ids = set(_campaign_trace_ids(db, campaign_id))
    if trace_ids:
        runs = db.scalars(
            select(AIRun).where(
                AIRun.trace_id.in_(trace_ids),
                AIRun.classification == "primary",
                AIRun.billable == True,  # noqa: E712 — portable across PG/SQLite
                AIRun.status == "succeeded",
            )
        ).all()
        charged_ids = {str(e.ai_run_id) for e in entries if e.ai_run_id is not None}
        for run in runs:
            if str(run.id) not in charged_ids:
                # Missing cost is an ambiguity to surface, not a charge gap.
                if run.cost_usd is None:
                    errors.append(f"run {run.id} succeeded without cost: ambiguous, not auto-charged")
                else:
                    errors.append(f"run {run.id} has no spend entry (reprocess idempotently)")
    return errors

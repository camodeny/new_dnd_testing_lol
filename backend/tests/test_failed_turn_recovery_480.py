"""Issue #480 — a failed turn never freezes the table.

Playtest 2026-10-03: a duel turn exhausted validation and ended
failed_visible; for ~15 minutes both players' messages were accepted but
never answered, until the owner pressed Retry (which then succeeded).
"""
from __future__ import annotations

import uuid

import pytest

from app.dm.contract import normalize_contract
from app.dm.execution import _is_recovery_attempt, execute_dm_attempt, run_dm_execute_sweep
from app.dm.turns import coordinate_turn
from app.dm.validators import ValidatorRejectionError
from app.submissions.service import accept_submission
from models.campaigns import Campaign
from models.dm import DmTurn, DmTurnAttempt
from tests.test_dm_mechanics_229 import _contract, _mech, _submit, table  # noqa: F401  (fixture)


def _good(packet, feedback=None):
    return normalize_contract(_contract([], text="The gate creaks open."))


def _failing(char_id):
    # Removing a condition the PC does not have is refused every time.
    bad = _contract([_mech(char_id, kind="condition", condition="poisoned", condition_op="remove")])
    return lambda packet, feedback=None: normalize_contract(bad)


def _failed_turn(s, camp_id, thread_id, char_id):
    turn, attempt = _submit(s, camp_id, thread_id)
    with pytest.raises(ValidatorRejectionError):
        execute_dm_attempt(s, attempt.id, adjudicate=_failing(char_id), narrator="deterministic")
    s.expire_all()
    assert s.get(DmTurn, turn.id).status == "failed_visible"
    return turn, attempt


def test_sweep_retries_a_failed_turn_once_as_recovery(table):
    s, camp_id, thread_id, char_id = table
    turn, failed = _failed_turn(s, camp_id, thread_id, char_id)

    out = run_dm_execute_sweep(s, adjudicate=_good, narrator="deterministic")

    [retry_id] = out["auto_retried"]
    assert out["executed"] == [retry_id]
    s.expire_all()
    assert s.get(DmTurn, turn.id).status == "succeeded"
    assert s.get(DmTurnAttempt, failed.id).abandonment_reason == "auto_retry"
    assert _is_recovery_attempt(s, s.get(DmTurnAttempt, uuid.UUID(retry_id)))  # our failure: not billed


def test_automatic_retry_happens_only_once(table):
    s, camp_id, thread_id, char_id = table
    turn, _ = _failed_turn(s, camp_id, thread_id, char_id)

    first = run_dm_execute_sweep(s, adjudicate=_failing(char_id), narrator="deterministic")
    assert len(first["auto_retried"]) == 1
    s.expire_all()
    assert s.get(DmTurn, turn.id).status == "failed_visible"

    second = run_dm_execute_sweep(s, adjudicate=_good, narrator="deterministic")
    assert second["auto_retried"] == []  # a poison turn never loops


def test_new_player_input_retries_the_failed_turn_with_it(table):
    s, camp_id, thread_id, char_id = table
    turn, failed = _failed_turn(s, camp_id, thread_id, char_id)

    accept_submission(s, campaign_id=camp_id, user_id=s.get(Campaign, camp_id).owner_id,
                      raw_content="Is anyone there?", segments=[{"type": "ic", "text": "Is anyone there?"}],
                      thread_id=str(thread_id))
    s.commit()
    retried_turn, attempt = coordinate_turn(s, camp_id, str(thread_id), commit=True)

    assert retried_turn.id == turn.id and attempt.status == "prepared"
    assert len(attempt.submission_ids) == 2 and set(failed.submission_ids) < set(attempt.submission_ids)
    assert s.get(DmTurnAttempt, failed.id).abandonment_reason == "new_player_input"
    assert not _is_recovery_attempt(s, attempt)  # new player work stays billable

    execute_dm_attempt(s, attempt.id, adjudicate=_good, narrator="deterministic")
    s.expire_all()
    assert s.get(DmTurn, turn.id).status == "succeeded"

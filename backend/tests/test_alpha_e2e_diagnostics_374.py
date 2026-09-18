"""Issue #374 — E2E trace artifacts and stage diagnostics.

Verification: intentionally break submission→execution, decision
candidate/policy, stale decision revalidation, generative provider
execution, stream persistence, reconnect reconstruction, and
duplicate-commit boundaries; each must report the expected stage and
useful identifiers without leaking private state.

The reusable artifact shape lives in ``app.e2e.diagnostics`` so later #267
scenarios (decision, memory, roll, combat, completion, multiplayer) adopt
it instead of rebuilding harness-specific failure output.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest

from app.decisions.errors import DecisionError
from app.decisions.frames import (
    CANDIDATE_SCHEMA_VERSION,
    FRAME_SCHEMA_VERSION,
    CandidateRecord,
    assert_fresh,
    build_frame,
    resolve_candidate,
)
from app.decisions.policy import (
    POLICY_SCHEMA_VERSION,
    evaluate_execution,
)
from app.e2e.diagnostics import (
    CATEGORY_STAGE,
    STAGES,
    ScenarioDiagnostics,
    classify_decision_failure,
    format_commit_failure,
    format_duplicate_commit,
    format_failure_line,
    format_provider_failure,
    format_revision_mismatch,
    format_snapshot_mismatch,
    format_sweep_failure,
    frame_metadata,
    redact,
    verdict_metadata,
)

# Phase 0 production harness reused for the submission→execution integration
# proof: no test-only gameplay path, only the diagnostics wrapper is new.
from test_alpha_e2e_solo_dogfood_372 import (  # noqa: E402
    await_committed_reply,
    drain_dm_execution,
    make_synthetic_character,
    phase0_provider,
    scn,
    setup_solo_campaign,
    start_production_play,
    submit_player_turn,
)

__all__ = ["phase0_provider", "scn"]


def _frame(**overrides):
    kwargs = {
        "decision_class": "skirmish_action",
        "question_id": "maneuver",
        "instructions": "Which maneuver executes next?",
        # Authorized visible state only — and even this must never reach
        # diagnostics metadata.
        "state": {"visible": ["goblin"], "secret_plan": "ambush-at-dawn"},
        "state_revision": 7,
        "candidates": (
            CandidateRecord(
                id="flank",
                label="Flank the goblin",
                source="rules:attack",
                source_ref="state.visible[0]",
                payload_ref="action:flank@goblin",
                debug_hint="advantage via ally",
                risk="low",
                reversible=True,
            ),
            CandidateRecord(
                id="volley",
                label="Loose a volley",
                source="rules:attack",
                risk="standard",
                reversible=True,
            ),
        ),
    }
    kwargs.update(overrides)
    return build_frame(**kwargs)


def _distribution(frame, selected, selected_p=0.7):
    """Complete unit-sum distribution with ``selected`` on top."""
    others = [c.id for c in frame.candidates if c.id != selected]
    rest = (1.0 - selected_p) / len(others)
    return {c.id: (selected_p if c.id == selected else rest) for c in frame.candidates}


# ── collector: timeline, first failure, artifact shape ─────────────────────


def test_timeline_records_boundaries_and_first_failure(tmp_path):
    diag = ScenarioDiagnostics(scenario="unit")
    diag.begin_stage("setup")
    diag.end_stage("setup")
    diag.begin_stage("submission")
    diag.record_ids(campaign_id="camp-1", turn_ids=["t-1"])
    first = diag.fail("submission", "sweep failed", category="submission_execution")
    second = diag.fail("submission", "later noise", category="submission_execution")

    assert [entry["stage"] for entry in diag.timeline()] == [
        "setup",
        "setup",
        "submission",
        "submission",
        "submission",
    ]
    assert [entry["status"] for entry in diag.timeline()] == [
        "started",
        "completed",
        "started",
        "failed",
        "failed",
    ]
    assert diag.first_failure is first
    assert diag.first_failure is not second

    artifact = diag.to_artifact()
    assert artifact["issue"] == 374
    assert artifact["stages"] == list(STAGES)
    assert artifact["ids"]["campaign_id"] == "camp-1"
    assert artifact["first_failure"]["stage"] == "submission"
    assert artifact["generated_at"]

    path = diag.save_artifact(str(tmp_path))
    assert path is not None and os.path.isfile(path)
    loaded = json.load(open(path))
    assert loaded["first_failure"]["ids"]["turn_ids"] == ["t-1"]


def test_diagnostics_never_mask_the_original_failure(tmp_path):
    diag = ScenarioDiagnostics(scenario="unit")
    # Unserializable IDs and an unwritable directory must degrade, not raise.
    diag.record_ids(weird={uuid.uuid4(): object()})
    report = diag.fail("commit", "original boom")
    assert report["message"] == "original boom"
    assert diag.save_artifact(str(tmp_path / "fresh-subdir")) is not None
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir")
    assert diag.save_artifact(str(blocker)) is None


def test_redaction_strips_secrets_and_truncates():
    payload = {
        "auth_token": "abc123",
        "nested": {"api_key": "shh", "turn_id": "t-1"},
        "long": "x" * 600,
        "obj": object(),
    }
    out = redact(payload)
    assert out["auth_token"] == "[redacted]"
    assert out["nested"]["api_key"] == "[redacted]"
    assert out["nested"]["turn_id"] == "t-1"
    assert out["long"].endswith("…[truncated]")
    assert out["obj"] == "<object>"


def test_failure_line_names_stage_and_id_keys():
    line = format_failure_line(
        {
            "stage": "commit",
            "category": "stream_persistence",
            "message": "no chunks",
            "ids": {"turn_id": "t", "campaign_id": "c"},
        }
    )
    assert line.startswith("[374:commit] (stream_persistence) no chunks")
    assert "campaign_id,turn_id" in line


# ── boundary 1: submission→execution ───────────────────────────────────────


def test_sweep_failure_names_submission_stage_with_attempt_id():
    outcome = {
        "executed": ["a-0"],
        "failed": [{"attempt_id": "a-1", "error": "injected provider failure"}],
        "skipped": [],
    }
    failure = format_sweep_failure(outcome)
    assert failure["stage"] == "submission"
    assert failure["category"] == "submission_execution"
    assert failure["metadata"]["attempt_id"] == "a-1"
    assert "injected provider failure" in failure["detail"]


def test_integration_sweep_failure_reports_stage_and_ids(scn, phase0_provider, tmp_path):
    """Real harness prefix + injected execution failure through diagnostics."""
    diag = ScenarioDiagnostics(scenario="phase0-374-proof")
    diag.begin_stage("setup")
    char_id = make_synthetic_character(scn)
    setup_solo_campaign(scn, char_id)
    diag.record_ids(campaign_id=scn.campaign_id, character_id=char_id)
    diag.end_stage("setup")

    diag.begin_stage("opening")
    opening = start_production_play(scn, operation_key="phase0-374-start")
    outcome = drain_dm_execution(scn, "opening")
    assert not outcome.get("failed"), outcome
    await_committed_reply(scn, "opening", opening["dm_turn"]["id"])
    diag.end_stage("opening")

    diag.begin_stage("submission")

    def _boom(packet, feedback=None):
        raise RuntimeError("injected provider failure")

    submitted = submit_player_turn(scn, "I press on.", "phase0-374-exec")
    outcome = drain_dm_execution(scn, "play", adjudicate=_boom)
    assert outcome.get("failed"), "sabotaged sweep unexpectedly succeeded"

    failure = format_sweep_failure(outcome)
    diag.record_ids(
        turn_ids=[submitted["dm_turn"]["id"]],
        attempt_ids=[failure["metadata"]["attempt_id"]],
    )
    report = diag.fail(
        failure["stage"], "sweep failed", category=failure["category"], detail=failure
    )
    assert report["stage"] == "submission"
    assert report["ids"]["turn_ids"] == [submitted["dm_turn"]["id"]]
    assert "[374:submission]" in format_failure_line(report)

    path = diag.save_artifact(str(tmp_path))
    assert path is not None
    loaded = json.load(open(path))
    assert loaded["first_failure"]["category"] == "submission_execution"
    assert loaded["timeline"][-1] == {"stage": "submission", "status": "failed"}


# ── boundary 2: decision candidate/policy ──────────────────────────────────


def test_duplicate_candidates_classify_as_candidate_construction():
    kwargs = {
        "decision_class": "skirmish_action",
        "question_id": "maneuver",
        "instructions": "Pick one.",
        "state": {},
        "state_revision": 1,
        "candidates": (
            CandidateRecord(id="dup", label="One", source="code:t"),
            CandidateRecord(id="dup", label="Two", source="code:t"),
        ),
    }
    with pytest.raises(DecisionError) as exc_info:
        build_frame(**kwargs)
    classified = classify_decision_failure(
        exc_info.value, role="tactics", adapter="fake-decision", model="fake-v1"
    )
    assert classified["category"] == "candidate_construction"
    assert classified["stage"] == "candidate_frame"


def test_unknown_candidate_classifies_as_model_response_with_versions():
    frame = _frame()
    with pytest.raises(DecisionError, match="unknown candidate") as exc_info:
        resolve_candidate(frame, "not-a-candidate")
    classified = classify_decision_failure(
        exc_info.value,
        frame=frame,
        role="tactics",
        adapter="fake-decision",
        model="fake-v1",
        provider="test",
        selected_id="not-a-candidate",
    )
    assert classified["category"] == "model_response"
    assert classified["stage"] == "decision_policy"
    metadata = classified["metadata"]
    assert metadata["decision_role"] == "tactics"
    assert metadata["adapter"] == "fake-decision"
    assert metadata["question_id"] == "maneuver"
    assert metadata["candidate_schema_version"] == CANDIDATE_SCHEMA_VERSION
    assert metadata["frame_schema_version"] == FRAME_SCHEMA_VERSION
    assert metadata["candidate_count"] == len(frame.candidates)
    assert set(metadata["candidate_ids"]) == {c.id for c in frame.candidates}
    assert metadata["selected_id"] == "not-a-candidate"
    # Privacy: raw state, labels, and payload refs never cross into metadata.
    dumped = json.dumps(metadata)
    assert "ambush-at-dawn" not in dumped
    assert "Flank the goblin" not in dumped
    assert "action:flank@goblin" not in dumped


def test_missing_policy_classifies_as_execution_policy():
    frame = _frame(decision_class="unregistered_xyz")
    with pytest.raises(DecisionError, match="no execution policy") as exc_info:
        evaluate_execution(
            frame, "flank", _distribution(_frame(), "flank"), 0.9, verified=True
        )
    classified = classify_decision_failure(
        exc_info.value, frame=frame, role="tactics"
    )
    assert classified["category"] == "execution_policy"
    assert classified["stage"] == "decision_policy"


def test_unverified_selection_records_escalate_outcome():
    frame = _frame()
    verdict = evaluate_execution(
        frame, "flank", _distribution(frame, "flank"), 0.9, verified=False
    )
    assert verdict.directive == "escalate"
    metadata = verdict_metadata(frame, verdict)
    assert metadata["directive"] == "escalate"
    assert metadata["policy_schema_version"] == POLICY_SCHEMA_VERSION
    assert metadata["selected_id"] == "flank"
    assert set(metadata["candidate_ids"]) == {c.id for c in frame.candidates}


def test_frame_metadata_is_privacy_safe_by_construction():
    metadata = frame_metadata(
        _frame(), role="tactics", adapter="a", model="m", provider="p"
    )
    assert metadata["candidate_count"] == len(_frame().candidates)
    dumped = json.dumps(metadata)
    assert "ambush-at-dawn" not in dumped
    assert "Flank the goblin" not in dumped
    assert "action:flank@goblin" not in dumped


# ── boundary 3: stale decision revalidation ────────────────────────────────


def test_stale_frame_classifies_as_stale_revalidation():
    frame = _frame()
    with pytest.raises(DecisionError) as exc_info:
        assert_fresh(frame, 8)
    assert exc_info.value.kind == "stale"
    classified = classify_decision_failure(
        exc_info.value, frame=frame, role="tactics", current_revision=8
    )
    assert classified["category"] == "stale_revalidation"
    assert classified["stage"] == "decision_policy"
    assert classified["metadata"]["error_kind"] == "stale"
    assert classified["metadata"]["state_revision"] == 7
    assert classified["metadata"]["current_revision"] == 8


def test_domain_execution_error_is_distinguished_from_model_errors():
    error = RuntimeError("domain code blew up executing the revalidated candidate")
    classified = classify_decision_failure(error, role="tactics", selected_id="flank")
    assert classified["category"] == "domain_execution"
    assert classified["stage"] == "commit"


# ── boundary 4: generative provider execution ──────────────────────────────


def test_provider_failure_names_logical_request():
    from app.dm.fake_provider import FakeProviderUsageError

    error = FakeProviderUsageError(
        "fake-provider has no fixture for this logical request: "
        "role='forward_dm' step='play-1'"
    )
    failure = format_provider_failure(error, role="forward_dm", step="play-1")
    assert failure["stage"] == "generative_execution"
    assert failure["category"] == "generative_execution"
    assert failure["metadata"]["decision_role"] == "forward_dm"
    assert failure["metadata"]["fixture_step"] == "play-1"
    assert "no fixture" in failure["detail"]


# ── boundary 5: stream persistence / commit ────────────────────────────────


def test_commit_failure_carries_durable_ids():
    failure = format_commit_failure(
        turn_id="t-1",
        attempt_id="a-1",
        stream_id="s-1",
        detail="DM reply has no durable stream chunks",
    )
    assert failure["stage"] == "commit"
    assert failure["category"] == "stream_persistence"
    assert failure["metadata"] == {
        "turn_id": "t-1",
        "attempt_id": "a-1",
        "stream_id": "s-1",
    }


def test_revision_mismatch_shows_expected_vs_actual_sequences():
    failure = format_revision_mismatch(
        expected=[1, 2, 3], actual=[1, 3], revision=3
    )
    assert failure["stage"] == "commit"
    assert failure["category"] == "revision_ordering"
    assert failure["metadata"]["missing_sequences"] == [2]
    assert failure["metadata"]["extra_sequences"] == []
    assert failure["metadata"]["actual_sequences"] == [1, 3]


# ── boundary 6: reconnect reconstruction ───────────────────────────────────


def test_snapshot_mismatch_names_diverged_keys_without_content_by_default():
    before = {
        "campaign": {"id": "c-1"},
        "revision": 4,
        "history": {"messages": [{"id": "m-1"}, {"id": "m-2"}]},
        "dm_messages": [{"id": "s-1"}, {"id": "s-2"}],
    }
    after = {
        "campaign": {"id": "c-1"},
        "revision": 4,
        "history": {"messages": [{"id": "m-1"}, {"id": "m-2"}]},
        "dm_messages": [{"id": "s-1"}],
    }
    failure = format_snapshot_mismatch(
        before, after, keys=("campaign", "revision", "history", "dm_messages")
    )
    assert failure["stage"] == "refresh_reconnect"
    assert failure["category"] == "reconnect_reconstruction"
    assert failure["metadata"]["diverged_keys"] == ["dm_messages"]
    assert "dm_messages" in failure["detail"]
    diverged = failure["metadata"]["diverged"]["dm_messages"]
    assert diverged["expected_ref"]["length"] == 2
    assert diverged["actual_ref"]["length"] == 1
    assert diverged["expected_ref"]["ids"] == ["s-1", "s-2"]
    assert diverged["actual_ref"]["ids"] == ["s-1"]
    assert "expected" not in diverged  # content gated by default


def test_snapshot_content_stays_redacted_when_included():
    before = {"history": [{"id": "m-1", "auth_token": "abc"}]}
    after = {"history": [{"id": "m-1", "auth_token": "abc"}]}
    same = format_snapshot_mismatch(
        before, after, keys=("history",), include_content=True
    )
    assert same["metadata"]["diverged_keys"] == []
    assert same["detail"] == "snapshots match"
    altered = format_snapshot_mismatch(
        before,
        {"history": [{"id": "m-1", "auth_token": "rotated"}]},
        keys=("history",),
        include_content=True,
    )
    entry = altered["metadata"]["diverged"]["history"]
    assert entry["actual"][0]["auth_token"] == "[redacted]"


# ── boundary 7: duplicate commit ───────────────────────────────────────────


def test_duplicate_commit_names_submission_and_turns():
    failure = format_duplicate_commit(submission_id="s-1", turn_ids=["t-1", "t-2"])
    assert failure["stage"] == "submission"
    assert failure["category"] == "duplicate_commit"
    assert failure["metadata"]["submission_id"] == "s-1"
    assert failure["metadata"]["turn_ids"] == ["t-1", "t-2"]
    assert "s-1" in failure["detail"]


def test_every_category_maps_to_a_canonical_stage():
    for category, stage in CATEGORY_STAGE.items():
        assert stage in STAGES, f"{category} -> unknown stage {stage}"

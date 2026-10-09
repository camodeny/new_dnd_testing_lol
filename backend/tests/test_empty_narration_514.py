"""Issue #514 — an empty narration never strands a turn on cron retries.

In a private thread the visibility validator allows ``dm_private`` claims, so
a respond contract whose every claim was DM-private validated, projected to
nothing, streamed zero chunks, and then failed the commit at the stream-start
boundary. The attempt was requeued and only resolved minutes later. These
tests pin the in-process resolution paths.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from database import Base  # noqa: E402
import models  # noqa: E402, F401
from app.dm.contract import (  # noqa: E402
    CONTRACT_VERSION,
    has_audience_visible_content,
    normalize_contract,
)
from app.dm.execution import execute_dm_attempt  # noqa: E402
from app.dm.narration import NarrationProjectionError, stream_narration  # noqa: E402
from app.dm.turns import coordinate_turn  # noqa: E402
from app.dm.validators import AudienceContentValidator, run_with_bounded_regeneration  # noqa: E402
from app.submissions.service import accept_submission  # noqa: E402
from app.threads.service import create_private_thread  # noqa: E402
from models.campaigns import Campaign, CampaignMember  # noqa: E402
from models.dm import DMStream, DmTurn, DmTurnAttempt  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.threads import CampaignThread  # noqa: E402

PUBLIC_TEXT = "The brass lantern slips quietly into your pack; no one looks up."


@pytest.fixture
def ctx():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    owner, alice, camp_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    db.add_all([
        Profile(id=owner, email="owner@example.com"),
        Profile(id=alice, email="alice@example.com"),
        Campaign(id=camp_id, owner_id=owner, name="Empty Narration", revision=0),
        CampaignMember(campaign_id=camp_id, user_id=owner, role="owner"),
        CampaignMember(campaign_id=camp_id, user_id=alice, role="player"),
        CampaignThread(id=uuid.uuid4(), campaign_id=camp_id, thread_type="campaign",
                       title="Campaign", created_by=owner),
    ])
    db.commit()
    private = create_private_thread(db, campaign_id=camp_id, created_by=alice, member_ids=[], title="Whispers")
    db.commit()
    yield {"db": db, "campaign_id": camp_id, "alice": alice, "private_id": private.id}
    db.close()


def _submit_private(ctx, text="(privately) I pocket the brass lantern and tell no one."):
    db = ctx["db"]
    accept_submission(
        db, campaign_id=ctx["campaign_id"], user_id=ctx["alice"], raw_content=text,
        segments=[{"type": "ic", "text": text}],
        thread_id=str(ctx["private_id"]), audience="private",
    )
    db.commit()
    coord = coordinate_turn(db, ctx["campaign_id"], str(ctx["private_id"]), audience="private", commit=False)
    db.commit()
    assert coord is not None
    return coord


def _respond(visibility: str, text: str = PUBLIC_TEXT):
    return normalize_contract({
        "contract_version": CONTRACT_VERSION,
        "mode": "respond",
        "reason": "private object interaction",
        "beats": [{
            "id": "beat_1", "type": "narration",
            "claims": [{
                "text": text, "claim_kind": "observation",
                "origin": "dm_adjudication", "visibility": visibility,
            }],
        }],
    })


def _streams(db, turn_id):
    return db.execute(select(DMStream).where(DMStream.turn_id == str(turn_id))).scalars().all()


def test_all_private_respond_regenerates_in_process_not_cron(ctx):
    """The G01 shape: first contract is all dm_private in a private thread."""
    db = ctx["db"]
    turn, attempt = _submit_private(ctx)
    calls: list[str | None] = []

    def adjudicate(packet, feedback=None):
        calls.append(feedback)
        return _respond("dm_private" if len(calls) == 1 else "public")

    result = execute_dm_attempt(db, attempt.id, adjudicate=adjudicate, narrator="deterministic")

    assert result is not None
    assert len(calls) == 2
    assert "no_audience_visible_content" in (calls[1] or "")
    fresh = db.get(DmTurnAttempt, attempt.id)
    assert fresh.status == "succeeded"
    assert int(fresh.retry_count or 0) == 0
    assert db.get(DmTurn, turn.id).status == "succeeded"
    assert result.narration.visible_text == PUBLIC_TEXT


def test_blank_provider_narration_falls_back_in_process(ctx):
    """A narrator that streams nothing for a non-empty projection still commits."""
    db = ctx["db"]
    turn, attempt = _submit_private(ctx)

    def blank_narrator(request):
        yield ""
        yield "   "

    result = execute_dm_attempt(
        db, attempt.id, adjudicate=lambda packet, feedback=None: _respond("public"),
        narrator=blank_narrator,
    )

    assert result is not None
    assert db.get(DmTurn, turn.id).status == "succeeded"
    fresh = db.get(DmTurnAttempt, attempt.id)
    assert fresh.status == "succeeded"
    assert int(fresh.retry_count or 0) == 0
    assert result.narration.chunk_count >= 1
    assert PUBLIC_TEXT in result.narration.visible_text


def test_empty_projection_never_creates_a_zero_chunk_stream(ctx):
    """Defense in depth: stream_narration refuses before any header exists."""
    db = ctx["db"]
    turn, attempt = _submit_private(ctx)

    with pytest.raises(NarrationProjectionError):
        stream_narration(
            db, campaign_id=ctx["campaign_id"], thread_id=ctx["private_id"],
            turn_id=str(turn.id), attempt_id=str(attempt.id),
            contract=_respond("dm_private"), audience="private", publish_realtime=False,
        )
    assert _streams(db, turn.id) == []


def test_audience_content_validator_scopes_to_narrated_modes():
    validator = AudienceContentValidator()
    assert not has_audience_visible_content(_respond("dm_private"))
    rejected = validator.validate(_respond("dm_private"), None)
    assert not rejected.passed
    assert rejected.violations[0].code == "no_audience_visible_content"
    assert validator.validate(_respond("public"), None).passed
    silent = normalize_contract({"contract_version": CONTRACT_VERSION, "mode": "silent", "reason": "nothing to say"})
    assert validator.validate(silent, None).passed
    # A private beat beside a visible open choice still gives the player something to read.
    mixed = _respond("dm_private").model_copy(update={"open_player_choice": "What do you do next?"})
    assert validator.validate(mixed, None).passed


def test_persistent_all_private_contract_exhausts_bounded_regeneration():
    """Never streamed: exhausting the bound is a pre-narration validator rejection."""
    from app.dm.validators import ValidatorRejectionError

    calls = []

    def adjudicate(packet, feedback=None):
        calls.append(feedback)
        return _respond("dm_private")

    with pytest.raises(ValidatorRejectionError):
        run_with_bounded_regeneration(adjudicate, None, max_regenerations=2)
    assert len(calls) == 3

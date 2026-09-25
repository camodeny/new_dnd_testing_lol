"""Issue #262 — optional player epilogues as canonical post-adventure play.

Verification: no epilogue, one-player epilogue, multiplayer partial
participation, epilogue roll, private epilogue, canonical effect, duplicate
retry, and phase close.
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
from app.adventures.epilogues import (  # noqa: E402
    EPILOGUE_EVENT,
    EpilogueAuthorizationError,
    EpilogueDuplicateError,
    EpilogueError,
    EpilogueStateError,
    close_epilogues,
    epilogue_stats,
    fulfill_epilogue_roll,
    list_epilogues,
    open_epilogues,
    skip_epilogue,
    submit_epilogue,
)
from app.adventures.service import complete_adventure, start_adventure  # noqa: E402
from app.campaigns.events import RevisionConflictError  # noqa: E402
from models.campaigns import (  # noqa: E402
    Adventure,
    AdventureEpilogue,
    Campaign,
    CampaignDomainEvent,
    CampaignMember,
)
from models.characters import Character  # noqa: E402
from models.profiles import Profile  # noqa: E402


def _factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def table():
    """Owner + two players, each with a selected PC, adventure completed."""
    factory = _factory()
    with factory() as db:
        owner, alice, bob = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        db.add_all([
            Profile(id=owner, email="owner@example.com"),
            Profile(id=alice, email="alice@example.com"),
            Profile(id=bob, email="bob@example.com"),
        ])
        camp = Campaign(id=uuid.uuid4(), owner_id=owner, name="Table", status="active", revision=0)
        db.add(camp)
        db.flush()
        pcs = {
            "alice": Character(id=uuid.uuid4(), owner_id=alice, name="Alice PC"),
            "bob": Character(id=uuid.uuid4(), owner_id=bob, name="Bob PC"),
        }
        db.add_all(pcs.values())
        db.flush()
        db.add_all([
            CampaignMember(campaign_id=camp.id, user_id=alice, role="player",
                           selected_character_id=pcs["alice"].id),
            CampaignMember(campaign_id=camp.id, user_id=bob, role="player",
                           selected_character_id=pcs["bob"].id),
        ])
        adv = start_adventure(db, camp.id, "The Goblin Arc")
        camp = db.get(Campaign, camp.id)
        adv, _ = complete_adventure(
            db, camp.id, outcome="victory", reason="Goblins routed",
            public_summary="The village is safe.",
            operation_id="op-complete-262", expected_revision=int(camp.revision),
        )
        db.commit()
        yield factory, camp.id, adv.id, owner, alice, bob, pcs["alice"].id, pcs["bob"].id


def _open(factory, camp_id, adv_id):
    with factory() as db:
        return open_epilogues(db, camp_id, adv_id)


# ── No epilogue: completion stands alone ──────────────────────────────────────


def test_adventure_complete_with_no_epilogues(table):
    factory, camp_id, adv_id, *_ = table
    with factory() as db:
        adv = db.get(Adventure, adv_id)
        assert adv.status == "completed"
        assert adv.epilogue_status == "none"
        stats = epilogue_stats(db, adv_id)
        assert stats["phase"] == "none" and stats["total"] == 0
        assert stats["canonical_effects"] == 0


def test_close_without_open_is_rejected_and_submit_requires_open_phase(table):
    factory, camp_id, adv_id, _, alice, _, alice_pc, _ = table
    with factory() as db:
        with pytest.raises(EpilogueStateError):
            close_epilogues(db, camp_id, adv_id)
        with pytest.raises(EpilogueStateError):
            submit_epilogue(db, camp_id, adv_id, user_id=alice,
                            character_id=alice_pc, content="I retire.")
    _open(factory, camp_id, adv_id)
    with factory() as db:
        stats = close_epilogues(db, camp_id, adv_id)
        assert stats["phase"] == "closed"
        assert stats["total"] == 0 and stats["resolved"] == 0
        adv = db.get(Adventure, adv_id)
        assert adv.status == "completed"  # close never disturbs completion
        with pytest.raises(EpilogueStateError):
            submit_epilogue(db, camp_id, adv_id, user_id=alice,
                            character_id=alice_pc, content="Too late.")


def test_submit_before_completion_is_rejected(table):
    factory, camp_id, *_ = table
    with factory() as db:
        adv2 = start_adventure(db, camp_id, "Second arc")
        db.commit()
        adv2_id = adv2.id
    with factory() as db:
        alice_id = db.execute(select(Profile.id).where(Profile.email == "alice@example.com")).scalar()
        alice_pc = db.execute(select(Character.id).where(Character.owner_id == alice_id)).scalar()
        with pytest.raises(EpilogueStateError):
            open_epilogues(db, camp_id, adv2_id)
        with pytest.raises(EpilogueStateError):
            submit_epilogue(db, camp_id, adv2_id, user_id=alice_id,
                            character_id=alice_pc, content="Early.")


# ── One-player simple epilogue → canonical effect ─────────────────────────────


def test_one_player_simple_epilogue_resolves_canonically(table):
    factory, camp_id, adv_id, _, alice, _, alice_pc, _ = table
    _open(factory, camp_id, adv_id)
    with factory() as db:
        camp = db.get(Campaign, camp_id)
        rev_before = int(camp.revision)
        row, event = submit_epilogue(
            db, camp_id, adv_id, user_id=alice, character_id=alice_pc,
            content="Mira opens a bakery in the village.",
            operation_id="op-epi-alice-1", expected_revision=rev_before,
        )
        db.commit()
        assert row.status == "resolved" and row.kind == "simple"
        assert row.outcome_text == "Mira opens a bakery in the village."
        assert row.source_event_id == event.id
    with factory() as db:
        ev = db.get(CampaignDomainEvent, event.id)
        assert ev.event_type == EPILOGUE_EVENT
        assert ev.visibility == "public"
        assert ev.actor_id == alice
        assert ev.payload["adventure_id"] == str(adv_id)
        assert ev.payload["character_id"] == str(alice_pc)
        assert ev.payload["outcome_text"] == "Mira opens a bakery in the village."
        assert ev.provenance["declared_by"] == "player"
        assert ev.provenance["source"] == "epilogue"
        assert ev.targets["epilogue_id"] == str(row.id)
        camp = db.get(Campaign, camp_id)
        assert ev.sequence == int(camp.revision) == rev_before + 1
        feed = db.execute(
            select(CampaignDomainEvent)
            .where(CampaignDomainEvent.campaign_id == camp_id, CampaignDomainEvent.sequence > rev_before)
        ).scalars().all()
        assert {e.event_type for e in feed} == {EPILOGUE_EVENT}


# ── Multiplayer partial participation ─────────────────────────────────────────


def test_partial_participation_close_reports_skipped_and_missing(table):
    factory, camp_id, adv_id, _, alice, bob, alice_pc, bob_pc = table
    _open(factory, camp_id, adv_id)
    with factory() as db:
        submit_epilogue(db, camp_id, adv_id, user_id=alice, character_id=alice_pc,
                        content="Mira opens a bakery.", operation_id="op-epi-a")
        skip_epilogue(db, camp_id, adv_id, user_id=bob, character_id=bob_pc)
        db.commit()
    with factory() as db:
        # A roster PC with no row stays missing — close still succeeds.
        stats = close_epilogues(db, camp_id, adv_id)
        assert stats["resolved"] == 1 and stats["skipped"] == 1
        assert stats["simple"] == 1 and stats["canonical_effects"] == 1
        assert stats["missing"] == []  # both roster PCs answered
        adv = db.get(Adventure, adv_id)
        assert adv.status == "completed" and adv.epilogue_status == "closed"


def test_missing_roster_pc_does_not_block_close(table):
    factory, camp_id, adv_id, owner, alice, _, alice_pc, bob_pc = table
    with factory() as db:
        owner_pc = Character(id=uuid.uuid4(), owner_id=owner, name="DM PC")
        db.add(owner_pc)
        db.flush()
        member = db.get(CampaignMember, {"campaign_id": camp_id, "user_id": owner})
        if member is None:
            db.add(CampaignMember(campaign_id=camp_id, user_id=owner, role="owner",
                                  selected_character_id=owner_pc.id))
        else:
            member.selected_character_id = owner_pc.id
        db.commit()
        owner_pc_id = owner_pc.id
    _open(factory, camp_id, adv_id)
    with factory() as db:
        submit_epilogue(db, camp_id, adv_id, user_id=alice, character_id=alice_pc,
                        content="Mira opens a bakery.", operation_id="op-epi-a2")
        db.commit()
    with factory() as db:
        stats = close_epilogues(db, camp_id, adv_id)
        assert stats["phase"] == "closed" and stats["resolved"] == 1
        assert set(stats["missing"]) == {str(owner_pc_id), str(bob_pc)}


def test_owner_may_record_skip_for_silent_player_but_not_submit(table):
    factory, camp_id, adv_id, owner, _, bob, _, bob_pc = table
    _open(factory, camp_id, adv_id)
    with factory() as db:
        row = skip_epilogue(db, camp_id, adv_id, user_id=owner, character_id=bob_pc)
        assert row.status == "skipped"
        with pytest.raises(EpilogueAuthorizationError):
            submit_epilogue(db, camp_id, adv_id, user_id=owner, character_id=bob_pc,
                            content="DM writes Bob's ending.")
        db.commit()


# ── Agency: DM never invents voluntary PC choices ─────────────────────────────


def test_dm_and_other_players_cannot_author_or_roll_for_a_pc(table):
    factory, camp_id, adv_id, owner, alice, bob, alice_pc, _ = table
    _open(factory, camp_id, adv_id)
    with factory() as db:
        with pytest.raises(EpilogueAuthorizationError):
            submit_epilogue(db, camp_id, adv_id, user_id=owner, character_id=alice_pc,
                            content="DM decides Mira's fate.")
        with pytest.raises(EpilogueAuthorizationError):
            submit_epilogue(db, camp_id, adv_id, user_id=bob, character_id=alice_pc,
                            content="Bob decides Mira's fate.")
        row, _ = submit_epilogue(
            db, camp_id, adv_id, user_id=alice, character_id=alice_pc,
            content="Mira attempts the perilous climb.",
            needs_adjudication=True,
            roll_spec={"roll_kind": "check", "ability_or_skill": "Athletics",
                       "label": "Cliff climb", "dc": 15},
            operation_id="op-epi-climb",
        )
        assert row.status == "awaiting_roll"
        db.commit()
        epi_id = row.id
    with factory() as db:
        with pytest.raises(EpilogueAuthorizationError):
            fulfill_epilogue_roll(db, epi_id, user_id=bob, die_value=20)
        with pytest.raises(EpilogueAuthorizationError):
            fulfill_epilogue_roll(db, epi_id, user_id=owner, die_value=20)


# ── Adjudicated epilogue rolls (deterministic arithmetic) ─────────────────────


def _submit_climb(factory, camp_id, adv_id, alice, alice_pc, op):
    with factory() as db:
        row, none_event = submit_epilogue(
            db, camp_id, adv_id, user_id=alice, character_id=alice_pc,
            content="Mira attempts the perilous climb.",
            needs_adjudication=True,
            roll_spec={"roll_kind": "check", "ability_or_skill": "Athletics",
                       "label": "Cliff climb", "dc": 15},
            operation_id=op,
        )
        assert none_event is None
        db.commit()
        return row.id


def test_epilogue_roll_success_resolves_canonically(table):
    factory, camp_id, adv_id, _, alice, _, alice_pc, _ = table
    _open(factory, camp_id, adv_id)
    epi_id = _submit_climb(factory, camp_id, adv_id, alice, alice_pc, "op-climb-ok")
    with factory() as db:
        row, event = fulfill_epilogue_roll(db, epi_id, user_id=alice, die_value=15, modifier=2)
        db.commit()
        assert row.status == "resolved" and row.kind == "adjudicated"
        assert row.roll_result == {"die_value": 15, "modifier": 2, "total": 17,
                                   "dc": 15, "success": True, "natural": None}
        assert "success" in (row.outcome_text or "")
        assert row.source_event_id == event.id
    with factory() as db:
        ev = db.get(CampaignDomainEvent, event.id)
        assert ev.event_type == EPILOGUE_EVENT
        assert ev.payload["roll_result"]["success"] is True


def test_epilogue_roll_failure_and_nat_rules(table):
    factory, camp_id, adv_id, _, alice, _, alice_pc, _ = table
    _open(factory, camp_id, adv_id)
    epi_id = _submit_climb(factory, camp_id, adv_id, alice, alice_pc, "op-climb-fail")
    with factory() as db:
        row, _ = fulfill_epilogue_roll(db, epi_id, user_id=alice, die_value=5, modifier=2)
        assert row.roll_result["success"] is False and row.roll_result["total"] == 7
        assert "failure" in (row.outcome_text or "")
        db.commit()
    # Same-PC second epilogue is a duplicate even across roll specs.
    with factory() as db:
        with pytest.raises(EpilogueDuplicateError):
            submit_epilogue(db, camp_id, adv_id, user_id=alice, character_id=alice_pc,
                            content="Mira climbs again.",
                            needs_adjudication=True,
                            roll_spec={"roll_kind": "check", "ability_or_skill": "Athletics",
                                       "label": "Climb", "dc": 5},
                            operation_id="op-climb-second")


def test_nat20_succeeds_and_nat1_fails_regardless_of_dc(table):
    factory, camp_id, adv_id, _, alice, bob, alice_pc, bob_pc = table
    _open(factory, camp_id, adv_id)
    with factory() as db:
        row, _ = submit_epilogue(
            db, camp_id, adv_id, user_id=bob, character_id=bob_pc,
            content="Bram attempts the impossible leap.",
            needs_adjudication=True,
            roll_spec={"roll_kind": "check", "ability_or_skill": "Acrobatics",
                       "label": "Impossible leap", "dc": 30},
            operation_id="op-leap-nat20",
        )
        db.commit()
        nat20_id = row.id
    with factory() as db:
        row, _ = fulfill_epilogue_roll(db, nat20_id, user_id=bob, die_value=20, modifier=0)
        assert row.roll_result["success"] is True
        assert row.roll_result["natural"] == "crit"
        db.commit()
    with factory() as db:
        row, _ = submit_epilogue(
            db, camp_id, adv_id, user_id=alice, character_id=alice_pc,
            content="Mira attempts a trivial hop.",
            needs_adjudication=True,
            roll_spec={"roll_kind": "check", "ability_or_skill": "Acrobatics",
                       "label": "Trivial hop", "dc": 2},
            operation_id="op-hop-nat1",
        )
        db.commit()
        nat1_id = row.id
    with factory() as db:
        row, _ = fulfill_epilogue_roll(db, nat1_id, user_id=alice, die_value=1, modifier=10)
        assert row.roll_result["success"] is False
        assert row.roll_result["natural"] == "fumble"
        db.commit()


def test_invalid_rolls_rejected(table):
    factory, camp_id, adv_id, _, alice, _, alice_pc, _ = table
    _open(factory, camp_id, adv_id)
    epi_id = _submit_climb(factory, camp_id, adv_id, alice, alice_pc, "op-climb-bad")
    with factory() as db:
        for bad in (0, 21, "x", None):
            with pytest.raises(EpilogueError):
                fulfill_epilogue_roll(db, epi_id, user_id=alice, die_value=bad)
        with pytest.raises(EpilogueError):
            fulfill_epilogue_roll(db, epi_id, user_id=alice, die_value=10, modifier=99)
        # Entry still awaiting its roll after invalid attempts.
        db.rollback()
        assert db.get(AdventureEpilogue, epi_id).status == "awaiting_roll"


def test_double_fulfillment_rejected(table):
    factory, camp_id, adv_id, _, alice, _, alice_pc, _ = table
    _open(factory, camp_id, adv_id)
    epi_id = _submit_climb(factory, camp_id, adv_id, alice, alice_pc, "op-climb-double")
    with factory() as db:
        fulfill_epilogue_roll(db, epi_id, user_id=alice, die_value=18, modifier=0)
        db.commit()
    with factory() as db:
        with pytest.raises(EpilogueStateError):
            fulfill_epilogue_roll(db, epi_id, user_id=alice, die_value=18, modifier=0)


# ── Private epilogues ─────────────────────────────────────────────────────────


def test_private_epilogue_keeps_visibility_and_filters_reads(table):
    factory, camp_id, adv_id, owner, alice, bob, alice_pc, _ = table
    _open(factory, camp_id, adv_id)
    with factory() as db:
        row, event = submit_epilogue(
            db, camp_id, adv_id, user_id=alice, character_id=alice_pc,
            content="Mira buries the cursed amulet where none will find it.",
            visibility="private", operation_id="op-epi-secret",
        )
        db.commit()
        assert row.visibility == "private"
        assert event.visibility == "private"
    with factory() as db:
        # Owning player sees content; another player gets metadata only.
        mine = list_epilogues(db, adv_id, viewer_id=alice)
        assert mine[0]["content"] == "Mira buries the cursed amulet where none will find it."
        theirs = list_epilogues(db, adv_id, viewer_id=bob)
        assert "content" not in theirs[0]
        assert theirs[0]["character_id"] == str(alice_pc)
        owner_view = list_epilogues(db, adv_id, viewer_id=owner, is_owner=True)
        assert owner_view[0]["content"] is not None
        stats = epilogue_stats(db, adv_id)
        assert stats["private"] == 1 and stats["canonical_effects"] == 1


# ── Duplicate retry idempotency ───────────────────────────────────────────────


def test_duplicate_operation_replay_returns_original_without_duplicates(table):
    factory, camp_id, adv_id, _, alice, _, alice_pc, _ = table
    _open(factory, camp_id, adv_id)
    with factory() as db:
        row1, event1 = submit_epilogue(
            db, camp_id, adv_id, user_id=alice, character_id=alice_pc,
            content="Mira opens a bakery.", operation_id="op-epi-retry",
        )
        db.commit()
        id1, ev1 = row1.id, event1.id
    with factory() as db:
        row2, event2 = submit_epilogue(
            db, camp_id, adv_id, user_id=alice, character_id=alice_pc,
            content="Mira opens a bakery.", operation_id="op-epi-retry",
        )
        db.commit()
        assert row2.id == id1 and event2.id == ev1
    with factory() as db:
        rows = db.execute(
            select(AdventureEpilogue).where(AdventureEpilogue.adventure_id == adv_id)
        ).scalars().all()
        assert len(rows) == 1
        events = db.execute(
            select(CampaignDomainEvent).where(
                CampaignDomainEvent.campaign_id == camp_id,
                CampaignDomainEvent.event_type == EPILOGUE_EVENT,
            )
        ).scalars().all()
        assert len(events) == 1


def test_second_epilogue_for_same_pc_is_rejected(table):
    factory, camp_id, adv_id, _, alice, _, alice_pc, _ = table
    _open(factory, camp_id, adv_id)
    with factory() as db:
        submit_epilogue(db, camp_id, adv_id, user_id=alice, character_id=alice_pc,
                        content="First ending.", operation_id="op-epi-first")
        db.commit()
    with factory() as db:
        with pytest.raises(EpilogueDuplicateError):
            submit_epilogue(db, camp_id, adv_id, user_id=alice, character_id=alice_pc,
                            content="Second ending.", operation_id="op-epi-second")


def test_skip_after_submit_rejected_and_resubmit_after_skip_allowed(table):
    factory, camp_id, adv_id, _, alice, _, alice_pc, _ = table
    _open(factory, camp_id, adv_id)
    with factory() as db:
        submit_epilogue(db, camp_id, adv_id, user_id=alice, character_id=alice_pc,
                        content="Mira stays.", operation_id="op-epi-nosw")
        with pytest.raises(EpilogueDuplicateError):
            skip_epilogue(db, camp_id, adv_id, user_id=alice, character_id=alice_pc)
        db.commit()
    with factory() as db:
        bob_id = db.execute(select(Profile.id).where(Profile.email == "bob@example.com")).scalar()
        bob_pc = db.execute(select(Character.id).where(Character.owner_id == bob_id)).scalar()
        skipped = skip_epilogue(db, camp_id, adv_id, user_id=bob_id, character_id=bob_pc)
        assert skipped.status == "skipped"
        # Changing their mind converts the skip row — still one row per PC.
        row, event = submit_epilogue(
            db, camp_id, adv_id, user_id=bob_id, character_id=bob_pc,
            content="Bram stays after all.", operation_id="op-epi-mind",
        )
        assert row.id == skipped.id and row.status == "resolved"
        assert event is not None
        db.commit()


# ── Failure/recovery: adventure stays completed ───────────────────────────────


def test_failed_resolution_leaves_adventure_completed_without_half_state(table):
    factory, camp_id, adv_id, _, alice, _, alice_pc, _ = table
    _open(factory, camp_id, adv_id)
    with factory() as db:
        with pytest.raises(RevisionConflictError):
            submit_epilogue(db, camp_id, adv_id, user_id=alice, character_id=alice_pc,
                            content="Mira opens a bakery.",
                            operation_id="op-epi-stale", expected_revision=0)
        db.rollback()
    with factory() as db:
        adv = db.get(Adventure, adv_id)
        assert adv.status == "completed" and adv.epilogue_status == "open"
        assert db.execute(
            select(AdventureEpilogue).where(AdventureEpilogue.adventure_id == adv_id)
        ).scalars().all() == []
        assert db.execute(
            select(CampaignDomainEvent).where(
                CampaignDomainEvent.campaign_id == camp_id,
                CampaignDomainEvent.event_type == EPILOGUE_EVENT,
            )
        ).scalars().all() == []
        # The phase still works after the failed attempt.
        row, event = submit_epilogue(
            db, camp_id, adv_id, user_id=alice, character_id=alice_pc,
            content="Mira opens a bakery.", operation_id="op-epi-stale",
        )
        assert row.status == "resolved" and event is not None
        db.commit()


def test_closed_phase_is_terminal(table):
    factory, camp_id, adv_id, _, alice, _, alice_pc, _ = table
    _open(factory, camp_id, adv_id)
    with factory() as db:
        stats = close_epilogues(db, camp_id, adv_id)
        assert stats["phase"] == "closed"
        again = close_epilogues(db, camp_id, adv_id)
        assert again["phase"] == "closed"
        with pytest.raises(EpilogueStateError):
            open_epilogues(db, camp_id, adv_id)
        with pytest.raises(EpilogueStateError):
            skip_epilogue(db, camp_id, adv_id, user_id=alice, character_id=alice_pc)

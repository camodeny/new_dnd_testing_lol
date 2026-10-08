"""Issue #261 — the adventure-closing sweep awards XP exactly once."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

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
from app.adventures.rewards import level_for_xp  # noqa: E402
from app.adventures.service import (  # noqa: E402
    handle_adventure_closing,
    run_adventure_closing_sweep,
    stage_adventure_closing,
    start_adventure,
)
from app.clock import utcnow  # noqa: E402
from app.rules.bestiary import get_stat_block, stat_block_details  # noqa: E402
from models.campaigns import Adventure, AdventureXpAward, Campaign, CampaignDomainEvent  # noqa: E402
from models.characters import Character, Dnd5eCharacterSheet  # noqa: E402
from models.combat import Encounter, EncounterEndFollowup, EncounterParticipant  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.world import CampaignClock, WorldEntity  # noqa: E402


def _factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _event(db, camp_id, sequence, event_type, *, provenance=None):
    ev = CampaignDomainEvent(
        id=uuid.uuid4(), campaign_id=camp_id, sequence=sequence,
        event_type=event_type, payload={}, visibility="dm_only", provenance=provenance,
    )
    db.add(ev)
    db.flush()
    return ev


def _pc(db, camp_id, owner, name, *, xp=0, level=1):
    char = Character(id=uuid.uuid4(), owner_id=owner, name=name)
    db.add(char)
    db.flush()
    db.add(Dnd5eCharacterSheet(
        character_id=char.id, owner_id=owner, character_name=name,
        level=level, experience_points=xp, hit_points_max=12, hit_points_current=5,
    ))
    db.flush()
    return char.id


def _npc(db, camp_id, name, monster_id=None):
    details = stat_block_details(get_stat_block(monster_id)) if monster_id else {"hit_points": {"current": 4, "maximum": 4}}
    ent = WorldEntity(id=uuid.uuid4(), campaign_id=camp_id, entity_type="npc", name=name, details=details)
    db.add(ent)
    db.flush()
    return ent.id


def _ended_encounter(db, camp_id, *, sequence, pcs, foes, provenance=None):
    """An ended encounter: ``pcs`` character ids, ``foes`` (entity_id, fate) pairs."""
    enc = Encounter(id=uuid.uuid4(), campaign_id=camp_id, thread_id="t", status="ended", ended_at=utcnow())
    db.add(enc)
    db.flush()
    fates = {}
    for cid in pcs:
        p = EncounterParticipant(
            encounter_id=enc.id, campaign_id=camp_id, participant_key=f"pc:{cid}",
            kind="pc", character_id=cid, display_name="pc",
        )
        db.add(p)
        db.flush()
        fates[str(p.id)] = "standing"
    for n, (entity_id, fate) in enumerate(foes):
        p = EncounterParticipant(
            encounter_id=enc.id, campaign_id=camp_id, participant_key=f"npc:{entity_id}:{n}",
            kind="npc", npc_entity_id=entity_id, display_name="foe",
        )
        db.add(p)
        db.flush()
        fates[str(p.id)] = fate
    enc.end_participant_outcomes = fates
    enc.ended_event_id = _event(db, camp_id, sequence, "encounter.ended", provenance=provenance).id
    db.add(EncounterEndFollowup(encounter_id=enc.id, campaign_id=camp_id, hook_type="xp_progression"))
    db.flush()
    return enc.id


@pytest.fixture
def world():
    """Arc covering sequences 5..10, completed by the event at sequence 10.

    - before (seq 3): a slain ogre outside the arc — must not count.
    - fight (seq 6): Ana + Bo vs ogre slain (450), goblin warrior fled (50),
      wolf retreated (0), bandit standing (0), statless thug slain (0) → 250 each.
    - finale (seq 11, staged by the completing turn): Ana alone vs a captured
      wolf (50) → +50.
    - later (seq 12, a later turn): a slain ogre after the arc — must not count.
    """
    factory = _factory()
    with factory() as db:
        owner = uuid.uuid4()
        db.add(Profile(id=owner, email="owner@example.com"))
        camp_id = uuid.uuid4()
        db.add(Campaign(id=camp_id, owner_id=owner, name="Long campaign", status="active", revision=12))
        db.commit()
        adv = start_adventure(db, camp_id, "Goblin warrens", start_sequence=5)
        ana = _pc(db, camp_id, owner, "Ana")
        bo = _pc(db, camp_id, owner, "Bo", xp=6400, level=4)
        ogre = _npc(db, camp_id, "Ogre", "ogre")
        before = _ended_encounter(db, camp_id, sequence=3, pcs=[ana, bo], foes=[(ogre, "slain")])
        fight = _ended_encounter(db, camp_id, sequence=6, pcs=[ana, bo], foes=[
            (ogre, "slain"),
            (_npc(db, camp_id, "Goblin", "goblin-warrior"), "fled"),
            (_npc(db, camp_id, "Wolf", "wolf"), "retreated"),
            (_npc(db, camp_id, "Bandit", "bandit"), "standing"),
            (_npc(db, camp_id, "Thug"), "slain"),
        ])
        completion = _event(db, camp_id, 10, "adventure.completed")
        finale = _ended_encounter(
            db, camp_id, sequence=11, pcs=[ana],
            foes=[(_npc(db, camp_id, "Wolf", "wolf"), "captured")],
            provenance={"source": "dm_effect", "turn_event_id": str(completion.id)},
        )
        later = _ended_encounter(
            db, camp_id, sequence=12, pcs=[ana, bo], foes=[(ogre, "slain")],
            provenance={"source": "dm_effect", "turn_event_id": str(uuid.uuid4())},
        )
        clock = CampaignClock(
            campaign_id=camp_id, name="Warband musters", progress=2, threshold=6,
            advancement_criteria={"kind": "manual"}, provenance={"source": "test"},
        )
        db.add(clock)
        adv.status = "completed"
        adv.outcome = "retreat"
        adv.completed_at = utcnow()
        adv.source_event_id = completion.id
        adv.end_sequence = 10
        stage_adventure_closing(db, adv, operation_id="op-close")
        db.commit()
        yield SimpleNamespace(
            factory=factory, camp_id=camp_id, adv_id=adv.id, ana=ana, bo=bo, ogre=ogre,
            before=before, fight=fight, finale=finale, later=later, clock_id=clock.id,
        )


def _xp(db, character_id):
    return db.execute(
        select(Dnd5eCharacterSheet.experience_points).where(Dnd5eCharacterSheet.character_id == character_id)
    ).scalar_one()


def _hook(db, encounter_id):
    return db.execute(select(EncounterEndFollowup).where(EncounterEndFollowup.encounter_id == encounter_id)).scalar_one()


def test_level_thresholds_follow_2024_advancement():
    assert level_for_xp(0) == 1
    assert level_for_xp(299) == 1
    assert level_for_xp(300) == 2
    assert level_for_xp(6650) == 5
    assert level_for_xp(400000) == 20


def test_closing_awards_defeated_foe_xp_split_among_participating_pcs(world):
    with world.factory() as db:
        assert run_adventure_closing_sweep(db)["failed"] == []
    with world.factory() as db:
        # 450 ogre + 50 fled goblin = 500 over two PCs; Ana adds the captured wolf.
        assert _xp(db, world.ana) == 250 + 50
        assert _xp(db, world.bo) == 6400 + 250
        rows = {r.character_id: r for r in db.execute(select(AdventureXpAward)).scalars()}
        assert rows[world.ana].xp_awarded == 300
        assert rows[world.ana].qualifies_for_level == 2
        assert rows[world.bo].xp_before == 6400 and rows[world.bo].xp_after == 6650
        assert rows[world.bo].qualifies_for_level == 5
        assert [b["share"] for b in rows[world.ana].breakdown] == [250, 50]
        assert _hook(db, world.fight).status == "complete"
        assert _hook(db, world.fight).result["encounter_xp"] == 500
        assert _hook(db, world.finale).status == "complete"
        # Fights outside the arc are left for whichever arc owns them.
        assert _hook(db, world.before).status == "pending"
        assert _hook(db, world.later).status == "pending"
        adv = db.get(Adventure, world.adv_id)
        assert adv.closing_status == "succeeded"
        assert adv.adventure_metadata["closing"]["xp"]["total_awarded"] == 550


def test_closing_twice_never_double_awards(world):
    with world.factory() as db:
        run_adventure_closing_sweep(db)
        # Second sweep: the outbox row is retired, nothing runs.
        assert run_adventure_closing_sweep(db)["executed"] == []
    # Force a full re-run: closing reset, hooks reset, a fresh closing job.
    with world.factory() as db:
        adv = db.get(Adventure, world.adv_id)
        adv.closing_status = "pending"
        for enc_id in (world.fight, world.finale):
            _hook(db, enc_id).status = "pending"
        stage_adventure_closing(db, adv, operation_id="op-close-again")
        db.commit()
        assert len(run_adventure_closing_sweep(db)["executed"]) == 1
    with world.factory() as db:
        handle_adventure_closing(SimpleNamespace(job_id=uuid.uuid4(), payload={"adventure_id": str(world.adv_id)}), db)
    with world.factory() as db:
        assert _xp(db, world.ana) == 300
        assert _xp(db, world.bo) == 6650
        assert len(db.execute(select(AdventureXpAward)).scalars().all()) == 2
        assert db.get(Adventure, world.adv_id).adventure_metadata["closing"]["xp"]["total_awarded"] == 550


def test_closing_leaves_world_characters_and_clocks_unchanged(world):
    def snapshot(db):
        sheets = {
            s.character_id: (s.level, s.hit_points_current, s.hit_points_max, s.conditions)
            for s in db.execute(select(Dnd5eCharacterSheet)).scalars()
        }
        clock = db.get(CampaignClock, world.clock_id)
        camp = db.get(Campaign, world.camp_id)
        return (
            sheets,
            (clock.status, clock.progress, clock.revision),
            db.get(WorldEntity, world.ogre).details,
            (camp.status, camp.revision),
            sorted((e.id, e.status) for e in db.execute(select(Encounter)).scalars()),
        )

    with world.factory() as db:
        before = snapshot(db)
    with world.factory() as db:
        run_adventure_closing_sweep(db)
    with world.factory() as db:
        assert snapshot(db) == before


def test_failed_closing_rolls_back_partial_awards(world, monkeypatch):
    import app.adventures.rewards as rewards

    real = rewards.award_adventure_xp

    def _award_then_fail(db, adventure):
        real(db, adventure)
        raise RuntimeError("recap exploded after awarding")

    monkeypatch.setattr(rewards, "award_adventure_xp", _award_then_fail)
    env = SimpleNamespace(job_id=uuid.uuid4(), payload={"adventure_id": str(world.adv_id)})
    with world.factory() as db:
        with pytest.raises(Exception):
            handle_adventure_closing(env, db)
    with world.factory() as db:
        assert _xp(db, world.ana) == 0
        assert db.execute(select(AdventureXpAward)).scalars().all() == []
        assert _hook(db, world.fight).status == "pending"
        failed = db.get(Adventure, world.adv_id)
        assert failed.closing_status == "failed"
        assert failed.closing_attempts == 1
    monkeypatch.undo()
    with world.factory() as db:
        handle_adventure_closing(env, db)
    with world.factory() as db:
        assert _xp(db, world.ana) == 300
        assert db.get(Adventure, world.adv_id).closing_status == "succeeded"

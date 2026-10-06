"""Issue #252 — leak regression suite: secrets never reach the wrong player.

One campaign holds every secret type at once:

- NPC motive: a dm_only fact and DM-only NPC state (goals, disposition).
- Hidden DC: ``dc_private`` on roll requests, plus another player's private roll result.
- Private action: Alice's submission and narration in her private DM thread.
- Hidden clock: a dm_only clock and a campaign-visible one. Clocks never reach humans.
- Restricted map: a dm_only trap zone and a hidden-entity NPC combatant.

Each surface — narration, snapshot, dm-turn reads, realtime, event feed,
player-facing DM retrieval, and adventure epilogues/summaries — is checked
for an ordinary member (Bob) and for the campaign owner. The AI is the only DM, so the owner must receive exactly a member's
view. Alice is the positive control: the secrets are real, and she does see
the ones that belong to her.
"""

from __future__ import annotations

import json
import uuid
from unittest import mock

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
from app.campaigns.events import list_campaign_events  # noqa: E402
from app.combat.service import (  # noqa: E402
    fulfill_human_initiative,
    get_active_encounter,
    player_event_dict,
)
from app.dm.contract import CONTRACT_VERSION, normalize_contract  # noqa: E402
from app.dm.execution import execute_dm_attempt  # noqa: E402
from app.dm.narration import build_narration_projection, validate_narration_fidelity  # noqa: E402
from app.dm.turns import coordinate_turn  # noqa: E402
from app.realtime.channels import live_table_channel  # noqa: E402
from app.realtime.service import publish_submission_created, set_realtime_publisher  # noqa: E402
from app.rolls.service import fulfill_roll  # noqa: E402
from app.snapshot.service import (  # noqa: E402
    SnapshotAuthorizationError,
    SnapshotNotFoundError,
    build_live_table_snapshot,
)
from app.submissions.service import accept_submission  # noqa: E402
from app.threads.service import (  # noqa: E402
    create_private_thread,
    get_or_create_campaign_thread,
    list_threads_for_user,
)
from app.world import clocks as _clocks  # noqa: E402
from app.world import facts as _facts  # noqa: E402
from app.world import npcs as _npcs  # noqa: E402
from app.world import retrieval as _retrieval  # noqa: E402
from app.world import service as _world  # noqa: E402
from models.campaigns import Campaign, CampaignMember  # noqa: E402
from models.characters import Character, Dnd5eCharacterSheet  # noqa: E402
from models.combat import EncounterParticipant  # noqa: E402
from models.world import CampaignClock  # noqa: E402
from models.dm import DMStream, PlayerRollRequest  # noqa: E402
from models.profiles import Profile  # noqa: E402
from tests.support.realtime import InMemoryRealtimePublisher  # noqa: E402

# One distinctive marker per secret type, so any leak is a plain substring hit.
MOTIVE = "MOTIVE-252"
CLOCK = "CLOCK-252"
PRIVATE_ACTION = "PRIVATE-ACTION-252"
TRAP = "TRAP-252"
DM_NOTE = "DM-NOTE-252"  # DM-private reasoning in Alice's own thread: no human sees it.
LURKER = "LURKER-252"
SHARED_DC = 19
PRIVATE_DC = 17

ALL_MARKERS = (MOTIVE, CLOCK, PRIVATE_ACTION, TRAP, LURKER, DM_NOTE)
# Bob and the owner may see none of these. Alice may see her own private action.
NON_GRANTEE_VIEWERS = ("bob", "owner")


def _contract(claims, *, mode="respond", staged=(), roll_request=None, beats=()):
    payload = {
        "contract_version": CONTRACT_VERSION, "mode": mode, "reason": "leak regression",
        "beats": [{"id": "beat_1", "type": "narration", "claims": claims}, *beats],
        "staged_effects": list(staged),
    }
    if roll_request is not None:
        payload["roll_request"] = roll_request
    return normalize_contract(payload)


def _claim(text, visibility="public"):
    return {"text": text, "claim_kind": "observation", "origin": "dm_adjudication",
            "visibility": visibility}


def _mirela_lies(npc_id, private_truth):
    """Mirela's deceptive line; the truth rides only in DM-private context."""
    ref = {"type": "npc", "id": str(npc_id)}
    return {
        "id": "beat_2", "type": "npc_dialogue", "speaker_ref": ref,
        "speaker_public_name": "Mirela", "truth_status": "deceptive",
        "dm_private_context": private_truth,
        "claims": [{"text": "The cellar is perfectly safe, friends.", "claim_kind": "npc_utterance",
                    "origin": "dm_adjudication", "actor_ref": ref, "visibility": "public"}],
    }


def _run_turn(db, campaign_id, thread_id, user_id, character_id, text, contract, *, audience="campaign"):
    submission = accept_submission(
        db, campaign_id=campaign_id, user_id=user_id, character_id=character_id,
        raw_content=text, segments=[{"type": "ic", "text": text}],
        thread_id=str(thread_id), audience=audience,
    )
    db.commit()
    # Post-commit realtime delivery, as the submissions router does.
    publish_submission_created(db, submission)
    turn, attempt = coordinate_turn(db, campaign_id, str(thread_id), audience=audience, commit=False)
    db.commit()
    result = execute_dm_attempt(db, attempt.id, adjudicate=lambda packet, feedback=None: contract,
                                narrator="deterministic")
    assert result is not None
    return turn


def _seed(db):
    owner, alice, bob = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    campaign_id = uuid.uuid4()
    pcs = {"owner": uuid.uuid4(), "alice": uuid.uuid4(), "bob": uuid.uuid4()}
    users = {"owner": owner, "alice": alice, "bob": bob}
    db.add_all([Profile(id=uid, email=f"{name}@example.com") for name, uid in users.items()])
    db.add(Campaign(id=campaign_id, owner_id=owner, name="Leak Regression", revision=0))
    db.flush()
    for name, uid in users.items():
        db.add(CampaignMember(campaign_id=campaign_id, user_id=uid,
                              role="owner" if name == "owner" else "player",
                              selected_character_id=pcs[name]))
        db.add(Character(id=pcs[name], owner_id=uid, name=f"{name.title()} PC", system="dnd5e"))
        db.add(Dnd5eCharacterSheet(character_id=pcs[name], owner_id=uid,
                                   character_name=f"{name.title()} PC", dexterity=14, level=3))
    db.commit()
    shared = get_or_create_campaign_thread(db, campaign_id, created_by=owner)
    private = create_private_thread(db, campaign_id=campaign_id, created_by=alice,
                                    member_ids=[], title="Whispers")
    db.commit()
    camp = db.get(Campaign, campaign_id)

    # NPC motive: a campaign-visible NPC whose motive lives only in DM state.
    mirela, _ = _world.create_entity(db, camp, entity_type="npc", name="Mirela",
                                     summary="A kindly innkeeper.", visibility="campaign",
                                     operation_id="op-mirela-252")
    motive_fact, _ = _facts.create_fact(
        db, camp, content=f"Mirela secretly serves the Ashen Cult ({MOTIVE})",
        visibility="dm_only", operation_id="op-motive-252")
    # Hidden clocks: campaign-visible or not, clocks are DM machinery.
    _clocks.create_clock(db, camp, name=f"Ashen Rite {CLOCK}", threshold=6,
                         advancement_criteria={"kind": "deterministic"}, visibility="dm_only",
                         provenance={"source": "test-252"}, operation_id="op-clock-252")
    _clocks.create_clock(db, camp, name=f"Bell Tower {CLOCK}", threshold=4,
                         advancement_criteria={"kind": "deterministic"}, visibility="campaign",
                         provenance={"source": "test-252"}, operation_id="op-clock-open-252")
    # Restricted map: a hidden-entity combatant.
    lurker = _world.create_entity(db, camp, entity_type="npc", name=f"Veiled {LURKER}",
                                  visibility="dm_only",
                                  details={"initiative_modifier": 2, "dex_modifier": 1},
                                  operation_id="op-lurker-252")[0]
    db.commit()

    publisher = InMemoryRealtimePublisher()
    set_realtime_publisher(publisher)

    # Private turns run before shared combat: a private-thread attempt cannot
    # yet assemble context while shared encounter events await post-turn
    # processing (tracked separately from this leak suite).
    # Private action in Alice's DM thread, then a private roll with a hidden DC.
    _run_turn(db, campaign_id, private.id, alice, pcs["alice"],
              f"I pocket the cult ledger ({PRIVATE_ACTION}).",
              _contract([_claim(f"You slip the ledger into your coat ({PRIVATE_ACTION}).")]),
              audience="private")
    private_roll_contract = _contract(
        [_claim("Something glints in the ledger's spine."),
         _claim(f"The cipher is a forgery ({DM_NOTE}).", visibility="dm_private")],
        mode="await_roll",
        roll_request={"request_id": "private-roll-252", "character_id": str(pcs["alice"]),
                      "roll_kind": "check", "ability_or_skill": "Investigation",
                      "label": "Study the ledger", "reason_public": "Study the ledger",
                      "dc_private": PRIVATE_DC})
    private_turn = _run_turn(db, campaign_id, private.id, alice, pcs["alice"],
                             f"I study the ledger ({PRIVATE_ACTION}).", private_roll_contract,
                             audience="private")
    private_roll = db.execute(select(PlayerRollRequest).where(
        PlayerRollRequest.thread_id == str(private.id))).scalars().one()
    fulfill_roll(db, request_id=private_roll.id, actor_id=alice, payload={
        "source": "app", "raw_rolls": [16], "modifier": 0, "total": 16, "visibility": "private"})
    db.commit()


    # Shared turn: the DM privately knows the motive, clock and trap, starts a
    # fight with a hidden combatant on a map with a hidden trap.
    shared_contract = _contract(
        [_claim("Mirela smiles and waves you toward the cellar.")],
        beats=[_mirela_lies(mirela.id, f"Mirela leads them to the {TRAP} pit ({MOTIVE}); "
                                       f"the {CLOCK} rite advances.")],
        staged=[{"id": "fx-start-252", "effect_type": "start_encounter", "arguments": {
            "participants": [{"character_id": str(pcs["alice"])},
                             {"npc_entity_id": str(lurker.id)}],
            "map": {"width": 8, "height": 8, "terrain": [
                {"kind": "difficult", "rect": {"col": 0, "row": 0, "width": 2, "height": 2},
                 "visibility": "public"},
                {"kind": "blocked", "rect": {"col": 5, "row": 5, "width": 1, "height": 1},
                 "visibility": "dm_only", "label": f"Hidden pit {TRAP}"},
            ]},
        }}],
    )
    with mock.patch("app.combat.service.secrets.randbelow", return_value=19):
        shared_turn = _run_turn(db, campaign_id, shared.id, owner, pcs["owner"],
                                "We follow Mirela downstairs.", shared_contract)
    # The motive also lives in DM-only NPC state, sourced from that turn.
    camp = db.get(Campaign, campaign_id)
    _npcs.apply_npc_state(
        db, camp, mirela.id, new_revision=int(camp.revision or 0),
        role="innkeeper", goals=[f"deliver the party to the cult ({MOTIVE})"],
        disposition={"toward_party": f"treacherous ({MOTIVE})"},
        field_visibility={"role": "campaign", "goals": "dm_only", "disposition": "dm_only"},
        provenance={"source": "test-252"}, source_turn_id=shared_turn.id,
        operation_id="op-mirela-state-252")
    db.commit()

    encounter = get_active_encounter(db, campaign_id)
    alice_part = db.execute(select(EncounterParticipant).where(
        EncounterParticipant.encounter_id == encounter.id,
        EncounterParticipant.character_id == pcs["alice"])).scalars().one()
    lurker_part = db.execute(select(EncounterParticipant).where(
        EncounterParticipant.encounter_id == encounter.id,
        EncounterParticipant.npc_entity_id == lurker.id)).scalars().one()
    fulfill_human_initiative(db, encounter.id, alice_part.id, actor_id=alice, payload={
        "source": "app", "raw_rolls": [2], "modifier": alice_part.initiative_modifier,
        "total": 2 + alice_part.initiative_modifier})

    # Hidden DC on a shared roll, fulfilled privately by Alice.
    roll_contract = _contract(
        [_claim("The cellar door is stiff.")], mode="await_roll",
        roll_request={"request_id": "shared-roll-252", "character_id": str(pcs["alice"]),
                      "roll_kind": "check", "ability_or_skill": "Athletics",
                      "label": "Force the door", "reason_public": "Force the door",
                      "dc_private": SHARED_DC})
    _run_turn(db, campaign_id, shared.id, alice, pcs["alice"], "I shoulder the door.", roll_contract)
    shared_roll = db.execute(select(PlayerRollRequest).where(
        PlayerRollRequest.thread_id == str(shared.id),
        PlayerRollRequest.roll_kind == "check")).scalars().one()
    fulfill_roll(db, request_id=shared_roll.id, actor_id=alice, payload={
        "source": "app", "raw_rolls": [11], "modifier": 0, "total": 11, "visibility": "private"})
    db.commit()

    return {
        "campaign_id": campaign_id, **users, "pcs": pcs, "shared_id": shared.id,
        "private_id": private.id, "mirela_id": mirela.id, "motive_fact_id": motive_fact.id,
        "lurker_id": lurker.id, "lurker_part_id": lurker_part.id,
        "shared_turn_id": shared_turn.id, "private_turn_id": private_turn.id,
        "publisher": publisher,
    }


def _build_world():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with factory() as db:
            ctx = _seed(db)
    finally:
        set_realtime_publisher(None)
    ctx["factory"] = factory
    ctx["engine"] = engine
    return ctx


@pytest.fixture(scope="module")
def world():
    """Shared read-only scenario. Tests that write use ``fresh_world``."""
    ctx = _build_world()
    yield ctx
    ctx["engine"].dispose()


@pytest.fixture
def fresh_world():
    ctx = _build_world()
    yield ctx
    ctx["engine"].dispose()


@pytest.fixture
def db(world):
    session = world["factory"]()
    yield session
    session.close()


def _blob(value) -> str:
    return json.dumps(value, default=str, sort_keys=True)


def _hidden_ids(world) -> tuple[str, ...]:
    return (str(world["lurker_id"]), str(world["lurker_part_id"]), str(world["motive_fact_id"]),
            str(world["private_id"]), str(world["private_turn_id"]))


def _assert_clean(blob: str, world, *, where: str) -> None:
    for marker in ALL_MARKERS:
        assert marker not in blob, f"{marker} leaked through {where}"
    for hidden_id in _hidden_ids(world):
        assert hidden_id not in blob, f"hidden id {hidden_id} leaked through {where}"
    assert "dc_private" not in blob, f"hidden DC field leaked through {where}"
    for dc in (SHARED_DC, PRIVATE_DC):
        assert f'"dc": {dc}' not in blob, f"hidden DC value leaked through {where}"


# ── Positive control: the secrets really exist and the grantee sees hers ────

def test_secrets_exist_in_dm_state_and_reach_their_grantee(world, db):
    camp = db.get(Campaign, world["campaign_id"])
    assert any(MOTIVE in f.content for f in _facts.list_facts(db, camp.id, limit=200))
    clocks = db.execute(select(CampaignClock).where(CampaignClock.campaign_id == camp.id)).scalars()
    assert {c.name for c in clocks} >= {
        f"Ashen Rite {CLOCK}", f"Bell Tower {CLOCK}"}
    rolls = db.execute(select(PlayerRollRequest).where(PlayerRollRequest.roll_kind == "check")).scalars().all()
    assert {r.dc_private for r in rolls} == {SHARED_DC, PRIVATE_DC}

    alice_private = build_live_table_snapshot(db, camp.id, world["alice"],
                                              thread_id=str(world["private_id"]))
    blob = _blob(alice_private)
    assert PRIVATE_ACTION in blob
    assert DM_NOTE not in blob
    # Alice sees her own private roll result, never the DC.
    (roll,) = alice_private["roll_requests"]
    fulfillment = roll["fulfillment"]
    assert fulfillment["total"] == 16
    assert "dc_private" not in blob


def test_dm_private_reasoning_stays_out_of_the_roller_s_own_turn(world, db):
    """Alice reads her own private dm-turn; DM-private claims are not in it."""
    from models.dm import DmTurn, DmTurnAttempt

    turn = db.get(DmTurn, world["private_turn_id"])
    attempts = db.execute(select(DmTurnAttempt).where(DmTurnAttempt.turn_id == turn.id)).scalars()
    payload = _blob([turn.to_dict(), *[a.to_dict() for a in attempts]])
    assert "Something glints" in payload
    assert DM_NOTE not in payload


# ── Snapshot ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("viewer", NON_GRANTEE_VIEWERS)
def test_shared_snapshot_carries_no_secret(world, db, viewer):
    snap = build_live_table_snapshot(db, world["campaign_id"], world[viewer])
    _assert_clean(_blob(snap), world, where=f"{viewer} shared snapshot")
    # The dm_only trap zone is absent outright: kind and rect, not just its label.
    for zones in (snap["surfaces"]["maps"]["map"]["zones"], snap["encounter"]["map"]["zones"]):
        assert [(z["kind"], z["visibility"]) for z in zones] == [("difficult", "public")]
    # Alice's privately-rolled result stays hers.
    (roll,) = [r for r in snap["roll_requests"] if r["roll_kind"] == "check"]
    assert "total" not in roll["fulfillment"]
    assert "raw_rolls" not in roll["fulfillment"]


@pytest.mark.parametrize("viewer", NON_GRANTEE_VIEWERS)
def test_private_thread_snapshot_refused(world, db, viewer):
    with pytest.raises((SnapshotAuthorizationError, SnapshotNotFoundError)):
        build_live_table_snapshot(db, world["campaign_id"], world[viewer],
                                  thread_id=str(world["private_id"]))
    thread_ids = {str(t.id) for t in list_threads_for_user(db, world["campaign_id"], world[viewer])}
    assert str(world["private_id"]) not in thread_ids


def test_owner_snapshot_equals_member_snapshot(world, db):
    def normalized(viewer):
        snap = build_live_table_snapshot(db, world["campaign_id"], world[viewer])
        snap["meta"].pop("generated_at")
        # Their own character sheet is the only viewer-specific section.
        snap["table"].pop("character")
        for member in snap["table"]["party"]:
            member.pop("is_self")
        return snap

    assert normalized("owner") == normalized("bob")


def test_shared_dm_turns_carry_no_secret(world, db):
    """``GET .../dm-turns`` serializes turns and attempts for any thread reader."""
    from models.dm import DmTurn, DmTurnAttempt

    turns = db.execute(select(DmTurn).where(DmTurn.thread_id == str(world["shared_id"]))).scalars().all()
    attempts = db.execute(select(DmTurnAttempt).where(
        DmTurnAttempt.turn_id.in_([t.id for t in turns]))).scalars().all()
    assert turns and attempts
    _assert_clean(_blob([t.to_dict() for t in turns] + [a.to_dict() for a in attempts]), world,
                  where="shared dm-turn reads")


# ── Realtime ───────────────────────────────────────────────────────────────

def _subscribable_channels(db, world, viewer) -> set[str]:
    return {live_table_channel(world["campaign_id"], t.id)
            for t in list_threads_for_user(db, world["campaign_id"], world[viewer])}


@pytest.mark.parametrize("viewer", NON_GRANTEE_VIEWERS)
def test_realtime_payloads_on_readable_channels_carry_no_secret(world, db, viewer):
    published = world["publisher"].published
    channels = _subscribable_channels(db, world, viewer)
    received = [rec for rec in published if rec["channel"] in channels]
    assert received, "scenario should have published to the shared channel"
    for rec in received:
        _assert_clean(_blob(rec["payload"]), world, where=f"realtime {rec['event']}")


def test_private_action_realtime_only_on_private_channel(world):
    private_channel = live_table_channel(world["campaign_id"], world["private_id"])
    carrying = [rec for rec in world["publisher"].published if PRIVATE_ACTION in _blob(rec)]
    assert {rec["event"] for rec in carrying} >= {"submission.created", "dm.chunk"}
    assert {rec["channel"] for rec in carrying} == {private_channel}


# ── Event feed ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("viewer", NON_GRANTEE_VIEWERS)
def test_event_feed_carries_no_secret(world, db, viewer):
    events = list_campaign_events(db, world["campaign_id"], viewer_id=world[viewer], limit=500)
    assert events
    assert all(e.visibility not in ("dm_only", "dm_private") for e in events)
    # Serialized exactly as GET /events does.
    feed = [player_event_dict(db, e, world[viewer]) for e in events]
    _assert_clean(_blob(feed), world, where=f"{viewer} event feed")


# ── Player-facing DM retrieval ─────────────────────────────────────────────

@pytest.mark.parametrize("viewer", NON_GRANTEE_VIEWERS)
def test_player_facing_retrieval_denies_secrets(world, db, viewer):
    cid, viewers = world["campaign_id"], [world[viewer]]
    assert _retrieval.lookup_fact(db, cid, world["motive_fact_id"], viewers).packets == []
    assert _retrieval.retrieve_entity(db, cid, world["lurker_id"], viewers).packets == []
    assert _retrieval.lookup_source_turn(db, cid, world["private_turn_id"], viewers).packets == []
    timeline = _retrieval.query_timeline(db, cid, viewers, limit=50)
    _assert_clean(_blob([p.to_dict() for p in timeline.packets]), world,
                  where=f"{viewer} timeline retrieval")
    knowledge = _retrieval.query_character_knowledge(db, cid, world["mirela_id"], viewers)
    _assert_clean(_blob([p.to_dict() for p in knowledge.packets]), world,
                  where=f"{viewer} NPC knowledge retrieval")


def test_dm_internal_retrieval_still_sees_secrets(world, db):
    """The AI DM keeps full access; only human-facing reads are filtered."""
    cid = world["campaign_id"]
    fact = _retrieval.lookup_fact(db, cid, world["motive_fact_id"], [], dm_internal=True)
    assert MOTIVE in _blob([p.to_dict() for p in fact.packets])
    lurker = _retrieval.retrieve_entity(db, cid, world["lurker_id"], [], dm_internal=True)
    assert LURKER in _blob([p.to_dict() for p in lurker.packets])


# ── Narration ──────────────────────────────────────────────────────────────

def test_shared_narration_streams_carry_no_secret(world, db):
    streams = db.execute(select(DMStream).where(
        DMStream.campaign_id == world["campaign_id"],
        DMStream.thread_id == world["shared_id"])).scalars().all()
    assert streams
    for stream in streams:
        _assert_clean(stream.final_text or "", world, where="shared narration")


_SECRET_CONTRACTS = {
    "npc_motive": (_contract([_claim("Mirela pours the ale.")], beats=[_mirela_lies("npc:mirela", f"Mirela serves the cult ({MOTIVE}).")]),
                   f"Mirela serves the cult ({MOTIVE})."),
    "hidden_dc": (_contract([_claim("The lock looks old.")], mode="await_roll",
                            roll_request={"request_id": "dc252", "character_id": str(uuid.uuid4()),
                                          "roll_kind": "check", "ability_or_skill": "Sleight of Hand",
                                          "label": "Pick the lock", "reason_public": "Pick the lock",
                                          "dc_private": SHARED_DC}),
                  f"It will take a DC {SHARED_DC} to open."),
    "private_action": (_contract([_claim("The common room is quiet."),
                                  _claim(f"Alice pocketed the ledger ({PRIVATE_ACTION}).",
                                         "dm_private")]),
                       f"Alice pocketed the ledger ({PRIVATE_ACTION})."),
    "hidden_clock": (_contract([_claim("Bells ring in the distance."),
                                _claim(f"The Ashen Rite {CLOCK} is two steps from done.", "dm_private")]),
                     f"The Ashen Rite {CLOCK} is two steps from done."),
    "restricted_map": (_contract([_claim("The cellar floor is dusty."),
                                  _claim(f"A hidden pit {TRAP} lies by the stair.", "dm_private")]),
                       f"A hidden pit {TRAP} lies by the stair."),
}


@pytest.mark.parametrize("secret_type", sorted(_SECRET_CONTRACTS))
def test_narrator_never_receives_the_secret(secret_type):
    contract, _ = _SECRET_CONTRACTS[secret_type]
    projection = _blob(build_narration_projection(contract))
    for marker in ALL_MARKERS:
        assert marker not in projection
    assert str(SHARED_DC) not in projection


@pytest.mark.parametrize("secret_type", sorted(_SECRET_CONTRACTS))
def test_narration_that_states_the_secret_is_rejected(secret_type):
    contract, leaking_text = _SECRET_CONTRACTS[secret_type]
    violations = validate_narration_fidelity(leaking_text, contract)
    assert any(v["category"] == "secret_leakage" for v in violations), violations


def test_dm_events_attributed_to_the_owner_stay_hidden_from_the_owner(fresh_world):
    """The AI's lifecycle events are committed with the owner as actor
    (e.g. ``encounter.ended``). The feed, retrieval, the adventure recap and
    the owner's summary controls must not hand them back."""
    from types import SimpleNamespace

    from app.adventures.router import get_recap, retry_generate
    from app.campaigns.events import commit_campaign_mutation
    from models.campaigns import Adventure

    world = fresh_world
    owner = world["owner"]
    with world["factory"]() as db:
        camp = db.get(Campaign, world["campaign_id"])
        for visibility in ("dm_only", "dm_private"):
            commit_campaign_mutation(
                db, camp.id, int(camp.revision), event_type="encounter.ended",
                payload={"summary": f"The {LURKER} slips away ({MOTIVE})",
                         "thread_id": str(world["shared_id"])},
                operation_id=f"op-owner-actor-{visibility}-252", actor_id=owner,
                visibility=visibility,
            )
            db.refresh(camp)
        adventure = Adventure(campaign_id=camp.id, title="The Ashen Cellar", status="completed",
                              reason=f"The cult won ({MOTIVE})", start_sequence=0,
                              end_sequence=int(camp.revision))
        db.add(adventure)
        db.commit()

        events = list_campaign_events(db, camp.id, viewer_id=owner, limit=500)
        _assert_clean(_blob([player_event_dict(db, e, owner) for e in events]), world,
                      where="owner-actor DM events")
        timeline = _retrieval.query_timeline(db, camp.id, [owner], limit=50)
        _assert_clean(_blob([p.to_dict() for p in timeline.packets]), world,
                      where="owner-actor DM events in retrieval")

        profile = SimpleNamespace(id=owner)
        generated = retry_generate(adventure_id=str(adventure.id), payload={},
                                   profile=profile, camp=camp, db=db)
        assert generated["summary"]["status"] == "current"
        assert "historical_text" not in generated["summary"]
        _assert_clean(_blob(generated), world, where="owner summary controls")
        recap = get_recap(adventure_id=str(adventure.id), profile=profile, camp=camp, db=db)
        assert recap["recap_text"]
        _assert_clean(_blob(recap), world, where="owner recap")


# ── Adventure close: private epilogues and owner summary controls ─────────

def _adventure(db, world, *, reason):
    from models.campaigns import Adventure

    adventure = Adventure(campaign_id=world["campaign_id"], title="The Ashen Cellar",
                          status="completed", reason=reason)
    db.add(adventure)
    db.flush()
    return adventure


def test_private_epilogue_reaches_only_its_author(fresh_world):
    from app.adventures.epilogues import list_epilogues
    from models.campaigns import AdventureEpilogue

    world = fresh_world
    db = world["factory"]()
    adventure = _adventure(db, world, reason="Epilogue test")
    db.add(AdventureEpilogue(
        adventure_id=adventure.id, campaign_id=world["campaign_id"],
        character_id=world["pcs"]["alice"], user_id=world["alice"], kind="adjudicated",
        status="resolved", visibility="private",
        content=f"Alice burns the ledger ({PRIVATE_ACTION})",
        roll_spec={"ability_or_skill": "Stealth", "dc": PRIVATE_DC},
        roll_result={"die_value": 15, "modifier": 2, "total": 17, "success": True},
        outcome_text=f"Alice burns the ledger ({PRIVATE_ACTION}) [d20 15+2=17 vs DC {PRIVATE_DC}]",
    ))
    db.commit()
    (mine,) = list_epilogues(db, adventure.id, viewer_id=world["alice"])
    assert PRIVATE_ACTION in mine["outcome_text"]
    for viewer in NON_GRANTEE_VIEWERS:
        (entry,) = list_epilogues(db, adventure.id, viewer_id=world[viewer])
        assert entry["status"] == "resolved"
        assert "kind" not in entry and "has_roll" not in entry
        _assert_clean(_blob(entry), world, where=f"{viewer} epilogue roster")
        assert f"DC {PRIVATE_DC}" not in _blob(entry)
    db.close()


def test_owner_summary_controls_return_no_hidden_sources():
    """``summaries/generate`` and ``mark-stale`` answer the owner with status only."""
    from models.campaigns import Adventure, AdventureSummary

    adventure = Adventure(id=uuid.uuid4(), campaign_id=uuid.uuid4(), title="Arc",
                          status="completed", reason=f"DM-only close ({MOTIVE})",
                          adventure_metadata={"note": MOTIVE})
    summary = AdventureSummary(
        id=uuid.uuid4(), adventure_id=adventure.id, campaign_id=adventure.campaign_id,
        version=1, status="failed", attempts=2, stale_count=0, rebuild_count=0,
        historical_text=f"Hidden: {MOTIVE}", recap_text="Public recap",
        error=f"recap leak validation failed: {MOTIVE}",
        summary_metadata={"hidden_event_count": 3},
    )
    blob = _blob({"adventure": adventure.to_public_dict(), "summary": summary.to_public_dict()})
    assert MOTIVE not in blob
    assert "hidden_event_count" not in blob
    assert summary.to_public_dict()["error"] == "summary generation failed"


def test_redaction_keeps_visible_turn_index_and_strips_nested_final_state():
    from app.combat.service import redact_hidden_combatants

    hidden = {"hidden"}
    # Realtime turn events carry an index but no order: re-derive it from
    # the visible order instead of counting the hidden slot.
    turn = redact_hidden_combatants(
        {"active_participant_id": "b", "active_index": 2}, hidden, visible_order=["a", "b"])
    assert turn == {"active_participant_id": "b", "active_index": 1}
    ended = redact_hidden_combatants({"final_state": {
        "participant_count": 2,
        "participants": [{"id": "hidden", "display_name": LURKER}, {"id": "a"}],
    }}, hidden)
    assert ended["final_state"] == {"participant_count": 1, "participants": [{"id": "a"}]}
    moved = redact_hidden_combatants({"participant_id": "hidden", "position_redacted": True}, hidden)
    assert moved == {"participant_id": None}

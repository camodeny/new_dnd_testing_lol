"""Issue #372 — Alpha E2E Phase 0 solo dogfood scenario through reconnect.

Production solo slice: synthetic setup -> character select/ready -> start via
the current #355 temporary solo bootstrap -> opening AI-DM turn -> three
freeform player turns with committed DM replies -> refresh/reconnect snapshot
verification -> post-reconnect turn -> ordering invariants throughout.

Boundaries exercised (no test-only gameplay engine, no alternate DM
orchestration path):
- HTTP campaign creation, character select/readiness, solo-bootstrap start.
- HTTP player-submission acceptance + production ``coordinate_turn``.
- Production ``run_dm_execute_sweep`` (the same sweeper behind the
  ``/api/cron/dm-execute`` trigger) through context assembly, contract
  validation, deterministic narration, and durable stream persistence.
- HTTP live-table snapshot/dm-turns/events projections for reconnect.

Explicit non-goals owned by sibling issues (do NOT absorb them here):
- #373 replaces the inline deterministic adjudicator below with the shared
  fake-provider mode. The adjudicator only stands in for the external model;
  orchestration, state, and persistence stay production.
- #374 owns durable failure-artifact preservation. This scenario exposes
  stable campaign/turn/attempt/stream identifiers and stage-tagged assertion
  context in failure messages/logs instead.
- #245/#246 replace ``start_production_play`` below with the production
  world seed/start path. Only that seam changes; the harness stays.

Auth: tests reuse the established per-router ``resolve_profile`` override
pattern against synthetic profiles. Production Supabase JWT is untouched —
no mock auth module is introduced.
"""

from __future__ import annotations

import logging
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from app.auth.service import TEST_USER_ID  # noqa: E402
from app.dm.contract import CONTRACT_VERSION, normalize_contract  # noqa: E402
from app.dm.execution import run_dm_execute_sweep  # noqa: E402
from app.dm.turns import list_turns  # noqa: E402
from app.dm_streams.service import reconstruct_text  # noqa: E402
from database import Base, get_db  # noqa: E402
from main import app  # noqa: E402
from models.characters import Character, Dnd5eCharacterSheet  # noqa: E402
from models.dm import DMStreamChunk, DmTurn, DmTurnAttempt  # noqa: E402
from models.profiles import Profile  # noqa: E402

logger = logging.getLogger(__name__)

# Three representative freeform player inputs; a fourth follows reconnect.
FREEFORM_TURNS = [
    "I look around the tavern and ask the keeper about the strange lights.",
    "I draw my blade, keep my back to the wall, and move toward the door.",
    "I call out to see if anyone else in the room saw the lights move.",
]
POST_RECONNECT_TURN = "I step outside into the fog to investigate the lights."

# Snapshot fields that must be byte-identical across a reconnect (generated_at
# and other wall-clock envelope fields are intentionally excluded).
SNAPSHOT_COMPARE_KEYS = (
    "campaign",
    "revision",
    "threads",
    "active_thread_id",
    "history",
    "dm_state",
    "reconciliation",
)


# ── deterministic provider stand-in (replaced by #373's fake-provider mode) ──


def make_phase0_adjudicate(state: dict):
    """Stand in ONLY for the external model call.

    Returns a valid production ``respond`` contract with a unique per-call
    marker so each committed reply is attributable. Context assembly,
    validation, narration, stream persistence, and commit all run the
    production path inside ``execute_dm_attempt``.
    """

    def _adjudicate(packet, feedback=None):
        state["calls"] += 1
        n = state["calls"]
        return normalize_contract(
            {
                "contract_version": CONTRACT_VERSION,
                "mode": "respond",
                "reason": f"phase0 deterministic reply {n}",
                "beats": [
                    {
                        "id": "beat_1",
                        "type": "narration",
                        "claims": [
                            {
                                "text": (
                                    f"Phase0 deterministic DM reply {n}: embers shift in "
                                    f"the tavern hearth. (phase0-reply-{n})"
                                ),
                                "claim_kind": "observation",
                                "origin": "dm_adjudication",
                                "visibility": "public",
                            }
                        ],
                    }
                ],
                "open_player_choice": "What do you do?",
            }
        )

    return _adjudicate


# ── scenario harness ──────────────────────────────────────────────────────────


class Scenario:
    """Owns one disposable Phase 0 run: stage-tagged checks plus identifiers."""

    def __init__(self, client: TestClient, factory, owner_id: uuid.UUID):
        self.client = client
        self.factory = factory
        self.owner_id = owner_id
        self.campaign_id: str | None = None
        self.stage = "init"
        self.last_revision = -1
        self.ids: dict = {
            "campaign_id": None,
            "thread_id": None,
            "character_id": None,
            "submission_ids": [],
            "turn_ids": [],
            "attempt_ids": [],
            "stream_ids": [],
            "revisions": {},
        }

    def note(self, stage: str) -> None:
        self.stage = stage
        logger.info("phase0-372 stage=%s campaign_id=%s", stage, self.campaign_id)

    def check(self, condition: bool, stage: str, message: str):
        if not condition:
            raise AssertionError(f"[372:{stage}] {message} | ids={self.ids}")
        return True

    def diagnostics(self) -> dict:
        return dict(self.ids)


def _resolve_test_profile(request, db):
    return db.get(Profile, TEST_USER_ID)


@pytest.fixture
def scn(monkeypatch):
    """Clean disposable database + HTTP client on production routers."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    owner_id = TEST_USER_ID
    with factory() as db:
        db.add(Profile(id=owner_id, email="phase0-solo-372@example.com"))
        db.commit()

    def override_db():
        with factory() as db:
            yield db

    monkeypatch.setenv("NODE_ENV", "test")
    for module in (
        "app.campaigns.router",
        "app.runtime.router",
        "app.snapshot.router",
        "app.dm.router",
        "app.characters.router",
    ):
        monkeypatch.setattr(
            f"{module}.resolve_profile",
            _resolve_test_profile,
            raising=False,
        )
    app.dependency_overrides[get_db] = override_db
    try:
        yield Scenario(TestClient(app), factory, owner_id)
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


# ── production-boundary steps (the harness; #245/#246 replace one seam) ──────


def make_synthetic_character(scn: Scenario, name: str = "Phase0 Hero") -> str:
    """Disposable synthetic PC fixture (profile/user already synthetic)."""
    with scn.factory() as db:
        char = Character(owner_id=scn.owner_id, name=name, system="dnd5e")
        db.add(char)
        db.flush()
        db.add(
            Dnd5eCharacterSheet(
                character_id=char.id,
                owner_id=scn.owner_id,
                character_name=name,
                race="Human",
                char_class="Fighter",
                level=1,
            )
        )
        db.commit()
        char_id = str(char.id)
    scn.ids["character_id"] = char_id
    return char_id


def setup_solo_campaign(scn: Scenario, char_id: str) -> str:
    """Create campaign + select character + ready through production endpoints."""
    scn.note("setup")
    r = scn.client.post(
        "/api/campaigns", json={"name": "Phase0 Solo 372", "required_players": 1}
    )
    scn.check(r.status_code == 200, "setup", f"campaign creation failed: {r.text}")
    campaign_id = r.json()["campaign"]["id"]
    scn.campaign_id = campaign_id
    scn.ids["campaign_id"] = campaign_id

    r = scn.client.put(
        f"/api/campaigns/{campaign_id}/members/me/character",
        json={"expected_revision": 0, "character_id": char_id},
        headers={"Idempotency-Key": "phase0-select"},
    )
    scn.check(r.status_code == 200, "setup", f"character select failed: {r.text}")

    r = scn.client.put(
        f"/api/campaigns/{campaign_id}/members/me/readiness",
        json={"expected_revision": 1, "ready": True},
        headers={"Idempotency-Key": "phase0-ready"},
    )
    scn.check(r.status_code == 200, "setup", f"readiness failed: {r.text}")

    lobby = scn.client.get(f"/api/campaigns/{campaign_id}/lobby")
    scn.check(lobby.status_code == 200, "setup", f"lobby read failed: {lobby.text}")
    scn.check(
        lobby.json()["eligibility"]["eligible"] is True,
        "setup",
        "solo lobby not eligible after select+ready",
    )
    return campaign_id


def start_production_play(scn: Scenario, *, operation_key: str) -> dict:
    """Production-start seam — Phase 0 implementation: #355 solo bootstrap.

    When #245/#246 land, replace ONLY this function with the production
    world seed/start/opening call and delete the temporary bootstrap path.
    The rest of the harness (setup/play/reconnect/verify) is unchanged.
    """
    scn.note("start")
    assert scn.campaign_id is not None
    r = scn.client.post(
        f"/api/campaigns/{scn.campaign_id}/solo-bootstrap",
        json={"operation_id": operation_key},
        headers={"Idempotency-Key": operation_key},
    )
    scn.check(r.status_code == 200, "start", f"solo bootstrap failed: {r.text}")
    body = r.json()
    scn.check(body["campaign"]["status"] == "active", "start", "campaign not active")
    scn.check(body.get("solo_bootstrap") is True, "start", "bootstrap marker missing")
    scn.check(body.get("thread_id"), "start", "no gameplay thread returned")
    scn.check((body.get("dm_turn") or {}).get("id"), "start", "no opening turn")
    scn.check((body.get("dm_attempt") or {}).get("id"), "start", "no opening attempt")
    scn.ids["thread_id"] = body["thread_id"]
    return body


def submit_player_turn(scn: Scenario, text: str, key: str) -> dict:
    """One normal freeform submission through the production submissions API."""
    assert scn.campaign_id is not None
    r = scn.client.post(
        f"/api/campaigns/{scn.campaign_id}/submissions",
        json={"content": text},
        headers={"Idempotency-Key": key},
    )
    scn.check(r.status_code == 201, "play", f"submission {key} failed: {r.text}")
    body = r.json()
    scn.check((body.get("dm_turn") or {}).get("id"), "play", "no DM turn coordinated")
    scn.ids["submission_ids"].append(body["submission"]["id"])
    return body


def drain_dm_execution(scn: Scenario, adjudicate, stage: str) -> dict:
    """Run the production execute sweep (cron path) in worker-like sessions."""
    assert scn.campaign_id is not None
    cid = uuid.UUID(scn.campaign_id)
    outcome: dict = {"executed": [], "failed": [], "skipped": []}
    for _ in range(6):
        with scn.factory() as db:
            outcome = run_dm_execute_sweep(
                db, limit=10, adjudicate=adjudicate, narrator="deterministic"
            )
            db.commit()
        if outcome.get("failed"):
            return outcome
        with scn.factory() as db:
            pending = [
                t
                for t in list_turns(db, cid, limit=200)
                if t.status in ("pending", "streaming", "awaiting_roll")
            ]
            prepared = (
                db.execute(
                    select(DmTurnAttempt).where(
                        DmTurnAttempt.campaign_id == cid,
                        DmTurnAttempt.status == "prepared",
                    )
                )
                .scalars()
                .all()
            )
        if not pending and not prepared:
            break
    return outcome


def await_committed_reply(scn: Scenario, stage: str, turn_id: str) -> str:
    """Assert one logical turn has exactly one committed, durable DM result."""
    with scn.factory() as db:
        turn = db.get(DmTurn, uuid.UUID(turn_id))
        scn.check(turn is not None, stage, f"turn {turn_id} missing")
        assert turn is not None
        attempt = (
            db.get(DmTurnAttempt, turn.current_attempt_id)
            if turn.current_attempt_id
            else None
        )
        scn.check(
            turn.status == "succeeded",
            stage,
            f"turn {turn_id} not succeeded (status={turn.status}, "
            f"attempt_error={(attempt.last_error if attempt else None)})",
        )
        scn.check(attempt is not None, stage, f"turn {turn_id} has no attempt")
        assert attempt is not None
        scn.check(
            attempt.status == "succeeded",
            stage,
            f"attempt {attempt.id} not succeeded (status={attempt.status})",
        )
        scn.check(
            attempt.stream_id is not None,
            stage,
            f"attempt {attempt.id} has no persisted stream",
        )
        assert attempt.stream_id is not None
        chunks = (
            db.execute(
                select(DMStreamChunk)
                .where(DMStreamChunk.stream_id == attempt.stream_id)
                .order_by(DMStreamChunk.sequence)
            )
            .scalars()
            .all()
        )
        scn.check(len(chunks) >= 1, stage, "DM reply has no durable stream chunks")
        text = reconstruct_text(db, attempt.stream_id)
        scn.check(
            "phase0-reply-" in text,
            stage,
            "durable narration lacks the committed-reply marker",
        )
        if str(turn.id) not in scn.ids["turn_ids"]:
            scn.ids["turn_ids"].append(str(turn.id))
        if str(attempt.id) not in scn.ids["attempt_ids"]:
            scn.ids["attempt_ids"].append(str(attempt.id))
        if str(attempt.stream_id) not in scn.ids["stream_ids"]:
            scn.ids["stream_ids"].append(str(attempt.stream_id))
        return text


def assert_ordering_invariants(scn: Scenario, stage: str) -> int:
    """Campaign revision == domain-event sequence invariant (#188)."""
    assert scn.campaign_id is not None
    r = scn.client.get(f"/api/campaigns/{scn.campaign_id}/events")
    scn.check(r.status_code == 200, stage, f"events read failed: {r.text}")
    body = r.json()
    seqs = [e["sequence"] for e in body["events"]]
    scn.check(
        seqs == sorted(seqs) and len(set(seqs)) == len(seqs),
        stage,
        f"event sequences not strictly increasing: {seqs}",
    )
    scn.check(
        seqs == list(range(1, len(seqs) + 1)),
        stage,
        f"event sequences not contiguous from 1: {seqs}",
    )
    scn.check(
        body["revision"] == len(seqs),
        stage,
        f"revision {body['revision']} != event count {len(seqs)} "
        "(revision==sequence invariant broken)",
    )
    scn.check(
        body["revision"] >= scn.last_revision,
        stage,
        f"revision went backwards: {body['revision']} < {scn.last_revision}",
    )
    scn.last_revision = body["revision"]
    scn.ids["revisions"][stage] = body["revision"]
    return body["revision"]


def assert_single_result_per_submission(
    scn: Scenario, stage: str, expected_turns: int
) -> list:
    """One accepted logical player intent -> one committed gameplay result."""
    assert scn.campaign_id is not None
    r = scn.client.get(f"/api/campaigns/{scn.campaign_id}/dm-turns")
    scn.check(r.status_code == 200, stage, f"dm-turns read failed: {r.text}")
    turns = r.json()["turns"]
    scn.check(
        len(turns) == expected_turns,
        stage,
        f"expected {expected_turns} turns, found {len(turns)}",
    )
    turn_ids = [t["id"] for t in turns]
    scn.check(len(set(turn_ids)) == len(turn_ids), stage, "duplicate turn ids listed")
    consumed = [s for t in turns for s in (t["submission_ids"] or [])]
    scn.check(
        len(consumed) == len(set(consumed)),
        stage,
        "one submission consumed by multiple turns (duplicate gameplay)",
    )
    subs = scn.client.get(f"/api/campaigns/{scn.campaign_id}/submissions")
    scn.check(subs.status_code == 200, stage, f"submissions read failed: {subs.text}")
    for sub in subs.json()["submissions"]:
        scn.check(
            consumed.count(sub["id"]) == 1,
            stage,
            f"submission {sub['id']} committed {consumed.count(sub['id'])} times",
        )
    return turns


def read_snapshot(scn: Scenario, stage: str, client=None) -> dict:
    assert scn.campaign_id is not None
    http = client or scn.client
    r = http.get(f"/api/campaigns/{scn.campaign_id}/snapshot")
    scn.check(r.status_code == 200, stage, f"snapshot read failed: {r.text}")
    return r.json()


def assert_same_authoritative_projection(
    scn: Scenario, stage: str, before: dict, after: dict
) -> None:
    for key in SNAPSHOT_COMPARE_KEYS:
        scn.check(
            before[key] == after[key],
            stage,
            f"reconnect divergence in snapshot[{key}]",
        )


# ── main scenario ─────────────────────────────────────────────────────────────


def test_phase0_solo_dogfood_through_reconnect_and_continued_play(scn):
    state = {"calls": 0}
    adjudicate = make_phase0_adjudicate(state)

    # Setup: synthetic fixtures + authoritative select/ready.
    char_id = make_synthetic_character(scn)
    setup_solo_campaign(scn, char_id)

    # Start through the current #355 bootstrap seam.
    opening = start_production_play(scn, operation_key="phase0-start-372")
    opening_turn_id = opening["dm_turn"]["id"]
    assert_ordering_invariants(scn, "start")

    # Opening DM turn completes through the production execution path.
    scn.note("opening")
    outcome = drain_dm_execution(scn, adjudicate, "opening")
    scn.check(not outcome.get("failed"), "opening", f"sweep failed: {outcome}")
    opening_text = await_committed_reply(scn, "opening", opening_turn_id)
    assert opening_text
    assert_single_result_per_submission(scn, "opening", 1)

    # Three freeform player turns, each with its committed DM reply.
    stream_texts = []
    for index, text in enumerate(FREEFORM_TURNS):
        stage = f"play-{index + 1}"
        scn.note(stage)
        submitted = submit_player_turn(scn, text, f"phase0-turn-{index + 1}")
        turn_id = submitted["dm_turn"]["id"]
        outcome = drain_dm_execution(scn, adjudicate, stage)
        scn.check(not outcome.get("failed"), stage, f"sweep failed: {outcome}")
        stream_texts.append(await_committed_reply(scn, stage, turn_id))
        assert_ordering_invariants(scn, stage)
        assert_single_result_per_submission(scn, stage, 2 + index)
    scn.check(
        len(set(stream_texts)) == len(stream_texts),
        "play-3",
        "DM replies are not distinct per player turn",
    )

    # Duplicate delivery of an accepted submission must not duplicate gameplay.
    scn.note("duplicate-guard")
    replay = scn.client.post(
        f"/api/campaigns/{scn.campaign_id}/submissions",
        json={"content": FREEFORM_TURNS[0]},
        headers={"Idempotency-Key": "phase0-turn-1"},
    )
    scn.check(
        replay.status_code == 201, "duplicate-guard", f"replay failed: {replay.text}"
    )
    scn.check(
        replay.headers.get("X-Idempotent-Replay") == "true",
        "duplicate-guard",
        "duplicate submission was not recognized as a replay",
    )
    assert_single_result_per_submission(scn, "duplicate-guard", 4)

    # Refresh/reconnect: a brand-new client + fresh sessions must reconstruct
    # the same authoritative transcript/state with no duplicate visible turns.
    scn.note("reconnect")
    before = read_snapshot(scn, "reconnect")
    reconnected_client = TestClient(app)
    after = read_snapshot(scn, "reconnect", client=reconnected_client)
    assert_same_authoritative_projection(scn, "reconnect", before, after)
    contents = [m["raw_content"] for m in after["history"]["messages"]]
    for text in FREEFORM_TURNS:
        scn.check(
            text in contents,
            "reconnect",
            f"player turn lost across reconnect: {text!r}",
        )
    with scn.factory() as db:
        for stream_id in scn.ids["stream_ids"]:
            scn.check(
                bool(reconstruct_text(db, uuid.UUID(stream_id)).strip()),
                "reconnect",
                f"stream {stream_id} not reconstructable after reconnect",
            )

    # Play continues after reconnect through the same production path.
    scn.note("post-reconnect")
    submitted = submit_player_turn(scn, POST_RECONNECT_TURN, "phase0-turn-post")
    outcome = drain_dm_execution(scn, adjudicate, "post-reconnect")
    scn.check(not outcome.get("failed"), "post-reconnect", f"sweep failed: {outcome}")
    await_committed_reply(scn, "post-reconnect", submitted["dm_turn"]["id"])
    assert_ordering_invariants(scn, "post-reconnect")
    assert_single_result_per_submission(scn, "post-reconnect", 5)
    grown = read_snapshot(scn, "post-reconnect")
    scn.check(
        POST_RECONNECT_TURN in [m["raw_content"] for m in grown["history"]["messages"]],
        "post-reconnect",
        "post-reconnect turn missing from live-table snapshot",
    )

    # Stable identifiers for later diagnostics (#374) — asserted present, never hidden.
    scn.note("diagnostics")
    diag = scn.diagnostics()
    scn.check(diag["campaign_id"], "diagnostics", "campaign id not exposed")
    scn.check(diag["thread_id"], "diagnostics", "thread id not exposed")
    scn.check(diag["character_id"], "diagnostics", "character id not exposed")
    scn.check(
        len(diag["submission_ids"]) == 4, "diagnostics", "submission ids incomplete"
    )
    scn.check(len(diag["turn_ids"]) == 5, "diagnostics", "turn ids incomplete")
    scn.check(len(diag["attempt_ids"]) == 5, "diagnostics", "attempt ids incomplete")
    scn.check(len(diag["stream_ids"]) == 5, "diagnostics", "stream ids incomplete")
    logger.info("phase0-372 diagnostics=%s", diag)


# ── intentional-break proofs: each sabotage must fail at its assertion ────────


def _run_to_opening_reply(scn: Scenario):
    """Shared prefix for break proofs: setup -> start -> committed opening."""
    state = {"calls": 0}
    char_id = make_synthetic_character(scn)
    setup_solo_campaign(scn, char_id)
    opening = start_production_play(scn, operation_key="phase0-break-start")
    outcome = drain_dm_execution(scn, make_phase0_adjudicate(state), "opening")
    assert not outcome.get("failed"), outcome
    await_committed_reply(scn, "opening", opening["dm_turn"]["id"])
    return opening


def test_break_failing_execution_surfaces_at_dm_reply_assertion(scn):
    opening = _run_to_opening_reply(scn)
    scn.note("play")

    def _boom(packet, feedback=None):
        raise RuntimeError("injected provider failure")

    submitted = submit_player_turn(scn, "I press on.", "phase0-break-exec")
    outcome = drain_dm_execution(scn, _boom, "play")
    scn.check(
        bool(outcome.get("failed")), "play", "sabotaged sweep unexpectedly succeeded"
    )
    with pytest.raises(AssertionError, match=r"\[372:play\]"):
        await_committed_reply(scn, "play", submitted["dm_turn"]["id"])
    # The failed turn stays observable, never silently committed.
    with scn.factory() as db:
        turn = db.get(DmTurn, uuid.UUID(submitted["dm_turn"]["id"]))
        assert turn is not None and turn.status != "succeeded"
    assert opening["dm_turn"]["id"] != submitted["dm_turn"]["id"]


def test_break_missing_stream_chunks_fail_durability_assertion(scn):
    opening = _run_to_opening_reply(scn)
    with scn.factory() as db:
        turn = db.get(DmTurn, uuid.UUID(opening["dm_turn"]["id"]))
        assert turn is not None
        stream_id = db.get(DmTurnAttempt, turn.current_attempt_id).stream_id
        assert stream_id is not None
        db.execute(
            DMStreamChunk.__table__.delete().where(DMStreamChunk.stream_id == stream_id)
        )
        db.commit()
    with pytest.raises(AssertionError, match=r"\[372:opening\]"):
        await_committed_reply(scn, "opening", opening["dm_turn"]["id"])


def test_break_duplicate_turn_for_one_submission_is_detected(scn):
    opening = _run_to_opening_reply(scn)
    with scn.factory() as db:
        original = db.get(DmTurn, uuid.UUID(opening["dm_turn"]["id"]))
        assert original is not None
        db.add(
            DmTurn(
                id=uuid.uuid4(),
                campaign_id=original.campaign_id,
                thread_id=original.thread_id,
                audience=original.audience,
                status="succeeded",
                source_revision=original.source_revision,
                input_set_revision=1,
                submission_ids=list(original.submission_ids or []),
            )
        )
        db.commit()
    with pytest.raises(AssertionError, match=r"\[372:integrity\]"):
        assert_single_result_per_submission(scn, "integrity", 2)


def test_break_extra_commit_breaks_reconnect_equality(scn):
    _run_to_opening_reply(scn)
    scn.note("reconnect")
    before = read_snapshot(scn, "reconnect")
    submit_player_turn(scn, "An unexecuted extra line.", "phase0-break-extra")
    after = read_snapshot(scn, "reconnect")
    with pytest.raises(AssertionError, match=r"\[372:reconnect\]"):
        assert_same_authoritative_projection(scn, "reconnect", before, after)


def test_break_event_gap_breaks_ordering_invariant(scn):
    _run_to_opening_reply(scn)
    from models.campaigns import CampaignDomainEvent

    with scn.factory() as db:
        events = (
            db.execute(
                select(CampaignDomainEvent)
                .where(CampaignDomainEvent.campaign_id == uuid.UUID(scn.campaign_id))
                .order_by(CampaignDomainEvent.sequence.asc())
            )
            .scalars()
            .all()
        )
        assert len(events) >= 3
        # Delete a middle event while keeping the revision: sequences are no
        # longer contiguous and revision != event count.
        db.delete(events[1])
        db.commit()
    # Tampering that breaks revision==sequence contiguity must fail here.
    with pytest.raises(AssertionError, match=r"\[372:integrity\]"):
        assert_ordering_invariants(scn, "integrity")

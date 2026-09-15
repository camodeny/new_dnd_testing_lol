"""Issue #265 — campaign archive/restore the same persistent campaign exactly once."""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from app.auth.service import TEST_USER_ID  # noqa: E402
from database import Base, get_db  # noqa: E402
from main import app  # noqa: E402
from models.campaigns import Campaign  # noqa: E402
from models.campaigns import CampaignDomainEvent  # noqa: E402
from models.campaigns import CampaignMember  # noqa: E402
from models.characters import Character  # noqa: E402
from models.characters import Dnd5eCharacterSheet  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.threads import CampaignThread  # noqa: E402
from models.threads import CampaignThreadMember  # noqa: E402


def _ready_lobby(factory, campaign_id: str, user_ids: list) -> None:
    from datetime import datetime, timezone

    cid = uuid.UUID(campaign_id)
    with factory() as db:
        for uid in user_ids:
            char = Character(owner_id=uid, name=f"Hero {str(uid)[:8]}", system="dnd5e")
            db.add(char)
            db.flush()
            db.add(Dnd5eCharacterSheet(
                character_id=char.id, owner_id=uid, character_name=char.name,
                race="Human", char_class="Fighter", level=1,
            ))
            db.flush()
            member = db.get(CampaignMember, {"campaign_id": cid, "user_id": uid})
            assert member is not None
            member.selected_character_id = char.id
            member.is_ready = True
            member.ready_at = datetime.now(timezone.utc)
        db.commit()


@pytest.fixture
def api(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    owner_id = TEST_USER_ID
    member_id = uuid.uuid4()
    outsider_id = uuid.uuid4()
    with factory() as db:
        db.add_all([
            Profile(id=owner_id, email="owner@example.com"),
            Profile(id=member_id, email="member@example.com"),
            Profile(id=outsider_id, email="outsider@example.com"),
        ])
        db.commit()

    actor = {"id": owner_id}

    def override_db():
        with factory() as db:
            yield db

    monkeypatch.setattr(
        "app.campaigns.router.resolve_profile",
        lambda request, db: db.get(Profile, actor["id"]),
    )
    # World/submission/snapshot routers import resolve_profile into their own
    # namespaces — patch each transport surface this suite exercises.
    for module in (
        "app.world.router",
        "app.runtime.router",
        "app.snapshot.router",
        "app.rolls.router",
    ):
        monkeypatch.setattr(
            f"{module}.resolve_profile",
            lambda request, db: db.get(Profile, actor["id"]),
        )
    app.dependency_overrides[get_db] = override_db
    try:
        yield TestClient(app), factory, actor, owner_id, member_id, outsider_id
    finally:
        app.dependency_overrides.clear()


def _create(client: TestClient, **overrides) -> dict:
    payload = {"name": "Archive Test", **overrides}
    response = client.post("/api/campaigns", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["campaign"]


def _transition(client: TestClient, campaign_id: str, revision: int, status: str, key: str):
    return client.post(
        f"/api/campaigns/{campaign_id}/lifecycle",
        json={"expected_revision": revision, "status": status},
        headers={"Idempotency-Key": key},
    )


def _get(client: TestClient, campaign_id: str) -> dict:
    response = client.get(f"/api/campaigns/{campaign_id}")
    assert response.status_code == 200, response.text
    return response.json()["campaign"]


def _drive_to_active(client, factory, owner_id, **overrides) -> dict:
    campaign = _create(client, required_players=1, random_seed="seed-265", **overrides)
    cid = campaign["id"]
    _ready_lobby(factory, cid, [owner_id])
    assert _transition(client, cid, 0, "starting", "up-starting").status_code == 200
    assert _transition(client, cid, 1, "active", "up-active").status_code == 200
    return _get(client, cid)


def _set_scene(client, cid: str, revision: int, key: str, fictional_time: str = "Day 3, dusk"):
    response = client.put(
        f"/api/campaigns/{cid}/world/current-scene",
        json={"expected_revision": revision, "fictional_time": fictional_time},
        headers={"Idempotency-Key": key},
    )
    return response


def _state_fingerprint(factory, cid: uuid.UUID) -> dict:
    """Durable state that archive/restore must preserve exactly."""
    from models.world import CampaignCurrentScene, WorldEntity

    with factory() as db:
        camp = db.get(Campaign, cid)
        threads = db.execute(
            select(CampaignThread).where(CampaignThread.campaign_id == cid)
        ).scalars().all()
        members = db.execute(
            select(CampaignMember).where(CampaignMember.campaign_id == cid)
        ).scalars().all()
        thread_members = db.execute(
            select(CampaignThreadMember).where(
                CampaignThreadMember.thread_id.in_([t.id for t in threads] or [uuid.uuid4()])
            )
        ).scalars().all()
        entities = db.execute(
            select(WorldEntity).where(WorldEntity.campaign_id == cid)
        ).scalars().all()
        scene = db.get(CampaignCurrentScene, cid)
        events = db.execute(
            select(CampaignDomainEvent.event_type, CampaignDomainEvent.sequence)
            .where(CampaignDomainEvent.campaign_id == cid)
            .order_by(CampaignDomainEvent.sequence.asc())
        ).all()
        return {
            "seed": camp.random_seed,
            "thread_ids": sorted(str(t.id) for t in threads),
            "thread_types": sorted(t.thread_type for t in threads),
            "member_ids": sorted(str(m.user_id) for m in members),
            "thread_member_pairs": sorted((str(m.thread_id), str(m.user_id)) for m in thread_members),
            "entity_ids": sorted(str(e.id) for e in entities),
            "entity_names": sorted(e.name for e in entities),
            "fictional_time": scene.fictional_time if scene else None,
            "event_types": [e[0] for e in events],
        }


def test_archive_preserves_all_durable_state_and_hides_from_active_list(api):
    client, factory, _, owner_id, member_id, _ = api
    campaign = _drive_to_active(client, factory, owner_id)
    cid = campaign["id"]
    rev = campaign["revision"]

    # Durable world/canon state established before archiving.
    assert _set_scene(client, cid, rev, "scene-1").status_code == 200
    rev += 1
    entity = client.post(
        f"/api/campaigns/{cid}/world/entities",
        json={"expected_revision": rev, "entity_type": "npc", "name": "Mira"},
        headers={"Idempotency-Key": "entity-1"},
    )
    assert entity.status_code == 200, entity.text
    rev += 1
    # Private thread with restricted membership.
    private_id = uuid.uuid4()
    with factory() as db:
        db.add(CampaignThread(
            id=private_id, campaign_id=uuid.UUID(cid), thread_type="private",
            title="Secrets", created_by=owner_id,
        ))
        db.add(CampaignMember(campaign_id=uuid.UUID(cid), user_id=member_id, role="player"))
        db.add(CampaignThreadMember(thread_id=private_id, user_id=member_id))
        db.commit()

    before = _state_fingerprint(factory, uuid.UUID(cid))
    assert before["fictional_time"] == "Day 3, dusk"
    assert before["entity_names"] == ["Mira"]

    archived = _transition(client, cid, rev, "archived", "archive-1")
    assert archived.status_code == 200, archived.text
    assert archived.json()["campaign"]["status"] == "archived"
    assert archived.json()["campaign"]["revision"] == rev + 1

    after = _state_fingerprint(factory, uuid.UUID(cid))
    assert after == {**before, "event_types": before["event_types"] + ["campaign.lifecycle.archived"]}

    # Active surfaces hide the dormant campaign; authorized review still lists it.
    listed = client.get("/api/campaigns").json()["campaigns"]
    assert all(c["id"] != cid for c in listed)
    reviewed = client.get("/api/campaigns", params={"include_archived": "true"}).json()["campaigns"]
    assert any(c["id"] == cid and c["status"] == "archived" for c in reviewed)
    # Reads stay available to members while archived.
    assert _get(client, cid)["status"] == "archived"
    events = client.get(f"/api/campaigns/{cid}/events")
    assert events.status_code == 200


def test_restore_reactivates_same_campaign_and_live_table_flow(api):
    client, factory, _, owner_id, member_id, _ = api
    campaign = _drive_to_active(client, factory, owner_id)
    cid = campaign["id"]
    rev = campaign["revision"]
    assert _set_scene(client, cid, rev, "scene-1", fictional_time="Day 5, dawn").status_code == 200
    rev += 1
    with factory() as db:
        db.add(CampaignMember(campaign_id=uuid.UUID(cid), user_id=member_id, role="player"))
        db.commit()

    before = _state_fingerprint(factory, uuid.UUID(cid))
    assert _transition(client, cid, rev, "archived", "archive-1").status_code == 200
    restored = _transition(client, cid, rev + 1, "active", "restore-1")
    assert restored.status_code == 200, restored.text
    body = restored.json()["campaign"]
    assert body["id"] == cid
    assert body["status"] == "active"
    assert body["random_seed"] == "seed-265"
    assert body["revision"] == rev + 2

    after = _state_fingerprint(factory, uuid.UUID(cid))
    assert after == {
        **before,
        "event_types": before["event_types"]
        + ["campaign.lifecycle.archived", "campaign.lifecycle.active"],
    }

    # Restored campaign opens through the normal live-table snapshot/realtime flow.
    snapshot = client.get(f"/api/campaigns/{cid}/snapshot")
    assert snapshot.status_code == 200, snapshot.text
    snap = snapshot.json()
    assert snap["campaign"]["id"] == cid
    assert snap["campaign"]["status"] == "active"
    assert snap["revision"] == rev + 2
    assert snap["reconciliation"]["realtime_resume_token"]
    assert snap["active_thread"]["id"]
    # Active again on normal surfaces.
    assert any(c["id"] == cid for c in client.get("/api/campaigns").json()["campaigns"])


def test_duplicate_archive_and_restore_converge(api):
    client, factory, _, owner_id, _, _ = api
    campaign = _drive_to_active(client, factory, owner_id)
    cid = campaign["id"]
    rev = campaign["revision"]

    first = _transition(client, cid, rev, "archived", "archive-1")
    assert first.status_code == 200
    # Same-key replay.
    replay = _transition(client, cid, rev, "archived", "archive-1")
    assert replay.status_code == 200
    assert replay.headers.get("X-Idempotent-Replay") == "true"
    # Different-key duplicate converges without a revision bump or new event.
    dup = _transition(client, cid, rev + 1, "archived", "archive-2")
    assert dup.status_code == 200, dup.text
    assert dup.json()["converged"] is True
    assert _get(client, cid)["revision"] == rev + 1

    restored = _transition(client, cid, rev + 1, "active", "restore-1")
    assert restored.status_code == 200
    dup_restore = _transition(client, cid, rev + 2, "active", "restore-2")
    assert dup_restore.status_code == 200, dup_restore.text
    assert dup_restore.json()["converged"] is True
    final = _get(client, cid)
    assert (final["status"], final["revision"]) == ("active", rev + 2)
    with factory() as db:
        types = db.execute(
            select(CampaignDomainEvent.event_type)
            .where(CampaignDomainEvent.campaign_id == uuid.UUID(cid))
            .order_by(CampaignDomainEvent.sequence.asc())
        ).scalars().all()
        assert list(types).count("campaign.lifecycle.archived") == 1
        # One active event for the initial start, one for the restore — no more.
        assert list(types).count("campaign.lifecycle.active") == 2
        assert list(types)[-2:] == ["campaign.lifecycle.archived", "campaign.lifecycle.active"]


def test_archived_table_freezes_fictional_time(api):
    client, factory, _, owner_id, _, _ = api
    campaign = _drive_to_active(client, factory, owner_id)
    cid = campaign["id"]
    rev = campaign["revision"]
    assert _set_scene(client, cid, rev, "scene-1", fictional_time="Day 7, noon").status_code == 200
    rev += 1
    assert _transition(client, cid, rev, "archived", "archive-1").status_code == 200

    # New fictional writes are rejected while dormant.
    submission = client.post(
        f"/api/campaigns/{cid}/submissions",
        json={"content": "I light a torch."},
        headers={"Idempotency-Key": "sub-1"},
    )
    assert submission.status_code == 409
    scene = _set_scene(client, cid, rev + 1, "scene-2", fictional_time="Day 8")
    assert scene.status_code == 409
    entity = client.post(
        f"/api/campaigns/{cid}/world/entities",
        json={"expected_revision": rev + 1, "entity_type": "npc", "name": "Sneaky"},
        headers={"Idempotency-Key": "entity-sneaky"},
    )
    assert entity.status_code == 409

    # Autonomous post-turn work neither triggers nor consolidates while archived.
    from app.post_turn.service import (
        get_checkpoint,
        get_max_sequence,
        maybe_trigger_post_turn,
        run_post_turn_range,
    )

    with factory() as db:
        assert maybe_trigger_post_turn(db, uuid.UUID(cid)) is None
        max_seq = get_max_sequence(db, uuid.UUID(cid))
        assert max_seq and max_seq > 0
        result = run_post_turn_range(db, uuid.UUID(cid), 1, max_seq)
        assert result.get("skipped") is True
        db.commit()
    with factory() as db:
        assert get_checkpoint(db, uuid.UUID(cid)).processed_through_sequence == 0

    # Wall-clock dormancy alone advanced nothing.
    assert _state_fingerprint(factory, uuid.UUID(cid))["fictional_time"] == "Day 7, noon"

    # After restore the frozen table resumes and the backlog backfills.
    assert _transition(client, cid, rev + 1, "active", "restore-1").status_code == 200
    with factory() as db:
        max_seq = get_max_sequence(db, uuid.UUID(cid))
        result = run_post_turn_range(db, uuid.UUID(cid), 1, max_seq)
        assert result.get("skipped") is not True
        db.commit()
    with factory() as db:
        assert get_checkpoint(db, uuid.UUID(cid)).processed_through_sequence == max_seq
    assert _set_scene(client, cid, rev + 2, "scene-3", fictional_time="Day 8").status_code == 200


def test_post_turn_checkpoint_cannot_advance_after_concurrent_archive(api):
    """CAS-time re-check: archive committed during consolidation retires the run."""
    from app.post_turn.service import get_checkpoint, get_max_sequence, run_post_turn_range

    client, factory, _, owner_id, _, _ = api
    campaign = _drive_to_active(client, factory, owner_id)
    cid = campaign["id"]

    def archive_mid_consolidation(events):
        from sqlalchemy import update as _update

        with factory() as other:
            other.execute(
                _update(Campaign)
                .where(Campaign.id == uuid.UUID(cid))
                .values(status="archived")
            )
            other.commit()
        return {"processed_span": [1, len(events)], "event_count": len(events)}

    with factory() as db:
        max_seq = get_max_sequence(db, uuid.UUID(cid))
        assert max_seq and max_seq > 0
        result = run_post_turn_range(
            db, uuid.UUID(cid), 1, max_seq, consolidate_fn=archive_mid_consolidation
        )
        assert result.get("skipped") is True
        assert result.get("reason") == "campaign_archived_at_commit"
        db.commit()
    with factory() as db:
        assert get_checkpoint(db, uuid.UUID(cid)).processed_through_sequence == 0


def test_dm_execution_defers_while_archived(api):
    client, factory, _, owner_id, _, _ = api
    campaign = _drive_to_active(client, factory, owner_id)
    cid = campaign["id"]
    rev = campaign["revision"]

    from models.dm import DmTurn, DmTurnAttempt
    from app.dm.execution import execute_dm_attempt

    shared_id = None
    with factory() as db:
        shared = db.execute(
            select(CampaignThread).where(
                CampaignThread.campaign_id == uuid.UUID(cid),
                CampaignThread.thread_type == "campaign",
            )
        ).scalars().first()
        shared_id = str(shared.id)
        turn = DmTurn(
            campaign_id=uuid.UUID(cid), thread_id=shared_id, status="pending",
            source_revision=rev, submission_ids=[],
        )
        db.add(turn)
        db.flush()
        attempt = DmTurnAttempt(
            turn_id=turn.id, attempt_number=1, status="prepared",
            campaign_id=uuid.UUID(cid), thread_id=shared_id,
            source_revision=rev, input_set_revision=rev, submission_ids=[],
        )
        db.add(attempt)
        db.flush()
        attempt_id = attempt.id
        turn.current_attempt_id = attempt.id
        db.commit()

    assert _transition(client, cid, rev, "archived", "archive-1").status_code == 200
    with factory() as db:
        assert execute_dm_attempt(db, attempt_id) is None
        assert db.get(DmTurnAttempt, attempt_id).status == "prepared"
        db.commit()
    # Restored table resumes the exact same prepared attempt.
    assert _transition(client, cid, rev + 1, "active", "restore-1").status_code == 200
    with factory() as db:
        assert db.get(DmTurnAttempt, attempt_id).status == "prepared"


def test_archive_refused_while_dm_inflight_and_recovery_unblocks(api):
    """Archive serializes with visible execution: running/streaming blocks.

    Executor death is never inferred from timestamps: even a stale running
    claim blocks until recover_stuck_attempts resets it to prepared, which
    then unblocks archive. Prepared work defers instead of blocking.
    """
    from datetime import datetime, timedelta, timezone

    from app.dm.turns import mark_streaming_started, recover_stuck_attempts
    from models.dm import DmTurn, DmTurnAttempt

    client, factory, _, owner_id, _, _ = api
    campaign = _drive_to_active(client, factory, owner_id)
    cid = campaign["id"]
    rev = campaign["revision"]
    with factory() as db:
        thread = db.execute(
            select(CampaignThread).where(
                CampaignThread.campaign_id == uuid.UUID(cid),
                CampaignThread.thread_type == "campaign",
            )
        ).scalars().first()
        turn = DmTurn(
            campaign_id=uuid.UUID(cid), thread_id=str(thread.id), status="pending",
            source_revision=rev, submission_ids=[],
        )
        db.add(turn)
        db.flush()
        attempt = DmTurnAttempt(
            turn_id=turn.id, attempt_number=1, status="running",
            campaign_id=uuid.UUID(cid), thread_id=str(thread.id),
            source_revision=rev, input_set_revision=rev, submission_ids=[],
            started_at=datetime.now(timezone.utc),
        )
        db.add(attempt)
        db.flush()
        turn.current_attempt_id = attempt.id
        db.commit()
        attempt_id, turn_id = attempt.id, turn.id

    blocked = _transition(client, cid, rev, "archived", "archive-streaming")
    assert blocked.status_code == 409, blocked.text
    assert _get(client, cid)["status"] == "active"

    # Stale claims block too — death requires recovery, not timestamps.
    with factory() as db:
        db.get(DmTurnAttempt, attempt_id).started_at = datetime.now(timezone.utc) - timedelta(hours=2)
        db.commit()
    assert _transition(client, cid, rev, "archived", "archive-stale").status_code == 409

    # Recovery resets the dead claim to prepared, unblocking dormancy.
    with factory() as db:
        assert recover_stuck_attempts(db, lease_seconds=0) == 1
        db.commit()
    assert _transition(client, cid, rev, "archived", "archive-1").status_code == 200

    # A worker that claimed before the archive commits cannot cross the
    # first-visible boundary afterwards: chunk 0 and the dormancy decision
    # share one commit, so the boundary raises and nothing persists.
    from app.campaigns.service import CampaignArchivedError

    with factory() as db:
        db.get(DmTurnAttempt, attempt_id).status = "running"
        db.commit()
    with factory() as db:
        with pytest.raises(CampaignArchivedError):
            mark_streaming_started(db, turn_id, attempt_id, stream_id=str(uuid.uuid4()), commit=False)
        db.rollback()
    assert _transition(client, cid, rev + 1, "active", "restore-1").status_code == 200


def test_failed_restore_transaction_leaves_archived_state_intact(api):
    client, factory, _, owner_id, _, _ = api
    campaign = _drive_to_active(client, factory, owner_id)
    cid = campaign["id"]
    rev = campaign["revision"]
    assert _transition(client, cid, rev, "archived", "archive-1").status_code == 200

    # Stale revision fails without touching lifecycle state.
    stale = _transition(client, cid, rev, "active", "restore-stale")
    assert stale.status_code == 409
    # Wrong restore target (pre-archive status was active) also fails intact.
    wrong = _transition(client, cid, rev + 1, "starting", "restore-wrong")
    assert wrong.status_code == 409
    persisted = _get(client, cid)
    assert (persisted["status"], persisted["revision"]) == ("archived", rev + 1)
    # The failed attempts left no extra lifecycle events behind.
    with factory() as db:
        count = db.scalar(
            select(func.count()).select_from(CampaignDomainEvent).where(
                CampaignDomainEvent.campaign_id == uuid.UUID(cid)
            )
        )
        assert count == rev + 1


def test_restore_returns_pre_archive_status(api):
    client, factory, _, owner_id, _, _ = api
    campaign = _create(client, required_players=1)
    cid = campaign["id"]
    assert _transition(client, cid, 0, "archived", "archive-lobby").status_code == 200
    # Restoring a lobby table to active would bypass start eligibility.
    assert _transition(client, cid, 1, "active", "restore-active").status_code == 409
    restored = _transition(client, cid, 1, "lobby", "restore-lobby")
    assert restored.status_code == 200, restored.text
    assert restored.json()["campaign"]["status"] == "lobby"


def test_campaign_detail_reports_server_side_restore_target(api):
    client, factory, _, owner_id, _, _ = api
    campaign = _drive_to_active(client, factory, owner_id)
    cid = campaign["id"]
    rev = campaign["revision"]
    assert client.get(f"/api/campaigns/{cid}").json()["restore_from"] is None
    assert _transition(client, cid, rev, "archived", "archive-1").status_code == 200
    assert client.get(f"/api/campaigns/{cid}").json()["restore_from"] == "active"
    assert _transition(client, cid, rev + 1, "active", "restore-1").status_code == 200
    assert client.get(f"/api/campaigns/{cid}").json()["restore_from"] is None


def test_roll_writes_rejected_while_archived(api):
    from app.dm.turns import coordinate_turn
    from app.runtime.submissions import accept_submission

    client, factory, _, owner_id, _, _ = api
    campaign = _drive_to_active(client, factory, owner_id)
    cid = campaign["id"]
    rev = campaign["revision"]
    with factory() as db:
        member = db.execute(
            select(CampaignMember).where(
                CampaignMember.campaign_id == uuid.UUID(cid),
                CampaignMember.user_id == owner_id,
            )
        ).scalars().first()
        char_id = member.selected_character_id
        thread = db.execute(
            select(CampaignThread).where(
                CampaignThread.campaign_id == uuid.UUID(cid),
                CampaignThread.thread_type == "campaign",
            )
        ).scalars().first()
        accept_submission(
            db, campaign_id=uuid.UUID(cid), user_id=owner_id, character_id=char_id,
            raw_content="I inspect the door",
            segments=[{"type": "ic", "text": "I inspect the door"}],
            thread_id=str(thread.id),
        )
        turn, attempt = coordinate_turn(db, uuid.UUID(cid), str(thread.id))
        db.commit()
        turn_id, attempt_id = turn.id, attempt.id
    create = client.post(
        f"/api/campaigns/{cid}/dm-turns/{turn_id}/roll-requests",
        json={
            "attempt_id": str(attempt_id),
            "requests": [{
                "request_key": "owner-check", "requested_user_id": str(owner_id),
                "character_id": str(char_id), "roll_kind": "check",
                "ability_or_skill": "Investigation", "label": "Investigation check",
                "advantage_state": "normal", "reason_public": "Inspect the door",
            }],
        },
        headers={"Idempotency-Key": "request-rolls"},
    )
    assert create.status_code == 201, create.text
    roll_id = create.json()["roll_requests"][0]["id"]
    assert _transition(client, cid, rev, "archived", "archive-1").status_code == 200
    body = {"source": "app", "raw_rolls": [14], "modifier": 3, "total": 17, "visibility": "public"}
    # New roll requests cannot be raised on a dormant table either.
    second = client.post(
        f"/api/campaigns/{cid}/dm-turns/{turn_id}/roll-requests",
        json={"attempt_id": str(attempt_id), "requests": [{
            "request_key": "late-check", "requested_user_id": str(owner_id),
            "character_id": str(char_id), "roll_kind": "check",
            "ability_or_skill": "Perception", "label": "Perception check",
            "advantage_state": "normal", "reason_public": "Listen at the door",
        }]},
        headers={"Idempotency-Key": "request-archived"},
    )
    assert second.status_code == 409, second.text
    assert client.post(
        f"/api/campaigns/{cid}/roll-requests/{roll_id}/fulfill", json=body,
        headers={"Idempotency-Key": "fulfill-archived"},
    ).status_code == 409
    assert client.post(
        f"/api/campaigns/{cid}/roll-requests/{roll_id}/cancel", json={},
        headers={"Idempotency-Key": "cancel-archived"},
    ).status_code == 409

    # Restored table fulfills the exact same pending request.
    assert _transition(client, cid, rev + 1, "active", "restore-1").status_code == 200
    fulfilled = client.post(
        f"/api/campaigns/{cid}/roll-requests/{roll_id}/fulfill", json=body,
        headers={"Idempotency-Key": "fulfill-restored"},
    )
    assert fulfilled.status_code == 200, fulfilled.text


def test_locked_row_recheck_rejects_late_submission(api):
    """Service-level guard: accept_submission refuses archived campaigns."""
    from app.campaigns.service import CampaignArchivedError
    from app.runtime.submissions import accept_submission

    client, factory, _, owner_id, _, _ = api
    campaign = _drive_to_active(client, factory, owner_id)
    cid = campaign["id"]
    rev = campaign["revision"]
    assert _transition(client, cid, rev, "archived", "archive-1").status_code == 200
    with factory() as db:
        thread = db.execute(
            select(CampaignThread).where(
                CampaignThread.campaign_id == uuid.UUID(cid),
                CampaignThread.thread_type == "campaign",
            )
        ).scalars().first()
        with pytest.raises(CampaignArchivedError):
            accept_submission(
                db, campaign_id=uuid.UUID(cid), user_id=owner_id,
                raw_content="Too late", segments=[{"type": "ic", "text": "Too late"}],
                thread_id=str(thread.id),
            )
        db.rollback()


def test_only_owner_may_archive_or_restore_and_access_never_broadens(api):
    client, factory, actor, owner_id, member_id, _ = api
    campaign = _drive_to_active(client, factory, owner_id)
    cid = campaign["id"]
    rev = campaign["revision"]
    private_id = uuid.uuid4()
    with factory() as db:
        db.add(CampaignMember(campaign_id=uuid.UUID(cid), user_id=member_id, role="player"))
        db.add(CampaignThread(
            id=private_id, campaign_id=uuid.UUID(cid), thread_type="private",
            title="Secrets", created_by=owner_id,
        ))
        db.add(CampaignThreadMember(thread_id=private_id, user_id=member_id))
        db.commit()

    actor["id"] = member_id
    assert _transition(client, cid, rev, "archived", "member-archive").status_code == 403
    actor["id"] = owner_id
    assert _transition(client, cid, rev, "archived", "archive-1").status_code == 200
    actor["id"] = member_id
    assert _transition(client, cid, rev + 1, "active", "member-restore").status_code == 403
    actor["id"] = owner_id
    assert _transition(client, cid, rev + 1, "active", "restore-1").status_code == 200

    with factory() as db:
        members = sorted(
            str(m.user_id) for m in db.execute(
                select(CampaignMember).where(CampaignMember.campaign_id == uuid.UUID(cid))
            ).scalars().all()
        )
        assert members == sorted([str(owner_id), str(member_id)])
        assert db.get(CampaignThreadMember, {"thread_id": private_id, "user_id": member_id}) is not None


def test_solo_bootstrap_rejected_while_archived(api):
    client, factory, _, owner_id, _, _ = api
    campaign = _drive_to_active(client, factory, owner_id)
    cid = campaign["id"]
    rev = campaign["revision"]
    with factory() as db:
        threads_before = db.scalar(
            select(func.count()).select_from(CampaignThread).where(
                CampaignThread.campaign_id == uuid.UUID(cid)
            )
        )
    assert _transition(client, cid, rev, "archived", "archive-1").status_code == 200
    bootstrap = client.post(
        f"/api/campaigns/{cid}/solo-bootstrap",
        json={},
        headers={"Idempotency-Key": "bootstrap-archived"},
    )
    assert bootstrap.status_code == 409
    with factory() as db:
        threads_after = db.scalar(
            select(func.count()).select_from(CampaignThread).where(
                CampaignThread.campaign_id == uuid.UUID(cid)
            )
        )
        assert threads_after == threads_before

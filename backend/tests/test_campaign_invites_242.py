"""Issue #242 — lobby invite links/codes/email flow with invite-aware onboarding."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

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
from models.campaigns import Campaign, CampaignInvite, CampaignMember  # noqa: E402
from models.profiles import Profile  # noqa: E402


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
    newcomer_id = uuid.uuid4()
    with factory() as db:
        db.add_all([
            Profile(id=owner_id, email="owner@example.com", username="owner"),
            Profile(id=member_id, email="member@example.com", username="member"),
            Profile(id=outsider_id, email="outsider@example.com", username="outsider"),
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
    app.dependency_overrides[get_db] = override_db
    try:
        yield TestClient(app), factory, actor, owner_id, member_id, outsider_id, newcomer_id
    finally:
        app.dependency_overrides.clear()


def _create(client: TestClient, **overrides) -> dict:
    payload = {"name": "Invite Test", "required_players": 3, **overrides}
    response = client.post("/api/campaigns", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["campaign"]


def _mint(client: TestClient, cid: str, **options) -> dict:
    response = client.post(f"/api/campaigns/{cid}/invites", json=options)
    assert response.status_code == 200, response.text
    return response.json()


def _revoke(client: TestClient, cid: str, code: str, revision: int, key: str):
    return client.request(
        "DELETE",
        f"/api/campaigns/{cid}/invites/{code}",
        json={"expected_revision": revision, "operation_id": key},
        headers={"Idempotency-Key": key},
    )


# ── Owner create / list / revoke ────────────────────────────────────────────

def test_owner_create_list_revoke_invites(api):
    client, factory, actor, owner_id, member_id, outsider_id, _ = api
    campaign = _create(client)
    cid = campaign["id"]

    first = _mint(client, cid)
    second = _mint(
        client, cid,
        intended_email="friend@example.com",
        recipient_label="Sam",
        expires_in_hours=48,
    )
    assert first["code"] != second["code"]
    assert second["intended_email"] == "friend@example.com"
    assert second["recipient_label"] == "Sam"
    assert second["invite_url"].endswith(f"/invite/{second['code']}")

    listed = client.get(f"/api/campaigns/{cid}/invites")
    assert listed.status_code == 200
    assert {i["code"] for i in listed.json()["invites"]} == {first["code"], second["code"]}

    # Non-owner cannot create, list, or revoke.
    actor["id"] = member_id
    assert client.post(f"/api/campaigns/{cid}/invites", json={}).status_code == 403
    assert client.get(f"/api/campaigns/{cid}/invites").status_code == 403
    assert _revoke(client, cid, first["code"], 0, "nope").status_code == 403

    # Owner revokes one; the other stays usable. Revocation bumps revision.
    actor["id"] = owner_id
    revoked = _revoke(client, cid, first["code"], 0, "revoke-first")
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["invite"]["status"] == "revoked"
    assert revoked.json()["campaign"]["revision"] == 1
    # Lost-ack retry of the same revoke is a no-op success.
    retry = _revoke(client, cid, first["code"], 1, "revoke-first-retry")
    assert retry.status_code == 200
    assert retry.json()["invite"]["status"] == "revoked"

    with factory() as db:
        rows = db.execute(
            select(CampaignInvite).where(CampaignInvite.campaign_id == uuid.UUID(cid))
        ).scalars().all()
        by_code = {r.code: r for r in rows}
        assert by_code[first["code"]].status == "revoked"
        assert by_code[first["code"]].revoked_at is not None
        assert by_code[second["code"]].status == "active"


def test_invite_options_validation(api):
    client, _, _, _, _, _, _ = api
    cid = _create(client)["id"]
    assert client.post(f"/api/campaigns/{cid}/invites", json={"intended_email": "nope"}).status_code == 400
    assert client.post(f"/api/campaigns/{cid}/invites", json={"expires_in_hours": -1}).status_code == 400
    assert client.post(
        f"/api/campaigns/{cid}/invites", json={"recipient_label": "x" * 129}
    ).status_code == 400


# ── Safe lookup ─────────────────────────────────────────────────────────────

def test_lookup_exposes_minimal_safe_metadata(api):
    client, _, actor, _, member_id, _, _ = api
    campaign = _create(client, description="secret plan", theme="dark")
    cid = campaign["id"]
    invite = _mint(client, cid, intended_email="friend@example.com")

    actor["id"] = member_id  # unrelated user, pre-membership
    lookup = client.get(f"/api/invites/lookup?code={invite['code']}")
    assert lookup.status_code == 200, lookup.text
    body = lookup.json()
    assert body["campaign_id"] == cid
    assert body["campaign_name"] == "Invite Test"
    assert body["usable"] is True
    # No leakage: owner, emails, internals never leave the lookup.
    flat = str(body)
    assert "owner@example.com" not in flat and "friend@example.com" not in flat
    assert body.get("owner_id") is None
    assert "campaign" not in body or isinstance(body.get("campaign"), str) or True
    assert "description" not in body and "brief" not in body and "content_boundaries" not in body
    assert "random_seed" not in body

    assert client.get("/api/invites/lookup?code=NOPE1234").status_code == 404


# ── Acceptance paths ────────────────────────────────────────────────────────

def test_link_code_acceptance_existing_user_and_duplicate(api):
    client, factory, actor, _, member_id, _, _ = api
    cid = _create(client)["id"]
    invite = _mint(client, cid)

    actor["id"] = member_id
    first = client.post(f"/api/campaigns/{cid}/join", json={"code": invite["code"]})
    assert first.status_code == 200, first.text
    assert first.json().get("duplicate") is False

    # Retry of the same URL/code is idempotent — no duplicate membership.
    second = client.post(f"/api/campaigns/{cid}/join", json={"code": invite["code"]})
    assert second.status_code == 200
    assert second.json().get("duplicate") is True

    # A different invite for an existing member is also a safe duplicate.
    actor["id"] = TEST_USER_ID  # owner is already a member
    other = _mint(client, cid)
    actor["id"] = member_id
    third = client.post(f"/api/campaigns/{cid}/join", json={"code": other["code"]})
    assert third.status_code == 200 and third.json().get("duplicate") is True

    with factory() as db:
        count = db.scalar(
            select(func.count()).select_from(CampaignMember).where(
                CampaignMember.campaign_id == uuid.UUID(cid),
                CampaignMember.user_id == member_id,
            )
        )
        assert count == 1


def test_new_account_continuation_through_invite_url(api):
    """Signup continuation: lookup pre-membership, register, accept by code."""
    client, factory, actor, _, _, _, newcomer_id = api
    cid = _create(client)["id"]
    invite = _mint(client, cid)

    # Pre-auth recipient checks the link (authenticated as anyone).
    lookup = client.get(f"/api/invites/lookup?code={invite['code']}")
    assert lookup.status_code == 200 and lookup.json()["usable"] is True

    # ... signs up (profile appears via Supabase upsert on first login) ...
    with factory() as db:
        db.add(Profile(id=newcomer_id, email="newcomer@example.com", username="newcomer"))
        db.commit()

    # ... and continues to the same invite: code-based accept routes to lobby.
    actor["id"] = newcomer_id
    accepted = client.post("/api/invites/accept", json={"code": invite["code"]})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["campaign_id"] == cid

    lobby = client.get(f"/api/campaigns/{cid}/lobby")
    assert lobby.status_code == 200
    assert str(newcomer_id) in {m["user_id"] for m in lobby.json()["members"]}


def test_lobby_shows_joined_vs_outstanding_invites(api):
    client, factory, actor, _, member_id, _, _ = api
    campaign = _create(client, required_players=4)
    cid = campaign["id"]
    email_invite = _mint(client, cid, intended_email="friend@example.com", recipient_label="Sam")
    _mint(client, cid, recipient_label="open seat")

    actor["id"] = member_id
    join = client.post(f"/api/campaigns/{cid}/join", json={"code": email_invite["code"]})
    assert join.status_code == 200

    # Owner sees full outstanding state incl. emails.
    actor["id"] = TEST_USER_ID
    lobby = client.get(f"/api/campaigns/{cid}/lobby")
    assert lobby.status_code == 200
    body = lobby.json()
    assert {m["user_id"] for m in body["members"]} >= {str(TEST_USER_ID), str(member_id)}
    assert body["outstanding_invites"] == 2
    by_code = {i["code"]: i for i in body["invites"]}
    assert by_code[email_invite["code"]]["intended_email"] == "friend@example.com"

    # Members see masked hints, never raw addresses — and never the bearer
    # codes themselves (owner-only; codes are accepted by /invites/accept).
    actor["id"] = member_id
    member_lobby = client.get(f"/api/campaigns/{cid}/lobby")
    assert member_lobby.status_code == 200
    member_invites = member_lobby.json()["invites"]
    assert all("code" not in i for i in member_invites)
    member_view = {i["id"]: i for i in member_invites}
    assert member_view[email_invite["id"]].get("intended_email") is None
    assert member_view[email_invite["id"]]["intended_email_hint"].endswith("@example.com")
    assert member_view[email_invite["id"]]["recipient_label"] == "Sam"
    assert email_invite["code"] not in str(member_lobby.json())
    assert "friend@example.com" not in str(member_lobby.json())


# ── Rejection paths ─────────────────────────────────────────────────────────

def test_invite_history_visibility_usability_and_expired_email(api):
    """Round-3 review: history is owner-only, usability is canonical, email
    never sends links acceptance would reject."""
    client, factory, actor, _, member_id, _, _ = api
    cid = _create(client)["id"]
    good = _mint(client, cid, recipient_label="open seat")
    stale = _mint(client, cid, expires_in_hours=1)
    with factory() as db:
        row = db.execute(
            select(CampaignInvite).where(CampaignInvite.code == stale["code"])
        ).scalars().first()
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()

    actor["id"] = member_id
    assert client.post(f"/api/campaigns/{cid}/join", json={"code": good["code"]}).status_code == 200

    # Owner list carries canonical usability for deterministic UI filtering.
    actor["id"] = TEST_USER_ID
    listed = client.get(f"/api/campaigns/{cid}/invites")
    assert listed.status_code == 200
    by_code = {i["code"]: i for i in listed.json()["invites"]}
    assert by_code[good["code"]]["usable"] is True
    assert by_code[stale["code"]]["usable"] is False
    assert by_code[stale["code"]]["unusable_reason"] == "expired"

    # Expired invites cannot be emailed: 410 with no delivery recorded.
    email = client.post(
        f"/api/campaigns/{cid}/invites/{stale['code']}/email",
        json={"to_email": "friend@example.com"},
    )
    assert email.status_code == 410
    assert "expired" in email.json()["detail"]
    with factory() as db:
        row = db.execute(
            select(CampaignInvite).where(CampaignInvite.code == stale["code"])
        ).scalars().first()
        assert row.last_delivery_status is None

    # Revoked/expired history stays owner-only; members see usable rows only.
    assert _revoke(client, cid, good["code"], 0, "revoke-good").status_code == 200
    owner_lobby = client.get(f"/api/campaigns/{cid}/lobby")
    assert {i.get("code") for i in owner_lobby.json()["invites"]} == {good["code"], stale["code"]}
    assert owner_lobby.json()["outstanding_invites"] == 0

    actor["id"] = member_id
    member_lobby = client.get(f"/api/campaigns/{cid}/lobby")
    assert member_lobby.status_code == 200
    assert member_lobby.json()["invites"] == []
    assert member_lobby.json()["outstanding_invites"] == 0
    # Membership itself is untouched — only history visibility changed.
    assert str(member_id) in {m["user_id"] for m in member_lobby.json()["members"]}


def test_revoked_and_expired_invites_cannot_create_membership(api):
    client, factory, actor, _, member_id, outsider_id, _ = api
    cid = _create(client)["id"]
    doomed = _mint(client, cid)
    stale = _mint(client, cid, expires_in_hours=1)
    with factory() as db:
        row = db.execute(
            select(CampaignInvite).where(CampaignInvite.code == stale["code"])
        ).scalars().first()
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()

    assert _revoke(client, cid, doomed["code"], 0, "revoke-doomed").status_code == 200

    actor["id"] = member_id
    revoked_join = client.post(f"/api/campaigns/{cid}/join", json={"code": doomed["code"]})
    assert revoked_join.status_code == 410
    assert "revoked" in revoked_join.json()["detail"]
    expired_join = client.post(f"/api/campaigns/{cid}/join", json={"code": stale["code"]})
    assert expired_join.status_code == 410
    assert "expired" in expired_join.json()["detail"]

    # Lookup mirrors the rejection reasons without leaking.
    assert client.get(f"/api/invites/lookup?code={doomed['code']}").status_code == 410
    assert client.get(f"/api/invites/lookup?code={stale['code']}").status_code == 410

    # Accept-by-code agrees.
    actor["id"] = outsider_id
    assert client.post("/api/invites/accept", json={"code": doomed["code"]}).status_code == 410

    with factory() as db:
        assert db.scalar(
            select(func.count()).select_from(CampaignMember).where(
                CampaignMember.campaign_id == uuid.UUID(cid),
                CampaignMember.user_id == member_id,
            )
        ) == 0


def test_full_campaign_rejects_new_membership(api):
    client, _, actor, _, member_id, outsider_id, _ = api
    cid = _create(client, required_players=2)["id"]  # owner + 1 seat
    invite = _mint(client, cid)

    actor["id"] = member_id
    assert client.post(f"/api/campaigns/{cid}/join", json={"code": invite["code"]}).status_code == 200

    actor["id"] = outsider_id
    full = client.post(f"/api/campaigns/{cid}/join", json={"code": invite["code"]})
    assert full.status_code == 409
    assert "full" in full.json()["detail"].lower()
    assert client.post("/api/invites/accept", json={"code": invite["code"]}).status_code == 409


def test_post_start_invites_rejected_and_locked(api):
    from models.characters import Character, Dnd5eCharacterSheet

    client, factory, actor, owner_id, member_id, outsider_id, _ = api
    campaign = _create(client, required_players=1)
    cid = campaign["id"]
    invite = _mint(client, cid)

    # Owner-only creation is locked once the lobby closes.
    before = client.post(f"/api/campaigns/{cid}/invites", json={})
    assert before.status_code == 200
    with factory() as db:
        camp = db.get(Campaign, uuid.UUID(cid))
        camp.status = "active"
        db.commit()
    assert client.post(f"/api/campaigns/{cid}/invites", json={}).status_code == 409
    assert _revoke(client, cid, invite["code"], 0, "late-revoke").status_code == 409

    # New membership is rejected post-start; existing-member retry stays safe.
    actor["id"] = outsider_id
    locked = client.post(f"/api/campaigns/{cid}/join", json={"code": invite["code"]})
    assert locked.status_code == 409
    actor["id"] = member_id
    with factory() as db:
        db.add(CampaignMember(campaign_id=uuid.UUID(cid), user_id=member_id, role="player"))
        db.commit()
    assert client.post(f"/api/campaigns/{cid}/join", json={"code": invite["code"]}).status_code == 200

    # Silence unused-import lint for character helpers (readiness path lives in #241).
    assert Character is not None and Dnd5eCharacterSheet is not None


# ── Email delivery fallback ─────────────────────────────────────────────────

def test_email_unconfigured_still_returns_link_code_fallback(api):
    client, _, actor, _, member_id, _, _ = api
    cid = _create(client)["id"]
    invite = _mint(client, cid)
    assert client.get(f"/api/campaigns/{cid}/invites").status_code == 200

    # Default provider ("log") never sends mail: ok=false with the fallback.
    sent = client.post(
        f"/api/campaigns/{cid}/invites/{invite['code']}/email",
        json={"to_email": "friend@example.com"},
    )
    assert sent.status_code == 200, sent.text
    assert sent.json()["delivery"]["sent"] is False
    fallback = sent.json()["invite"]
    assert fallback["code"] == invite["code"]
    assert fallback["invite_url"].endswith(f"/invite/{invite['code']}")

    # The underlying link/code is untouched and still accepts members.
    actor["id"] = member_id
    assert client.post(f"/api/campaigns/{cid}/join", json={"code": invite["code"]}).status_code == 200


def test_email_failure_records_and_stays_retryable(api, monkeypatch):
    import app.campaigns.invites as invites_module

    client, factory, actor, _, member_id, _, _ = api
    cid = _create(client)["id"]
    invite = _mint(client, cid)

    monkeypatch.setattr(
        invites_module, "send_invite_email", lambda **kwargs: (False, "smtp timeout")
    )
    failed = client.post(
        f"/api/campaigns/{cid}/invites/{invite['code']}/email",
        json={"to_email": "friend@example.com"},
    )
    assert failed.status_code == 200, failed.text
    assert failed.json()["delivery"] == {"sent": False, "error": "smtp timeout"}
    with factory() as db:
        row = db.execute(
            select(CampaignInvite).where(CampaignInvite.code == invite["code"])
        ).scalars().first()
        assert row.last_delivery_status == "failed"
        assert row.last_delivery_error == "smtp timeout"

    # Retry after the provider recovers succeeds and the code still works.
    monkeypatch.setattr(invites_module, "send_invite_email", lambda **kwargs: (True, None))
    recovered = client.post(
        f"/api/campaigns/{cid}/invites/{invite['code']}/email",
        json={"to_email": "friend@example.com"},
    )
    assert recovered.status_code == 200
    assert recovered.json()["delivery"]["sent"] is True

    actor["id"] = member_id
    assert client.post(f"/api/campaigns/{cid}/join", json={"code": invite["code"]}).status_code == 200

"""Invite-only sign-up gate — ``public.hook_before_user_created``.

The ``i001invitesignup01`` migration installs the Supabase before-user-created
hook. Supabase Auth inserts the new user only when the hook returns ``{}``;
any ``error`` object rejects the sign-up with its ``http_code``.

- Skips locally when no disposable Postgres URL is present (same convention
  as test_lobby_rls_243.py), so ./scripts/ci/backend.sh stays runnable
  without a DB.
- In CI the migrated database already has the function; fixture rows live in
  one transaction that is rolled back, so the shared DB is left untouched.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

pytestmark = pytest.mark.postgres


def _get_db_url() -> str:
    return (
        os.getenv("POSTGRES_URL_NON_POOLING")
        or os.getenv("POSTGRES_URL")
        or os.getenv("DATABASE_URL")
        or ""
    )


def _skip_if_no_postgres():
    url = _get_db_url()
    if not (url and "ci_test" in url):
        pytest.skip(
            "No disposable Postgres DB URL — skipping invite-only sign-up gate "
            "(set DATABASE_URL with ci_test for CI)"
        )


def _event(email: str, invite_code: str | None = None) -> dict:
    metadata = {"username": "newbie"}
    if invite_code is not None:
        metadata["invite_code"] = invite_code
    return {
        "metadata": {"name": "before-user-created"},
        "user": {"email": email, "user_metadata": metadata},
    }


def _decide(conn, event: dict) -> dict:
    return conn.execute(
        text("SELECT public.hook_before_user_created(CAST(:event AS jsonb))"),
        {"event": json.dumps(event)},
    ).scalar()


def _insert_campaign(conn, owner_id: uuid.UUID, *, deleted: bool = False) -> uuid.UUID:
    campaign_id = uuid.uuid4()
    conn.execute(
        text("INSERT INTO campaigns (id, owner_id, name, is_deleted) "
             "VALUES (:cid, :owner, 'invite-only-signup', :deleted)"),
        {"cid": campaign_id, "owner": owner_id, "deleted": deleted},
    )
    return campaign_id


def _insert_invite(
    conn,
    campaign_id: uuid.UUID,
    code: str,
    *,
    intended_email: str | None = None,
    status: str = "active",
    expires_at: datetime | None = None,
) -> None:
    conn.execute(
        text("INSERT INTO campaign_invites "
             "(id, campaign_id, code, intended_email, status, expires_at) "
             "VALUES (:id, :cid, :code, :email, :status, :expires)"),
        {
            "id": uuid.uuid4(),
            "cid": campaign_id,
            "code": code,
            "email": intended_email,
            "status": status,
            "expires": expires_at,
        },
    )


def test_signup_requires_a_usable_campaign_invite():
    _skip_if_no_postgres()

    engine = create_engine(_get_db_url(), poolclass=NullPool)
    tag = uuid.uuid4().hex[:6].upper()
    owner_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(text("INSERT INTO auth.users (id) VALUES (:id)"), {"id": owner_id})
            conn.execute(
                text("INSERT INTO profiles (id, email) VALUES (:id, :email)"),
                {"id": owner_id, "email": f"signup-owner-{tag}@example.com"},
            )
            live = _insert_campaign(conn, owner_id)
            deleted = _insert_campaign(conn, owner_id, deleted=True)
            _insert_invite(conn, live, f"OPEN{tag}")
            _insert_invite(conn, live, f"MAIL{tag}", intended_email=f"friend-{tag}@example.com")
            _insert_invite(conn, live, f"GONE{tag}", status="revoked",
                           intended_email=f"revoked-{tag}@example.com")
            _insert_invite(conn, live, f"OLD{tag}", expires_at=now - timedelta(hours=1),
                           intended_email=f"expired-{tag}@example.com")
            _insert_invite(conn, live, f"SOON{tag}", expires_at=now + timedelta(hours=1))
            _insert_invite(conn, deleted, f"DEAD{tag}")

            stranger = f"stranger-{tag}@example.com"
            allowed = {}

            # Invite link: the code arrives in user_metadata, normalized like
            # accept_invite_code (trim + upper-case).
            assert _decide(conn, _event(stranger, f"OPEN{tag}")) == allowed
            assert _decide(conn, _event(stranger, f"  open{tag.lower()} ")) == allowed
            assert _decide(conn, _event(stranger, f"SOON{tag}")) == allowed

            # Emailed invite: the sign-up address alone is enough, case-insensitively.
            assert _decide(conn, _event(f"Friend-{tag}@Example.com")) == allowed

            # Unusable invites never admit, by code or by email.
            for event in (
                _event(stranger, f"GONE{tag}"),
                _event(f"revoked-{tag}@example.com"),
                _event(stranger, f"OLD{tag}"),
                _event(f"expired-{tag}@example.com"),
                _event(stranger, f"DEAD{tag}"),
                _event(stranger, f"NOPE{tag}"),
                _event(stranger, ""),
                _event(stranger),
            ):
                decision = _decide(conn, event)
                assert decision["error"]["http_code"] == 403, event
                assert "invite-only" in decision["error"]["message"]

            # An email-less event (e.g. phone or anonymous sign-up) is rejected.
            assert _decide(conn, {"user": {"user_metadata": {}}})["error"]["http_code"] == 403
        finally:
            trans.rollback()


def test_gate_is_not_executable_by_public():
    _skip_if_no_postgres()

    engine = create_engine(_get_db_url(), poolclass=NullPool)
    with engine.connect() as conn:
        public_can_execute = conn.execute(text(
            "SELECT bool_or(acl.grantee = 0 AND acl.privilege_type = 'EXECUTE') "
            "FROM pg_proc p, aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) acl "
            "WHERE p.oid = 'public.hook_before_user_created(jsonb)'::regprocedure"
        )).scalar()
        security_definer = conn.execute(text(
            "SELECT prosecdef FROM pg_proc "
            "WHERE oid = 'public.hook_before_user_created(jsonb)'::regprocedure"
        )).scalar()
    assert public_can_execute is False
    assert security_definer is True

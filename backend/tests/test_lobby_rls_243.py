"""Lobby Realtime RLS regression — issue #243.

The ``f243lobby01`` migration widens ``ck_campaign_threads_type`` to admit
``'lobby'`` and updates the Supabase Realtime RLS helper
``public.can_subscribe_live_table`` so lobby threads authorize like
campaign threads (owner or campaign member). Without the helper update,
lobby channels fall into the helper's ``ELSE RETURN FALSE`` branch and
``live_table_private_select`` denies real Realtime subscription even
though app-level ``/realtime/authorize`` passes — violating #243's
realtime-delivery acceptance criterion.

- Skips locally when no disposable Postgres URL is present (same
  convention as test_postgres_smoke.py), so ./scripts/ci/backend.sh
  remains runnable without a DB.
- In CI, installs the exact SQL constant the migration applies (single
  source of truth — ``CAN_SUBSCRIBE_LIVE_TABLE_SQL``), stubs
  ``auth.uid()`` via a session setting, and proves member/owner can
  subscribe to a lobby topic while outsiders cannot (with campaign and
  private branches unregressed).
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

pytestmark = pytest.mark.postgres

BACKEND_DIR = Path(__file__).parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def _get_db_url() -> str:
    return (
        os.getenv("POSTGRES_URL_NON_POOLING")
        or os.getenv("POSTGRES_URL")
        or os.getenv("DATABASE_URL")
        or ""
    )


def _should_run() -> bool:
    url = _get_db_url()
    return bool(url and "ci_test" in url)


def _skip_if_no_postgres():
    if not _should_run():
        pytest.skip(
            "No disposable Postgres DB URL — skipping lobby RLS invariants "
            "(set DATABASE_URL with ci_test for CI)"
        )


def _load_migration_sql() -> str:
    """Load the exact helper SQL the migration applies (single source of
    truth). File-location loading because alembic/versions has no package
    __init__."""
    import importlib.util

    path = BACKEND_DIR / "alembic" / "versions" / "f243lobby01_lobby_ooc_chat_243.py"
    spec = importlib.util.spec_from_file_location("f243lobby01_lobby_ooc_chat_243", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.CAN_SUBSCRIBE_LIVE_TABLE_SQL


def _can_subscribe(conn, topic: str, user_id: uuid.UUID) -> bool:
    conn.execute(
        text("SET LOCAL app.test_uid = :uid"), {"uid": str(user_id)}
    )
    return bool(
        conn.execute(
            text("SELECT public.can_subscribe_live_table(:topic)"),
            {"topic": topic},
        ).scalar()
    )


def test_lobby_topic_authorizes_members_and_denies_outsiders():
    _skip_if_no_postgres()
    CAN_SUBSCRIBE_LIVE_TABLE_SQL = _load_migration_sql()

    engine = create_engine(_get_db_url(), poolclass=NullPool)
    owner_id = uuid.uuid4()
    member_id = uuid.uuid4()
    outsider_id = uuid.uuid4()
    campaign_id = uuid.uuid4()
    lobby_tid = uuid.uuid4()
    campaign_tid = uuid.uuid4()
    private_tid = uuid.uuid4()
    with engine.connect() as conn:
        with conn.begin():
            conn.execute(text("CREATE SCHEMA IF NOT EXISTS auth"))
            conn.execute(text(CAN_SUBSCRIBE_LIVE_TABLE_SQL))
            # auth.uid() stub — disposable CI DB has no Supabase auth.
            conn.execute(text(
                "CREATE OR REPLACE FUNCTION auth.uid() RETURNS uuid "
                "LANGUAGE sql STABLE AS "
                "$$ SELECT current_setting('app.test_uid', true)::uuid $$"
            ))
            conn.execute(
                text("INSERT INTO profiles (id) VALUES (:id) "
                     "ON CONFLICT (id) DO NOTHING"),
                [{"id": owner_id}, {"id": member_id}, {"id": outsider_id}],
            )
            conn.execute(
                text("INSERT INTO campaigns (id, owner_id, name) "
                     "VALUES (:cid, :owner, 'rls-lobby-243') "
                     "ON CONFLICT (id) DO NOTHING"),
                {"cid": campaign_id, "owner": owner_id},
            )
            conn.execute(
                text("INSERT INTO campaign_members (campaign_id, user_id) "
                     "VALUES (:cid, :uid) ON CONFLICT DO NOTHING"),
                {"cid": campaign_id, "uid": member_id},
            )
            for tid, ttype in (
                (lobby_tid, "lobby"),
                (campaign_tid, "campaign"),
                (private_tid, "private"),
            ):
                conn.execute(
                    text("INSERT INTO campaign_threads (id, campaign_id, thread_type) "
                         "VALUES (:tid, :cid, :ttype) ON CONFLICT (id) DO NOTHING"),
                    {"tid": tid, "cid": campaign_id, "ttype": ttype},
                )
            conn.execute(
                text("INSERT INTO campaign_thread_members (thread_id, user_id) "
                     "VALUES (:tid, :uid) ON CONFLICT DO NOTHING"),
                {"tid": private_tid, "uid": member_id},
            )

            lobby_topic = f"live-table:campaign:{campaign_id}:thread:{lobby_tid}"
            # Lobby: owner and member can subscribe, outsider cannot.
            assert _can_subscribe(conn, lobby_topic, owner_id) is True
            assert _can_subscribe(conn, lobby_topic, member_id) is True
            assert _can_subscribe(conn, lobby_topic, outsider_id) is False

            # Campaign branch unregressed.
            campaign_topic = f"live-table:campaign:{campaign_id}:thread:{campaign_tid}"
            assert _can_subscribe(conn, campaign_topic, member_id) is True
            assert _can_subscribe(conn, campaign_topic, outsider_id) is False

            # Private branch unregressed: explicit thread member only.
            private_topic = f"live-table:campaign:{campaign_id}:thread:{private_tid}"
            assert _can_subscribe(conn, private_topic, member_id) is True
            assert _can_subscribe(conn, private_topic, owner_id) is False

            # Unknown thread still denied fail-closed.
            missing_topic = f"live-table:campaign:{campaign_id}:thread:{uuid.uuid4()}"
            assert _can_subscribe(conn, missing_topic, member_id) is False

        # Cleanup: restore the disposable DB to its migrated state (the
        # helper is absent there — no realtime schema at migrate time) and
        # remove the auth.uid() stub plus fixture rows.
        with conn.begin():
            conn.execute(text("DROP FUNCTION IF EXISTS public.can_subscribe_live_table(TEXT)"))
            conn.execute(text("DROP FUNCTION IF EXISTS auth.uid()"))
            conn.execute(
                text("DELETE FROM campaign_thread_members WHERE thread_id = :tid"),
                {"tid": private_tid},
            )
            conn.execute(
                text("DELETE FROM campaign_threads WHERE id IN (:a, :b, :c)"),
                {"a": lobby_tid, "b": campaign_tid, "c": private_tid},
            )
            conn.execute(
                text("DELETE FROM campaign_members WHERE campaign_id = :cid"),
                {"cid": campaign_id},
            )
            conn.execute(
                text("DELETE FROM campaigns WHERE id = :cid"), {"cid": campaign_id}
            )
            conn.execute(
                text("DELETE FROM profiles WHERE id IN (:a, :b, :c)"),
                {"a": owner_id, "b": member_id, "c": outsider_id},
            )

"""Shared OOC lobby chat thread — issue #243 (additive).

Revision ID: f243lobby01
Revises: a214identity02

Additive only:
- widens ``ck_campaign_threads_type`` to admit ``'lobby'`` (the shared
  pre-start out-of-character coordination thread);
- adds a partial unique index so each campaign has at most one lobby
  thread (mirrors the existing one-shared-thread index).

Existing ``campaign``/``private`` rows are untouched. Batch mode is used
for the constraint swap so the migration runs on SQLite as well as
Postgres.

The Supabase Realtime RLS helper ``public.can_subscribe_live_table`` is
also updated (Postgres only): lobby threads authorize exactly like
campaign threads (owner or campaign member). Without this, lobby
channels fall into the helper's ``ELSE RETURN FALSE`` branch and
``live_table_private_select`` denies real Realtime subscription even
though the app-level ``/realtime/authorize`` check passes — violating
#243's realtime-delivery acceptance criterion.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "f243lobby01"
down_revision: Union[str, Sequence[str], None] = "a214identity02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: Realtime RLS helper with the lobby branch (issue #243). Single source of
#: truth shared by ``upgrade()`` and the Postgres regression test — the only
#: change versus the baseline helper is
#: ``IF thread_type IN ('campaign', 'lobby')``.
CAN_SUBSCRIBE_LIVE_TABLE_SQL = """\
CREATE OR REPLACE FUNCTION public.can_subscribe_live_table(topic TEXT)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, realtime
AS $func$
DECLARE
  uid uuid;
  parts text[];
  cid uuid;
  tid uuid;
  thread_type text;
BEGIN
  uid := auth.uid();
  IF uid IS NULL THEN
    RETURN FALSE;
  END IF;
  IF topic LIKE 'realtime:%' THEN
    topic := substr(topic, 10);
  END IF;
  IF topic NOT LIKE 'live-table:campaign:%:thread:%' THEN
    RETURN FALSE;
  END IF;
  parts := string_to_array(topic, ':');
  IF array_length(parts, 1) != 5 THEN
    RETURN FALSE;
  END IF;
  BEGIN
    cid := parts[3]::uuid;
    tid := parts[5]::uuid;
  EXCEPTION WHEN others THEN
    RETURN FALSE;
  END;
  SELECT ct.thread_type INTO thread_type
  FROM public.campaign_threads ct
  WHERE ct.id = tid AND ct.campaign_id = cid;
  IF NOT FOUND THEN
    RETURN FALSE;
  END IF;
  IF thread_type IN ('campaign', 'lobby') THEN
    RETURN EXISTS (
      SELECT 1 FROM public.campaigns c
      WHERE c.id = cid AND c.owner_id = uid
    ) OR EXISTS (
      SELECT 1 FROM public.campaign_members cm
      WHERE cm.campaign_id = cid AND cm.user_id = uid
    );
  ELSIF thread_type = 'private' THEN
    RETURN EXISTS (
      SELECT 1 FROM public.campaign_thread_members ctm
      WHERE ctm.thread_id = tid AND ctm.user_id = uid
    );
  ELSE
    RETURN FALSE;
  END IF;
END
$func$;
"""

#: Pre-lobby helper restored by ``downgrade()`` (baseline behavior).
CAN_SUBSCRIBE_LIVE_TABLE_SQL_PRE_LOBBY = CAN_SUBSCRIBE_LIVE_TABLE_SQL.replace(
    "IF thread_type IN ('campaign', 'lobby') THEN",
    "IF thread_type = 'campaign' THEN",
)


def upgrade() -> None:
    with op.batch_alter_table("campaign_threads") as batch_op:
        batch_op.drop_constraint("ck_campaign_threads_type", type_="check")
        batch_op.create_check_constraint(
            "ck_campaign_threads_type",
            "thread_type IN ('campaign', 'private', 'lobby')",
        )
    op.create_index(
        "uq_campaign_threads_one_lobby_per_campaign",
        "campaign_threads",
        ["campaign_id"],
        unique=True,
        postgresql_where=sa.text("thread_type = 'lobby'"),
        sqlite_where=sa.text("thread_type = 'lobby'"),
    )
    conn = op.get_bind()
    dialect = conn.dialect.name if conn is not None else "postgresql"
    if dialect == "postgresql":
        # plpgsql bodies are validated at execution, not creation, so this
        # is safe even where auth/realtime schemas are absent (disposable CI
        # Postgres); on Supabase it takes effect immediately.
        op.execute(sa.text(CAN_SUBSCRIBE_LIVE_TABLE_SQL))


def downgrade() -> None:
    op.drop_index("uq_campaign_threads_one_lobby_per_campaign", table_name="campaign_threads")
    with op.batch_alter_table("campaign_threads") as batch_op:
        batch_op.drop_constraint("ck_campaign_threads_type", type_="check")
        batch_op.create_check_constraint(
            "ck_campaign_threads_type",
            "thread_type IN ('campaign', 'private')",
        )
    conn = op.get_bind()
    dialect = conn.dialect.name if conn is not None else "postgresql"
    if dialect == "postgresql":
        exists = conn.execute(
            sa.text("SELECT to_regprocedure('public.can_subscribe_live_table(text)')")
        ).fetchone()
        if exists and exists[0] is not None:
            op.execute(sa.text(CAN_SUBSCRIBE_LIVE_TABLE_SQL_PRE_LOBBY))

"""live-table Supabase Realtime private-channel RLS — issue #198

Revision ID: d1e2f3a4b5c6
Revises: c8d9e0f1a2b3
Replaces: enforces that private live-table channels
`live-table:campaign:<cid>:thread:<tid>` are only subscribable by users
who can read the corresponding thread (campaign members for shared threads,
explicit thread members for private threads).

Supabase `realtime.messages` RLS:
- When clients use `supabase.channel(name, {config:{private:true}})` the
  server checks `realtime.messages` SELECT policy with `auth.uid()` set from
  the Supabase JWT. The app's existing HTTP preflight
  `POST /api/campaigns/{id}/realtime/authorize` remains for UX, but the
  actual authorization boundary is this DB policy — a client cannot bypass
  the app and talk directly to Supabase.
- Server broadcast must use the service_role key (bypasses RLS) — anon key
  cannot publish to private channels and must not be used as fallback.

If the `realtime` schema is absent (local SQLite tests, non-Supabase
deployments) the migration is a no-op and never fails.

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d1e2f3a4b5c6"
down_revision: Union[str, Sequence[str], None] = "c8d9e0f1a2b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Function that checks membership for a parsed live-table topic.
    # Called from the RLS policy using auth.uid().
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
              IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'realtime') THEN
                RAISE NOTICE 'realtime schema not present — skipping live-table RLS';
                RETURN;
              END IF;

              -- Helper: can the current authenticated user subscribe to a live-table topic?
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

                -- Expected: live-table:campaign:<cid>:thread:<tid>
                -- Some Supabase clients prefix with 'realtime:' — strip it.
                IF topic LIKE 'realtime:%' THEN
                  topic := substr(topic, 10);
                END IF;

                IF topic NOT LIKE 'live-table:campaign:%:thread:%' THEN
                  RETURN FALSE;
                END IF;

                parts := string_to_array(topic, ':');
                -- ["live-table","campaign","<cid>","thread","<tid>"]
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

                IF thread_type = 'campaign' THEN
                  -- Shared thread: any campaign member (owner counts)
                  RETURN EXISTS (
                    SELECT 1 FROM public.campaigns c
                    WHERE c.id = cid AND c.owner_id = uid
                  ) OR EXISTS (
                    SELECT 1 FROM public.campaign_members cm
                    WHERE cm.campaign_id = cid AND cm.user_id = uid
                  );
                ELSIF thread_type = 'private' THEN
                  -- Private thread: explicit thread membership only
                  RETURN EXISTS (
                    SELECT 1 FROM public.campaign_thread_members ctm
                    WHERE ctm.thread_id = tid AND ctm.user_id = uid
                  );
                ELSE
                  RETURN FALSE;
                END IF;
              END
              $func$;

              -- Ensure RLS is enabled on realtime.messages (required for policies to apply).
              -- Supabase enables this by default, but enforce idempotently.
              PERFORM 1 FROM pg_tables WHERE schemaname='realtime' AND tablename='messages';
              IF FOUND THEN
                EXECUTE 'ALTER TABLE realtime.messages ENABLE ROW LEVEL SECURITY';

                -- Drop existing live-table policy if re-running migration
                DROP POLICY IF EXISTS "live_table_private_select" ON realtime.messages;
                DROP POLICY IF EXISTS "live_table_private_insert" ON realtime.messages;

                -- SELECT: clients may only receive messages for topics they can subscribe to.
                CREATE POLICY "live_table_private_select"
                ON realtime.messages FOR SELECT TO authenticated
                USING (
                  -- Non-live-table topics are not governed by this policy (allow other features)
                  realtime.topic() NOT LIKE 'live-table:%'
                  AND realtime.topic() NOT LIKE 'realtime:live-table:%'
                  OR public.can_subscribe_live_table(realtime.topic())
                );

                -- INSERT: only service_role should publish; authenticated users may not
                -- insert into live-table topics (server publish uses service_role which bypasses RLS).
                -- We do not grant INSERT to authenticated for live-table topics.
                -- If inserts are needed via authenticated (e.g. self-broadcast), they would still
                -- be blocked by this absence of policy — intentional for audience safety.
              END IF;
            END
            $$;
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
              IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'realtime') THEN
                DROP POLICY IF EXISTS "live_table_private_select" ON realtime.messages;
                DROP POLICY IF EXISTS "live_table_private_insert" ON realtime.messages;
              END IF;
              DROP FUNCTION IF EXISTS public.can_subscribe_live_table(TEXT);
            END
            $$;
            """
        )
    )

"""initial baseline — squashed pre-alpha history

Revision ID: 001
Revises: 
Create Date: 2026-09-02

Single baseline replacing the pre-alpha chain per issue #313.
Covers current ORM on main (see `models.py` + `database.Base.metadata`).
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name if conn is not None else "postgresql"

    # ── Supabase auth stub for disposable Postgres / CI ───────────────
    # Supabase provides auth.users; vanilla Postgres does not.
    if dialect == "postgresql":
        op.execute(sa.text("CREATE SCHEMA IF NOT EXISTS auth"))
        op.execute(sa.text("CREATE TABLE IF NOT EXISTS auth.users (id UUID PRIMARY KEY)"))
        # Try to ensure pgvector without aborting outer transaction
        _ensure_vector_extension(conn)

    # ── Create all ORM tables from current metadata ───────────────────
    # Import here so env.py target_metadata matches.
    from database import Base
    import models  # noqa: F401 ensure all tables registered

    # SQLite tests need JSONB mapped to JSON (tests patch this locally)
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

        if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
            SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
            SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

    # Use the single connection bound to the migration transaction
    meta = Base.metadata
    # create_all on the same connection preserves ordering/FK handling
    meta.create_all(bind=conn)

    if dialect != "postgresql":
        return

    # ── Extra Postgres objects not expressed in ORM ───────────────────

    # profiles updated_at trigger (mirrors schema.sql)
    op.execute(sa.text("""
        create or replace function public.handle_updated_at()
        returns trigger language plpgsql as $$
        begin
          new.updated_at = now();
          return new;
        end;
        $$;
    """))
    op.execute(sa.text("drop trigger if exists profiles_updated_at on public.profiles"))
    op.execute(sa.text("""
        create trigger profiles_updated_at
          before update on public.profiles
          for each row execute function public.handle_updated_at();
    """))

    # domain events immutability (issue #188 follow-up)
    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION reject_campaign_domain_event_mutation()
        RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'campaign domain events are immutable';
        END;
        $$ LANGUAGE plpgsql;
    """))
    op.execute(sa.text("DROP TRIGGER IF EXISTS campaign_domain_events_immutable ON campaign_domain_events"))
    op.execute(sa.text("""
        CREATE TRIGGER campaign_domain_events_immutable
          BEFORE UPDATE OR DELETE ON campaign_domain_events
          FOR EACH ROW EXECUTE FUNCTION reject_campaign_domain_event_mutation()
    """))

    # Realtime private-channel RLS (issue #198)
    op.execute(sa.text("""
            DO $$
            BEGIN
              IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'realtime') THEN
                RAISE NOTICE 'realtime schema not present — skipping live-table RLS';
                RETURN;
              END IF;

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
                IF thread_type = 'campaign' THEN
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

              PERFORM 1 FROM pg_tables WHERE schemaname='realtime' AND tablename='messages';
              IF FOUND THEN
                DROP POLICY IF EXISTS "live_table_private_select" ON realtime.messages;
                DROP POLICY IF EXISTS "live_table_private_insert" ON realtime.messages;
                CREATE POLICY "live_table_private_select"
                ON realtime.messages FOR SELECT TO authenticated
                USING (
                  (
                    (select realtime.topic()) LIKE 'live-table:campaign:%:thread:%'
                    OR (select realtime.topic()) LIKE 'realtime:live-table:campaign:%:thread:%'
                  )
                  AND public.can_subscribe_live_table((select realtime.topic()))
                  AND realtime.messages.extension in ('broadcast')
                );
              END IF;
            END
            $$;
    """))

    # Optional full-text index for rules corpus (postgres only)
    try:
        conn.execute(sa.text(
            "CREATE INDEX IF NOT EXISTS ix_rules_sections_fts "
            "ON rules_sections USING gin (to_tsvector('english', title || ' ' || body))"
        ))
    except Exception:
        pass

    # Optional vector index if pgvector is active and rules_embeddings used TEXT fallback,
    # no vector column to index — skip. If extension active and we want hnsw, it would
    # require vector column type; current ORM uses TEXT so we degrade gracefully.


def downgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name if conn is not None else "postgresql"

    if dialect == "postgresql":
        # realtime RLS
        op.execute(sa.text("""
            DO $$
            BEGIN
              IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'realtime') THEN
                DROP POLICY IF EXISTS "live_table_private_select" ON realtime.messages;
                DROP POLICY IF EXISTS "live_table_private_insert" ON realtime.messages;
              END IF;
              DROP FUNCTION IF EXISTS public.can_subscribe_live_table(TEXT);
            END
            $$;
        """))
        op.execute(sa.text("DROP TRIGGER IF EXISTS campaign_domain_events_immutable ON campaign_domain_events"))
        op.execute(sa.text("DROP FUNCTION IF EXISTS reject_campaign_domain_event_mutation()"))
        op.execute(sa.text("DROP TRIGGER IF EXISTS profiles_updated_at ON public.profiles"))
        op.execute(sa.text("DROP FUNCTION IF EXISTS public.handle_updated_at()"))

    from database import Base
    import models  # noqa: F401
    # drop in reverse sorted order
    meta = Base.metadata
    meta.drop_all(bind=conn)
    if dialect == "postgresql":
        try:
            op.execute(sa.text("DROP TABLE IF EXISTS auth.users CASCADE"))
        except Exception:
            pass


def _ensure_vector_extension(conn) -> bool:
    """Best-effort pgvector provisioning without aborting transaction."""
    try:
        avail = conn.execute(sa.text("SELECT 1 FROM pg_available_extensions WHERE name='vector'")).fetchone()
        if not avail:
            return False
        has = conn.execute(sa.text("SELECT 1 FROM pg_extension WHERE extname='vector'")).fetchone()
        if has:
            return True
        savepoint = conn.begin_nested()
        try:
            conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
            savepoint.commit()
        except Exception as exc:
            try:
                savepoint.rollback()
            except Exception:
                pass
            print(f"WARNING: pgvector extension not available: {exc}")
            return False
        has_after = conn.execute(sa.text("SELECT 1 FROM pg_extension WHERE extname='vector'")).fetchone()
        return bool(has_after)
    except Exception as exc:
        print(f"WARNING: pgvector check failed: {exc}")
        return False

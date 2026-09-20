"""world pgvector semantic index — issue #213 (additive).

Revision ID: e2130a17c213
Revises: f231turnprog01

Additive only: creates world_embeddings (rebuildable derived index over
authoritative source records). Does not touch existing tables.

pgvector branch (same pattern as rules_embeddings in the baseline): when the
``vector`` extension is available on Postgres the ``embedding`` column is
``vector(1536)`` with a mandatory HNSW index; otherwise (or on SQLite) the
column is TEXT holding the portable JSON snapshot. ``embedding_text`` is
TEXT on both branches so the graceful-degradation search path always has a
readable copy.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "e2130a17c213"
down_revision: Union[str, Sequence[str], None] = "f231turnprog01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


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


def upgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name if conn is not None else "postgresql"
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
        if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
            SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
            SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

    has_vector = False
    if dialect == "postgresql":
        has_vector = _ensure_vector_extension(conn)

    if dialect == "postgresql" and has_vector:
        conn.execute(sa.text("""
            CREATE TABLE IF NOT EXISTS world_embeddings (
                id UUID NOT NULL PRIMARY KEY,
                campaign_id UUID NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
                source_type VARCHAR(32) NOT NULL,
                source_id UUID NOT NULL,
                source_version VARCHAR(64) NOT NULL,
                embedding_model VARCHAR(64) NOT NULL,
                embedding_version VARCHAR(32) NOT NULL DEFAULT '1',
                embedding vector(1536),
                embedding_text TEXT,
                status VARCHAR(16) NOT NULL DEFAULT 'active',
                error VARCHAR(512),
                created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
                updated_at TIMESTAMPTZ DEFAULT now() NOT NULL,
                UNIQUE (campaign_id, source_type, source_id, embedding_model, embedding_version)
            )
        """))
        # Mandatory, not optional (same rule as rules_embeddings): if HNSW
        # cannot be created on the pgvector branch the migration must fail
        # loudly instead of warning-and-skipping into an unindexed deployment.
        conn.execute(sa.text(
            "CREATE INDEX IF NOT EXISTS ix_world_embeddings_vector "
            "ON world_embeddings USING hnsw (embedding vector_cosine_ops)"
        ))
        conn.execute(sa.text(
            "CREATE INDEX IF NOT EXISTS ix_world_embeddings_campaign_status "
            "ON world_embeddings (campaign_id, status)"
        ))
        conn.execute(sa.text(
            "CREATE INDEX IF NOT EXISTS ix_world_embeddings_campaign_source "
            "ON world_embeddings (campaign_id, source_type)"
        ))
    else:
        op.create_table(
            "world_embeddings",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("source_type", sa.String(length=32), nullable=False),
            sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("source_version", sa.String(length=64), nullable=False),
            sa.Column("embedding_model", sa.String(length=64), nullable=False),
            sa.Column("embedding_version", sa.String(length=32), nullable=False, server_default=sa.text("'1'")),
            sa.Column("embedding", sa.Text(), nullable=True),
            sa.Column("embedding_text", sa.Text(), nullable=True),
            sa.Column("status", sa.String(length=16), nullable=False, server_default=sa.text("'active'")),
            sa.Column("error", sa.String(length=512), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "campaign_id", "source_type", "source_id",
                "embedding_model", "embedding_version",
                name="uq_world_embeddings_source_model",
            ),
        )
        op.create_index("ix_world_embeddings_campaign_status", "world_embeddings", ["campaign_id", "status"])
        op.create_index("ix_world_embeddings_campaign_source", "world_embeddings", ["campaign_id", "source_type"])


def downgrade() -> None:
    op.drop_index("ix_world_embeddings_campaign_source", table_name="world_embeddings")
    op.drop_index("ix_world_embeddings_campaign_status", table_name="world_embeddings")
    # HNSW index on the pgvector branch (IF EXISTS so downgrade works on both).
    try:
        op.execute(sa.text("DROP INDEX IF EXISTS ix_world_embeddings_vector"))
    except Exception:
        pass
    op.drop_table("world_embeddings")

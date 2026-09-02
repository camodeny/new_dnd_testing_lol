"""add rules corpus with stable citations — issue #223

Revision ID: f3a1c9d8e2b4
Revises: a9b8c7d6e5f4
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "f3a1c9d8e2b4"
down_revision: Union[str, Sequence[str], None] = "a9b8c7d6e5f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name

    # pgvector extension — transaction-safe provisioning.
    # The CI Postgres image may lack the extension; a failed CREATE EXTENSION
    # aborts the entire migration transaction (InFailedSqlTransaction). Use a
    # savepoint and an availability check so the migration can degrade to TEXT
    # fallback visibly without aborting. Required canonical schema still fails
    # visibly; only the optional vector index degrades.
    has_vector_extension = False
    if dialect == "postgresql":
        has_vector_extension = _ensure_vector_extension(conn)

    # ── rules_corpora ──────────────────────────────────────────────────
    if not _table_exists(conn, "rules_corpora"):
        op.create_table(
            "rules_corpora",
            sa.Column("corpus_id", sa.String(64), nullable=False),
            sa.Column("corpus_version", sa.String(32), nullable=False),
            sa.Column("source_url", sa.String(512), nullable=False),
            sa.Column("source_checksum", sa.String(128), nullable=True),
            sa.Column("source_artifact_hash", sa.String(128), nullable=True),
            sa.Column("license", sa.String(64), nullable=False, server_default="CC BY 4.0"),
            sa.Column("attribution", sa.Text, nullable=True),
            sa.Column("import_build_id", sa.String(64), nullable=True),
            sa.Column("imported_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("pinned_inputs", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.PrimaryKeyConstraint("corpus_id", "corpus_version"),
        )

    # ── rules_sections (canonical) ────────────────────────────────────
    if not _table_exists(conn, "rules_sections"):
        op.create_table(
            "rules_sections",
            sa.Column("rule_id", sa.String(256), nullable=False),
            sa.Column("corpus_id", sa.String(64), nullable=False),
            sa.Column("corpus_version", sa.String(32), nullable=False),
            sa.Column("source_section_id", sa.String(256), nullable=False),
            sa.Column("source_locator", sa.String(256), nullable=False),
            sa.Column("document", sa.String(256), nullable=False),
            sa.Column("heading_path", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("title", sa.String(512), nullable=False),
            sa.Column("body", sa.Text, nullable=False),
            sa.Column("structured_tables", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("source_hash", sa.String(128), nullable=True),
            sa.Column("content_hash", sa.String(128), nullable=True),
            sa.Column("citation_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("import_build_id", sa.String(64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.PrimaryKeyConstraint("rule_id"),
            sa.ForeignKeyConstraint(["corpus_id", "corpus_version"], ["rules_corpora.corpus_id", "rules_corpora.corpus_version"], ondelete="CASCADE"),
            sa.UniqueConstraint("corpus_id", "corpus_version", "source_section_id", name="uq_rules_sections_corpus_source"),
        )
        op.create_index("ix_rules_sections_corpus", "rules_sections", ["corpus_id", "corpus_version"])
        op.create_index("ix_rules_sections_source_section_id", "rules_sections", ["source_section_id"])
        op.create_index("ix_rules_sections_document", "rules_sections", ["document"])
        # Full-text index — postgres only; sqlite fallback uses LIKE
        if dialect == "postgresql":
            try:
                conn.execute(sa.text(
                    "CREATE INDEX IF NOT EXISTS ix_rules_sections_fts "
                    "ON rules_sections USING gin (to_tsvector('english', title || ' ' || body))"
                ))
            except Exception:
                pass

    # ── rules_section_aliases (override manifest persistence) ─────────
    if not _table_exists(conn, "rules_section_aliases"):
        op.create_table(
            "rules_section_aliases",
            sa.Column("alias", sa.String(256), nullable=False),
            sa.Column("rule_id", sa.String(256), nullable=False),
            sa.Column("corpus_id", sa.String(64), nullable=False),
            sa.Column("corpus_version", sa.String(32), nullable=False),
            sa.Column("reason", sa.String(512), nullable=True),
            sa.PrimaryKeyConstraint("alias", "corpus_id", "corpus_version"),
            sa.ForeignKeyConstraint(["rule_id"], ["rules_sections.rule_id"], ondelete="CASCADE"),
        )

    # ── rules_embeddings (derived, rebuildable) ───────────────────────
    if not _table_exists(conn, "rules_embeddings"):
        # Use TEXT for embedding in sqlite; vector in postgres if available
        if dialect == "postgresql":
            has_vector = has_vector_extension
            if has_vector:
                # Use generic vector (no fixed dim) so Gemini 768/3072 and stub 1536 all work
                conn.execute(sa.text("""
                    CREATE TABLE IF NOT EXISTS rules_embeddings (
                        rule_id VARCHAR(256) NOT NULL REFERENCES rules_sections(rule_id) ON DELETE CASCADE,
                        corpus_id VARCHAR(64) NOT NULL,
                        corpus_version VARCHAR(32) NOT NULL,
                        embedding_model VARCHAR(64) NOT NULL,
                        embedding_version VARCHAR(32) NOT NULL,
                        build_id VARCHAR(64) NOT NULL,
                        embedding vector,
                        embedding_text TEXT,
                        chunk_strategy VARCHAR(64),
                        created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
                        PRIMARY KEY (rule_id, embedding_model, build_id)
                    )
                """))
                try:
                    conn.execute(sa.text(
                        "CREATE INDEX IF NOT EXISTS ix_rules_embeddings_vector "
                        "ON rules_embeddings USING hnsw (embedding vector_cosine_ops)"
                    ))
                except Exception:
                    pass
            else:
                op.create_table(
                    "rules_embeddings",
                    sa.Column("rule_id", sa.String(256), sa.ForeignKey("rules_sections.rule_id", ondelete="CASCADE"), nullable=False),
                    sa.Column("corpus_id", sa.String(64), nullable=False),
                    sa.Column("corpus_version", sa.String(32), nullable=False),
                    sa.Column("embedding_model", sa.String(64), nullable=False),
                    sa.Column("embedding_version", sa.String(32), nullable=False),
                    sa.Column("build_id", sa.String(64), nullable=False),
                    sa.Column("embedding", sa.Text, nullable=True),
                    sa.Column("embedding_text", sa.Text, nullable=True),
                    sa.Column("chunk_strategy", sa.String(64), nullable=True),
                    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
                    sa.PrimaryKeyConstraint("rule_id", "embedding_model", "build_id"),
                )
        else:
            # sqlite
            op.create_table(
                "rules_embeddings",
                sa.Column("rule_id", sa.String(256), sa.ForeignKey("rules_sections.rule_id", ondelete="CASCADE"), nullable=False),
                sa.Column("corpus_id", sa.String(64), nullable=False),
                sa.Column("corpus_version", sa.String(32), nullable=False),
                sa.Column("embedding_model", sa.String(64), nullable=False),
                sa.Column("embedding_version", sa.String(32), nullable=False),
                sa.Column("build_id", sa.String(64), nullable=False),
                sa.Column("embedding", sa.Text, nullable=True),
                sa.Column("embedding_text", sa.Text, nullable=True),
                sa.Column("chunk_strategy", sa.String(64), nullable=True),
                sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
                sa.PrimaryKeyConstraint("rule_id", "embedding_model", "build_id"),
            )
        try:
            op.create_index("ix_rules_embeddings_corpus", "rules_embeddings", ["corpus_id", "corpus_version"])
        except Exception:
            pass
        try:
            op.create_index("ix_rules_embeddings_model_build", "rules_embeddings", ["embedding_model", "build_id"])
        except Exception:
            pass

    # ── rules_corpus_imports (provenance log) ─────────────────────────
    if not _table_exists(conn, "rules_corpus_imports"):
        op.create_table(
            "rules_corpus_imports",
            sa.Column("build_id", sa.String(64), nullable=False),
            sa.Column("corpus_id", sa.String(64), nullable=False),
            sa.Column("corpus_version", sa.String(32), nullable=False),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("source_checksum", sa.String(128), nullable=True),
            sa.Column("pinned_inputs", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("validation_errors", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("canary_results", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.PrimaryKeyConstraint("build_id"),
        )
        op.create_index("ix_rules_corpus_imports_corpus", "rules_corpus_imports", ["corpus_id", "corpus_version"])


def downgrade() -> None:
    conn = op.get_bind()
    for tbl in ["rules_corpus_imports", "rules_embeddings", "rules_section_aliases", "rules_sections", "rules_corpora"]:
        try:
            op.drop_table(tbl)
        except Exception:
            try:
                conn.execute(sa.text(f"DROP TABLE IF EXISTS {tbl} CASCADE"))
            except Exception:
                pass


def _ensure_vector_extension(conn) -> bool:
    """Try to ensure pgvector is available without aborting outer transaction.

    Returns True if vector extension is available/active, False if we must
    degrade to TEXT fallback. Uses a savepoint so a failed CREATE EXTENSION
    does not leave the migration transaction in InFailedSqlTransaction.
    """
    try:
        with conn.begin_nested():
            avail = conn.execute(sa.text("SELECT 1 FROM pg_available_extensions WHERE name='vector'")).fetchone()
            if not avail:
                return False
            has = conn.execute(sa.text("SELECT 1 FROM pg_extension WHERE extname='vector'")).fetchone()
            if has:
                return True
            conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
            has_after = conn.execute(sa.text("SELECT 1 FROM pg_extension WHERE extname='vector'")).fetchone()
            return has_after is not None
    except Exception as exc:
        # Savepoint rolled back — log visibly but don't abort outer migration
        # Required canonical tables will still be created; only vector index degrades
        print(f"WARNING: pgvector extension not available, degrading to TEXT fallback: {exc}")
        return False
    return False


def _table_exists(conn, name: str) -> bool:
    try:
        insp = sa.inspect(conn)
        return name in set(insp.get_table_names())
    except Exception:
        return False

"""Rename rules_corpus_imports.source_checksum to source_artifact_hash.

Revision ID: rename_source_checksum
Revises: a262epil01
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "rename_source_checksum"
down_revision: Union[str, Sequence[str], None] = "a262epil01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Live DBs were built from an older revision of the initial schema that
    # named this column source_checksum; the canonical name is
    # source_artifact_hash (models/rules.py RulesCorpusImport). The table holds
    # only provenance rows (empty until first corpus promotion), so a rename is
    # lossless. Guarded for DBs already on the canonical name.
    conn = op.get_bind()
    cols = {r[0] for r in conn.exec_driver_sql(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'rules_corpus_imports'"
    ).fetchall()}
    if "source_checksum" in cols and "source_artifact_hash" not in cols:
        op.alter_column("rules_corpus_imports", "source_checksum", new_column_name="source_artifact_hash")
    elif "source_artifact_hash" not in cols:
        op.add_column(
            "rules_corpus_imports",
            sa.Column("source_artifact_hash", sa.String(length=128), nullable=True),
        )


def downgrade() -> None:
    conn = op.get_bind()
    cols = {r[0] for r in conn.exec_driver_sql(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'rules_corpus_imports'"
    ).fetchall()}
    if "source_artifact_hash" in cols and "source_checksum" not in cols:
        op.alter_column("rules_corpus_imports", "source_artifact_hash", new_column_name="source_checksum")

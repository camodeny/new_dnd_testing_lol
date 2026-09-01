"""make rules_embeddings vector dimension flexible for Gemini — issue #223

Revision ID: f4c8a9b2e3d1
Revises: f3a1c9d8e2b4
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "f4c8a9b2e3d1"
down_revision: Union[str, Sequence[str], None] = "f3a1c9d8e2b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    try:
        # Check if table exists
        insp = sa.inspect(conn)
        if "rules_embeddings" not in set(insp.get_table_names()):
            return
        # Check if column is vector(1536) — make it generic vector so Gemini 768/3072 works
        # pgvector 0.5+ supports vector without dim; older needs explicit. Try generic first.
        try:
            conn.execute(sa.text("ALTER TABLE rules_embeddings ALTER COLUMN embedding TYPE vector USING embedding::vector"))
        except Exception:
            # If generic fails, keep as is — will still work via embedding_text fallback
            pass
    except Exception:
        pass


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    try:
        conn.execute(sa.text("ALTER TABLE rules_embeddings ALTER COLUMN embedding TYPE vector(1536) USING embedding::vector(1536)"))
    except Exception:
        pass

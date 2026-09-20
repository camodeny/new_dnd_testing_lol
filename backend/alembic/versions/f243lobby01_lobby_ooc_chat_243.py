"""Shared OOC lobby chat thread — issue #243 (additive).

Revision ID: f243lobby01
Revises: a215npcstate01

Additive only:
- widens ``ck_campaign_threads_type`` to admit ``'lobby'`` (the shared
  pre-start out-of-character coordination thread);
- adds a partial unique index so each campaign has at most one lobby
  thread (mirrors the existing one-shared-thread index).

Existing ``campaign``/``private`` rows are untouched. Batch mode is used
for the constraint swap so the migration runs on SQLite as well as
Postgres.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "f243lobby01"
down_revision: Union[str, Sequence[str], None] = "a215npcstate01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


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


def downgrade() -> None:
    op.drop_index("uq_campaign_threads_one_lobby_per_campaign", table_name="campaign_threads")
    with op.batch_alter_table("campaign_threads") as batch_op:
        batch_op.drop_constraint("ck_campaign_threads_type", type_="check")
        batch_op.create_check_constraint(
            "ck_campaign_threads_type",
            "thread_type IN ('campaign', 'private')",
        )

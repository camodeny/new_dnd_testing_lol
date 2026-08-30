"""enforce domain event immutability and remove duplicate index

Revision ID: d3a7c1e9f2b6
Revises: c9f5a2e6b1d4
Create Date: 2026-08-29
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d3a7c1e9f2b6"
down_revision: Union[str, Sequence[str], None] = "c9f5a2e6b1d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS ix_campaign_domain_events_campaign_sequence"))
    op.drop_constraint(
        "campaign_domain_events_campaign_id_fkey",
        "campaign_domain_events",
        type_="foreignkey",
    )
    op.drop_constraint(
        "campaign_domain_events_actor_id_fkey",
        "campaign_domain_events",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "campaign_domain_events_campaign_id_fkey",
        "campaign_domain_events",
        "campaigns",
        ["campaign_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "campaign_domain_events_actor_id_fkey",
        "campaign_domain_events",
        "profiles",
        ["actor_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION reject_campaign_domain_event_mutation()
            RETURNS trigger AS $$
            BEGIN
              RAISE EXCEPTION 'campaign domain events are immutable';
            END;
            $$ LANGUAGE plpgsql;
            """
        )
    )
    op.execute(sa.text("DROP TRIGGER IF EXISTS campaign_domain_events_immutable ON campaign_domain_events"))
    op.execute(
        sa.text(
            """
            CREATE TRIGGER campaign_domain_events_immutable
              BEFORE UPDATE OR DELETE ON campaign_domain_events
              FOR EACH ROW EXECUTE FUNCTION reject_campaign_domain_event_mutation()
            """
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DROP TRIGGER IF EXISTS campaign_domain_events_immutable ON campaign_domain_events"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS reject_campaign_domain_event_mutation()"))
    op.drop_constraint(
        "campaign_domain_events_campaign_id_fkey",
        "campaign_domain_events",
        type_="foreignkey",
    )
    op.drop_constraint(
        "campaign_domain_events_actor_id_fkey",
        "campaign_domain_events",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "campaign_domain_events_campaign_id_fkey",
        "campaign_domain_events",
        "campaigns",
        ["campaign_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "campaign_domain_events_actor_id_fkey",
        "campaign_domain_events",
        "profiles",
        ["actor_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_campaign_domain_events_campaign_sequence",
        "campaign_domain_events",
        ["campaign_id", "sequence"],
        unique=True,
    )

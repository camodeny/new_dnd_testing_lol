"""create profiles mirror for supabase auth

Revision ID: 001
Revises: 
Create Date: 2026-08-20

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
    """Upgrade schema."""
    # Supabase provides auth.users; vanilla Postgres (CI disposable DB) does not.
    # Create the schema and a minimal stub so the FK below can be created on a
    # clean database and the same Alembic path works in CI (#286).
    op.execute(sa.text("CREATE SCHEMA IF NOT EXISTS auth"))
    op.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS auth.users (
                id UUID PRIMARY KEY
            )
            """
        )
    )
    # profiles mirrors auth.users.id — Supabase recommends a public table for app data
    op.create_table(
        "profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        # FK to auth.users — deferrable in case auth insert hasn't committed yet
        sa.ForeignKeyConstraint(["id"], ["auth.users.id"], ondelete="CASCADE"),
    )
    op.create_index("profiles_email_idx", "profiles", ["email"])
    op.create_index("profiles_username_idx", "profiles", ["username"])

    # keep updated_at fresh — matches schema.sql
    op.execute(
        sa.text(
            """
            create or replace function public.handle_updated_at()
            returns trigger language plpgsql as $$
            begin
              new.updated_at = now();
              return new;
            end;
            $$;
            """
        )
    )
    op.execute(
        sa.text(
            """
            create trigger profiles_updated_at
              before update on public.profiles
              for each row execute function public.handle_updated_at();
            """
        )
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(sa.text("drop trigger if exists profiles_updated_at on public.profiles"))
    op.execute(sa.text("drop function if exists public.handle_updated_at()"))
    op.drop_index("profiles_username_idx", table_name="profiles")
    op.drop_index("profiles_email_idx", table_name="profiles")
    op.drop_table("profiles")

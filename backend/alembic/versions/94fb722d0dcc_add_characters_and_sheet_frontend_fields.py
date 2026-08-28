"""add generic characters table and frontend-aligned sheet fields

Revision ID: 94fb722d0dcc
Revises: e3942ea3c8e4
Create Date: 2026-08-28

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "94fb722d0dcc"
down_revision: Union[str, Sequence[str], None] = "e3942ea3c8e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # generic characters identity table
    op.create_table(
        "characters",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("system", sa.String(length=32), nullable=False, server_default="dnd5e"),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("characters_owner_idx", "characters", ["owner_id"])
    op.create_index("characters_system_idx", "characters", ["system"])
    op.execute(sa.text(
        """
        create trigger characters_updated_at
          before update on public.characters
          for each row execute function public.handle_updated_at();
        """
    ))

    # add frontend-aligned fields to dnd5e sheet
    op.add_column("dnd5e_character_sheets", sa.Column("character_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_index("dnd5e_sheets_character_idx", "dnd5e_character_sheets", ["character_id"])
    op.create_foreign_key("fk_dnd5e_sheets_character_id", "dnd5e_character_sheets", "characters", ["character_id"], ["id"], ondelete="CASCADE")

    op.add_column("dnd5e_character_sheets", sa.Column("exhaustion_level", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("dnd5e_character_sheets", sa.Column("encumbrance_status", sa.String(length=32), nullable=True))
    op.add_column("dnd5e_character_sheets", sa.Column("skills", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("dnd5e_character_sheets", sa.Column("saving_throws", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("dnd5e_character_sheets", sa.Column("weapons", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("dnd5e_character_sheets", sa.Column("features", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("dnd5e_character_sheets", sa.Column("proficiencies", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("dnd5e_character_sheets", sa.Column("resources", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("dnd5e_character_sheets", sa.Column("companions", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("dnd5e_character_sheets", sa.Column("conditions", postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column("dnd5e_character_sheets", "conditions")
    op.drop_column("dnd5e_character_sheets", "companions")
    op.drop_column("dnd5e_character_sheets", "resources")
    op.drop_column("dnd5e_character_sheets", "proficiencies")
    op.drop_column("dnd5e_character_sheets", "features")
    op.drop_column("dnd5e_character_sheets", "weapons")
    op.drop_column("dnd5e_character_sheets", "saving_throws")
    op.drop_column("dnd5e_character_sheets", "skills")
    op.drop_column("dnd5e_character_sheets", "encumbrance_status")
    op.drop_column("dnd5e_character_sheets", "exhaustion_level")
    op.drop_constraint("fk_dnd5e_sheets_character_id", "dnd5e_character_sheets", type_="foreignkey")
    op.drop_index("dnd5e_sheets_character_idx", table_name="dnd5e_character_sheets")
    op.drop_column("dnd5e_character_sheets", "character_id")
    op.execute(sa.text("drop trigger if exists characters_updated_at on public.characters"))
    op.drop_index("characters_system_idx", table_name="characters")
    op.drop_index("characters_owner_idx", table_name="characters")
    op.drop_table("characters")

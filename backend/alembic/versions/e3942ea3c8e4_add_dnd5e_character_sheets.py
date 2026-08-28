"""add dnd5e character sheets

Revision ID: e3942ea3c8e4
Revises: 001
Create Date: 2026-08-28

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "e3942ea3c8e4"
down_revision: Union[str, Sequence[str], None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dnd5e_character_sheets",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("character_name", sa.String(length=128), nullable=False),
        sa.Column("player_name", sa.String(length=128), nullable=True),
        sa.Column("race", sa.String(length=64), nullable=True),
        sa.Column("subrace", sa.String(length=64), nullable=True),
        sa.Column("background", sa.String(length=64), nullable=True),
        sa.Column("alignment", sa.String(length=32), nullable=True),
        sa.Column("char_class", sa.String(length=64), nullable=True),
        sa.Column("classes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("level", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("experience_points", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("age", sa.String(length=32), nullable=True),
        sa.Column("height", sa.String(length=32), nullable=True),
        sa.Column("weight", sa.String(length=32), nullable=True),
        sa.Column("eyes", sa.String(length=32), nullable=True),
        sa.Column("skin", sa.String(length=32), nullable=True),
        sa.Column("hair", sa.String(length=32), nullable=True),
        sa.Column("appearance", sa.Text(), nullable=True),
        sa.Column("backstory", sa.Text(), nullable=True),
        sa.Column("allies_and_organizations", sa.Text(), nullable=True),
        sa.Column("personality_traits", sa.Text(), nullable=True),
        sa.Column("ideals", sa.Text(), nullable=True),
        sa.Column("bonds", sa.Text(), nullable=True),
        sa.Column("flaws", sa.Text(), nullable=True),
        sa.Column("strength", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("dexterity", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("constitution", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("intelligence", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("wisdom", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("charisma", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("inspiration", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("proficiency_bonus", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("str_save_prof", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("dex_save_prof", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("con_save_prof", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("int_save_prof", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("wis_save_prof", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("cha_save_prof", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("acrobatics_prof", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("animal_handling_prof", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("arcana_prof", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("athletics_prof", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("deception_prof", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("history_prof", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("insight_prof", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("intimidation_prof", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("investigation_prof", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("medicine_prof", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("nature_prof", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("perception_prof", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("performance_prof", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("persuasion_prof", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("religion_prof", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("sleight_of_hand_prof", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("stealth_prof", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("survival_prof", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("skill_expertise", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("passive_perception", sa.Integer(), nullable=True),
        sa.Column("armor_class", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("initiative_bonus", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("speed", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("speed_details", sa.String(length=128), nullable=True),
        sa.Column("hit_points_max", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("hit_points_current", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("hit_points_temp", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("hit_dice", sa.String(length=32), nullable=True),
        sa.Column("hit_dice_total", sa.String(length=32), nullable=True),
        sa.Column("hit_dice_remaining", sa.String(length=32), nullable=True),
        sa.Column("death_save_successes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("death_save_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attacks", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("spellcasting_ability", sa.String(length=16), nullable=True),
        sa.Column("spell_save_dc", sa.Integer(), nullable=True),
        sa.Column("spell_attack_bonus", sa.Integer(), nullable=True),
        sa.Column("spell_slots", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("spells", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("cantrips", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("equipment", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("equipment_text", sa.Text(), nullable=True),
        sa.Column("treasure", sa.Text(), nullable=True),
        sa.Column("cp", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sp", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ep", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("gp", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pp", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("encumbrance", sa.String(length=32), nullable=True),
        sa.Column("features_and_traits", sa.Text(), nullable=True),
        sa.Column("other_proficiencies_languages", sa.Text(), nullable=True),
        sa.Column("proficiencies_languages_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("extras", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("portrait_url", sa.String(length=512), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("dnd5e_sheets_owner_idx", "dnd5e_character_sheets", ["owner_id"])
    op.create_index("dnd5e_sheets_name_idx", "dnd5e_character_sheets", ["character_name"])
    op.execute(
        sa.text(
            """
            create trigger dnd5e_sheets_updated_at
              before update on public.dnd5e_character_sheets
              for each row execute function public.handle_updated_at();
            """
        )
    )


def downgrade() -> None:
    op.execute(sa.text("drop trigger if exists dnd5e_sheets_updated_at on public.dnd5e_character_sheets"))
    op.drop_index("dnd5e_sheets_name_idx", table_name="dnd5e_character_sheets")
    op.drop_index("dnd5e_sheets_owner_idx", table_name="dnd5e_character_sheets")
    op.drop_table("dnd5e_character_sheets")

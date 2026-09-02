"""initial baseline — squashed pre-alpha history

Revision ID: 2a04bc8c83ba

Revises: 

Create Date: 2026-09-02

Single baseline replacing the pre-alpha chain per issue #313.

Frozen explicit schema — do not import live ORM.

"""

from typing import Sequence, Union

from alembic import op

import sqlalchemy as sa

from sqlalchemy.dialects import postgresql

revision: str = "2a04bc8c83ba"

down_revision: Union[str, Sequence[str], None] = None

branch_labels: Union[str, Sequence[str], None] = None

depends_on: Union[str, Sequence[str], None] = None



def upgrade() -> None:

    conn = op.get_bind()

    dialect = conn.dialect.name if conn is not None else "postgresql"

    # SQLite JSONB patch for frozen explicit tables

    if dialect == "sqlite":

        from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

        if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):

            SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore

            SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

    # ── Supabase auth stub for disposable Postgres / CI ──

    if dialect == "postgresql":

        op.execute(sa.text("CREATE SCHEMA IF NOT EXISTS auth"))

        op.execute(sa.text("CREATE TABLE IF NOT EXISTS auth.users (id UUID PRIMARY KEY)"))

        has_vector = _ensure_vector_extension(conn)

    else:

        has_vector = False


    op.create_table(
        "ai_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("operation_id", sa.String(length=128), nullable=False),
        sa.Column("parent_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("logical_operation", sa.String(length=128), nullable=False),
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("classification", sa.String(length=16), nullable=False),
        sa.Column("billable", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_token_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("result_code", sa.String(length=64), nullable=True),
        sa.Column("error_type", sa.String(length=128), nullable=True),
        sa.Column("content_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ai_runs_trace_id", "ai_runs", ["trace_id"])
    op.create_index("ix_ai_runs_operation_id", "ai_runs", ["operation_id"])

    op.create_table(
        "profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["id"], ["auth.users.id"], ondelete="CASCADE"),
    )
    op.create_index("profiles_email_idx", "profiles", ["email"])
    op.create_index("profiles_username_idx", "profiles", ["username"])

    op.create_table(
        "rules_corpora",
        sa.Column("corpus_id", sa.String(length=64), nullable=False),
        sa.Column("corpus_version", sa.String(length=32), nullable=False),
        sa.Column("source_url", sa.String(length=512), nullable=False),
        sa.Column("source_checksum", sa.String(length=128), nullable=True),
        sa.Column("source_artifact_hash", sa.String(length=128), nullable=True),
        sa.Column("license", sa.String(length=64), nullable=False, server_default=sa.text("'CC BY 4.0'")),
        sa.Column("attribution", sa.String(), nullable=True),
        sa.Column("import_build_id", sa.String(length=64), nullable=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column("pinned_inputs", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint("corpus_id", "corpus_version"),
    )

    op.create_table(
        "rules_corpus_imports",
        sa.Column("build_id", sa.String(length=64), nullable=False),
        sa.Column("corpus_id", sa.String(length=64), nullable=False),
        sa.Column("corpus_version", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("source_checksum", sa.String(length=128), nullable=True),
        sa.Column("pinned_inputs", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("validation_errors", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("canary_results", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint("build_id"),
    )

    op.create_table(
        "rules_sections",
        sa.Column("rule_id", sa.String(length=256), nullable=False),
        sa.Column("corpus_id", sa.String(length=64), nullable=False),
        sa.Column("corpus_version", sa.String(length=32), nullable=False),
        sa.Column("source_section_id", sa.String(length=256), nullable=False),
        sa.Column("source_locator", sa.String(length=256), nullable=False),
        sa.Column("document", sa.String(length=256), nullable=False),
        sa.Column("heading_path", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("body", sa.String(), nullable=False),
        sa.Column("structured_tables", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("source_hash", sa.String(length=128), nullable=True),
        sa.Column("content_hash", sa.String(length=128), nullable=True),
        sa.Column("citation_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("import_build_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint("rule_id"),
        sa.UniqueConstraint("corpus_id", "corpus_version", "source_section_id", name="uq_rules_sections_corpus_source"),
    )
    op.create_index("ix_rules_sections_source_section_id", "rules_sections", ["source_section_id"])
    op.create_index("ix_rules_sections_corpus", "rules_sections", ["corpus_id", "corpus_version"])

    op.create_table(
        "campaigns",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("random_seed", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default=sa.text("'lobby'")),
        sa.Column("theme", sa.String(length=128), nullable=True),
        sa.Column("brief", sa.String(), nullable=True),
        sa.Column("difficulty", sa.String(length=16), nullable=False, server_default=sa.text("'medium'")),
        sa.Column("required_players", sa.Integer(), nullable=False, server_default=sa.text('1')),
        sa.Column("content_boundaries", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("loot_mode", sa.String(length=32), nullable=False, server_default=sa.text("'frequent_gamble'")),
        sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.CheckConstraint("status IN ('lobby', 'starting', 'active', 'archived')", name="ck_campaigns_lifecycle_status"),
        sa.CheckConstraint('required_players BETWEEN 1 AND 6', name="ck_campaigns_required_players_launch_range"),
        sa.ForeignKeyConstraint(["owner_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_campaigns_owner_id", "campaigns", ["owner_id"])

    op.create_table(
        "characters",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("system", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["owner_id"], ["profiles.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_characters_owner_id", "characters", ["owner_id"])

    op.create_table(
        "idempotent_commands",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("command_type", sa.String(length=128), nullable=False),
        sa.Column("scope_type", sa.String(length=32), nullable=False),
        sa.Column("scope_id", sa.String(length=128), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("actor_id", "idempotency_key", "command_type", "scope_type", "scope_id", name="uq_idempotent_commands_identity"),
        sa.ForeignKeyConstraint(["actor_id"], ["profiles.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_idempotent_commands_actor_id", "idempotent_commands", ["actor_id"])

    op.create_table(
        "rules_section_aliases",
        sa.Column("alias", sa.String(length=256), nullable=False),
        sa.Column("corpus_id", sa.String(length=64), nullable=False),
        sa.Column("corpus_version", sa.String(length=32), nullable=False),
        sa.Column("rule_id", sa.String(length=256), nullable=False),
        sa.Column("reason", sa.String(length=512), nullable=True),
        sa.PrimaryKeyConstraint("alias", "corpus_id", "corpus_version"),
        sa.ForeignKeyConstraint(["rule_id"], ["rules_sections.rule_id"], ondelete="CASCADE"),
    )

    op.create_table(
        "campaign_domain_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("operation_id", sa.String(length=128), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("targets", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("visibility", sa.String(length=32), nullable=False, server_default=sa.text("'public'")),
        sa.Column("provenance", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(["actor_id"], ["profiles.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("campaign_id", "sequence", name="uq_campaign_domain_events_campaign_sequence"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_campaign_domain_events_actor_id", "campaign_domain_events", ["actor_id"])
    op.create_index("ix_campaign_domain_events_trace_id", "campaign_domain_events", ["trace_id"])
    op.create_index("ix_campaign_domain_events_campaign_id", "campaign_domain_events", ["campaign_id"])

    op.create_table(
        "campaign_invites",
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint("campaign_id"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("code"),
    )

    op.create_table(
        "campaign_members",
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("campaign_id", "user_id"),
        sa.ForeignKeyConstraint(["user_id"], ["profiles.id"], ondelete="CASCADE"),
    )

    op.create_table(
        "campaign_threads",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("thread_type", sa.String(length=32), nullable=False, server_default=sa.text("'campaign'")),
        sa.Column("private_kind", sa.String(length=16), nullable=True),
        sa.Column("private_key", sa.String(length=160), nullable=True),
        sa.Column("title", sa.String(length=128), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(["created_by"], ["profiles.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.CheckConstraint("thread_type IN ('campaign', 'private')", name="ck_campaign_threads_type"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_campaign_threads_campaign_id", "campaign_threads", ["campaign_id"])
    op.create_index("uq_campaign_threads_one_campaign_per_campaign", "campaign_threads", ["campaign_id"], unique=True, postgresql_where=sa.text("thread_type = 'campaign'"), sqlite_where=sa.text("thread_type = 'campaign'"))
    op.create_index("ix_campaign_threads_created_by", "campaign_threads", ["created_by"])
    op.create_index("uq_campaign_threads_private_key", "campaign_threads", ["campaign_id", "private_key"], unique=True, postgresql_where=sa.text("private_key IS NOT NULL"), sqlite_where=sa.text("private_key IS NOT NULL"))

    op.create_table(
        "character_chat_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("character_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["owner_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["character_id"], ["characters.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_character_chat_messages_character_id", "character_chat_messages", ["character_id"])
    op.create_index("ix_character_chat_messages_owner_id", "character_chat_messages", ["owner_id"])

    op.create_table(
        "dm_turns",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("thread_id", sa.String(length=128), nullable=False),
        sa.Column("audience", sa.String(length=32), nullable=False, server_default=sa.text("'campaign'")),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("input_set_revision", sa.Integer(), nullable=False, server_default=sa.text('1')),
        sa.Column("submission_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("current_attempt_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("streaming_attempt_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("assembly_window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("assembly_window_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("streaming_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("abandoned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("abandonment_reason", sa.String(length=64), nullable=True),
        sa.Column("time_waiting_ms", sa.Integer(), nullable=True),
        sa.Column("time_executing_ms", sa.Integer(), nullable=True),
        sa.Column("commit_duration_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_dm_turns_status", "dm_turns", ["status"])
    op.create_index("ix_dm_turns_campaign_thread_status", "dm_turns", ["campaign_id", "thread_id", "status"])
    op.create_index("uq_dm_turns_active_per_thread", "dm_turns", ["campaign_id", "thread_id"], unique=True, postgresql_where=sa.text("status IN ('pending','awaiting_roll','streaming','failed_visible')"), sqlite_where=sa.text("status IN ('pending','awaiting_roll','streaming','failed_visible')"))
    op.create_index("ix_dm_turns_campaign_id", "dm_turns", ["campaign_id"])
    op.create_index("ix_dm_turns_current_attempt_id", "dm_turns", ["current_attempt_id"])
    op.create_index("ix_dm_turns_thread_id", "dm_turns", ["thread_id"])
    op.create_index("ix_dm_turns_campaign_status", "dm_turns", ["campaign_id", "status"])

    op.create_table(
        "dnd5e_character_sheets",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("character_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("character_name", sa.String(length=128), nullable=False),
        sa.Column("player_name", sa.String(length=128), nullable=True),
        sa.Column("race", sa.String(length=64), nullable=True),
        sa.Column("subrace", sa.String(length=64), nullable=True),
        sa.Column("background", sa.String(length=64), nullable=True),
        sa.Column("alignment", sa.String(length=32), nullable=True),
        sa.Column("char_class", sa.String(length=64), nullable=True),
        sa.Column("classes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("experience_points", sa.Integer(), nullable=False),
        sa.Column("age", sa.String(length=32), nullable=True),
        sa.Column("height", sa.String(length=32), nullable=True),
        sa.Column("weight", sa.String(length=32), nullable=True),
        sa.Column("eyes", sa.String(length=32), nullable=True),
        sa.Column("skin", sa.String(length=32), nullable=True),
        sa.Column("hair", sa.String(length=32), nullable=True),
        sa.Column("appearance", sa.String(), nullable=True),
        sa.Column("backstory", sa.String(), nullable=True),
        sa.Column("allies_and_organizations", sa.String(), nullable=True),
        sa.Column("personality_traits", sa.String(), nullable=True),
        sa.Column("ideals", sa.String(), nullable=True),
        sa.Column("bonds", sa.String(), nullable=True),
        sa.Column("flaws", sa.String(), nullable=True),
        sa.Column("strength", sa.Integer(), nullable=False),
        sa.Column("dexterity", sa.Integer(), nullable=False),
        sa.Column("constitution", sa.Integer(), nullable=False),
        sa.Column("intelligence", sa.Integer(), nullable=False),
        sa.Column("wisdom", sa.Integer(), nullable=False),
        sa.Column("charisma", sa.Integer(), nullable=False),
        sa.Column("inspiration", sa.Boolean(), nullable=False),
        sa.Column("proficiency_bonus", sa.Integer(), nullable=False),
        sa.Column("str_save_prof", sa.Boolean(), nullable=False),
        sa.Column("dex_save_prof", sa.Boolean(), nullable=False),
        sa.Column("con_save_prof", sa.Boolean(), nullable=False),
        sa.Column("int_save_prof", sa.Boolean(), nullable=False),
        sa.Column("wis_save_prof", sa.Boolean(), nullable=False),
        sa.Column("cha_save_prof", sa.Boolean(), nullable=False),
        sa.Column("acrobatics_prof", sa.Boolean(), nullable=False),
        sa.Column("animal_handling_prof", sa.Boolean(), nullable=False),
        sa.Column("arcana_prof", sa.Boolean(), nullable=False),
        sa.Column("athletics_prof", sa.Boolean(), nullable=False),
        sa.Column("deception_prof", sa.Boolean(), nullable=False),
        sa.Column("history_prof", sa.Boolean(), nullable=False),
        sa.Column("insight_prof", sa.Boolean(), nullable=False),
        sa.Column("intimidation_prof", sa.Boolean(), nullable=False),
        sa.Column("investigation_prof", sa.Boolean(), nullable=False),
        sa.Column("medicine_prof", sa.Boolean(), nullable=False),
        sa.Column("nature_prof", sa.Boolean(), nullable=False),
        sa.Column("perception_prof", sa.Boolean(), nullable=False),
        sa.Column("performance_prof", sa.Boolean(), nullable=False),
        sa.Column("persuasion_prof", sa.Boolean(), nullable=False),
        sa.Column("religion_prof", sa.Boolean(), nullable=False),
        sa.Column("sleight_of_hand_prof", sa.Boolean(), nullable=False),
        sa.Column("stealth_prof", sa.Boolean(), nullable=False),
        sa.Column("survival_prof", sa.Boolean(), nullable=False),
        sa.Column("skill_expertise", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("skills", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("saving_throws", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("passive_perception", sa.Integer(), nullable=True),
        sa.Column("armor_class", sa.Integer(), nullable=False),
        sa.Column("initiative_bonus", sa.Integer(), nullable=False),
        sa.Column("speed", sa.Integer(), nullable=False),
        sa.Column("speed_details", sa.String(length=128), nullable=True),
        sa.Column("hit_points_max", sa.Integer(), nullable=False),
        sa.Column("hit_points_current", sa.Integer(), nullable=False),
        sa.Column("hit_points_temp", sa.Integer(), nullable=False),
        sa.Column("hit_dice", sa.String(length=32), nullable=True),
        sa.Column("hit_dice_total", sa.String(length=32), nullable=True),
        sa.Column("hit_dice_remaining", sa.String(length=32), nullable=True),
        sa.Column("death_save_successes", sa.Integer(), nullable=False),
        sa.Column("death_save_failures", sa.Integer(), nullable=False),
        sa.Column("exhaustion_level", sa.Integer(), nullable=False),
        sa.Column("encumbrance_status", sa.String(length=32), nullable=True),
        sa.Column("attacks", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("weapons", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("spellcasting_ability", sa.String(length=16), nullable=True),
        sa.Column("spell_save_dc", sa.Integer(), nullable=True),
        sa.Column("spell_attack_bonus", sa.Integer(), nullable=True),
        sa.Column("spell_slots", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("spells", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("cantrips", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("equipment", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("equipment_text", sa.String(), nullable=True),
        sa.Column("treasure", sa.String(), nullable=True),
        sa.Column("cp", sa.Integer(), nullable=False),
        sa.Column("sp", sa.Integer(), nullable=False),
        sa.Column("ep", sa.Integer(), nullable=False),
        sa.Column("gp", sa.Integer(), nullable=False),
        sa.Column("pp", sa.Integer(), nullable=False),
        sa.Column("encumbrance", sa.String(length=32), nullable=True),
        sa.Column("features_and_traits", sa.String(), nullable=True),
        sa.Column("features", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("proficiencies", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("other_proficiencies_languages", sa.String(), nullable=True),
        sa.Column("proficiencies_languages_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("resources", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("companions", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("conditions", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("extras", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("portrait_url", sa.String(length=512), nullable=True),
        sa.Column("notes", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["owner_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["character_id"], ["characters.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_dnd5e_character_sheets_owner_id", "dnd5e_character_sheets", ["owner_id"])
    op.create_index("ix_dnd5e_character_sheets_character_id", "dnd5e_character_sheets", ["character_id"])

    op.create_table(
        "operation_traces",
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("operation_id", sa.String(length=128), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("worker_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_visible_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("narration_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'submitted'")),
        sa.Column("telemetry_dropped", sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("trace_id"),
    )
    op.create_index("ix_operation_traces_operation_id", "operation_traces", ["operation_id"])
    op.create_index("ix_operation_traces_campaign_id", "operation_traces", ["campaign_id"])

    op.create_table(
        "outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False, server_default=sa.text("'campaign'")),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("operation_id", sa.String(length=128), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by", sa.String(length=128), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_outbox_operation_id", "outbox", ["operation_id"])
    op.create_index("ix_outbox_trace_id", "outbox", ["trace_id"])
    op.create_index("ix_outbox_campaign_id", "outbox", ["campaign_id"])
    op.create_index("ix_outbox_aggregate_id", "outbox", ["aggregate_id"])

    op.create_table(
        "player_submissions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("character_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("thread_id", sa.String(length=128), nullable=False, server_default=sa.text("'main'")),
        sa.Column("audience", sa.String(length=32), nullable=False, server_default=sa.text("'campaign'")),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("raw_content", sa.String(), nullable=False),
        sa.Column("resolution_status", sa.String(length=32), nullable=False, server_default=sa.text("'accepted'")),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["profiles.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint('sequence > 0', name="ck_player_submissions_positive_sequence"),
        sa.UniqueConstraint("campaign_id", "thread_id", "sequence", name="uq_player_submissions_campaign_thread_sequence"),
        sa.ForeignKeyConstraint(["character_id"], ["characters.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_player_submissions_resolution_status", "player_submissions", ["resolution_status"])
    op.create_index("ix_player_submissions_user_id", "player_submissions", ["user_id"])
    op.create_index("ix_player_submissions_campaign_id", "player_submissions", ["campaign_id"])
    op.create_index("ix_player_submissions_character_id", "player_submissions", ["character_id"])

    op.create_table(
        "worker_executions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_type", sa.String(length=128), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("aggregate_type", sa.String(length=64), nullable=True),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("expected_revision", sa.Integer(), nullable=True),
        sa.Column("operation_id", sa.String(length=128), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default=sa.text('5')),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("error_class", sa.String(length=16), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processing_duration_ms", sa.Integer(), nullable=True),
        sa.Column("claim_token", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_worker_executions_operation_id", "worker_executions", ["operation_id"])
    op.create_index("ix_worker_executions_campaign_id", "worker_executions", ["campaign_id"])
    op.create_index("ix_worker_executions_claim_token", "worker_executions", ["claim_token"])
    op.create_index("ix_worker_executions_idempotency_key", "worker_executions", ["idempotency_key"])
    op.create_index("ix_worker_executions_trace_id", "worker_executions", ["trace_id"])

    op.create_table(
        "campaign_thread_members",
        sa.Column("thread_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False, server_default=sa.text("'member'")),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint("thread_id", "user_id"),
        sa.ForeignKeyConstraint(["thread_id"], ["campaign_threads.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("thread_id", "user_id", name="uq_campaign_thread_members_identity"),
    )

    op.create_table(
        "dm_streams",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("thread_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("turn_id", sa.String(length=64), nullable=False),
        sa.Column("attempt_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default=sa.text("'streaming'")),
        sa.Column("audience", sa.String(length=32), nullable=False, server_default=sa.text("'campaign'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column("first_chunk_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_chunk_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("abandoned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column("total_bytes", sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column("last_sequence", sa.Integer(), nullable=True),
        sa.Column("completion_reason", sa.String(length=64), nullable=True),
        sa.Column("abandonment_reason", sa.String(length=64), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("operation_id", sa.String(length=128), nullable=True),
        sa.Column("final_text", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("turn_id", "attempt_id", name="uq_dm_streams_turn_attempt"),
        sa.ForeignKeyConstraint(["thread_id"], ["campaign_threads.id"], ondelete="CASCADE"),
        sa.CheckConstraint("status IN ('streaming', 'completed', 'abandoned', 'failed')", name="ck_dm_streams_status"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_dm_streams_campaign_id", "dm_streams", ["campaign_id"])
    op.create_index("ix_dm_streams_trace_id", "dm_streams", ["trace_id"])
    op.create_index("ix_dm_streams_turn_id", "dm_streams", ["turn_id"])
    op.create_index("ix_dm_streams_campaign_thread", "dm_streams", ["campaign_id", "thread_id"])
    op.create_index("ix_dm_streams_operation_id", "dm_streams", ["operation_id"])
    op.create_index("ix_dm_streams_attempt_id", "dm_streams", ["attempt_id"])
    op.create_index("ix_dm_streams_thread_id", "dm_streams", ["thread_id"])

    op.create_table(
        "player_submission_segments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("submission_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("segment_type", sa.String(length=8), nullable=False),
        sa.Column("text", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("submission_id", "position", name="uq_player_submission_segments_position"),
        sa.CheckConstraint('position >= 0', name="ck_player_submission_segments_nonnegative_position"),
        sa.ForeignKeyConstraint(["submission_id"], ["player_submissions.id"], ondelete="CASCADE"),
        sa.CheckConstraint("segment_type IN ('ic', 'ooc')", name="ck_player_submission_segments_type"),
    )
    op.create_index("ix_player_submission_segments_submission_id", "player_submission_segments", ["submission_id"])

    op.create_table(
        "dm_stream_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stream_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("text", sa.String(), nullable=False),
        sa.Column("byte_length", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.UniqueConstraint("stream_id", "sequence", name="uq_dm_stream_chunks_stream_sequence"),
        sa.CheckConstraint('sequence >= 0', name="ck_dm_stream_chunks_nonnegative_sequence"),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["stream_id"], ["dm_streams.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_dm_stream_chunks_stream_id", "dm_stream_chunks", ["stream_id"])

    op.create_table(
        "dm_turn_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("turn_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'prepared'")),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("thread_id", sa.String(length=128), nullable=False),
        sa.Column("audience", sa.String(length=32), nullable=False, server_default=sa.text("'campaign'")),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("input_set_revision", sa.Integer(), nullable=False),
        sa.Column("submission_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("parent_attempt_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("worker_job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("invalidation_reason", sa.String(length=256), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("streaming_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("error_class", sa.String(length=32), nullable=True),
        sa.Column("assembly_window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("assembly_window_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processing_duration_ms", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("roll_evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("staged_effects", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("contract_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("stream_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("commit_operation_id", sa.String(length=128), nullable=True),
        sa.Column("abandoned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("abandonment_reason", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(["worker_job_id"], ["worker_executions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["stream_id"], ["dm_streams.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["parent_attempt_id"], ["dm_turn_attempts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["turn_id"], ["dm_turns.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("turn_id", "attempt_number", name="uq_dm_turn_attempts_turn_number"),
    )
    op.create_index("ix_dm_turn_attempts_thread_id", "dm_turn_attempts", ["thread_id"])
    op.create_index("ix_dm_turn_attempts_stream_id", "dm_turn_attempts", ["stream_id"])
    op.create_index("ix_dm_turn_attempts_worker_job_id", "dm_turn_attempts", ["worker_job_id"])
    op.create_index("ix_dm_turn_attempts_status", "dm_turn_attempts", ["status"])
    op.create_index("ix_dm_turn_attempts_turn_id", "dm_turn_attempts", ["turn_id"])
    op.create_index("ix_dm_turn_attempts_turn_status", "dm_turn_attempts", ["turn_id", "status"])
    op.create_index("ix_dm_turn_attempts_campaign_status", "dm_turn_attempts", ["campaign_id", "status"])
    op.create_index("ix_dm_turn_attempts_campaign_id", "dm_turn_attempts", ["campaign_id"])
    op.create_index("ix_dm_turn_attempts_commit_operation_id", "dm_turn_attempts", ["commit_operation_id"])

    op.create_table(
        "player_roll_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("thread_id", sa.String(length=128), nullable=False),
        sa.Column("turn_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_key", sa.String(length=48), nullable=False),
        sa.Column("requested_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("character_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("roll_kind", sa.String(length=24), nullable=False),
        sa.Column("ability_or_skill", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("advantage_state", sa.String(length=16), nullable=False, server_default=sa.text("'normal'")),
        sa.Column("reason_public", sa.String(length=600), nullable=False),
        sa.Column("dc_private", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("replacement_of_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column("fulfilled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("roll_kind IN ('check','save','attack','ability','initiative','other')", name="ck_player_roll_requests_kind"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["character_id"], ["characters.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("turn_id", "request_key", name="uq_player_roll_requests_turn_key"),
        sa.ForeignKeyConstraint(["requested_user_id"], ["profiles.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["replacement_of_id"], ["player_roll_requests.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["turn_id"], ["dm_turns.id"], ondelete="CASCADE"),
        sa.CheckConstraint("advantage_state IN ('normal','advantage','disadvantage')", name="ck_player_roll_requests_advantage"),
        sa.CheckConstraint("status IN ('pending','fulfilled','cancelled','replaced')", name="ck_player_roll_requests_status"),
        sa.ForeignKeyConstraint(["attempt_id"], ["dm_turn_attempts.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_player_roll_requests_turn_id", "player_roll_requests", ["turn_id"])
    op.create_index("ix_player_roll_requests_player_status", "player_roll_requests", ["requested_user_id", "status"])
    op.create_index("ix_player_roll_requests_campaign_thread_status", "player_roll_requests", ["campaign_id", "thread_id", "status"])
    op.create_index("ix_player_roll_requests_character_id", "player_roll_requests", ["character_id"])
    op.create_index("ix_player_roll_requests_campaign_id", "player_roll_requests", ["campaign_id"])
    op.create_index("ix_player_roll_requests_thread_id", "player_roll_requests", ["thread_id"])
    op.create_index("ix_player_roll_requests_status", "player_roll_requests", ["status"])
    op.create_index("ix_player_roll_requests_requested_user_id", "player_roll_requests", ["requested_user_id"])
    op.create_index("ix_player_roll_requests_attempt_id", "player_roll_requests", ["attempt_id"])

    op.create_table(
        "player_roll_fulfillments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("roll_request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("submitted_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("visibility", sa.String(length=16), nullable=False, server_default=sa.text("'public'")),
        sa.Column("raw_rolls", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("modifier", sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("raw_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.UniqueConstraint("roll_request_id", name="uq_player_roll_fulfillments_request"),
        sa.ForeignKeyConstraint(["submitted_by"], ["profiles.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("visibility IN ('public','private')", name="ck_player_roll_fulfillments_visibility"),
        sa.CheckConstraint("source IN ('app','physical')", name="ck_player_roll_fulfillments_source"),
        sa.ForeignKeyConstraint(["roll_request_id"], ["player_roll_requests.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_player_roll_fulfillments_submitted_by", "player_roll_fulfillments", ["submitted_by"])
    op.create_index("ix_player_roll_fulfillments_roll_request_id", "player_roll_fulfillments", ["roll_request_id"])

    # ── rules_embeddings: pgvector vs TEXT fallback (issue #223) ──
    if dialect == "postgresql" and has_vector:
        # vector type with HNSW index when pgvector available
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
            sa.Column("rule_id", sa.String(length=256), nullable=False),
            sa.Column("corpus_id", sa.String(length=64), nullable=False),
            sa.Column("corpus_version", sa.String(length=32), nullable=False),
            sa.Column("embedding_model", sa.String(length=64), nullable=False),
            sa.Column("embedding_version", sa.String(length=32), nullable=False, server_default=sa.text('1')),
            sa.Column("build_id", sa.String(length=64), nullable=False),
            sa.Column("embedding", sa.Text(), nullable=True),
            sa.Column("embedding_text", sa.Text(), nullable=True),
            sa.Column("chunk_strategy", sa.String(length=64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
            sa.ForeignKeyConstraint(["rule_id"], ["rules_sections.rule_id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("rule_id", "embedding_model", "build_id"),
        )
    # indexes common to both variants
    try:
        op.create_index("ix_rules_embeddings_corpus", "rules_embeddings", ["corpus_id", "corpus_version"])
    except Exception:
        pass
    try:
        op.create_index("ix_rules_embeddings_model_build", "rules_embeddings", ["embedding_model", "build_id"])
    except Exception:
        pass

    if dialect != "postgresql":
        return
    # ── Extra Postgres objects not expressed in ORM ──
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
                  AND public.can_subscribe_live_table((select realtime.topic()
                  AND realtime.messages.extension in ('broadcast')
                );
              END IF;
            END
            $$;
    """))
    try:
        conn.execute(sa.text(
            "CREATE INDEX IF NOT EXISTS ix_rules_sections_fts "
            "ON rules_sections USING gin (to_tsvector('english', title || ' ' || body))"
        ))
    except Exception:
        pass

def downgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name if conn is not None else "postgresql"
    if dialect == "postgresql":
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
    # drop tables reverse order (respect FK)
    op.drop_table("player_roll_fulfillments")
    op.drop_table("player_roll_requests")
    op.drop_table("dm_turn_attempts")
    op.drop_table("dm_stream_chunks")
    op.drop_table("player_submission_segments")
    op.drop_table("dm_streams")
    op.drop_table("campaign_thread_members")
    op.drop_table("worker_executions")
    op.drop_table("player_submissions")
    op.drop_table("outbox")
    op.drop_table("operation_traces")
    op.drop_table("dnd5e_character_sheets")
    op.drop_table("dm_turns")
    op.drop_table("character_chat_messages")
    op.drop_table("campaign_threads")
    op.drop_table("campaign_members")
    op.drop_table("campaign_invites")
    op.drop_table("campaign_domain_events")
    op.drop_table("rules_section_aliases")
    try:
        op.drop_table("rules_embeddings")
    except Exception:
        try:
            conn.execute(sa.text("DROP TABLE IF EXISTS rules_embeddings CASCADE"))
        except Exception:
            pass
    op.drop_table("idempotent_commands")
    op.drop_table("characters")
    op.drop_table("campaigns")
    op.drop_table("rules_sections")
    op.drop_table("rules_corpus_imports")
    op.drop_table("rules_corpora")
    op.drop_table("profiles")
    op.drop_table("ai_runs")
    # Do NOT drop auth.users — managed by Supabase Auth/GoTrue

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
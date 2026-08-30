"""Static migration checks for database-enforced domain-event immutability."""

from pathlib import Path


MIGRATION = (
    Path(__file__).parent.parent
    / "alembic"
    / "versions"
    / "d3a7c1e9f2b6_enforce_domain_event_immutability.py"
)


def test_migration_rejects_event_updates_and_deletes():
    source = MIGRATION.read_text(encoding="utf-8")
    assert "BEFORE UPDATE OR DELETE" in source
    assert "RAISE EXCEPTION 'campaign domain events are immutable'" in source


def test_migration_removes_duplicate_sequence_index():
    source = MIGRATION.read_text(encoding="utf-8")
    assert "DROP INDEX IF EXISTS ix_campaign_domain_events_campaign_sequence" in source


def test_migration_preserves_events_when_parent_rows_are_deleted():
    source = MIGRATION.read_text(encoding="utf-8")
    upgrade = source.split("def downgrade", 1)[0]
    assert upgrade.count('ondelete="RESTRICT"') == 2

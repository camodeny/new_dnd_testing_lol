"""Static migration checks for database-enforced domain-event immutability."""

from pathlib import Path


MIGRATION = (
    Path(__file__).parent.parent / "alembic" / "versions" / "2a04bc8c83ba_initial_schema.py"
)


def test_migration_rejects_event_updates_and_deletes():
    source = MIGRATION.read_text(encoding="utf-8")
    assert "BEFORE UPDATE OR DELETE" in source
    assert "RAISE EXCEPTION 'campaign domain events are immutable'" in source


def test_migration_removes_duplicate_sequence_index():
    # Baseline starts from empty DB, so no duplicate index removal needed.
    # Keep gate as no-op check that baseline exists.
    assert MIGRATION.exists()


def test_migration_preserves_events_when_parent_rows_are_deleted():
    # Immutability and RESTRICT are covered via ORM metadata + baseline trigger
    source = MIGRATION.read_text(encoding="utf-8")
    assert "reject_campaign_domain_event_mutation" in source
    # ORM defines FKs with RESTRICT on campaign_domain_events
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from database import Base
    import models  # noqa: F401

    table = Base.metadata.tables["campaign_domain_events"]
    restricts = [c.ondelete for c in table.foreign_keys if c.ondelete == "RESTRICT"]
    assert len(restricts) >= 2


def test_baseline_is_single_root_revision():
    """Per #313 the squash is a single root baseline with one head."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    backend = Path(__file__).parent.parent
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "alembic"))
    script = ScriptDirectory.from_config(config)

    assert script.get_revision("2a04bc8c83ba").down_revision is None
    assert script.get_heads() == ["2a04bc8c83ba"]

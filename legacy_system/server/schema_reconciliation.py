"""Idempotent startup schema reconciliation.

The application relies on ``db.create_all()`` at startup, which only creates
missing tables and never adds columns to existing ones. These ADD COLUMN
definitions mirror the additive changes in migrations/versions so that an
existing database is brought up to date on deploy without requiring a separate
migration step.
"""

import sqlalchemy as sa

# (table, column, DDL fragment) for every column added by a migration.
ADDED_COLUMNS = [
    # f8b2d109e23c add memory_anchors column
    ('campaign_sessions', 'memory_anchors', 'JSON'),
    # a1b2c3d4e5f6 add evidence provenance columns
    ('campaign_memory_logs', 'evidence_status', 'VARCHAR(50)'),
    ('campaign_memory_logs', 'provenance_json', 'JSON'),
    # b2c3d4e5f6a1 add reconciliation columns
    ('automation_runs', 'reconciliation_player_message_id', 'VARCHAR(120)'),
    ('automation_runs', 'reconciliation_timeout_phase', 'VARCHAR(40)'),
    ('automation_runs', 'reconciliation_timeout_error', 'TEXT'),
    ('automation_runs', 'reconciliation_started_at', 'DATETIME'),
    ('automation_runs', 'reconciliation_deadline', 'DATETIME'),
    # c3d4e5f6a1b2 add clock completion criteria
    ('campaign_clocks', 'completion_criteria', 'JSON'),
    ('campaign_clocks', 'completion_state', 'JSON'),
    # e1f2a3b4c5d6 add post_turn_revision
    ('session_dm_turns', 'post_turn_revision', 'INTEGER'),
    # f6a7b8c9d0e1 add bounded reclaim failure columns
    ('automation_runs', 'reclaim_failure_fingerprint', 'VARCHAR(160)'),
    ('automation_runs', 'reclaim_failure_count', 'INTEGER NOT NULL DEFAULT 0'),
    ('automation_runs', 'reclaim_failure_attempt', 'INTEGER'),
    ('automation_runs', 'reclaim_failure_stage', 'VARCHAR(120)'),
    ('automation_runs', 'reclaim_failure_error', 'TEXT'),
    ('automation_runs', 'reclaim_failure_at', 'DATETIME'),
]


def reconcile_schema(app):
    """Add any columns declared above that are missing from existing tables.

    Safe to call repeatedly; columns already present are skipped.
    """
    from models import db

    with app.app_context():
        engine = db.engine
        inspector = sa.inspect(engine)
        existing_tables = set(inspector.get_table_names())
        added = []
        with engine.begin() as conn:
            for table, column, ddl in ADDED_COLUMNS:
                if table not in existing_tables:
                    continue
                if column in {c['name'] for c in inspector.get_columns(table)}:
                    continue
                conn.exec_driver_sql(
                    f'ALTER TABLE {table} ADD COLUMN {column} {ddl}'
                )
                added.append(f'{table}.{column}')
        if added:
            print(f"Schema reconciliation added columns: {', '.join(added)}", flush=True)
        return added

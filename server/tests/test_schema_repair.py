import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ['DATABASE_URL'] = 'sqlite:///:memory:'

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app import (
    app,
    ensure_lightweight_schema,
    initialize_database,
    verify_required_schema,
)
from auth import generate_token
from models import (
    AutomationRun,
    AutomationRunAuditCycle,
    CampaignMemoryLog,
    User,
    db,
)
from services.automation_service import reconcile_stale_awaiting_audit_runs


def _memory_log_columns():
    return {
        row[1]
        for row in db.session.execute(text('PRAGMA table_info(campaign_memory_logs)')).fetchall()
    }


def _drop_evidence_provenance_columns():
    """Simulate a database created before the evidence/provenance migration."""
    db.session.execute(text('ALTER TABLE campaign_memory_logs DROP COLUMN evidence_status'))
    db.session.execute(text('ALTER TABLE campaign_memory_logs DROP COLUMN provenance_json'))
    db.session.commit()


def _clock_columns():
    return {
        row[1]
        for row in db.session.execute(text('PRAGMA table_info(campaign_clocks)')).fetchall()
    }


def _drop_clock_completion_columns():
    """Simulate a database created before the clock completion criteria migration."""
    db.session.execute(text('ALTER TABLE campaign_clocks DROP COLUMN completion_criteria'))
    db.session.execute(text('ALTER TABLE campaign_clocks DROP COLUMN completion_state'))
    db.session.commit()


class MemoryLogSchemaRepairTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        db_path = os.path.join(self.temp_dir.name, 'legacy.db')

        from flask import Flask
        self.file_app = Flask(__name__)
        self.file_app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
        self.file_app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        self.file_app.secret_key = 'test-key'
        db.init_app(self.file_app)
        self.addCleanup(self._teardown_db)

    def _teardown_db(self):
        with self.file_app.app_context():
            db.session.remove()
            db.engine.dispose()
        db._app_engines.pop(self.file_app, None)

    def test_fresh_database_exposes_evidence_provenance_columns(self):
        with self.file_app.app_context():
            db.create_all()
            columns = _memory_log_columns()
            self.assertIn('evidence_status', columns)
            self.assertIn('provenance_json', columns)
            verify_required_schema()

    def test_verify_required_schema_fails_loudly_on_legacy_table(self):
        with self.file_app.app_context():
            db.create_all()
            _drop_evidence_provenance_columns()

            with self.assertRaises(RuntimeError) as ctx:
                verify_required_schema()

            message = str(ctx.exception)
            self.assertIn('campaign_memory_logs.evidence_status', message)
            self.assertIn('campaign_memory_logs.provenance_json', message)

    def test_lightweight_schema_repairs_legacy_memory_log_table(self):
        with self.file_app.app_context():
            db.create_all()
            _drop_evidence_provenance_columns()
            self.assertNotIn('evidence_status', _memory_log_columns())
            self.assertNotIn('provenance_json', _memory_log_columns())

            ensure_lightweight_schema()

            columns = _memory_log_columns()
            self.assertIn('evidence_status', columns)
            self.assertIn('provenance_json', columns)
            verify_required_schema()
            # The query that previously raised "no such column" now succeeds.
            CampaignMemoryLog.query.limit(1).all()

    def test_lightweight_schema_repair_is_idempotent(self):
        with self.file_app.app_context():
            db.create_all()
            ensure_lightweight_schema()
            ensure_lightweight_schema()
            verify_required_schema()

    def test_initialize_database_repairs_legacy_memory_log_table(self):
        with self.file_app.app_context():
            db.create_all()
            _drop_evidence_provenance_columns()

        initialize_database(self.file_app)

        with self.file_app.app_context():
            columns = _memory_log_columns()
            self.assertIn('evidence_status', columns)
            self.assertIn('provenance_json', columns)
            CampaignMemoryLog.query.limit(1).all()


class CampaignClockSchemaRepairTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        db_path = os.path.join(self.temp_dir.name, 'legacy.db')

        from flask import Flask
        self.file_app = Flask(__name__)
        self.file_app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
        self.file_app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        self.file_app.secret_key = 'test-key'
        db.init_app(self.file_app)
        self.addCleanup(self._teardown_db)

    def _teardown_db(self):
        with self.file_app.app_context():
            db.session.remove()
            db.engine.dispose()
        db._app_engines.pop(self.file_app, None)

    def test_fresh_database_exposes_clock_completion_columns(self):
        with self.file_app.app_context():
            db.create_all()
            columns = _clock_columns()
            self.assertIn('completion_criteria', columns)
            self.assertIn('completion_state', columns)
            verify_required_schema()

    def test_verify_required_schema_fails_loudly_on_legacy_clock_table(self):
        with self.file_app.app_context():
            db.create_all()
            _drop_clock_completion_columns()

            with self.assertRaises(RuntimeError) as ctx:
                verify_required_schema()

            message = str(ctx.exception)
            self.assertIn('campaign_clocks.completion_criteria', message)
            self.assertIn('campaign_clocks.completion_state', message)

    def test_lightweight_schema_repairs_legacy_clock_table(self):
        with self.file_app.app_context():
            db.create_all()
            _drop_clock_completion_columns()
            self.assertNotIn('completion_criteria', _clock_columns())
            self.assertNotIn('completion_state', _clock_columns())

            ensure_lightweight_schema()

            columns = _clock_columns()
            self.assertIn('completion_criteria', columns)
            self.assertIn('completion_state', columns)
            verify_required_schema()

    def test_lightweight_schema_repair_is_idempotent(self):
        with self.file_app.app_context():
            db.create_all()
            ensure_lightweight_schema()
            ensure_lightweight_schema()
            verify_required_schema()

    def test_initialize_database_repairs_legacy_clock_table(self):
        with self.file_app.app_context():
            db.create_all()
            _drop_clock_completion_columns()

        initialize_database(self.file_app)

        with self.file_app.app_context():
            columns = _clock_columns()
            self.assertIn('completion_criteria', columns)
            self.assertIn('completion_state', columns)
            verify_required_schema()


class StaleAwaitingAuditReconciliationTest(unittest.TestCase):
    def setUp(self):
        with app.app_context():
            db.session.execute(text('PRAGMA foreign_keys=OFF'))
            db.drop_all()
            db.create_all()
            owner = User(username='owner', email='owner@example.com')
            owner.set_password('password')
            db.session.add(owner)
            db.session.flush()
            self.owner_id = owner.id
            db.session.commit()

    def tearDown(self):
        with app.app_context():
            db.session.remove()

    def _make_run(self, status, cycle_status, phase='post_turn'):
        run = AutomationRun(
            scenario_id=1,
            snapshot_id=1,
            user_id=self.owner_id,
            status=status,
        )
        db.session.add(run)
        db.session.flush()
        cycle = AutomationRunAuditCycle(
            run_id=run.id,
            cycle_number=1,
            phase=phase,
            status=cycle_status,
        )
        db.session.add(cycle)
        db.session.flush()
        if status == 'awaiting_audit':
            run.awaiting_audit_cycle_id = cycle.id
            run.awaiting_audit_phase = phase
        db.session.commit()
        return run, cycle

    def test_stale_awaiting_audit_run_is_continued(self):
        with app.app_context():
            run, cycle = self._make_run('awaiting_audit', 'audited')

            reconciled = reconcile_stale_awaiting_audit_runs()

            self.assertEqual(reconciled, 1)
            db.session.refresh(run)
            self.assertEqual(run.status, 'queued')
            self.assertIsNone(run.awaiting_audit_cycle_id)
            self.assertIsNone(run.awaiting_audit_phase)
            self.assertIsNotNone(run.audit_resumed_at)

    def test_run_with_pending_cycle_is_left_alone(self):
        with app.app_context():
            run, cycle = self._make_run('awaiting_audit', 'pending')

            reconciled = reconcile_stale_awaiting_audit_runs()

            self.assertEqual(reconciled, 0)
            db.session.refresh(run)
            self.assertEqual(run.status, 'awaiting_audit')
            self.assertEqual(run.awaiting_audit_cycle_id, cycle.id)

    def test_run_without_cycle_is_left_alone(self):
        with app.app_context():
            run = AutomationRun(
                scenario_id=1,
                snapshot_id=1,
                user_id=self.owner_id,
                status='awaiting_audit',
            )
            db.session.add(run)
            db.session.commit()

            reconciled = reconcile_stale_awaiting_audit_runs()

            self.assertEqual(reconciled, 0)
            db.session.refresh(run)
            self.assertEqual(run.status, 'awaiting_audit')


class AuditEndpointDatabaseErrorTest(unittest.TestCase):
    def setUp(self):
        with app.app_context():
            db.session.execute(text('PRAGMA foreign_keys=OFF'))
            db.drop_all()
            db.create_all()
            owner = User(username='owner', email='owner@example.com')
            owner.set_password('password')
            db.session.add(owner)
            db.session.flush()
            run = AutomationRun(
                scenario_id=1,
                snapshot_id=1,
                user_id=owner.id,
                status='awaiting_audit',
            )
            db.session.add(run)
            db.session.commit()
            self.run_id = run.id
            self.token = generate_token(owner.id)
        self.client = app.test_client()
        self.headers = {'Authorization': f'Bearer {self.token}'}

    def tearDown(self):
        with app.app_context():
            db.session.remove()

    def _db_error(self):
        return OperationalError(
            'SELECT * FROM campaign_memory_logs',
            {},
            Exception('no such column: campaign_memory_logs.evidence_status'),
        )

    def test_audit_bundle_returns_structured_json_on_database_error(self):
        with patch('routes.automation.get_current_audit_bundle_data', side_effect=self._db_error()):
            response = self.client.get(
                f'/api/automation/runs/{self.run_id}/audit-bundle',
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 500)
        self.assertTrue(response.is_json)
        payload = response.get_json()
        self.assertIn('error', payload)
        self.assertEqual(payload['error_class'], 'OperationalError')

    def test_auditor_tool_returns_structured_json_on_database_error(self):
        with patch('routes.automation.execute_auditor_tool', side_effect=self._db_error()):
            response = self.client.post(
                f'/api/automation/runs/{self.run_id}/auditor-tools/get_cycle_evidence_packet',
                headers=self.headers,
                json={'args': {}},
            )

        self.assertEqual(response.status_code, 500)
        self.assertTrue(response.is_json)
        payload = response.get_json()
        self.assertIn('get_cycle_evidence_packet', payload['error'])
        self.assertEqual(payload['error_class'], 'OperationalError')


if __name__ == '__main__':
    unittest.main()

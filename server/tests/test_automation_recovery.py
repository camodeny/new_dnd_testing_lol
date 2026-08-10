import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ['DATABASE_URL'] = 'sqlite:///:memory:'

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app import app
from auth import generate_token
from models import AutomationRun, AutomationRunAuditCycle, User, db
from services.automation_service import reconcile_stale_awaiting_audit_runs


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
            run, _ = self._make_run('awaiting_audit', 'audited')

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

    @staticmethod
    def _db_error():
        return OperationalError(
            'SELECT * FROM campaign_memory_logs',
            {},
            Exception('database unavailable'),
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

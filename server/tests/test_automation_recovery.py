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
from models import (
    AutomationRun,
    AutomationRunAuditAttempt,
    AutomationRunAuditCycle,
    AutomationRunAuditResult,
    User,
    db,
)
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


class AuditAcceptanceControlPlaneDecouplingTest(unittest.TestCase):
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
            db.session.flush()
            cycle = AutomationRunAuditCycle(
                run_id=run.id,
                cycle_number=1,
                phase='after_dm',
                status='pending',
                payload_json={},
            )
            db.session.add(cycle)
            db.session.flush()
            run.awaiting_audit_cycle_id = cycle.id
            run.awaiting_audit_phase = 'after_dm'
            db.session.commit()
            self.run_id = run.id
            self.cycle_id = cycle.id
            self.token = generate_token(owner.id)
        self.client = app.test_client()
        self.headers = {'Authorization': f'Bearer {self.token}'}

    def tearDown(self):
        with app.app_context():
            db.session.remove()

    def _audit_payload(self, **overrides):
        payload = {
            'summary': 'Cycle accepted.',
            'notes': 'Notes are free text.',
            'scorecard': {'overall_status': 'pass', 'overall_summary': 'Healthy cycle.'},
        }
        payload.update(overrides)
        return payload

    @staticmethod
    def _failed_diagnostic(correlation_id='corr-1'):
        return {
            'status': 'failed',
            'error_class': 'RuntimeError',
            'message': 'scorecard bug',
            'correlation_id': correlation_id,
        }

    def test_run_watch_available_when_scorecard_computation_raises(self):
        with patch('services.automation_service.refresh_run_scorecard', side_effect=RuntimeError('scorecard bug')):
            response = self.client.get(f'/api/automation/runs/{self.run_id}', headers=self.headers)

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['run']['status'], 'awaiting_audit')
        self.assertEqual(payload['current_audit_cycle']['id'], self.cycle_id)

    def test_run_watch_does_not_mutate_scorecard_rows(self):
        from services.automation_service import refresh_run_scorecard

        with app.app_context():
            run = db.session.get(AutomationRun, self.run_id)
            refresh_run_scorecard(run)
            rows = AutomationRunAuditResult.query.filter_by(run_id=self.run_id).order_by(AutomationRunAuditResult.id.asc()).all()
            before = [(row.id, row.check_id, row.status, row.summary) for row in rows]
            before_summary = dict(run.scorecard_summary_json or {})
            self.assertTrue(before)

        response = self.client.get(f'/api/automation/runs/{self.run_id}', headers=self.headers)
        self.assertEqual(response.status_code, 200)

        with app.app_context():
            rows = AutomationRunAuditResult.query.filter_by(run_id=self.run_id).order_by(AutomationRunAuditResult.id.asc()).all()
            after = [(row.id, row.check_id, row.status, row.summary) for row in rows]
            run = db.session.get(AutomationRun, self.run_id)
            self.assertEqual(before, after)
            self.assertEqual(before_summary, run.scorecard_summary_json)

    def test_audit_acceptance_durable_when_scorecard_refresh_fails(self):
        with patch(
            'routes.automation.try_refresh_run_scorecard',
            return_value=(False, None, self._failed_diagnostic()),
        ):
            response = self.client.post(
                f'/api/automation/runs/{self.run_id}/audit-cycles/{self.cycle_id}/audit',
                headers=self.headers,
                json=self._audit_payload(),
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['audit_cycle']['status'], 'audited')
        self.assertEqual(payload['scorecard_refresh']['status'], 'failed')
        self.assertEqual(payload['scorecard_refresh']['error']['correlation_id'], 'corr-1')

        with app.app_context():
            cycle = db.session.get(AutomationRunAuditCycle, self.cycle_id)
            self.assertEqual(cycle.status, 'audited')
            self.assertEqual(cycle.summary, 'Cycle accepted.')

    def test_audit_submission_rejects_dictionary_summary_before_mutation(self):
        response = self.client.post(
            f'/api/automation/runs/{self.run_id}/audit-cycles/{self.cycle_id}/audit',
            headers=self.headers,
            json=self._audit_payload(summary={'text': 'not a string'}),
        )

        self.assertEqual(response.status_code, 422)
        payload = response.get_json()
        self.assertEqual(payload['error']['code'], 'invalid_summary')
        self.assertTrue(payload['retryable'])

        with app.app_context():
            cycle = db.session.get(AutomationRunAuditCycle, self.cycle_id)
            self.assertNotEqual(cycle.status, 'audited')
            self.assertIsNone(cycle.summary)
            attempt = AutomationRunAuditAttempt.query.filter_by(
                cycle_id=self.cycle_id,
                status='failed',
            ).order_by(AutomationRunAuditAttempt.id.desc()).first()
            self.assertIsNotNone(attempt)
            self.assertEqual(attempt.error_class, 'AuditScorecardValidationError')
            self.assertEqual(attempt.error_message, 'audit summary must be a string.')

    def test_audit_submission_rejects_list_notes_before_mutation(self):
        response = self.client.post(
            f'/api/automation/runs/{self.run_id}/audit-cycles/{self.cycle_id}/audit',
            headers=self.headers,
            json=self._audit_payload(notes=['one', 'two']),
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.get_json()['error']['code'], 'invalid_notes')

        with app.app_context():
            cycle = db.session.get(AutomationRunAuditCycle, self.cycle_id)
            self.assertNotEqual(cycle.status, 'audited')
            self.assertIsNone(cycle.notes)

    def test_audit_submission_valid_strings_continue_to_work(self):
        response = self.client.post(
            f'/api/automation/runs/{self.run_id}/audit-cycles/{self.cycle_id}/audit',
            headers=self.headers,
            json=self._audit_payload(),
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['audit_cycle']['status'], 'audited')
        self.assertEqual(payload['scorecard_refresh']['status'], 'ok')
        self.assertTrue(payload['scorecard'])

    def test_idempotent_retry_does_not_duplicate_attempts_or_overwrite_feedback(self):
        first = self.client.post(
            f'/api/automation/runs/{self.run_id}/audit-cycles/{self.cycle_id}/audit',
            headers=self.headers,
            json=self._audit_payload(summary='Original summary.'),
        )
        self.assertEqual(first.status_code, 200)
        with app.app_context():
            self.assertEqual(
                AutomationRunAuditAttempt.query.filter_by(cycle_id=self.cycle_id, status='success').count(),
                1,
            )

        retry = self.client.post(
            f'/api/automation/runs/{self.run_id}/audit-cycles/{self.cycle_id}/audit',
            headers=self.headers,
            json=self._audit_payload(summary='Changed summary.'),
        )
        self.assertEqual(retry.status_code, 200)
        payload = retry.get_json()
        self.assertTrue(payload.get('idempotent_retry'))
        self.assertEqual(payload['audit_cycle']['summary'], 'Original summary.')

        with app.app_context():
            self.assertEqual(
                AutomationRunAuditAttempt.query.filter_by(cycle_id=self.cycle_id, status='success').count(),
                1,
            )
            cycle = db.session.get(AutomationRunAuditCycle, self.cycle_id)
            self.assertEqual(cycle.summary, 'Original summary.')

    def test_audit_bundle_available_when_scorecard_refresh_fails(self):
        with patch(
            'services.automation_auditor.try_refresh_run_scorecard',
            return_value=(False, None, self._failed_diagnostic('corr-bundle')),
        ):
            response = self.client.get(f'/api/automation/runs/{self.run_id}/audit-bundle', headers=self.headers)

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['scorecard_refresh']['status'], 'failed')
        self.assertIsNotNone(payload['audit_cycle'])
        self.assertIn('evidence_packet', payload)

    def test_audit_bundle_structured_error_on_unexpected_failure(self):
        with patch('routes.automation.get_current_audit_bundle_data', side_effect=RuntimeError('boom')):
            response = self.client.get(f'/api/automation/runs/{self.run_id}/audit-bundle', headers=self.headers)

        self.assertEqual(response.status_code, 500)
        self.assertTrue(response.is_json)
        payload = response.get_json()
        self.assertIn('error', payload)
        self.assertEqual(payload['error_class'], 'RuntimeError')
        self.assertIn('correlation_id', payload)

    def test_auditor_evidence_packet_available_when_scorecard_refresh_fails(self):
        with patch(
            'services.automation_auditor.try_refresh_run_scorecard',
            return_value=(False, None, self._failed_diagnostic('corr-evidence')),
        ):
            response = self.client.post(
                f'/api/automation/runs/{self.run_id}/auditor-tools/get_cycle_evidence_packet',
                headers=self.headers,
                json={'args': {}},
            )

        self.assertEqual(response.status_code, 200)
        result = response.get_json()['result']
        self.assertEqual(result['scorecard_refresh']['status'], 'failed')
        self.assertIn('transcript_window', result)

    def test_scorecard_endpoint_returns_structured_diagnostics_when_refresh_fails(self):
        with patch(
            'routes.automation.try_refresh_run_scorecard',
            return_value=(False, None, self._failed_diagnostic('corr-scorecard')),
        ):
            response = self.client.get(f'/api/automation/runs/{self.run_id}/scorecard', headers=self.headers)

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['scorecard_refresh']['status'], 'failed')
        self.assertEqual(payload['scorecard_refresh']['error']['correlation_id'], 'corr-scorecard')
        self.assertEqual(payload['scorecard'], [])

    def test_repair_endpoint_rebuilds_stale_aggregates(self):
        with app.app_context():
            run = db.session.get(AutomationRun, self.run_id)
            for cycle_number in (2, 3):
                db.session.add(AutomationRunAuditCycle(
                    run_id=run.id,
                    cycle_number=cycle_number,
                    phase='after_dm',
                    status='audited',
                    scorecard_json={},
                    scorecard_summary_json={},
                ))
            run.scorecard_summary_json = {}
            db.session.commit()

        response = self.client.post(f'/api/automation/runs/{self.run_id}/scorecard/repair', headers=self.headers)

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['scorecard_refresh']['status'], 'ok')
        self.assertEqual(payload['run']['scorecard_summary']['audited_cycle_count'], 2)
        self.assertTrue(payload['scorecard'])

    def test_scorecard_defect_cannot_block_post_audit_resume(self):
        # Reproduces Run 42's post-audit resume path: an audit is accepted even
        # when scorecard recomputation fails, and the run can then resume (cycle
        # 2 becomes reachable) while still being observable via the status read.
        with patch(
            'routes.automation.try_refresh_run_scorecard',
            return_value=(False, None, self._failed_diagnostic('corr-resume')),
        ):
            audit = self.client.post(
                f'/api/automation/runs/{self.run_id}/audit-cycles/{self.cycle_id}/audit',
                headers=self.headers,
                json=self._audit_payload(),
            )
        self.assertEqual(audit.status_code, 200)
        self.assertEqual(audit.get_json()['audit_cycle']['status'], 'audited')

        resume = self.client.post(
            f'/api/automation/runs/{self.run_id}/continue',
            headers=self.headers,
            json={},
        )
        self.assertEqual(resume.status_code, 200)
        self.assertEqual(resume.get_json()['run']['status'], 'queued')

        with patch('services.automation_service.refresh_run_scorecard', side_effect=RuntimeError('scorecard bug')):
            fetch = self.client.get(f'/api/automation/runs/{self.run_id}', headers=self.headers)
        self.assertEqual(fetch.status_code, 200)
        self.assertEqual(fetch.get_json()['run']['status'], 'queued')
        self.assertEqual(fetch.get_json()['audit_cycles'][0]['status'], 'audited')

    def test_scorecard_refresh_commit_false_participates_in_caller_transaction(self):
        from services.automation_service import refresh_run_scorecard

        with app.app_context():
            run = db.session.get(AutomationRun, self.run_id)
            self.assertEqual(AutomationRunAuditResult.query.filter_by(run_id=run.id).count(), 0)

            # commit=False flushes into the caller's session without committing;
            # the caller controls the outcome via commit/rollback.
            refresh_run_scorecard(run, commit=False)
            self.assertEqual(AutomationRunAuditResult.query.filter_by(run_id=run.id).count(), 6)
            db.session.rollback()
            self.assertEqual(AutomationRunAuditResult.query.filter_by(run_id=run.id).count(), 0)

        # A fresh session must not observe the un-committed rows.
        with app.app_context():
            self.assertEqual(AutomationRunAuditResult.query.filter_by(run_id=self.run_id).count(), 0)
            run = db.session.get(AutomationRun, self.run_id)
            refresh_run_scorecard(run, commit=True)
            self.assertGreater(AutomationRunAuditResult.query.filter_by(run_id=run.id).count(), 0)

        # Persisted rows are visible from a brand-new session after commit=True.
        with app.app_context():
            self.assertGreater(AutomationRunAuditResult.query.filter_by(run_id=self.run_id).count(), 0)


if __name__ == '__main__':
    unittest.main()

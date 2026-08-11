import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ['DATABASE_URL'] = 'sqlite:///:memory:'

from sqlalchemy import inspect, text

from app import app
from models import db
from schema_reconciliation import ADDED_COLUMNS, reconcile_schema


class SchemaReconciliationTest(unittest.TestCase):
    def setUp(self):
        with app.app_context():
            db.drop_all()
            db.create_all()

    def tearDown(self):
        with app.app_context():
            db.session.remove()

    def _columns(self, table):
        return {c['name'] for c in inspect(db.engine).get_columns(table)}

    def test_reconcile_adds_missing_columns(self):
        with app.app_context():
            db.session.execute(text(
                "ALTER TABLE automation_runs DROP COLUMN reclaim_failure_fingerprint"
            ))
            db.session.execute(text(
                "ALTER TABLE automation_runs DROP COLUMN reclaim_failure_count"
            ))
            db.session.commit()

            missing = [c for t, c, _ in ADDED_COLUMNS if t == 'automation_runs'
                       and c in ('reclaim_failure_fingerprint', 'reclaim_failure_count')]
            for col in missing:
                self.assertNotIn(col, self._columns('automation_runs'))

            added = reconcile_schema(app)
            self.assertTrue(any('reclaim_failure_fingerprint' in a for a in added))
            self.assertTrue(any('reclaim_failure_count' in a for a in added))

            self.assertIn('reclaim_failure_fingerprint', self._columns('automation_runs'))
            self.assertIn('reclaim_failure_count', self._columns('automation_runs'))

    def test_reconcile_is_idempotent(self):
        with app.app_context():
            first = reconcile_schema(app)
            second = reconcile_schema(app)
            self.assertEqual(second, [])
            self.assertTrue(all(f in ADDED_COLUMNS for f in first))

    def test_reconcile_covers_all_declared_columns_on_fresh_db(self):
        with app.app_context():
            existing = {c['name'] for c in inspect(db.engine).get_columns('automation_runs')}
            for table, column, _ in ADDED_COLUMNS:
                cols = {c['name'] for c in inspect(db.engine).get_columns(table)}
                self.assertIn(column, cols, f'{table}.{column} missing after create_all')


if __name__ == '__main__':
    unittest.main()

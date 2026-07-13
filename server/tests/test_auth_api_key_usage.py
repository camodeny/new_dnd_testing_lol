import os
import sys
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy.exc import OperationalError

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ['DATABASE_URL'] = 'sqlite:///:memory:'

from app import app
from auth import _touch_api_key_last_used


class ApiKeyUsageTrackingTest(unittest.TestCase):
    def test_recent_usage_does_not_write(self):
        now = datetime(2026, 7, 13, 6, 0, tzinfo=UTC)
        key = SimpleNamespace(last_used_at=now - timedelta(seconds=60))

        with (
            app.app_context(),
            patch('auth.utcnow', return_value=now),
            patch('auth.db.session.commit') as commit,
        ):
            _touch_api_key_last_used(key)

        commit.assert_not_called()
        self.assertEqual(key.last_used_at, now - timedelta(seconds=60))

    def test_stale_usage_is_persisted(self):
        now = datetime(2026, 7, 13, 6, 0, tzinfo=UTC)
        key = SimpleNamespace(last_used_at=now - timedelta(minutes=10))

        with (
            app.app_context(),
            patch('auth.utcnow', return_value=now),
            patch('auth.db.session.commit') as commit,
        ):
            _touch_api_key_last_used(key)

        commit.assert_called_once_with()
        self.assertEqual(key.last_used_at, now)

    def test_database_lock_does_not_fail_authentication_path(self):
        now = datetime(2026, 7, 13, 6, 0, tzinfo=UTC)
        key = SimpleNamespace(last_used_at=None)
        lock_error = OperationalError('UPDATE user_automation_keys', {}, Exception('database is locked'))

        with (
            app.app_context(),
            patch('auth.utcnow', return_value=now),
            patch('auth.db.session.commit', side_effect=lock_error),
            patch('auth.db.session.rollback') as rollback,
            patch.object(app.logger, 'warning') as warning,
        ):
            _touch_api_key_last_used(key)

        rollback.assert_called_once_with()
        warning.assert_called_once()


if __name__ == '__main__':
    unittest.main()

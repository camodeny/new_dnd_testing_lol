#!/usr/bin/env python3
from pathlib import Path
import re


def replace_once(text, old, new, label):
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f'{label}: expected one match, found {count}')
    return text.replace(old, new, 1)


SQ_CONFIG = '''import logging
import os
import re
import time
from functools import wraps

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

logger = logging.getLogger(__name__)

DEFAULT_SQLITE_BUSY_TIMEOUT_MS = 30000

SQLITE_LOCK_PATTERNS = [
    re.compile(r'database is locked', re.IGNORECASE),
    re.compile(r'database table is locked', re.IGNORECASE),
]


def _sqlite_url(url):
    if not url:
        return None
    try:
        parsed = make_url(str(url))
    except Exception:
        return None
    return parsed if parsed.get_backend_name() == 'sqlite' else None


def _is_sqlite_url(url):
    return _sqlite_url(url) is not None


def _is_sqlite_file_backed(url):
    parsed = _sqlite_url(url)
    if parsed is None:
        return False
    database = parsed.database
    return bool(database and database != ':memory:')


def _sqlite_busy_timeout_ms():
    value = os.environ.get('DND_SQLITE_BUSY_TIMEOUT_MS', str(DEFAULT_SQLITE_BUSY_TIMEOUT_MS))
    try:
        return max(1000, int(value))
    except (TypeError, ValueError):
        return DEFAULT_SQLITE_BUSY_TIMEOUT_MS


def configure_sqlite_engine_options(app):
    """Set SQLite DBAPI connection options before Flask-SQLAlchemy creates the engine."""
    database_uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
    if not _is_sqlite_url(database_uri):
        return False

    timeout_seconds = _sqlite_busy_timeout_ms() / 1000.0
    existing = dict(app.config.get('SQLALCHEMY_ENGINE_OPTIONS') or {})
    connect_args = dict(existing.get('connect_args') or {})
    connect_args['timeout'] = timeout_seconds
    existing['connect_args'] = connect_args
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = existing
    return True


def install_sqlite_pragmas(db, app):
    """Install per-connection busy timeout and initialize WAL once for file databases."""
    database_uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
    if not _is_sqlite_url(database_uri):
        return False

    with app.app_context():
        engine = db.engine

    if getattr(engine, '_dnd_sqlite_pragmas_installed', False):
        return True

    busy_ms = _sqlite_busy_timeout_ms()

    @event.listens_for(engine, 'connect')
    def _on_sqlite_connect(dbapi_connection, connection_record):
        del connection_record
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f'PRAGMA busy_timeout = {busy_ms}')
        finally:
            cursor.close()

    engine._dnd_sqlite_pragmas_installed = True

    actual_mode = None
    if _is_sqlite_file_backed(database_uri):
        try:
            with engine.connect() as connection:
                actual_mode = connection.exec_driver_sql('PRAGMA journal_mode = WAL').scalar()
                connection.exec_driver_sql(f'PRAGMA busy_timeout = {busy_ms}')
        except OperationalError:
            logger.warning(
                'Unable to initialize SQLite WAL mode during startup; continuing with busy timeout.',
                exc_info=True,
            )
        else:
            normalized_mode = str(actual_mode or '').strip().lower()
            if normalized_mode != 'wal':
                logger.warning(
                    'SQLite journal_mode is %s after WAL initialization; write concurrency may be affected.',
                    normalized_mode or 'unknown',
                )

    logger.info(
        'SQLite configured: busy_timeout=%dms, connection_timeout=%ss, journal_mode=%s',
        busy_ms,
        busy_ms / 1000.0,
        str(actual_mode or 'unchanged').lower(),
    )
    return True


def configure_sqlite_engine(db, app):
    """Compatibility helper for tests and external callers.

    Production must call configure_sqlite_engine_options(app) before db.init_app(app),
    then install_sqlite_pragmas(db, app) afterward.
    """
    configure_sqlite_engine_options(app)
    return install_sqlite_pragmas(db, app)


def _is_sqlite_lock_error(exc):
    if not isinstance(exc, OperationalError):
        return False
    message = str(exc)
    return any(pattern.search(message) for pattern in SQLITE_LOCK_PATTERNS)


def _rollback_for_retry(rollback_fn):
    if rollback_fn is not None:
        rollback_fn()
        return
    try:
        from flask import has_app_context
        if not has_app_context():
            return
        from models import db
        db.session.rollback()
    except Exception:
        logger.exception('Failed to roll back SQLAlchemy session after SQLite lock contention.')
        raise


def retry_on_sqlite_lock(max_attempts=3, backoff_ms=100, category='db', rollback_fn=None):
    """Retry an idempotent unit of work after rolling back a SQLite lock failure."""
    if max_attempts < 1:
        raise ValueError('max_attempts must be at least 1')

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            started = time.monotonic()
            for attempt in range(1, max_attempts + 1):
                try:
                    result = fn(*args, **kwargs)
                except OperationalError as exc:
                    if not _is_sqlite_lock_error(exc):
                        raise
                    _rollback_for_retry(rollback_fn)
                    elapsed_ms = int((time.monotonic() - started) * 1000)
                    if attempt >= max_attempts:
                        logger.error(
                            'SQLite lock exhaustion (category=%s, attempts=%d, elapsed_ms=%d, outcome=failed)',
                            category,
                            max_attempts,
                            elapsed_ms,
                        )
                        raise
                    logger.warning(
                        'SQLite lock contention (category=%s, attempt=%d/%d, elapsed_ms=%d, outcome=retry)',
                        category,
                        attempt,
                        max_attempts,
                        elapsed_ms,
                    )
                    delay_ms = backoff_ms * (2 ** (attempt - 1))
                    time.sleep(delay_ms / 1000.0)
                    continue

                if attempt > 1:
                    logger.info(
                        'SQLite lock recovered (category=%s, attempts=%d, elapsed_ms=%d, outcome=recovered)',
                        category,
                        attempt,
                        int((time.monotonic() - started) * 1000),
                    )
                return result

            raise RuntimeError('unreachable SQLite retry state')

        return wrapper

    return decorator
'''


# Replace SQLite configuration implementation.
Path('server/sqldb_config.py').write_text(SQ_CONFIG, encoding='utf-8')

# Ensure engine options are present before db.init_app creates the engine.
app_path = Path('server/app.py')
app_text = app_path.read_text(encoding='utf-8')
app_text = replace_once(
    app_text,
    'from sqldb_config import configure_sqlite_engine\n',
    'from sqldb_config import configure_sqlite_engine_options, install_sqlite_pragmas\n',
    'app import',
)
app_text = replace_once(
    app_text,
    "    db.init_app(app)\n\n    configure_sqlite_engine(db, app)\n",
    "    configure_sqlite_engine_options(app)\n    db.init_app(app)\n    install_sqlite_pragmas(db, app)\n",
    'app initialization ordering',
)
app_path.write_text(app_text, encoding='utf-8')

# Roll back before worker-activity retries and retry the correctness-critical heartbeat.
service_path = Path('server/services/automation_service.py')
service_text = service_path.read_text(encoding='utf-8')
service_text = replace_once(
    service_text,
    "    @retry_on_sqlite_lock(max_attempts=3, backoff_ms=100, category='worker_activity')\n",
    "    @retry_on_sqlite_lock(\n        max_attempts=3,\n        backoff_ms=100,\n        category='worker_activity',\n        rollback_fn=db.session.rollback,\n    )\n",
    'worker activity rollback',
)
old_heartbeat = '''def heartbeat_run(run, *, worker_id=None, lease_token=None, lease_seconds=None):
    ensure_worker_lease(run, worker_id=worker_id, lease_token=lease_token)
    now = _utcnow()
    run.heartbeat_at = now
    duration = lease_seconds if lease_seconds is not None else lease_seconds_for_run(run)
    run.lease_expires_at = now + timedelta(seconds=duration)
    run.updated_at = now
    db.session.commit()
    return run
'''
new_heartbeat = '''def heartbeat_run(run, *, worker_id=None, lease_token=None, lease_seconds=None):
    run_id = run.id

    @retry_on_sqlite_lock(
        max_attempts=3,
        backoff_ms=100,
        category='run_heartbeat',
        rollback_fn=db.session.rollback,
    )
    def _heartbeat_once():
        current_run = db.session.get(AutomationRun, run_id)
        if current_run is None:
            raise ValueError('Run not found')
        ensure_worker_lease(current_run, worker_id=worker_id, lease_token=lease_token)
        now = _utcnow()
        current_run.heartbeat_at = now
        duration = lease_seconds if lease_seconds is not None else lease_seconds_for_run(current_run)
        current_run.lease_expires_at = now + timedelta(seconds=duration)
        current_run.updated_at = now
        db.session.commit()
        return current_run

    return _heartbeat_once()
'''
service_text = replace_once(service_text, old_heartbeat, new_heartbeat, 'heartbeat retry')
service_path.write_text(service_text, encoding='utf-8')

# Replace the misleading heartbeat test with one that proves rollback + retry.
test_path = Path('server/tests/test_sqlite_hardening.py')
test_text = test_path.read_text(encoding='utf-8')
heartbeat_test_pattern = re.compile(
    r"    def test_heartbeat_tolerates_lock_and_retries\(self\):\n.*?(?=    def test_worker_activity_retries_on_lock)",
    re.DOTALL,
)
new_heartbeat_test = '''    def test_heartbeat_tolerates_lock_and_retries(self):
        from models import AutomationRun, db
        from services.automation_service import heartbeat_run

        with self.app.app_context():
            original_commit = db.session.commit
            commit_calls = [0]

            def flaky_commit():
                commit_calls[0] += 1
                if commit_calls[0] == 1:
                    raise OperationalError('database is locked', {}, None)
                return original_commit()

            with patch.object(db.session, 'commit', side_effect=flaky_commit), \
                    patch.object(db.session, 'rollback', wraps=db.session.rollback) as rollback:
                run = db.session.get(AutomationRun, self.run_id)
                result = heartbeat_run(
                    run,
                    worker_id='test-worker',
                    lease_token='test-token',
                    lease_seconds=45,
                )

            self.assertEqual(result.id, self.run_id)
            self.assertEqual(commit_calls[0], 2)
            rollback.assert_called_once_with()

'''
test_text, count = heartbeat_test_pattern.subn(new_heartbeat_test, test_text, count=1)
if count != 1:
    raise RuntimeError(f'heartbeat test replacement: expected one match, found {count}')

# Add focused tests for configuration ordering, rollback, and one-time WAL setup.
insert_marker = "\n\nif __name__ == '__main__':\n"
extra_tests = '''

class SQLiteReviewFixRegressionTest(unittest.TestCase):
    def test_engine_options_are_configured_before_init(self):
        from flask import Flask
        from sqldb_config import configure_sqlite_engine_options

        app = Flask(__name__)
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:////tmp/review-fix.db'
        with patch.dict(os.environ, {'DND_SQLITE_BUSY_TIMEOUT_MS': '31000'}):
            configured = configure_sqlite_engine_options(app)

        self.assertTrue(configured)
        self.assertEqual(
            app.config['SQLALCHEMY_ENGINE_OPTIONS']['connect_args']['timeout'],
            31.0,
        )

    def test_retry_rolls_back_before_second_attempt(self):
        from sqldb_config import retry_on_sqlite_lock

        events = []

        def operation():
            events.append('attempt')
            if events.count('attempt') == 1:
                raise OperationalError('database is locked', {}, None)
            return 'ok'

        decorated = retry_on_sqlite_lock(
            max_attempts=2,
            backoff_ms=0,
            category='test',
            rollback_fn=lambda: events.append('rollback'),
        )(operation)

        self.assertEqual(decorated(), 'ok')
        self.assertEqual(events, ['attempt', 'rollback', 'attempt'])

    def test_wal_pragma_is_not_registered_on_every_connection(self):
        source = Path(__file__).resolve().parents[1].joinpath('sqldb_config.py').read_text(encoding='utf-8')
        listener_body = source.split("@event.listens_for(engine, 'connect')", 1)[1].split(
            "engine._dnd_sqlite_pragmas_installed", 1
        )[0]
        self.assertIn('PRAGMA busy_timeout', listener_body)
        self.assertNotIn('journal_mode', listener_body)
'''
if insert_marker not in test_text:
    raise RuntimeError('test insertion marker not found')
test_text = test_text.replace(insert_marker, extra_tests + insert_marker, 1)
test_path.write_text(test_text, encoding='utf-8')

print('Applied PR 44 review fixes.')

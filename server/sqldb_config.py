"""SQLite engine setup and bounded lock-retry helpers."""

import logging
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

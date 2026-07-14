import logging
import os
import re
import time
from contextlib import contextmanager

from sqlalchemy import event
from sqlalchemy.exc import OperationalError

logger = logging.getLogger(__name__)

DEFAULT_SQLITE_BUSY_TIMEOUT_MS = 30000

SQLITE_LOCK_PATTERNS = [
    re.compile(r'database is locked', re.IGNORECASE),
    re.compile(r'database table is locked', re.IGNORECASE),
]


def _is_sqlite_url(url):
    if not url:
        return False
    return url.strip().startswith('sqlite:///')


def _is_sqlite_file_backed(url):
    if not _is_sqlite_url(url):
        return False
    path = url.strip()[len('sqlite:///'):]
    if not path:
        return False
    return path != ':memory:' and path != ''


def _sqlite_busy_timeout_ms():
    val = os.environ.get('DND_SQLITE_BUSY_TIMEOUT_MS', str(DEFAULT_SQLITE_BUSY_TIMEOUT_MS))
    try:
        return max(1000, int(val))
    except (TypeError, ValueError):
        return DEFAULT_SQLITE_BUSY_TIMEOUT_MS


def configure_sqlite_engine(db, app):
    database_uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')

    if not _is_sqlite_url(database_uri):
        return

    timeout_sec = int(_sqlite_busy_timeout_ms() / 1000)

    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        **app.config.get('SQLALCHEMY_ENGINE_OPTIONS', {}),
        'connect_args': {
            **app.config.get('SQLALCHEMY_ENGINE_OPTIONS', {}).get('connect_args', {}),
            'timeout': timeout_sec,
        },
    }

    if _is_sqlite_file_backed(database_uri):
        with app.app_context():
            engine = db.engine

        @event.listens_for(engine, 'connect')
        def _on_sqlite_connect(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            busy_ms = _sqlite_busy_timeout_ms()
            cursor.execute(f'PRAGMA busy_timeout = {busy_ms}')
            cursor.execute('PRAGMA journal_mode = WAL')
            result = cursor.fetchone()
            actual_mode = (result[0].lower() if result else 'unknown')
            if actual_mode != 'wal':
                logger.warning(
                    'SQLite journal_mode is %s after WAL pragma; '
                    'write concurrency may be affected',
                    actual_mode,
                )
            cursor.close()

    logger.info(
        'SQLite configured: busy_timeout=%dms, timeout=%ds, wal_pending=%s',
        _sqlite_busy_timeout_ms(),
        timeout_sec,
        _is_sqlite_file_backed(database_uri),
    )


def _is_sqlite_lock_error(exc):
    if not isinstance(exc, OperationalError):
        return False
    msg = str(exc)
    for pattern in SQLITE_LOCK_PATTERNS:
        if pattern.search(msg):
            return True
    return False


def retry_on_sqlite_lock(max_attempts=3, backoff_ms=100, category='db'):
    def decorator(fn):
        def wrapper(*args, **kwargs):
            last_error = None
            start = time.monotonic()
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except OperationalError as exc:
                    if not _is_sqlite_lock_error(exc):
                        raise
                    last_error = exc
                    elapsed_ms = int((time.monotonic() - start) * 1000)
                    logger.warning(
                        'SQLite lock contention (category=%s, attempt=%d/%d, '
                        'elapsed_ms=%d, outcome=retry)',
                        category, attempt, max_attempts, elapsed_ms,
                    )
                    if attempt < max_attempts:
                        delay = backoff_ms * (2 ** (attempt - 1))
                        time.sleep(delay / 1000.0)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            logger.error(
                'SQLite lock exhaustion (category=%s, attempts=%d, '
                'elapsed_ms=%d, outcome=failed)',
                category, max_attempts, elapsed_ms,
            )
            raise last_error
        return wrapper
    return decorator


@contextmanager
def sqlite_lock_retry_context(max_attempts=3, backoff_ms=100, category='db'):
    last_error = None
    start = time.monotonic()
    for attempt in range(1, max_attempts + 1):
        try:
            yield attempt
            return
        except OperationalError as exc:
            if not _is_sqlite_lock_error(exc):
                raise
            last_error = exc
            elapsed_ms = int((time.monotonic() - start) * 1000)
            logger.warning(
                'SQLite lock contention (category=%s, attempt=%d/%d, '
                'elapsed_ms=%d, outcome=retry)',
                category, attempt, max_attempts, elapsed_ms,
            )
            if attempt < max_attempts:
                delay = backoff_ms * (2 ** (attempt - 1))
                time.sleep(delay / 1000.0)
    elapsed_ms = int((time.monotonic() - start) * 1000)
    logger.error(
        'SQLite lock exhaustion (category=%s, attempts=%d, '
        'elapsed_ms=%d, outcome=failed)',
        category, max_attempts, elapsed_ms,
    )
    raise last_error

"""Execution ownership across commits, without locking campaign rows during model calls."""
from contextlib import contextmanager

from sqlalchemy import text


def execution_lock_key(attempt_id) -> int:
    # Stable signed bigint, namespaced to DM execution advisory locks.
    import hashlib
    return int.from_bytes(hashlib.blake2b(
        f"dm-execute:{attempt_id}".encode(), digest_size=8,
    ).digest(), "big", signed=True)


def try_execution_lock(connection, attempt_id) -> bool:
    return bool(connection.execute(
        text("SELECT pg_try_advisory_xact_lock(:key)"),
        {"key": execution_lock_key(attempt_id)},
    ).scalar())


@contextmanager
def execution_ownership(db, attempt_id):
    """Hold a dedicated transaction until execution ends; crash releases it.

    Transaction advisory locks also work through transaction-mode poolers.
    SQLite tests rely on the atomic prepared-to-running claim instead.
    """
    engine = db.get_bind()
    if engine.dialect.name != "postgresql":
        yield True
        return
    with engine.connect() as connection, connection.begin():
        yield try_execution_lock(connection, attempt_id)

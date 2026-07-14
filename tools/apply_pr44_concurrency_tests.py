#!/usr/bin/env python3
from pathlib import Path

path = Path('server/tests/test_sqlite_hardening.py')
text = path.read_text(encoding='utf-8')
marker = "\n\nif __name__ == '__main__':\n"
if marker not in text:
    raise RuntimeError('test insertion marker not found')

addition = r'''

class SQLiteRealContentionRegressionTest(unittest.TestCase):
    def test_real_sqlalchemy_session_lock_rolls_back_and_retries(self):
        import sqlite3
        from sqlalchemy import create_engine, text
        from sqlalchemy.orm import Session
        from sqldb_config import retry_on_sqlite_lock

        tmpdir_obj = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir_obj.cleanup)
        db_path = os.path.join(tmpdir_obj.name, 'real-lock.db')
        engine = create_engine(
            f'sqlite:///{db_path}',
            connect_args={'timeout': 0.05},
        )
        with engine.begin() as connection:
            connection.execute(text('CREATE TABLE counter (id INTEGER PRIMARY KEY, value INTEGER NOT NULL)'))
            connection.execute(text('INSERT INTO counter (id, value) VALUES (1, 0)'))

        lock_connection = sqlite3.connect(db_path, timeout=0.05)
        lock_connection.execute('BEGIN EXCLUSIVE')
        lock_connection.execute('UPDATE counter SET value = value WHERE id = 1')

        session = Session(engine)
        events = []

        def rollback_and_release_lock():
            events.append('rollback')
            session.rollback()
            lock_connection.commit()
            lock_connection.close()

        @retry_on_sqlite_lock(
            max_attempts=2,
            backoff_ms=0,
            category='real_session_lock',
            rollback_fn=rollback_and_release_lock,
        )
        def update_counter():
            events.append('attempt')
            session.execute(text('UPDATE counter SET value = value + 1 WHERE id = 1'))
            session.commit()

        try:
            update_counter()
            value = session.execute(text('SELECT value FROM counter WHERE id = 1')).scalar_one()
        finally:
            session.close()
            engine.dispose()

        self.assertEqual(events, ['attempt', 'rollback', 'attempt'])
        self.assertEqual(value, 1)

    def test_concurrent_file_database_startup_is_safe(self):
        import sqlite3
        from flask import Flask
        from flask_sqlalchemy import SQLAlchemy
        from sqldb_config import configure_sqlite_engine_options, install_sqlite_pragmas

        tmpdir_obj = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir_obj.cleanup)
        db_path = os.path.join(tmpdir_obj.name, 'concurrent-startup.db')
        barrier = threading.Barrier(2)
        errors = []

        def initialize_app(index):
            local_db = SQLAlchemy()
            app = Flask(f'concurrent-sqlite-{index}')
            app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
            app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
            try:
                configure_sqlite_engine_options(app)
                local_db.init_app(app)
                barrier.wait()
                install_sqlite_pragmas(local_db, app)
                with app.app_context():
                    local_db.engine.dispose()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=initialize_app, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertFalse(errors, f'Concurrent startup raised errors: {errors}')
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        with sqlite3.connect(db_path) as connection:
            mode = connection.execute('PRAGMA journal_mode').fetchone()[0]
        self.assertEqual(mode.lower(), 'wal')
'''

text = text.replace(marker, addition + marker, 1)
path.write_text(text, encoding='utf-8')
print('Added PR 44 real-contention regression tests.')

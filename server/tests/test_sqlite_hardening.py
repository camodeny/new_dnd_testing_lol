import os
import sys
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from sqlalchemy.exc import OperationalError

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


class SQLiteFileBackedInitTest(unittest.TestCase):
    def test_file_backed_db_has_timeout_and_busy_timeout(self):
        tmpdir_obj = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir_obj.cleanup)
        tmpdir = tmpdir_obj.name
        db_path = os.path.join(tmpdir, 'test.db')

        from flask import Flask
        from models import db

        old_db_url = os.environ.get('DATABASE_URL')
        old_timeout = os.environ.get('DND_SQLITE_BUSY_TIMEOUT_MS')
        os.environ['DATABASE_URL'] = f'sqlite:///{db_path}'
        os.environ['DND_SQLITE_BUSY_TIMEOUT_MS'] = '30000'

        try:
            app = Flask(__name__)
            app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
            app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
            app.secret_key = 'test-key'
            db.init_app(app)

            from sqldb_config import configure_sqlite_engine
            configure_sqlite_engine(db, app)

            with app.app_context():
                db.create_all()
                conn = db.engine.raw_connection()
                cursor = conn.cursor()
                cursor.execute('PRAGMA busy_timeout')
                busy_timeout = cursor.fetchone()[0]
                self.assertGreaterEqual(
                    busy_timeout, 1000,
                    f'PRAGMA busy_timeout should be >= 1000, got {busy_timeout}',
                )

                cursor.execute('PRAGMA journal_mode')
                journal_mode = cursor.fetchone()[0]
                self.assertEqual(
                    journal_mode.lower(), 'wal',
                    f'Expected WAL journal mode, got {journal_mode}',
                )
                cursor.close()
                conn.close()

                db.drop_all()
                db.engine.dispose()
                db._app_engines.pop(app, None)
        finally:
            if old_db_url is not None:
                os.environ['DATABASE_URL'] = old_db_url
            else:
                os.environ.pop('DATABASE_URL', None)
            if old_timeout is not None:
                os.environ['DND_SQLITE_BUSY_TIMEOUT_MS'] = old_timeout
            else:
                os.environ.pop('DND_SQLITE_BUSY_TIMEOUT_MS', None)

    def test_wal_startup_is_idempotent(self):
        tmpdir_obj = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir_obj.cleanup)
        tmpdir = tmpdir_obj.name
        db_path = os.path.join(tmpdir, 'test.db')

        old_db_url = os.environ.get('DATABASE_URL')
        os.environ['DATABASE_URL'] = f'sqlite:///{db_path}'

        try:
            from flask import Flask
            from models import db

            for loop_num in range(3):
                app = Flask(__name__)
                app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
                app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
                app.secret_key = 'test-key'
                db.init_app(app)

                from sqldb_config import configure_sqlite_engine
                configure_sqlite_engine(db, app)

                with app.app_context():
                    db.create_all()
                    conn = db.engine.raw_connection()
                    cursor = conn.cursor()
                    cursor.execute('PRAGMA journal_mode')
                    mode = cursor.fetchone()[0].lower()
                    self.assertEqual(
                        mode, 'wal',
                        f'WAL mode not preserved on startup {loop_num}: got {mode}',
                    )
                    cursor.close()
                    conn.close()
                    db.drop_all()
                    db.engine.dispose()
                db._app_engines.pop(app, None)
        finally:
            if old_db_url is not None:
                os.environ['DATABASE_URL'] = old_db_url
            else:
                os.environ.pop('DATABASE_URL', None)


class SQLiteInMemoryInitTest(unittest.TestCase):
    def test_in_memory_db_skips_wal_safely(self):
        tmpdir_obj = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir_obj.cleanup)
        tmpdir = tmpdir_obj.name

        old_db_url = os.environ.get('DATABASE_URL')
        os.environ['DATABASE_URL'] = 'sqlite:///:memory:'

        try:
            from flask import Flask
            from models import db

            app = Flask(__name__)
            app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
            app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
            app.secret_key = 'test-key'
            app.root_path = tmpdir
            db.init_app(app)

            from sqldb_config import configure_sqlite_engine
            configure_sqlite_engine(db, app)

            with app.app_context():
                db.create_all()

                conn = db.engine.raw_connection()
                cursor = conn.cursor()

                cursor.execute('PRAGMA journal_mode')
                _ = cursor.fetchone()[0].lower()

                cursor.execute('PRAGMA busy_timeout')
                busy = cursor.fetchone()[0]

                cursor.close()
                conn.close()

                self.assertGreaterEqual(busy, 1000)

                db.drop_all()
                db.engine.dispose()
                db._app_engines.pop(app, None)
        finally:
            if old_db_url is not None:
                os.environ['DATABASE_URL'] = old_db_url
            else:
                os.environ.pop('DATABASE_URL', None)


class NonSQLiteConfigTest(unittest.TestCase):
    def test_non_sqlite_engine_skips_pragmas(self):
        from flask import Flask

        app = Flask(__name__)
        app.secret_key = 'test-key'

        from sqldb_config import configure_sqlite_engine

        with patch('sqldb_config._is_sqlite_url', return_value=False):
            mock_db = unittest.mock.Mock()
            configure_sqlite_engine(mock_db, app)
            self.assertNotIn('SQLALCHEMY_ENGINE_OPTIONS', app.config)

    def test_postgres_url_is_not_treated_as_sqlite(self):
        from sqldb_config import _is_sqlite_url, _is_sqlite_file_backed

        self.assertFalse(_is_sqlite_url('postgresql://user:pass@localhost/db'))
        self.assertFalse(_is_sqlite_file_backed('postgresql://user:pass@localhost/db'))

    def test_mysql_url_is_not_treated_as_sqlite(self):
        from sqldb_config import _is_sqlite_url

        self.assertFalse(_is_sqlite_url('mysql://user:pass@localhost/db'))


class ApiKeyUsageLockToleranceTest(unittest.TestCase):
    def setUp(self):
        os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
        from app import app
        self.app = app
        self.ctx = app.app_context()
        self.ctx.push()
        from models import db
        db.create_all()

    def tearDown(self):
        from models import db
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_touch_api_key_under_lock_does_not_fail_auth(self):
        from auth import utcnow
        now = utcnow()
        from types import SimpleNamespace
        key = SimpleNamespace(last_used_at=None)

        from auth import _touch_api_key_last_used

        with (
            patch('auth.utcnow', return_value=now),
            patch('auth.db.session.commit', side_effect=OperationalError(
                'database is locked', {}, None,
            )),
            patch('auth.db.session.rollback') as rollback,
        ):
            _touch_api_key_last_used(key)

        rollback.assert_called_once_with()
        self.assertEqual(key.last_used_at, now)

    def test_repeated_key_usage_inside_coalescing_interval_skips_write(self):
        from auth import utcnow
        now = utcnow()
        from types import SimpleNamespace
        key = SimpleNamespace(last_used_at=now - timedelta(seconds=60))

        from auth import _touch_api_key_last_used

        with (
            patch('auth.utcnow', return_value=now),
            patch('auth.db.session.commit') as commit,
        ):
            _touch_api_key_last_used(key)

        commit.assert_not_called()
        self.assertEqual(key.last_used_at, now - timedelta(seconds=60))


class AutomationLockRetryTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir_obj = tempfile.TemporaryDirectory()
        self.tmpdir = self.tmpdir_obj.name
        self.db_path = os.path.join(self.tmpdir, 'test.db')

        self.old_db_url = os.environ.get('DATABASE_URL')
        os.environ['DATABASE_URL'] = f'sqlite:///{self.db_path}'

        from flask import Flask
        from models import db

        self.app = Flask(__name__)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{self.db_path}'
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        self.app.secret_key = 'test-key'
        self.app.config['TESTING'] = True
        db.init_app(self.app)

        from sqldb_config import configure_sqlite_engine
        configure_sqlite_engine(db, self.app)

        with self.app.app_context():
            db.create_all()
            from models import (
                AutomationRun, AutomationScenario, AutomationSnapshot,
                Campaign, User,
            )

            owner = User(username='owner', email='owner@test.com')
            owner.set_password('password')
            db.session.add(owner)
            db.session.flush()

            campaign = Campaign(name='test-campaign', user_id=owner.id)
            db.session.add(campaign)
            db.session.flush()

            scenario = AutomationScenario(
                user_id=owner.id,
                source_campaign_id=campaign.id,
                name='test-scenario',
            )
            db.session.add(scenario)
            db.session.flush()

            snapshot = AutomationSnapshot(
                scenario_id=scenario.id,
                source_campaign_id=campaign.id,
                label='test-snapshot',
            )
            db.session.add(snapshot)
            db.session.flush()

            run = AutomationRun(
                user_id=owner.id,
                scenario_id=scenario.id,
                snapshot_id=snapshot.id,
                status='claimed',
                worker_id='test-worker',
                lease_token='test-token',
                lease_expires_at=__import__('time_utils').utcnow() + timedelta(hours=1),
            )
            db.session.add(run)
            db.session.commit()
            self.run_id = run.id

    def tearDown(self):
        from models import db
        with self.app.app_context():
            db.drop_all()
            db.engine.dispose()
            db._app_engines.pop(self.app, None)
        self.tmpdir_obj.cleanup()
        if self.old_db_url is not None:
            os.environ['DATABASE_URL'] = self.old_db_url
        else:
            os.environ.pop('DATABASE_URL', None)

    def test_heartbeat_tolerates_lock_and_retries(self):
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

            with patch.object(db.session, 'commit', side_effect=flaky_commit),                     patch.object(db.session, 'rollback', wraps=db.session.rollback) as rollback:
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

    def test_worker_activity_retries_on_lock(self):
        from sqldb_config import retry_on_sqlite_lock

        call_count = [0]

        def fake_operation():
            call_count[0] += 1
            if call_count[0] <= 2:
                raise OperationalError('database is locked', {}, None)
            return True

        decorated = retry_on_sqlite_lock(max_attempts=3, backoff_ms=10, category='test')(
            fake_operation
        )
        result = decorated()
        self.assertTrue(result)
        self.assertGreater(call_count[0], 1, 'Should have retried')

    def test_worker_activity_lock_exhaustion_bounded(self):
        from sqldb_config import retry_on_sqlite_lock

        call_count = [0]

        def always_fails():
            call_count[0] += 1
            raise OperationalError('database is locked', {}, None)

        decorated = retry_on_sqlite_lock(max_attempts=3, backoff_ms=10, category='test')(
            always_fails
        )
        with self.assertRaises(OperationalError):
            decorated()

        self.assertEqual(call_count[0], 3, 'Should have tried max_attempts times')

    def test_worker_activity_logs_lock_retry(self):
        from services.automation_service import record_worker_activity
        from models import db

        with self.app.app_context():
            call_count = [0]

            def fake_commit():
                call_count[0] += 1
                if call_count[0] == 1:
                    raise OperationalError('database is locked', {}, None)
                return

            with patch.object(db.session, 'commit', side_effect=fake_commit), \
                 patch('sqldb_config.logger.warning') as warning_log:
                record_worker_activity('test-worker')

            warning_log.assert_called()
            log_args = str(warning_log.call_args)
            self.assertIn('SQLite lock contention', log_args)


class ContentionTwoConnectionTest(unittest.TestCase):
    def test_two_connections_contending_worker_poll_does_not_crash(self):
        tmpdir_obj = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir_obj.cleanup)
        tmpdir = tmpdir_obj.name
        db_path = os.path.join(tmpdir, 'test.db')

        old_db_url = os.environ.get('DATABASE_URL')
        os.environ['DATABASE_URL'] = f'sqlite:///{db_path}'

        try:
            from flask import Flask
            from models import db

            app = Flask(__name__)
            app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
            app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
            app.secret_key = 'test-key'
            db.init_app(app)

            from sqldb_config import configure_sqlite_engine
            configure_sqlite_engine(db, app)

            with app.app_context():
                db.create_all()
                from models import User

                owner = User(username='owner', email='owner@test.com')
                owner.set_password('password')
                db.session.add(owner)
                db.session.commit()

            results = {}
            barrier = threading.Barrier(2)
            errors = []

            def worker_poll(worker_id):
                try:
                    with app.app_context():
                        from services.automation_service import record_worker_activity
                        barrier.wait()
                        for _ in range(10):
                            record_worker_activity(worker_id)
                        results[worker_id] = True
                except Exception as e:
                    errors.append((worker_id, str(e)))

            threads = [
                threading.Thread(target=worker_poll, args=(f'worker-{i}',))
                for i in range(2)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(
                len(errors), 0,
                f'Worker poll errors occurred: {errors}',
            )
            self.assertEqual(
                len(results), 2,
                f'Expected 2 workers to complete, got {len(results)}: {results}',
            )

            with app.app_context():
                db.drop_all()
                db.engine.dispose()
                db._app_engines.pop(app, None)
        finally:
            if old_db_url is not None:
                os.environ['DATABASE_URL'] = old_db_url
            else:
                os.environ.pop('DATABASE_URL', None)


class RetryExhaustionTest(unittest.TestCase):
    def test_retry_exhaustion_raises_last_error(self):
        from sqldb_config import retry_on_sqlite_lock

        first_error = OperationalError('database is locked', {}, None)

        call_count = [0]

        def always_locked():
            call_count[0] += 1
            raise first_error

        decorated = retry_on_sqlite_lock(max_attempts=3, backoff_ms=10, category='test')(
            always_locked
        )

        with self.assertRaises(OperationalError) as ctx:
            decorated()

        self.assertIs(ctx.exception, first_error)
        self.assertEqual(call_count[0], 3)

    def test_retry_does_not_intercept_non_lock_errors(self):
        from sqldb_config import retry_on_sqlite_lock

        first_error = OperationalError(
            'UNIQUE constraint failed: users.username', {}, None,
        )
        call_count = [0]

        def integrity_error():
            call_count[0] += 1
            raise first_error

        decorated = retry_on_sqlite_lock(max_attempts=3, backoff_ms=10, category='test')(
            integrity_error
        )

        with self.assertRaises(OperationalError) as ctx:
            decorated()

        self.assertIs(ctx.exception, first_error)
        self.assertEqual(call_count[0], 1, 'Should not retry non-lock errors')

    def test_retry_logs_exhaustion(self):
        from sqldb_config import retry_on_sqlite_lock

        def always_locked():
            raise OperationalError('database is locked', {}, None)

        with patch('sqldb_config.logger.error') as error_log:
            decorated = retry_on_sqlite_lock(max_attempts=2, backoff_ms=10, category='test')(
                always_locked
            )
            try:
                decorated()
            except OperationalError:
                pass

        error_log.assert_called_once()
        log_str = str(error_log.call_args)
        self.assertIn('outcome=failed', log_str)
        self.assertIn('SQLite lock exhaustion', log_str)


class LeaseFencingUnderRetryTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir_obj = tempfile.TemporaryDirectory()
        self.tmpdir = self.tmpdir_obj.name
        self.db_path = os.path.join(self.tmpdir, 'test.db')

        self.old_db_url = os.environ.get('DATABASE_URL')
        os.environ['DATABASE_URL'] = f'sqlite:///{self.db_path}'

        from flask import Flask
        from models import db

        self.app = Flask(__name__)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{self.db_path}'
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        self.app.secret_key = 'test-key'
        db.init_app(self.app)

        from sqldb_config import configure_sqlite_engine
        configure_sqlite_engine(db, self.app)

        with self.app.app_context():
            db.create_all()
            from models import (
                AutomationRun, AutomationScenario, AutomationSnapshot,
                Campaign, User,
            )
            from time_utils import utcnow

            owner = User(username='owner', email='owner@test.com')
            owner.set_password('password')
            db.session.add(owner)
            db.session.flush()

            campaign = Campaign(name='test-campaign', user_id=owner.id)
            db.session.add(campaign)
            db.session.flush()

            scenario = AutomationScenario(
                user_id=owner.id,
                source_campaign_id=campaign.id,
                name='test-scenario',
            )
            db.session.add(scenario)
            db.session.flush()

            snapshot = AutomationSnapshot(
                scenario_id=scenario.id,
                source_campaign_id=campaign.id,
                label='test-snapshot',
            )
            db.session.add(snapshot)
            db.session.flush()

            run = AutomationRun(
                user_id=owner.id,
                scenario_id=scenario.id,
                snapshot_id=snapshot.id,
                status='claimed',
                worker_id='test-worker',
                lease_token='valid-token',
                lease_expires_at=utcnow() + timedelta(hours=1),
                heartbeat_at=utcnow(),
            )
            db.session.add(run)
            db.session.commit()
            self.run_id = run.id

    def tearDown(self):
        from models import db
        with self.app.app_context():
            db.drop_all()
            db.engine.dispose()
            db._app_engines.pop(self.app, None)
        self.tmpdir_obj.cleanup()
        if self.old_db_url is not None:
            os.environ['DATABASE_URL'] = self.old_db_url
        else:
            os.environ.pop('DATABASE_URL', None)

    def test_heartbeat_with_wrong_lease_token_is_rejected_not_retried(self):
        from models import db
        from services.automation_service import heartbeat_run

        with self.app.app_context():
            from models import AutomationRun
            run = db.session.get(AutomationRun, self.run_id)

            with self.assertRaises(ValueError) as ctx:
                heartbeat_run(
                    run,
                    worker_id='test-worker',
                    lease_token='wrong-token',
                )

            self.assertIn('lease token', str(ctx.exception).lower())

    def test_heartbeat_then_status_read_under_lock_is_consistent(self):
        from models import db
        import threading
        from services.automation_service import heartbeat_run

        errors = []
        barrier = threading.Barrier(2)

        def heartbeat_thread():
            with self.app.app_context():
                from models import AutomationRun
                run = db.session.get(AutomationRun, self.run_id)
                barrier.wait()
                for _ in range(5):
                    try:
                        heartbeat_run(
                            run,
                            worker_id='test-worker',
                            lease_token='valid-token',
                            lease_seconds=45,
                        )
                    except Exception as e:
                        errors.append(('heartbeat', str(e)))

        def status_read_thread():
            with self.app.app_context():
                from models import AutomationRun
                barrier.wait()
                for _ in range(5):
                    try:
                        run = db.session.get(AutomationRun, self.run_id)
                        _ = run.status
                        _ = run.worker_id
                    except Exception as e:
                        errors.append(('status_read', str(e)))

        threads = [
            threading.Thread(target=heartbeat_thread),
            threading.Thread(target=status_read_thread),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f'Concurrent operations failed: {errors}')

        with self.app.app_context():
            from models import AutomationRun
            run = db.session.get(AutomationRun, self.run_id)
            self.assertEqual(run.worker_id, 'test-worker')
            self.assertEqual(run.lease_token, 'valid-token')
            self.assertIsNotNone(run.heartbeat_at)


class BusyTimeoutConfigurableTest(unittest.TestCase):
    def test_custom_busy_timeout_via_env(self):
        tmpdir_obj = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir_obj.cleanup)
        tmpdir = tmpdir_obj.name
        db_path = os.path.join(tmpdir, 'test.db')

        old_db_url = os.environ.get('DATABASE_URL')
        old_timeout = os.environ.get('DND_SQLITE_BUSY_TIMEOUT_MS')
        os.environ['DATABASE_URL'] = f'sqlite:///{db_path}'
        os.environ['DND_SQLITE_BUSY_TIMEOUT_MS'] = '15000'

        try:
            from flask import Flask
            from models import db

            app = Flask(__name__)
            app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
            app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
            app.secret_key = 'test-key'
            db.init_app(app)

            from sqldb_config import configure_sqlite_engine
            configure_sqlite_engine(db, app)

            with app.app_context():
                db.create_all()
                conn = db.engine.raw_connection()
                cursor = conn.cursor()
                cursor.execute('PRAGMA busy_timeout')
                busy = cursor.fetchone()[0]
                cursor.close()
                conn.close()

                self.assertEqual(
                    busy, 15000,
                    f'expected busy_timeout=15000, got {busy}',
                )

                db.drop_all()
                db.engine.dispose()
                db._app_engines.pop(app, None)
        finally:
            if old_db_url is not None:
                os.environ['DATABASE_URL'] = old_db_url
            else:
                os.environ.pop('DATABASE_URL', None)
            if old_timeout is not None:
                os.environ['DND_SQLITE_BUSY_TIMEOUT_MS'] = old_timeout
            else:
                os.environ.pop('DND_SQLITE_BUSY_TIMEOUT_MS', None)


class TransactionBoundaryTest(unittest.TestCase):
    def test_record_worker_activity_commits_in_isolation(self):
        tmpdir_obj = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir_obj.cleanup)
        tmpdir = tmpdir_obj.name
        db_path = os.path.join(tmpdir, 'test.db')

        old_db_url = os.environ.get('DATABASE_URL')
        os.environ['DATABASE_URL'] = f'sqlite:///{db_path}'

        try:
            from flask import Flask
            from models import db

            app = Flask(__name__)
            app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
            app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
            app.secret_key = 'test-key'
            db.init_app(app)

            from sqldb_config import configure_sqlite_engine
            configure_sqlite_engine(db, app)

            with app.app_context():
                db.create_all()
                from services.automation_service import record_worker_activity
                from models import AutomationWorker

                record_worker_activity('worker-test', api_base='http://test')
                worker = AutomationWorker.query.filter_by(worker_id='worker-test').first()
                self.assertIsNotNone(worker)
                self.assertEqual(worker.api_base, 'http://test')

                record_worker_activity('worker-test', is_heartbeat=True)
                worker = AutomationWorker.query.filter_by(worker_id='worker-test').first()
                self.assertIsNotNone(worker.last_heartbeat_at)

                db.drop_all()
                db.engine.dispose()
                db._app_engines.pop(app, None)
        finally:
            if old_db_url is not None:
                os.environ['DATABASE_URL'] = old_db_url
            else:
                os.environ.pop('DATABASE_URL', None)


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


if __name__ == '__main__':
    unittest.main()

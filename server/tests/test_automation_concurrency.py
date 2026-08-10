import json
import os
import sys
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ['DATABASE_URL'] = 'sqlite:///:memory:'

from app import app
from auth import generate_token
from time_utils import utcnow
from models import (
    AutomationRun,
    AutomationRunEvent,
    AutomationSnapshot,
    AutomationScenario,
    Campaign,
    CampaignMember,
    CampaignSession,
    CampaignWorld,
    Character,
    SessionMessage,
    User,
    db,
)
from services.automation_service import (
    AUTOMATION_SNAPSHOT_SCHEMA_VERSION,
    CloneRetrievalPreflightError,
    InfrastructureFailureThresholdError,
    claim_run_for_worker,
    create_snapshot_for_scenario,
    ensure_worker_lease,
    max_reclaim_failures_for_run,
    record_worker_infrastructure_failure,
    reserve_run_lease,
    release_run_lease,
)
from services.automation_service import lease_is_expired
from services.character_service import update_character_relations


class _AutomationConcurrencyBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_root_path = app.root_path
        app.root_path = self.temp_dir.name

        with app.app_context():
            db.drop_all()
            db.create_all()

            owner = User(username='owner', email='owner@example.com')
            owner.set_password('password')
            db.session.add(owner)
            db.session.flush()
            self.owner_id = owner.id

            campaign = Campaign(name='Concurrency Campaign', user_id=owner.id)
            db.session.add(campaign)
            db.session.flush()
            self.campaign_id = campaign.id

            character = Character(
                user_id=owner.id,
                campaign_id=campaign.id,
                name='Test Character',
                race='Human',
                background='Soldier',
            )
            db.session.add(character)
            db.session.flush()
            update_character_relations(character, {'classes': [{'class_name': 'Fighter', 'level': 3}]})

            db.session.add(CampaignMember(
                campaign_id=campaign.id,
                user_id=owner.id,
                role='player',
                selected_character_id=character.id,
            ))

            session = CampaignSession(campaign_id=campaign.id, is_active=True)
            db.session.add(session)
            db.session.flush()

            db.session.add(SessionMessage(session_id=session.id, user_id=owner.id, role='player', content='I look around.'))
            db.session.add(SessionMessage(session_id=session.id, role='dm', content='The room is empty save for a dusty rug.'))

            world = CampaignWorld(
                campaign_id=campaign.id,
                public_intro=json.dumps({}),
                world_state=json.dumps({'location': 'foyer'}),
                knowledge_graph=json.dumps({'entities': [], 'relations': [], 'facts': []}),
                dm_private=json.dumps({}),
            )
            db.session.add(world)

            db.session.commit()

        self.client = app.test_client()
        with app.app_context():
            token_bytes = generate_token(self.owner_id)
            self.token = token_bytes.decode('utf-8') if isinstance(token_bytes, bytes) else token_bytes
        self.headers = {'Authorization': f'Bearer {self.token}'}

    def tearDown(self):
        app.root_path = self.old_root_path
        with app.app_context():
            db.session.remove()
        self.temp_dir.cleanup()

    def _create_scenario_and_run(self):
        with app.app_context():
            scenario = AutomationScenario(
                name='Concurrency Scenario',
                source_campaign_id=self.campaign_id,
                user_id=self.owner_id,
                roster_json=[{
                    'user_id': self.owner_id,
                    'character_id': 1,
                    'character_name': 'Test Character',
                    'label': 'Actor',
                }],
            )
            db.session.add(scenario)
            db.session.flush()
            scenario_id = scenario.id

            create_snapshot_for_scenario(scenario, label='Concurrency Snapshot')

            snapshot = AutomationSnapshot.query.filter_by(scenario_id=scenario_id).order_by(AutomationSnapshot.id.desc()).first()
            run = AutomationRun(
                scenario_id=scenario.id,
                snapshot_id=snapshot.id,
                user_id=self.owner_id,
                status='queued',
                runner_config_json={},
            )
            db.session.add(run)
            db.session.flush()
            run_id = run.id
            db.session.commit()
            return run_id


class AutomationConcurrencyTest(_AutomationConcurrencyBase):

    # ── Atomic-claim behaviour tests ──────────────────────────────

    def test_atomic_claim_first_wins_second_gets_409(self):
        run_id = self._create_scenario_and_run()
        with app.app_context():
            run_a = db.session.get(AutomationRun, run_id)
            result_a = claim_run_for_worker(run_a, 'worker-a')
            self.assertEqual(result_a['run'].worker_id, 'worker-a')

            run_b = db.session.get(AutomationRun, run_id)
            with self.assertRaises(ValueError):
                claim_run_for_worker(run_b, 'worker-b')

    def test_same_worker_id_cannot_rotate_live_lease(self):
        run_id = self._create_scenario_and_run()
        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            first = claim_run_for_worker(run, 'worker-a')
            first_token = first['run'].lease_token

            run = db.session.get(AutomationRun, run_id)
            with self.assertRaises(ValueError):
                claim_run_for_worker(run, 'worker-a')

            run = db.session.get(AutomationRun, run_id)
            self.assertEqual(run.lease_token, first_token)

    def test_expired_same_worker_can_reclaim(self):
        run_id = self._create_scenario_and_run()
        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            claim_run_for_worker(run, 'worker-a')

            run = db.session.get(AutomationRun, run_id)
            run.lease_expires_at = utcnow() - timedelta(seconds=5)
            db.session.commit()

            run = db.session.get(AutomationRun, run_id)
            result = claim_run_for_worker(run, 'worker-a')

            self.assertEqual(result['run'].attempt_count, 2)
            self.assertTrue(result['reclaimed'])

    def test_expired_lease_reclaimed_exactly_once_sequential(self):
        run_id = self._create_scenario_and_run()
        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            claim_run_for_worker(run, 'worker-a')

            run = db.session.get(AutomationRun, run_id)
            run.status = 'running'
            run.lease_expires_at = utcnow() - timedelta(seconds=5)
            db.session.commit()

            run = db.session.get(AutomationRun, run_id)
            winner = claim_run_for_worker(run, 'worker-b')

            self.assertTrue(winner['reclaimed'])
            self.assertEqual(winner['run'].reclaim_count, 1)
            self.assertEqual(winner['run'].attempt_count, 2)
            self.assertEqual(winner['run'].worker_id, 'worker-b')

            run = db.session.get(AutomationRun, run_id)
            with self.assertRaises(ValueError):
                claim_run_for_worker(run, 'worker-c')

    def test_multiple_workers_consume_distinct_queued_runs(self):
        run_ids = [self._create_scenario_and_run() for _ in range(3)]
        with app.app_context():
            workers = {}
            for i, run_id in enumerate(run_ids):
                wid = f'worker-{i}'
                run = db.session.get(AutomationRun, run_id)
                result = claim_run_for_worker(run, wid)
                workers[wid] = result['run'].id

            self.assertEqual(len(set(workers.values())), 3)
            self.assertEqual(set(workers.values()), set(run_ids))

            for run_id in run_ids:
                run = db.session.get(AutomationRun, run_id)
                self.assertEqual(run.status, 'claimed')
                self.assertIsNotNone(run.worker_id)
                self.assertIsNotNone(run.lease_token)

    def test_stale_cleanup_cannot_clear_newer_lease(self):
        run_id = self._create_scenario_and_run()
        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            first = claim_run_for_worker(run, 'worker-a')
            first_token = first['run'].lease_token

            run = db.session.get(AutomationRun, run_id)
            run.lease_expires_at = utcnow() - timedelta(seconds=5)
            db.session.commit()

            run = db.session.get(AutomationRun, run_id)
            second = claim_run_for_worker(run, 'worker-b')
            second_token = second['run'].lease_token

            released = release_run_lease(run_id, first_token, 'stale cleanup')
            self.assertFalse(released)

            run = db.session.get(AutomationRun, run_id)
            self.assertEqual(run.worker_id, 'worker-b')
            self.assertEqual(run.lease_token, second_token)

    def test_reserve_release_idempotent_for_stale_token(self):
        run_id = self._create_scenario_and_run()
        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            result = claim_run_for_worker(run, 'worker-a')
            token_a = result['run'].lease_token

            run = db.session.get(AutomationRun, run_id)
            run.lease_expires_at = utcnow() - timedelta(seconds=5)
            db.session.commit()

            run = db.session.get(AutomationRun, run_id)
            claim_run_for_worker(run, 'worker-b')

            released = release_run_lease(run_id, token_a, 'belated cleanup')
            self.assertFalse(released)

            run = db.session.get(AutomationRun, run_id)
            self.assertEqual(run.worker_id, 'worker-b')

    def test_reserve_run_lease_rejects_non_claimable_status(self):
        run_id = self._create_scenario_and_run()
        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            run.status = 'completed'
            db.session.commit()

            with self.assertRaises(ValueError):
                reserve_run_lease(run_id, 'worker-a', utcnow())

    def test_claim_normalizes_lease_to_runtime_value(self):
        run_id = self._create_scenario_and_run()
        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            result = claim_run_for_worker(run, 'worker-a')
            claimed = result['run']
            self.assertEqual(claimed.status, 'claimed')
            self.assertIsNotNone(claimed.lease_expires_at)
            self.assertIsNotNone(claimed.heartbeat_at)

    def test_claim_failure_releases_lease_and_preserves_failure_reason(self):
        run_id = self._create_scenario_and_run()
        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            snapshot = db.session.get(AutomationSnapshot, run.snapshot_id)
            snapshot.snapshot_json = {'missing': 'data'}
            db.session.commit()

            with self.assertRaises(CloneRetrievalPreflightError):
                claim_run_for_worker(run, 'worker-a')

            run = db.session.get(AutomationRun, run_id)
            self.assertEqual(run.status, 'queued')
            self.assertIsNone(run.worker_id)
            self.assertIsNone(run.lease_token)
            self.assertIsNone(run.heartbeat_at)
            self.assertIsNone(run.lease_expires_at)
            self.assertIsNotNone(run.claim_failure_reason)

    # ── Token safety tests ──────────────────────────────────────────

    def test_ensure_worker_lease_requires_both_credentials(self):
        with app.app_context():
            run = AutomationRun(
                status='running',
                worker_id='w',
                lease_token='tok',
                lease_expires_at=utcnow() + timedelta(hours=1),
            )
            with self.assertRaisesRegex(ValueError, 'worker_id'):
                ensure_worker_lease(run, worker_id=None, lease_token='tok')
            with self.assertRaisesRegex(ValueError, 'lease_token'):
                ensure_worker_lease(run, worker_id='w', lease_token=None)
            with self.assertRaisesRegex(ValueError, 'worker_id'):
                ensure_worker_lease(run, worker_id=None, lease_token=None)
            ensure_worker_lease(run, worker_id='w', lease_token='tok')

    def test_ensure_worker_lease_skips_credential_check_for_queued(self):
        with app.app_context():
            run = AutomationRun(status='queued')
            ensure_worker_lease(run, worker_id=None, lease_token=None)

    def test_ensure_worker_lease_rejects_wrong_worker_id(self):
        with app.app_context():
            run = AutomationRun(
                status='running',
                worker_id='worker-a',
                lease_token='tok',
                lease_expires_at=utcnow() + timedelta(hours=1),
            )
            with self.assertRaisesRegex(ValueError, 'leased by another worker'):
                ensure_worker_lease(run, worker_id='worker-b', lease_token='tok')

    def test_ensure_worker_lease_rejects_wrong_lease_token(self):
        with app.app_context():
            run = AutomationRun(
                status='running',
                worker_id='worker-a',
                lease_token='tok-a',
                lease_expires_at=utcnow() + timedelta(hours=1),
            )
            with self.assertRaisesRegex(ValueError, 'lease token'):
                ensure_worker_lease(run, worker_id='worker-a', lease_token='tok-b')

    # ── Lease-timestamp regression tests ───────────────────────────

    def test_lease_refresh_after_materialization_uses_fresh_timestamp(self):
        run_id = self._create_scenario_and_run()
        stale_time = utcnow() - timedelta(seconds=120)
        fresh_time = utcnow()
        call_log = []

        def fake_now():
            call_log.append(len(call_log))
            return stale_time if len(call_log) == 1 else fresh_time

        with patch('services.automation_service._utcnow', side_effect=fake_now):
            with app.app_context():
                run = db.session.get(AutomationRun, run_id)
                result = claim_run_for_worker(run, 'worker-a')
                expires = result['run'].lease_expires_at
                # The runtime lease expiry must be based on the fresh timestamp
                self.assertGreater(
                    expires,
                    stale_time + timedelta(seconds=45),
                )

    def test_long_materialization_does_not_truncate_runtime_lease(self):
        run_id = self._create_scenario_and_run()
        start = utcnow()
        end = start + timedelta(seconds=120)
        counter = [0]

        def fake_now():
            counter[0] += 1
            return start if counter[0] == 1 else end

        with patch('services.automation_service._utcnow', side_effect=fake_now):
            with app.app_context():
                run = db.session.get(AutomationRun, run_id)
                result = claim_run_for_worker(run, 'worker-a')
                expires = result['run'].lease_expires_at
                expected = end + timedelta(seconds=45)
                self.assertAlmostEqual(expires.timestamp(), expected.timestamp(), delta=2)

    # ── Lease-lost regression tests ────────────────────────────────

    def test_lease_lost_after_expiry_does_not_commit_clone(self):
        """A-expires / B-reclaims / A-finishes-late: A's stale lease update fails."""
        run_id = self._create_scenario_and_run()
        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            a_token = claim_run_for_worker(run, 'worker-a')['run'].lease_token

            run = db.session.get(AutomationRun, run_id)
            run.lease_expires_at = utcnow() - timedelta(seconds=5)
            db.session.commit()

            run = db.session.get(AutomationRun, run_id)
            b_result = claim_run_for_worker(run, 'worker-b')
            b_token = b_result['run'].lease_token

            # A tries a stale lease update — must affect 0 rows
            from sqlalchemy import update as sa_update
            stmt = (
                sa_update(AutomationRun)
                .where(
                    AutomationRun.id == run_id,
                    AutomationRun.lease_token == a_token,
                )
                .values(status='claimed')
            )
            result = db.session.execute(stmt)
            self.assertEqual(result.rowcount, 0)

            run = db.session.get(AutomationRun, run_id)
            self.assertEqual(run.worker_id, 'worker-b')
            self.assertEqual(run.lease_token, b_token)

            clones_for_run = Campaign.query.filter_by(
                is_automation_clone=True,
                automation_source_run_id=run_id,
            ).all()
            self.assertEqual(len(clones_for_run), 1)



# ── Concurrent contention tests (file-backed SQLite, threading barrier) ─────

class AutomationConcurrentClaimTest(unittest.TestCase):
    """True concurrent tests with separate app contexts/sessions and file-backed SQLite."""

    def _setup_file_backed_app(self):
        test_tmpdir = tempfile.mkdtemp()
        db_path = os.path.join(test_tmpdir, 'concurrent.db')
        from flask import Flask
        test_app = Flask(__name__)
        test_app.config.update(app.config)
        test_app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}?timeout=30'
        test_app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        test_app.secret_key = 'test-key'
        test_app.root_path = test_tmpdir
        db.init_app(test_app)

        from auth import auth_bp
        from routes.automation import automation_bp
        test_app.register_blueprint(auth_bp)
        test_app.register_blueprint(automation_bp)

        return test_app, test_tmpdir

    def _setup_concurrent_data(self, test_app):
        with test_app.app_context():
            db.create_all()
            owner = User(username='owner', email='owner@example.com')
            owner.set_password('password')
            db.session.add(owner)
            db.session.flush()
            owner_id = owner.id

            campaign = Campaign(name='Concurrent Campaign', user_id=owner.id)
            db.session.add(campaign)
            db.session.flush()
            campaign_id = campaign.id

            character = Character(
                user_id=owner.id, campaign_id=campaign.id,
                name='Test', race='Human', background='Soldier',
            )
            db.session.add(character)
            db.session.flush()
            update_character_relations(character, {'classes': [{'class_name': 'Fighter', 'level': 3}]})

            db.session.add(CampaignMember(
                campaign_id=campaign.id, user_id=owner.id,
                role='player', selected_character_id=character.id,
            ))
            session = CampaignSession(campaign_id=campaign.id, is_active=True)
            db.session.add(session)
            db.session.flush()
            db.session.add(SessionMessage(session_id=session.id, user_id=owner.id, role='player', content='I look around.'))
            db.session.add(SessionMessage(session_id=session.id, role='dm', content='The room is empty.'))

            world = CampaignWorld(
                campaign_id=campaign.id,
                public_intro=json.dumps({}),
                world_state=json.dumps({'location': 'foyer'}),
                knowledge_graph=json.dumps({'entities': [], 'relations': [], 'facts': []}),
                dm_private=json.dumps({}),
            )
            db.session.add(world)
            db.session.commit()

            scenario = AutomationScenario(
                name='Concurrent Scenario',
                source_campaign_id=campaign_id,
                user_id=owner_id,
                roster_json=[{
                    'user_id': owner_id, 'character_id': character.id,
                    'character_name': 'Test', 'label': 'Actor',
                }],
            )
            db.session.add(scenario)
            db.session.flush()
            scenario_id = scenario.id
            create_snapshot_for_scenario(scenario, label='Concurrent Snapshot')
            snapshot = AutomationSnapshot.query.filter_by(scenario_id=scenario_id).order_by(AutomationSnapshot.id.desc()).first()
            run = AutomationRun(
                scenario_id=scenario_id, snapshot_id=snapshot.id,
                user_id=owner_id, status='queued', runner_config_json={},
            )
            db.session.add(run)
            db.session.commit()
            return run.id, owner_id

    def _teardown_file_backed_app(self, test_app, test_tmpdir):
        with test_app.app_context():
            db.drop_all()
        engine_dict = db._app_engines.get(test_app)
        if engine_dict:
            for engine in engine_dict.values():
                engine.dispose()
            engine_dict.clear()
        db._app_engines.pop(test_app, None)
        import shutil
        shutil.rmtree(test_tmpdir, ignore_errors=True)

    def test_concurrent_queued_claim_exactly_one_wins(self):
        test_app, tmpdir = self._setup_file_backed_app()
        self.addCleanup(lambda: self._teardown_file_backed_app(test_app, tmpdir))
        run_id, _ = self._setup_concurrent_data(test_app)

        import threading
        barrier = threading.Barrier(2)
        results = []

        def _claim(worker_id):
            with test_app.app_context():
                run = db.session.get(AutomationRun, run_id)
                barrier.wait()
                try:
                    result = claim_run_for_worker(run, worker_id)
                    results.append(('success', worker_id, result['run'].lease_token))
                except ValueError as exc:
                    results.append(('fail', worker_id, str(exc)))

        threads = [threading.Thread(target=_claim, args=(f'worker-{i}',)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = [r for r in results if r[0] == 'success']
        failures = [r for r in results if r[0] == 'fail']
        self.assertEqual(len(successes), 1, f'Expected 1 winner, got {len(successes)}: {results}')
        self.assertEqual(len(failures), 1, f'Expected 1 loser, got {len(failures)}: {results}')

        with test_app.app_context():
            run = db.session.get(AutomationRun, run_id)
            self.assertEqual(run.status, 'claimed')
            self.assertIsNotNone(run.worker_id)
            self.assertIsNotNone(run.lease_token)
            clones = Campaign.query.filter_by(
                is_automation_clone=True, automation_source_run_id=run_id,
            ).all()
            self.assertEqual(len(clones), 1, 'Exactly one automation clone should exist')

    def test_concurrent_expired_reclaim_exactly_one_wins(self):
        test_app, tmpdir = self._setup_file_backed_app()
        self.addCleanup(lambda: self._teardown_file_backed_app(test_app, tmpdir))
        run_id, _ = self._setup_concurrent_data(test_app)

        with test_app.app_context():
            run = db.session.get(AutomationRun, run_id)
            claim_run_for_worker(run, 'worker-a')
            run = db.session.get(AutomationRun, run_id)
            run.lease_expires_at = utcnow() - timedelta(seconds=5)
            db.session.commit()

        import threading
        barrier = threading.Barrier(2)
        results = []

        def _reclaim(worker_id):
            with test_app.app_context():
                run = db.session.get(AutomationRun, run_id)
                barrier.wait()
                try:
                    result = claim_run_for_worker(run, worker_id)
                    results.append(('success', worker_id, result['run'].lease_token, result.get('reclaimed')))
                except ValueError as exc:
                    results.append(('fail', worker_id, str(exc)))

        threads = [threading.Thread(target=_reclaim, args=(f'worker-b-{i}',)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = [r for r in results if r[0] == 'success']
        failures = [r for r in results if r[0] == 'fail']
        self.assertEqual(len(successes), 1, f'Expected 1 winner, got {len(successes)}: {results}')
        self.assertEqual(len(failures), 1, f'Expected 1 loser, got {len(failures)}: {results}')
        self.assertTrue(successes[0][3], 'Winner must be a reclaim')

    def test_concurrent_completion_exactly_one_wins(self):
        test_app, tmpdir = self._setup_file_backed_app()
        self.addCleanup(lambda: self._teardown_file_backed_app(test_app, tmpdir))
        run_id, owner_id = self._setup_concurrent_data(test_app)

        with test_app.app_context():
            run = db.session.get(AutomationRun, run_id)
            claim_run_for_worker(run, 'worker-a')
            run = db.session.get(AutomationRun, run_id)
            lease_token = run.lease_token

        import threading
        barrier = threading.Barrier(2)
        results = []

        def _complete(worker_id):
            with test_app.app_context():
                token_bytes = generate_token(owner_id)
                token = token_bytes.decode('utf-8') if isinstance(token_bytes, bytes) else token_bytes

            with test_app.test_client() as client:
                headers = {'Authorization': f'Bearer {token}'}
                barrier.wait()
                try:
                    resp = client.post(
                        f'/api/automation/runs/{run_id}/complete',
                        headers=headers,
                        json={
                            'worker_id': 'worker-a',
                            'lease_token': lease_token,
                            'status': 'completed',
                            'dedupe_key': f'run_completed:{run_id}:attempt:{worker_id}',
                        }
                    )
                    results.append((resp.status_code, worker_id))
                except Exception as exc:
                    results.append(('error', worker_id, str(exc)))

        threads = [threading.Thread(target=_complete, args=(f'worker-t-{i}',)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = [r for r in results if r[0] == 200]
        conflicts = [r for r in results if r[0] == 409]
        self.assertEqual(len(successes), 1, f'Expected exactly 1 success, got {len(successes)}: {results}')
        self.assertEqual(len(conflicts), 1, f'Expected exactly 1 conflict (409), got {len(conflicts)}: {results}')

        with test_app.app_context():
            run = db.session.get(AutomationRun, run_id)
            self.assertEqual(run.status, 'completed')


# ── Bounded infrastructure-failure reclaim tests (issue #131) ───────────────

class InfrastructureFailureReclaimTest(_AutomationConcurrencyBase):
    """Repeated identical control-plane failures must terminalize the run at a
    configured threshold instead of allowing unbounded claim/restart reclaims."""

    def test_transient_infrastructure_failure_releases_lease_for_bounded_retry(self):
        run_id = self._create_scenario_and_run()
        fingerprint = 'fetch_run:/api/automation/runs/1:http:500'
        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            claim_run_for_worker(run, 'worker-a')
            run = db.session.get(AutomationRun, run_id)
            result = record_worker_infrastructure_failure(
                run_id, 'worker-a', run.lease_token,
                stage='fetch_run', fingerprint=fingerprint,
                error='HTTP 500: internal failure', attempt_number=1,
            )
            self.assertEqual(result['action'], 'released')
            self.assertEqual(result['count'], 1)

            run = db.session.get(AutomationRun, run_id)
            self.assertEqual(run.status, 'queued')
            self.assertIsNone(run.worker_id)
            self.assertIsNone(run.lease_token)
            self.assertEqual(run.reclaim_failure_count, 1)
            self.assertEqual(run.reclaim_failure_fingerprint, fingerprint)
            self.assertEqual(run.reclaim_failure_attempt, 1)
            self.assertEqual(run.reclaim_failure_stage, 'fetch_run')

    def test_repeated_identical_failures_terminalize_at_threshold(self):
        run_id = self._create_scenario_and_run()
        fingerprint = 'fetch_run:/api/automation/runs/1:http:500'
        with app.app_context():
            threshold = max_reclaim_failures_for_run(db.session.get(AutomationRun, run_id))
            self.assertEqual(threshold, 5)

        for attempt in range(1, 6):
            with app.app_context():
                run = db.session.get(AutomationRun, run_id)
                claim_run_for_worker(run, f'worker-{attempt}')
                run = db.session.get(AutomationRun, run_id)
                result = record_worker_infrastructure_failure(
                    run_id, f'worker-{attempt}', run.lease_token,
                    stage='fetch_run', fingerprint=fingerprint,
                    error='HTTP 500: internal failure', attempt_number=attempt,
                )
                if attempt < 5:
                    self.assertEqual(result['action'], 'released')
                else:
                    self.assertEqual(result['action'], 'terminalized')

        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            self.assertEqual(run.status, 'failed')
            self.assertEqual(run.reclaim_failure_count, 5)
            self.assertEqual(run.reclaim_failure_attempt, 5)
            self.assertIn('infrastructure_failure_reclaim_loop', run.error_text or '')
            self.assertIn(fingerprint, run.error_text or '')
            self.assertIsNone(run.lease_token)
            self.assertIsNone(run.worker_id)
            self.assertTrue(run.finished_at is not None)

    def test_different_fingerprint_resets_consecutive_count(self):
        run_id = self._create_scenario_and_run()
        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            claim_run_for_worker(run, 'worker-a')
            run = db.session.get(AutomationRun, run_id)
            record_worker_infrastructure_failure(
                run_id, 'worker-a', run.lease_token, stage='fetch_run',
                fingerprint='fetch_run:/api/automation/runs/1:http:500',
                error='HTTP 500', attempt_number=1,
            )
            run = db.session.get(AutomationRun, run_id)
            claim_run_for_worker(run, 'worker-b')
            run = db.session.get(AutomationRun, run_id)
            record_worker_infrastructure_failure(
                run_id, 'worker-b', run.lease_token, stage='heartbeat',
                fingerprint='heartbeat:/api/automation/runs/1:http:503',
                error='HTTP 503', attempt_number=2,
            )
            run = db.session.get(AutomationRun, run_id)
            self.assertEqual(run.reclaim_failure_count, 1)
            self.assertEqual(run.reclaim_failure_fingerprint, 'heartbeat:/api/automation/runs/1:http:503')

    def test_reserve_run_lease_refuses_reclaim_after_threshold(self):
        run_id = self._create_scenario_and_run()
        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            run.status = 'running'
            run.lease_expires_at = utcnow() - timedelta(seconds=5)
            run.reclaim_failure_fingerprint = 'fetch_run:/api/automation/runs/1:http:500'
            run.reclaim_failure_count = 5
            db.session.commit()

            with self.assertRaises(InfrastructureFailureThresholdError):
                reserve_run_lease(run_id, 'worker-b', utcnow())

    def test_each_failed_attempt_is_durably_observable(self):
        run_id = self._create_scenario_and_run()
        fingerprint = 'fetch_run:/api/automation/runs/1:http:500'
        with app.app_context():
            for attempt in range(1, 4):
                run = db.session.get(AutomationRun, run_id)
                claim_run_for_worker(run, f'worker-{attempt}')
                run = db.session.get(AutomationRun, run_id)
                record_worker_infrastructure_failure(
                    run_id, f'worker-{attempt}', run.lease_token,
                    stage='fetch_run', fingerprint=fingerprint,
                    error='HTTP 500', attempt_number=attempt,
                )
            events = (
                AutomationRunEvent.query
                .filter_by(run_id=run_id, event_type='worker_infrastructure_failure')
                .order_by(AutomationRunEvent.sequence_number.asc())
                .all()
            )
            self.assertEqual(len(events), 3)
            self.assertEqual([e.attempt_number for e in events], [1, 2, 3])
            self.assertEqual([e.payload_json.get('count') for e in events], [1, 2, 3])
            self.assertEqual([e.payload_json.get('fingerprint') for e in events], [fingerprint] * 3)
            self.assertEqual(len({e.dedupe_key for e in events}), 3)

    def test_worker_error_route_releases_lease(self):
        run_id = self._create_scenario_and_run()
        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            claim_run_for_worker(run, 'worker-a')
            run = db.session.get(AutomationRun, run_id)
            lease_token = run.lease_token

        resp = self.client.post(
            f'/api/automation/runs/{run_id}/worker-error',
            headers=self.headers,
            json={
                'worker_id': 'worker-a',
                'lease_token': lease_token,
                'stage': 'fetch_run',
                'fingerprint': 'fetch_run:/api/automation/runs/1:http:500',
                'error': 'HTTP 500',
                'attempt_number': 1,
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['result']['action'], 'released')
        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            self.assertEqual(run.status, 'queued')
            self.assertEqual(run.reclaim_failure_count, 1)

    def test_worker_error_route_rejects_stale_lease_token(self):
        run_id = self._create_scenario_and_run()
        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            claim_run_for_worker(run, 'worker-a')
            run = db.session.get(AutomationRun, run_id)
            token_a = run.lease_token
            run.lease_expires_at = utcnow() - timedelta(seconds=5)
            db.session.commit()
            run = db.session.get(AutomationRun, run_id)
            claim_run_for_worker(run, 'worker-b')

        resp = self.client.post(
            f'/api/automation/runs/{run_id}/worker-error',
            headers=self.headers,
            json={
                'worker_id': 'worker-a',
                'lease_token': token_a,
                'stage': 'fetch_run',
                'fingerprint': 'x:http:500',
                'error': 'HTTP 500',
                'attempt_number': 1,
            },
        )
        self.assertEqual(resp.status_code, 409)
        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            self.assertEqual(run.worker_id, 'worker-b')
            self.assertIsNone(run.reclaim_failure_fingerprint)


if __name__ == '__main__':
    unittest.main()

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
    claim_run_for_worker,
    create_snapshot_for_scenario,
    reserve_run_lease,
    release_run_lease,
)
from services.character_service import update_character_relations


class AutomationConcurrencyTest(unittest.TestCase):
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

    def test_atomic_claim_first_wins_second_gets_409(self):
        """First claim succeeds, second attempt gets 409."""
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

    def test_expired_lease_is_reclaimed_exactly_once_sequential(self):
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

    def test_claim_normalizes_provisioning_lease_to_runtime_value(self):
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


if __name__ == '__main__':
    unittest.main()

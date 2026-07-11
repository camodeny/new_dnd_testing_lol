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
    AutomationSnapshot,
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

    def _create_claimable_run(self):
        """Create a scenario, snapshot, and run via the API, returning the run_id."""
        scenario_resp = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={'source_campaign_id': self.campaign_id, 'name': 'Concurrency Scenario'},
        )
        scenario_id = scenario_resp.get_json()['scenario']['id']

        snapshot_resp = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/snapshots',
            headers=self.headers,
            json={},
        )
        snapshot_id = snapshot_resp.get_json()['snapshot']['id']

        run_resp = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': snapshot_id},
        )
        self.assertEqual(run_resp.status_code, 201)
        return run_resp.get_json()['run']['id']

    def _claim(self, run_id, worker_id):
        return self.client.post(
            f'/api/automation/runs/{run_id}/claim',
            headers=self.headers,
            json={'worker_id': worker_id},
        )

    def test_two_workers_race_for_one_queued_run(self):
        run_id = self._create_claimable_run()

        resp_a = self._claim(run_id, 'worker-a')
        resp_b = self._claim(run_id, 'worker-b')

        successes = [r for r in [resp_a, resp_b] if r.status_code == 200]
        conflicts = [r for r in [resp_a, resp_b] if r.status_code == 409]

        self.assertEqual(len(successes), 1, f'Expected 1 success, got {len(successes)}')
        self.assertEqual(len(conflicts), 1, f'Expected 1 conflict, got {len(conflicts)}')

        winner_id = successes[0].get_json()['run']['worker_id']
        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            self.assertEqual(run.worker_id, winner_id)
            self.assertIsNotNone(run.lease_token)

    def test_same_worker_id_cannot_rotate_live_lease(self):
        run_id = self._create_claimable_run()

        first = self._claim(run_id, 'worker-a')
        self.assertEqual(first.status_code, 200)
        first_token = first.get_json()['lease_token']

        second = self._claim(run_id, 'worker-a')
        self.assertEqual(second.status_code, 409)

        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            self.assertEqual(run.lease_token, first_token)

    def test_expired_lease_is_reclaimed_exactly_once(self):
        run_id = self._create_claimable_run()

        self._claim(run_id, 'worker-a')

        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            run.status = 'running'
            run.lease_expires_at = utcnow() - timedelta(seconds=5)
            db.session.commit()

        resp_b = self._claim(run_id, 'worker-b')
        resp_c = self._claim(run_id, 'worker-c')

        successes = [r for r in [resp_b, resp_c] if r.status_code == 200]
        conflicts = [r for r in [resp_b, resp_c] if r.status_code == 409]

        self.assertEqual(len(successes), 1)
        self.assertEqual(len(conflicts), 1)

        winner_data = successes[0].get_json()
        self.assertTrue(winner_data['reclaimed'])

        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            self.assertEqual(run.reclaim_count, 1)
            self.assertEqual(run.attempt_count, 2)

    def test_same_worker_id_cannot_skip_expired_lease(self):
        run_id = self._create_claimable_run()

        self._claim(run_id, 'worker-a')

        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            run.lease_expires_at = utcnow() - timedelta(seconds=5)
            db.session.commit()

        resp = self._claim(run_id, 'worker-a')
        self.assertEqual(resp.status_code, 200)
        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            self.assertEqual(run.attempt_count, 2)

    def test_multiple_workers_consume_distinct_queued_runs(self):
        run_ids = [self._create_claimable_run() for _ in range(3)]

        workers = {}
        for i, run_id in enumerate(run_ids):
            wid = f'worker-{i}'
            resp = self._claim(run_id, wid)
            self.assertEqual(resp.status_code, 200, f'Worker {wid} could not claim run {run_id}')
            workers[wid] = resp.get_json()['run']['id']

        self.assertEqual(len(set(workers.values())), 3)
        self.assertEqual(set(workers.values()), set(run_ids))

        with app.app_context():
            for run_id in run_ids:
                run = db.session.get(AutomationRun, run_id)
                self.assertEqual(run.status, 'claimed')
                self.assertIsNotNone(run.worker_id)
                self.assertIsNotNone(run.lease_token)

    def test_stale_cleanup_cannot_clear_newer_lease(self):
        run_id = self._create_claimable_run()

        first = self._claim(run_id, 'worker-a')
        first_token = first.get_json()['lease_token']

        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            run.lease_expires_at = utcnow() - timedelta(seconds=5)
            db.session.commit()

        second = self._claim(run_id, 'worker-b')
        self.assertEqual(second.status_code, 200)
        second_token = second.get_json()['lease_token']

        with app.app_context():
            from sqlalchemy import update
            stmt = (
                update(AutomationRun)
                .where(
                    AutomationRun.id == run_id,
                    AutomationRun.lease_token == first_token,
                )
                .values(
                    status='queued',
                    worker_id=None,
                    lease_token=None,
                    heartbeat_at=None,
                    lease_expires_at=None,
                    updated_at=utcnow(),
                )
            )
            result = db.session.execute(stmt)
            db.session.commit()
            self.assertEqual(result.rowcount, 0)

            run = db.session.get(AutomationRun, run_id)
            self.assertEqual(run.worker_id, 'worker-b')
            self.assertEqual(run.lease_token, second_token)


if __name__ == '__main__':
    unittest.main()

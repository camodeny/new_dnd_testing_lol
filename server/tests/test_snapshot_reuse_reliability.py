import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ['DATABASE_URL'] = 'sqlite:///:memory:'

from app import app
from auth import generate_token
from models import Campaign, User, db


class SnapshotReuseReliabilityTest(unittest.TestCase):
    def setUp(self):
        with app.app_context():
            db.drop_all()
            db.create_all()

            owner = User(username='snapshot-owner', email='snapshot-owner@example.com')
            owner.set_password('password')
            db.session.add(owner)
            db.session.flush()

            campaign = Campaign(name='Snapshot Reuse Source', user_id=owner.id)
            db.session.add(campaign)
            db.session.commit()

            self.campaign_id = campaign.id
            self.token = generate_token(owner.id)

        self.client = app.test_client()
        self.headers = {'Authorization': f'Bearer {self.token}'}

    def tearDown(self):
        with app.app_context():
            db.session.remove()
            db.drop_all()

    def _create_scenario(self, name):
        response = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={'source_campaign_id': self.campaign_id, 'name': name},
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()['scenario']['id']

    def _create_snapshot(self, scenario_id, label):
        response = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/snapshots',
            headers=self.headers,
            json={'label': label},
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()['snapshot']['id']

    def test_snapshot_reuse_and_selection_contract(self):
        scenario_id = self._create_scenario('Snapshot Reuse Scenario')
        snapshot_1_id = self._create_snapshot(scenario_id, 'Snapshot 1')

        # A snapshot is reusable and is not consumed by the first run.
        for _ in range(2):
            response = self.client.post(
                f'/api/automation/scenarios/{scenario_id}/runs',
                headers=self.headers,
                json={'snapshot_id': snapshot_1_id},
            )
            self.assertEqual(response.status_code, 201, response.get_json())
            self.assertEqual(response.get_json()['run']['snapshot_id'], snapshot_1_id)

        snapshot_2_id = self._create_snapshot(scenario_id, 'Snapshot 2')

        # An explicit older snapshot remains authoritative after a newer one exists.
        older_response = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': snapshot_1_id},
        )
        self.assertEqual(older_response.status_code, 201, older_response.get_json())
        self.assertEqual(older_response.get_json()['run']['snapshot_id'], snapshot_1_id)

        # Omitting snapshot_id preserves the documented latest-snapshot default.
        latest_response = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={},
        )
        self.assertEqual(latest_response.status_code, 201, latest_response.get_json())
        self.assertEqual(latest_response.get_json()['run']['snapshot_id'], snapshot_2_id)

        # A snapshot cannot be selected through a different scenario.
        other_scenario_id = self._create_scenario('Other Snapshot Scenario')
        other_snapshot_id = self._create_snapshot(other_scenario_id, 'Other Snapshot')
        invalid_response = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': other_snapshot_id},
        )
        self.assertEqual(invalid_response.status_code, 400, invalid_response.get_json())
        self.assertIn(
            'Snapshot does not belong to this scenario',
            invalid_response.get_json()['error'],
        )

        # Every run in an explicit matrix uses the selected snapshot.
        matrix_response = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={
                'snapshot_id': snapshot_1_id,
                'matrix': [
                    {'runner_config': {'max_cycles': 1}},
                    {'runner_config': {'max_cycles': 2}},
                ],
            },
        )
        self.assertEqual(matrix_response.status_code, 201, matrix_response.get_json())
        runs = matrix_response.get_json()['runs']
        self.assertEqual(len(runs), 2)
        self.assertTrue(all(run['snapshot_id'] == snapshot_1_id for run in runs))


if __name__ == '__main__':
    unittest.main()

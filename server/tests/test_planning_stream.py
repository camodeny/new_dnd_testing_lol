import os
import sys
import unittest
import json
import time
import queue
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import app
from models import db, Campaign, User, CharacterPlanningMessage
from services.planning_stream import PlanningMessageExtractor, PlanningStreamManager, PlanningGeneratorWorker

class PlanningStreamTest(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        self.app.config['TESTING'] = True
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        # Seed test user and campaign
        self.user = User(username='player_user', email='player@example.com')
        self.user.set_password('password')
        db.session.add(self.user)
        db.session.flush()

        self.campaign = Campaign(name='Test Campaign', user_id=self.user.id)
        db.session.add(self.campaign)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_message_extractor_partial_json(self):
        extractor = PlanningMessageExtractor()
        
        # Test feeding token-by-token
        tokens = [
            '{"', 'message', '":', ' "', 'Hello', ' ', 'world', '!",', ' "active_page":', ' null}'
        ]
        extracted = []
        for t in tokens:
            delta = extractor.feed(t)
            if delta:
                extracted.append(delta)
        
        self.assertEqual(''.join(extracted), 'Hello world!')

    def test_message_extractor_escapes(self):
        extractor = PlanningMessageExtractor()
        tokens = [
            '{"message": "Hello\\nworld\\\"s", "active_page": null}'
        ]
        extracted = []
        for t in tokens:
            extracted.append(extractor.feed(t))
        self.assertEqual(''.join(extracted), 'Hello\nworld"s')

    def test_worker_listener_management(self):
        worker = PlanningGeneratorWorker(
            campaign_id=self.campaign.id,
            user_id=self.user.id,
            player_message_id=1,
            content="Hello",
            draft_character=None,
            active_page=None
        )
        
        # Add listener
        q = worker.add_listener()
        self.assertTrue(q.empty())

        # Broadcast token
        worker.broadcast({'type': 'token', 'token': 'Hi'})
        self.assertFalse(q.empty())
        self.assertEqual(q.get(), {'type': 'token', 'token': 'Hi'})

        # Remove listener
        worker.remove_listener(q)
        worker.broadcast({'type': 'token', 'token': 'No one'})
        self.assertTrue(q.empty())

    def test_stream_manager_registration(self):
        manager = PlanningStreamManager()
        
        with patch.object(PlanningGeneratorWorker, 'run', side_effect=lambda app: time.sleep(0.5)):
            worker1 = manager.start_generation(
                campaign_id=self.campaign.id,
                user_id=self.user.id,
                player_message_id=1,
                content="Hello",
                draft_character=None,
                active_page=None
            )
            self.assertIsNotNone(worker1)

            # Requesting same campaign + user returns same worker because it's running
            worker2 = manager.start_generation(
                campaign_id=self.campaign.id,
                user_id=self.user.id,
                player_message_id=2,
                content="Hello 2",
                draft_character=None,
                active_page=None
            )
            self.assertEqual(worker1, worker2)

            retrieved = manager.get_worker(self.campaign.id, self.user.id)
            self.assertEqual(retrieved, worker1)

if __name__ == '__main__':
    unittest.main()

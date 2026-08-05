import os
import sys
import unittest
import json
import time
import queue
import tempfile
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import create_app
from models import db, Campaign, CampaignSession, User, SessionMessage
from services.stream_manager import SessionStreamManager, SessionGeneratorWorker

class StreamManagerTest(unittest.TestCase):
    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(prefix='stream-manager-test-', suffix='.db')
        os.close(self.db_fd)
        self.original_database_url = os.environ.get('DATABASE_URL')
        os.environ['DATABASE_URL'] = f'sqlite:///{self.db_path}'

        self.app = create_app()
        self.app.config['TESTING'] = True
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        # Seed test user, campaign, and session
        self.user = User(username='dm_user', email='dm@example.com')
        self.user.set_password('password')
        db.session.add(self.user)
        db.session.flush()

        self.campaign = Campaign(name='Test Campaign', user_id=self.user.id)
        db.session.add(self.campaign)
        db.session.flush()

        self.session = CampaignSession(campaign_id=self.campaign.id, is_active=True)
        db.session.add(self.session)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()
        if self.original_database_url is None:
            os.environ.pop('DATABASE_URL', None)
        else:
            os.environ['DATABASE_URL'] = self.original_database_url
        try:
            os.remove(self.db_path)
        except FileNotFoundError:
            pass

    def test_worker_listener_management(self):
        # Setup a worker without executing dm_turn
        worker = SessionGeneratorWorker(self.campaign.id, self.session.id, self.user.id, "Hello")
        self.assertEqual(worker.status, "Initializing DM response...")

        # Add listener
        q = worker.add_listener()
        self.assertFalse(q.empty())
        initial_msg = q.get()
        self.assertEqual(initial_msg, {"type": "status", "status": "Initializing DM response..."})

        # Broadcast status update
        worker.update_status("Planning response...")
        self.assertEqual(worker.status, "Planning response...")
        self.assertFalse(q.empty())
        update_msg = q.get()
        self.assertEqual(update_msg, {"type": "status", "status": "Planning response..."})

        # Remove listener
        worker.remove_listener(q)
        worker.update_status("Still thinking...")
        self.assertTrue(q.empty())

    def test_worker_catchup_on_done(self):
        worker = SessionGeneratorWorker(self.campaign.id, self.session.id, self.user.id, "Hello")
        worker.is_done = True
        worker.messages_result = [{"role": "dm", "content": "Welcome!"}]
        worker.status = "Finished turn"

        q = worker.add_listener()
        self.assertFalse(q.empty())
        msg1 = q.get()
        self.assertEqual(msg1, {"type": "status", "status": "Finished turn"})
        self.assertFalse(q.empty())
        msg2 = q.get()
        self.assertEqual(msg2, {
            "type": "done",
            "messages": [{"role": "dm", "content": "Welcome!"}],
            "sheet_proposals": []
        })

    @patch('services.stream_manager._post_chat')
    @patch('openrouter._api_key_for_provider', return_value="fake-key")
    def test_async_dynamic_summarization(self, mock_key, mock_post_chat):
        mock_post_chat.return_value = "Amending Gildor's gold"
        worker = SessionGeneratorWorker(self.campaign.id, self.session.id, self.user.id, "Hello")

        # Directly invoke dynamic summarization
        worker._run_dynamic_summarization("Executing character sheet update for Gildor gold")
        self.assertEqual(worker.status, "Amending Gildor's gold...")

        # Verify broadcast occurred
        # Worker has no listeners yet, so it won't crash on broadcast, but let's test with a listener
        q = worker.add_listener()
        # Drain initial message
        q.get()

        worker._run_dynamic_summarization("Another action")
        self.assertEqual(worker.status, "Amending Gildor's gold...")
        self.assertFalse(q.empty())
        broadcast_msg = q.get()
        self.assertEqual(broadcast_msg, {"type": "status", "status": "Amending Gildor's gold..."})

    def test_stream_manager_registration(self):
        manager = SessionStreamManager()

        # Mock _execute_dm_turn to prevent LLM calling during start_generation thread run
        # Use side_effect to sleep so the thread is active when the next line runs
        with patch.object(SessionGeneratorWorker, '_execute_dm_turn', side_effect=lambda: time.sleep(0.5)) as mock_execute:
            worker1 = manager.start_generation(self.campaign.id, self.session.id, self.user.id, "Hello")
            self.assertIsNotNone(worker1)

            # Requesting same session returns same worker because it's still running
            worker2 = manager.start_generation(self.campaign.id, self.session.id, self.user.id, "Hello again")
            self.assertEqual(worker1, worker2)

            # Get worker
            retrieved = manager.get_worker(self.session.id)
            self.assertEqual(retrieved, worker1)

            # Let it finish
            time.sleep(0.6)
            self.assertTrue(worker1.is_done)

            # Completed workers are retained briefly so late SSE listeners can receive done.
            retrieved_after_done = manager.get_worker(self.session.id)
            self.assertEqual(retrieved_after_done, worker1)

            worker1.finished_at = time.monotonic() - manager.DONE_WORKER_TTL_SECONDS - 1
            retrieved_after_ttl = manager.get_worker(self.session.id)
            self.assertIsNone(retrieved_after_ttl)

    def test_stream_manager_replays_latest_pending_player_message_after_active_worker_finishes(self):
        manager = SessionStreamManager()
        first_started = queue.Queue()
        second_finished = queue.Queue()
        release_first = queue.Queue()
        executed_player_message_ids = []

        def fake_execute(worker_self):
            executed_player_message_ids.append(worker_self.player_message_id)
            if worker_self.player_message_id == 1:
                first_started.put(True)
                release_first.get(timeout=1)
            worker_self.finish_success(
                [{"role": "player", "content": worker_self.content}],
                [],
            )
            if worker_self.player_message_id == 2:
                second_finished.put(True)

        with patch.object(SessionGeneratorWorker, '_execute_dm_turn', autospec=True, side_effect=fake_execute):
            worker1 = manager.start_generation(
                self.campaign.id,
                self.session.id,
                self.user.id,
                "First",
                player_message_id=1,
            )
            self.assertTrue(first_started.get(timeout=1))

            worker2 = manager.start_generation(
                self.campaign.id,
                self.session.id,
                self.user.id,
                "Second",
                player_message_id=2,
            )
            self.assertIs(worker1, worker2)

            release_first.put(True)
            self.assertTrue(second_finished.get(timeout=1))

        self.assertEqual(executed_player_message_ids, [1, 2])
        self.assertEqual(manager.pending_requests, {})
        latest_worker = manager.get_worker(self.session.id)
        self.assertIsNotNone(latest_worker)
        self.assertEqual(latest_worker.player_message_id, 2)

    @patch('routes.sessions._run_session_memory_update')
    @patch('services.stream_manager.get_session_dm_response_with_tools')
    @patch('services.stream_manager.SessionGeneratorWorker._run_dynamic_summarization')
    def test_execute_dm_turn_success(
        self,
        mock_summarize,
        mock_get_dm_response,
        mock_memory_update,
    ):
        mock_get_dm_response.return_value = {
            "mode": "speak",
            "content": "Greetings traveller!",
            "parts": [{"type": "narration", "content": "Greetings traveller!"}],
            "commit_action_ids": [],
            "_pending_actions": [],
        }

        player_msg = SessionMessage(
            session_id=self.session.id,
            user_id=self.user.id,
            role='player',
            content='Hello',
        )
        db.session.add(player_msg)
        db.session.commit()

        worker = SessionGeneratorWorker(
            self.campaign.id,
            self.session.id,
            self.user.id,
            "Hello",
            player_message_id=player_msg.id,
        )

        # Prepare campaign world state and character mapping
        from models import CampaignWorld, CampaignMember, Character
        character = Character(user_id=self.user.id, campaign_id=self.campaign.id, name="Gildor", race="Elf", gp=100)
        db.session.add(character)
        db.session.flush()
        db.session.add(CampaignMember(campaign_id=self.campaign.id, user_id=self.user.id, selected_character_id=character.id))
        world = CampaignWorld(
            campaign_id=self.campaign.id,
            public_intro='{}',
            knowledge_graph='{}',
            world_state=json.dumps({
                'current_scene': {
                    'location_id': 'woods',
                    'location_name': 'Woods',
                },
            }),
            dm_private='{}',
        )
        db.session.add(world)
        db.session.commit()

        # Let's run it
        worker.run(self.app)

        self.assertTrue(worker.is_done)
        self.assertIsNone(worker.error)
        self.assertEqual(len(worker.messages_result), 2)
        self.assertEqual(worker.messages_result[0]['content'], "Hello")
        self.assertEqual(worker.messages_result[1]['content'], "Greetings traveller!")

        # Verify message actually saved to DB
        msg = SessionMessage.query.filter_by(session_id=self.session.id, role='dm').first()
        self.assertIsNotNone(msg)
        self.assertEqual(msg.content, "Greetings traveller!")
        mock_memory_update.assert_called_once()

if __name__ == '__main__':
    unittest.main()

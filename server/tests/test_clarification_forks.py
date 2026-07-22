import os
import sys
import unittest
from unittest.mock import patch

from flask import Flask

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from models import (
    db,
    Campaign,
    CampaignClarificationForkMessage,
    CampaignResolverPacket,
    CampaignSession,
    CampaignWorld,
    SessionMessage,
    User,
)
from services.clarification_forks import add_message, archive_fork, create_fork, resolve_fork


class ClarificationForkTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        self.user = User(username='dm_user', email='dm@example.com')
        self.user.set_password('password')
        db.session.add(self.user)
        db.session.flush()
        self.campaign = Campaign(name='Fork Test', description='Test', user_id=self.user.id)
        db.session.add(self.campaign)
        db.session.flush()
        self.session = CampaignSession(campaign_id=self.campaign.id)
        db.session.add(self.session)
        db.session.flush()
        self.world = CampaignWorld(
            campaign_id=self.campaign.id,
            public_intro='{}',
            knowledge_graph='{"entities":[],"relations":[],"facts":[]}',
            world_state='{"current_scene":{"location_id":"dock"}}',
            dm_private='{}',
        )
        self.anchor = SessionMessage(session_id=self.session.id, user_id=self.user.id, role='player', content='Who is the watcher?')
        db.session.add_all([self.world, self.anchor])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    @patch('services.clarification_forks.build_session_hot_context', return_value={'current_scene': {'location_id': 'dock'}})
    @patch('services.clarification_forks._generate_reply', side_effect=['Initial clarification.', 'Follow-up clarification.'])
    def test_fork_messages_and_resolution_do_not_mutate_live_campaign(self, _generate_reply, _hot_context):
        original_world = self.world.world_state
        original_message_count = SessionMessage.query.filter_by(session_id=self.session.id).count()

        fork = create_fork(
            self.campaign,
            self.session,
            self.user,
            'Is the watcher the same person as the ferryman?',
            anchor_message_id=self.anchor.id,
        )
        self.assertEqual(fork.status, 'active')
        self.assertEqual(fork.snapshot_json['transcript'][-1]['id'], self.anchor.id)
        self.assertEqual(SessionMessage.query.filter_by(session_id=self.session.id).count(), original_message_count)
        self.assertEqual(CampaignResolverPacket.query.count(), 0)
        self.assertEqual(db.session.get(CampaignWorld, self.world.id).world_state, original_world)

        fork = add_message(fork, 'Compare the descriptions only.')
        fork_messages = CampaignClarificationForkMessage.query.filter_by(fork_id=fork.id).order_by(
            CampaignClarificationForkMessage.id,
        ).all()
        self.assertEqual([message.role for message in fork_messages], ['ai', 'operator', 'ai'])
        self.assertEqual(SessionMessage.query.filter_by(session_id=self.session.id).count(), original_message_count)
        self.assertEqual(db.session.get(CampaignWorld, self.world.id).world_state, original_world)

        fork = resolve_fork(fork, {'decision': 'unresolved', 'reason': 'Insufficient evidence.'})
        self.assertEqual(fork.status, 'resolved')
        self.assertEqual(fork.resolution_json['decision'], 'unresolved')
        self.assertEqual(CampaignResolverPacket.query.count(), 0)

    @patch('services.clarification_forks.build_session_hot_context', return_value={})
    @patch('services.clarification_forks._generate_reply', return_value='Clarification.')
    def test_archived_fork_rejects_new_messages(self, _generate_reply, _hot_context):
        fork = create_fork(self.campaign, self.session, self.user, 'Which entity should be used?')
        archive_fork(fork)
        with self.assertRaisesRegex(ValueError, 'archived'):
            add_message(fork, 'Try again.')

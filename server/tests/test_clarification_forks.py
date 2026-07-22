import os
import sys
import unittest
from unittest.mock import patch

from flask import Flask

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from models import (
    db,
    Campaign,
    CampaignClarificationFork,
    CampaignClarificationForkMessage,
    CampaignResolverPacket,
    CampaignSession,
    CampaignWorld,
    SessionMessage,
    User,
)
from services.clarification_forks import (
    _canonical_messages,
    add_message,
    archive_fork,
    create_fork,
    resolve_fork,
    retry_generation,
)


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
        self.assertNotIn('transcript', fork.snapshot_json)
        self.assertEqual(fork.anchor_message_id, self.anchor.id)
        self.assertEqual(fork.base_start_message_id, self.anchor.id)
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

    @patch('services.clarification_forks.build_session_hot_context', return_value={})
    @patch('services.clarification_forks._generate_reply', side_effect=RuntimeError('provider timeout'))
    def test_initial_generation_failure_is_persisted_for_retry(self, _generate_reply, _hot_context):
        fork = create_fork(self.campaign, self.session, self.user, 'Who is the watcher?')

        self.assertEqual(fork.status, 'failed')
        self.assertIn('provider timeout', fork.generation_error)
        self.assertEqual(fork.generation_attempt_count, 1)
        self.assertEqual(CampaignClarificationForkMessage.query.filter_by(fork_id=fork.id).count(), 0)

    @patch('services.clarification_forks.build_session_hot_context', return_value={})
    @patch('services.clarification_forks._generate_reply', side_effect=['Initial reply.', RuntimeError('provider timeout'), 'Retried reply.'])
    def test_follow_up_failure_preserves_one_operator_message_for_retry(self, _generate_reply, _hot_context):
        fork = create_fork(self.campaign, self.session, self.user, 'Who is the watcher?')
        fork = add_message(fork, 'Compare the coat descriptions.')

        self.assertEqual(fork.status, 'failed')
        messages = CampaignClarificationForkMessage.query.filter_by(fork_id=fork.id).order_by(
            CampaignClarificationForkMessage.id,
        ).all()
        self.assertEqual([message.role for message in messages], ['ai', 'operator'])

        fork = retry_generation(fork)
        self.assertEqual(fork.status, 'active')
        messages = CampaignClarificationForkMessage.query.filter_by(fork_id=fork.id).order_by(
            CampaignClarificationForkMessage.id,
        ).all()
        self.assertEqual([message.role for message in messages], ['ai', 'operator', 'ai'])
        self.assertEqual(fork.generation_attempt_count, 3)

    @patch('services.clarification_forks._generate_reply', return_value='Clarification.')
    def test_fork_uses_persisted_bounded_message_range_and_frozen_context(self, _generate_reply):
        for index in range(35):
            db.session.add(SessionMessage(
                session_id=self.session.id,
                user_id=self.user.id,
                role='dm' if index % 2 else 'player',
                content=f'Message {index}: ' + ('detail ' * 40),
            ))
        db.session.commit()
        canonical = SessionMessage.query.filter_by(session_id=self.session.id).order_by(SessionMessage.id).all()
        anchor = canonical[-1]

        fork = create_fork(
            self.campaign,
            self.session,
            self.user,
            'Which clue is supported?',
            anchor_message_id=anchor.id,
        )
        reconstructed = _canonical_messages(fork)
        self.assertEqual(len(reconstructed), 24)
        self.assertEqual(reconstructed[0].id, canonical[-24].id)
        self.assertEqual(reconstructed[-1].id, anchor.id)
        self.assertNotIn('transcript', fork.snapshot_json)
        frozen_scene = fork.snapshot_json['context']['current_scene']

        db.session.add(SessionMessage(
            session_id=self.session.id,
            user_id=self.user.id,
            role='player',
            content='This later canonical message must not enter the fork.',
        ))
        self.world.world_state = '{"current_scene":{"location_id":"later_scene"}}'
        db.session.commit()

        refreshed = db.session.get(CampaignClarificationFork, fork.id)
        self.assertEqual(refreshed.snapshot_json['context']['current_scene'], frozen_scene)
        reconstructed = _canonical_messages(refreshed)
        self.assertEqual(reconstructed[-1].id, anchor.id)
        self.assertNotEqual(reconstructed[-1].content, 'This later canonical message must not enter the fork.')

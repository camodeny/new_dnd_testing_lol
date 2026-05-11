import os
import sys
import unittest
from unittest.mock import patch

from flask import Flask

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from auth import generate_token
from models import (
    db,
    Campaign,
    CampaignAuditEvent,
    CampaignClock,
    CampaignMember,
    CampaignSession,
    CampaignWorld,
    Character,
    SessionMessage,
    User,
)
from routes.sessions import sessions_bp
from services.dm_tools import DM_TOOL_DEFINITIONS, apply_memory_patch, build_session_hot_context, context_manifest, execute_dm_tool


class DmToolsTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        self.app.config['SECRET_KEY'] = 'test-secret'
        self.app.config['JWT_EXPIRATION_HOURS'] = 1
        self.app.register_blueprint(sessions_bp)
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        self.user = User(username='player', email='player@example.com')
        self.user.set_password('password')
        self.campaign = Campaign(name='Tool Test', description='A test campaign.', user_id=1)
        db.session.add(self.user)
        db.session.flush()
        self.campaign.user_id = self.user.id
        db.session.add(self.campaign)
        db.session.flush()
        self.character = Character(
            user_id=self.user.id,
            campaign_id=self.campaign.id,
            name='Aria',
            race='Elf',
            background='Sage',
            armor_class=15,
            passive_perception=13,
        )
        db.session.add(self.character)
        db.session.flush()
        db.session.add(CampaignMember(
            campaign_id=self.campaign.id,
            user_id=self.user.id,
            selected_character_id=self.character.id,
        ))
        self.session = CampaignSession(campaign_id=self.campaign.id)
        db.session.add(self.session)
        db.session.add(CampaignWorld(
            campaign_id=self.campaign.id,
            public_intro='{}',
            knowledge_graph='{"entities":[],"relations":[],"facts":[]}',
            world_state='{"current_scene":{"location_name":"Dock Ward","immediate_tension":"A bell rings."}}',
            dm_private='{}',
        ))
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_tool_definitions_are_function_schemas(self):
        names = {tool['function']['name'] for tool in DM_TOOL_DEFINITIONS}
        self.assertIn('get_character_context', names)
        self.assertIn('search_campaign_memory', names)
        self.assertIn('advance_clock', names)
        for tool in DM_TOOL_DEFINITIONS:
            self.assertEqual(tool['type'], 'function')
            self.assertIn('parameters', tool['function'])
            self.assertEqual(tool['function']['parameters']['type'], 'object')

    def test_character_context_fetches_selected_character(self):
        result = execute_dm_tool(
            self.campaign,
            self.session,
            self.user,
            'get_character_context',
            {'scope': 'current_player', 'fields': ['combat', 'general']},
            {},
        )
        self.assertEqual(result['character']['name'], 'Aria')
        self.assertEqual(result['character']['combat']['armor_class'], 15)
        self.assertEqual(result['character']['general']['passive_perception'], 13)

    def test_context_manifest_reports_compact_strategy(self):
        hot_context = build_session_hot_context(self.campaign, self.session, self.user)
        manifest = context_manifest(hot_context, DM_TOOL_DEFINITIONS)
        self.assertEqual(manifest['strategy'], 'compact_hot_context_with_dm_tools')
        self.assertFalse(manifest['full_world_graph_included'])
        self.assertIn('get_character_context', manifest['available_tools'])
        self.assertIn('recent_messages', manifest['estimated_tokens_by_section'])

    def test_memory_patch_creates_clock_and_graph_fact(self):
        result = apply_memory_patch(
            self.campaign,
            self.session,
            {
                'running_summary': 'The party heard a warning bell at the docks.',
                'upsert_graph_facts': [
                    {
                        'id': 'dock_warning_bell',
                        'entity_ids': ['dock_ward'],
                        'text': 'A warning bell rang in the Dock Ward.',
                        'certainty': 'confirmed',
                        'visibility': 'party_known',
                    }
                ],
                'create_clocks': [
                    {
                        'id': 'dock_alarm_spreads',
                        'name': 'Dock Alarm Spreads',
                        'segments': 4,
                        'filled': 1,
                        'summary': 'The alarm draws more attention.',
                        'trigger': 'The party delays or makes noise.',
                        'on_complete': 'Guards lock down the docks.',
                    }
                ],
            },
            {},
        )
        clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='dock_alarm_spreads').first()
        self.assertIsNotNone(clock)
        self.assertEqual(clock.filled, 1)
        self.assertTrue(result['running_summary_updated'])
        self.assertEqual(self.session.running_summary, 'The party heard a warning bell at the docks.')

    def test_advance_clock_mutates_existing_clock(self):
        db.session.add(CampaignClock(
            campaign_id=self.campaign.id,
            clock_id='guards_arrive',
            name='Guards Arrive',
            segments=4,
            filled=3,
            status='active',
        ))
        db.session.commit()
        result = execute_dm_tool(
            self.campaign,
            self.session,
            self.user,
            'advance_clock',
            {'clock_id': 'guards_arrive', 'delta': 1, 'reason': 'The party made noise.'},
            {},
        )
        self.assertEqual(result['clock']['filled'], 4)
        self.assertEqual(result['clock']['status'], 'completed')

    def test_session_message_route_persists_dm_reply_before_memory_update(self):
        token = generate_token(self.user.id)
        client = self.app.test_client()

        with patch('routes.sessions.get_session_dm_response_with_tools', return_value='Yes, you are in a party.') as dm_response, \
                patch('routes.sessions.get_session_memory_patch', return_value={}) as memory_patch:
            response = client.post(
                f'/api/sessions/{self.session.id}/messages',
                json={'content': '<ooc>Am I in a party?</ooc>', 'role': 'player'},
                headers={'Authorization': f'Bearer {token}'},
            )

        self.assertEqual(response.status_code, 201)
        payload = response.get_json()
        self.assertEqual([message['role'] for message in payload['messages']], ['player', 'dm'])
        self.assertEqual(payload['messages'][1]['content'], 'Yes, you are in a party.')
        self.assertEqual(SessionMessage.query.filter_by(session_id=self.session.id).count(), 2)
        self.assertIsNotNone(CampaignAuditEvent.query.filter_by(event_type='dm_output_stored').first())
        self.assertTrue(dm_response.called)
        self.assertEqual(memory_patch.call_args.args[0]['latest_player_message'], '<ooc>Am I in a party?</ooc>')


if __name__ == '__main__':
    unittest.main()

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
    CharacterPlanningMessage,
    SessionMessage,
    User,
)
from routes.dev import _agent_runs_from_stream, _audit_stream_entry, _chat_flow_payload
from routes.sessions import sessions_bp
from services.audit_service import log_audit_event
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

    def test_agent_runs_ignore_self_parent_trace(self):
        stream = [
            {
                'id': 1,
                'trace_id': 'session_dm:session_2:message_15',
                'parent_trace_id': None,
                'trace_label': 'session_dm: session 2',
                'actor': 'session_dm',
            },
            {
                'id': 2,
                'trace_id': 'session_dm:session_2:message_15',
                'parent_trace_id': 'session_dm:session_2:message_15',
                'trace_label': 'session_dm: session 2',
                'actor': 'session_dm',
            },
        ]

        runs = _agent_runs_from_stream(stream)

        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]['trace_id'], 'session_dm:session_2:message_15')
        self.assertEqual(runs[0]['events'], stream)
        self.assertEqual(runs[0]['children'], [])

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

        def memory_patch_side_effect(_memory_context, audit_context=None):
            dm_event = CampaignAuditEvent.query.filter_by(event_type='dm_output_stored').first()
            self.assertIsNotNone(dm_event)
            self.assertEqual(audit_context['trace_id'].split(':')[0], 'session_memory_writer')
            self.assertEqual(audit_context['parent_trace_id'].split(':')[0], 'session_dm')
            return {}

        with patch('routes.sessions.get_session_dm_response_with_tools', return_value='Yes, you are in a party.') as dm_response, \
                patch('routes.sessions.get_session_memory_patch', side_effect=memory_patch_side_effect) as memory_patch:
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
        player_msg = SessionMessage.query.filter_by(session_id=self.session.id, role='player').first()
        expected_dm_trace_id = f'session_dm:session_{self.session.id}:message_{player_msg.id}'
        expected_memory_trace_id = f'session_memory_writer:session_{self.session.id}:message_{player_msg.id}'
        self.assertEqual(memory_patch.call_args.kwargs['audit_context']['parent_trace_id'], expected_dm_trace_id)
        self.assertEqual(memory_patch.call_args.kwargs['audit_context']['trace_id'], expected_memory_trace_id)

    def test_chat_flow_groups_visible_messages_and_nested_branches(self):
        planning_player = CharacterPlanningMessage(
            campaign_id=self.campaign.id,
            user_id=self.user.id,
            role='player',
            content='I want to be a dockside wizard.',
        )
        planning_dm = CharacterPlanningMessage(
            campaign_id=self.campaign.id,
            user_id=self.user.id,
            role='dm',
            content='Tie your wizard to the warning bell.',
        )
        session_player = SessionMessage(
            session_id=self.session.id,
            user_id=self.user.id,
            role='player',
            content='<ooc>What do I see?</ooc>',
        )
        db.session.add_all([planning_player, planning_dm, session_player])
        db.session.commit()

        session_trace_id = f'session_dm:session_{self.session.id}:message_{session_player.id}'
        memory_trace_id = f'session_memory_writer:session_{self.session.id}:message_{session_player.id}'
        log_audit_event(
            self.campaign.id,
            'model_request',
            'session_dm request: session_dm_response',
            {
                'operation': 'session_dm_response',
                'messages': [{'role': 'user', 'content': '<ooc>What do I see?</ooc>'}],
            },
            actor='session_dm',
            trace_id=session_trace_id,
            trace_label=f'session_dm: session {self.session.id}',
            audit_role='tools',
            commit=False,
        )
        log_audit_event(
            self.campaign.id,
            'model_response',
            'session_dm response: session_dm_response',
            {
                'operation': 'session_dm_response',
                'content': 'You see lanterns swinging in the mist.',
                'raw_response': {'choices': [{'message': {'content': 'You see lanterns swinging in the mist.'}}]},
            },
            actor='session_dm',
            trace_id=session_trace_id,
            trace_label=f'session_dm: session {self.session.id}',
            audit_role='agent',
            commit=False,
        )
        log_audit_event(
            self.campaign.id,
            'dm_tool_execution',
            'DM tool executed: get_current_scene',
            {
                'session_id': self.session.id,
                'tool_name': 'get_current_scene',
                'arguments': {'include_private': True},
                'result': {'current_scene': {'location_name': 'Dock Ward'}},
                'mutated': False,
                'affected_ids': {},
            },
            actor='session_dm',
            trace_id=session_trace_id,
            parent_trace_id=session_trace_id,
            trace_label=f'session_dm: session {self.session.id}',
            audit_role='tools',
            commit=False,
        )
        log_audit_event(
            self.campaign.id,
            'memory_writer_request',
            'Requested post-turn session memory update.',
            {'messages': [{'role': 'user', 'content': 'memory input'}]},
            actor='session_memory_writer',
            trace_id=memory_trace_id,
            parent_trace_id=session_trace_id,
            trace_label=f'session_memory_writer: session {self.session.id}',
            audit_role='tools',
            commit=False,
        )
        log_audit_event(
            self.campaign.id,
            'knowledge_graph_write',
            'Legacy unlinked write.',
            {'fact': 'The bell rang.'},
            actor='world_architect',
            audit_role='tools',
            commit=False,
        )
        db.session.commit()

        audit_events = CampaignAuditEvent.query.filter_by(campaign_id=self.campaign.id).order_by(CampaignAuditEvent.id.asc()).all()
        audit_stream = [_audit_stream_entry(event) for event in audit_events]
        agent_runs = _agent_runs_from_stream(audit_stream)
        flow = _chat_flow_payload(
            self.campaign.id,
            CharacterPlanningMessage.query.filter_by(campaign_id=self.campaign.id).order_by(CharacterPlanningMessage.created_at.asc()).all(),
            [self.session],
            list(self.campaign.members),
            audit_stream,
            agent_runs,
        )

        session_lane = next(lane for lane in flow['lanes'] if lane['id'] == f'session-{self.session.id}')
        session_message = next(message for message in session_lane['messages'] if message['id'] == session_player.id)
        self.assertEqual(session_message['branches'][0]['trace_id'], session_trace_id)
        self.assertEqual(session_message['branches'][0]['children'][0]['trace_id'], memory_trace_id)
        branch_steps = session_message['branches'][0]['steps']
        self.assertEqual([step['kind'] for step in branch_steps], ['model_response', 'tool_call', 'tool_result'])
        self.assertEqual([step['category'] for step in branch_steps], ['agents', 'tools', 'tools'])
        self.assertEqual(branch_steps[1]['title'], 'get_current_scene')
        self.assertEqual(branch_steps[2]['result']['current_scene']['location_name'], 'Dock Ward')
        self.assertEqual(flow['unlinked_branches'][0]['summary'], 'Legacy unlinked write.')
        planning_lane = next(lane for lane in flow['lanes'] if lane['type'] == 'planning')
        self.assertEqual([message['content'] for message in planning_lane['messages']], [
            'I want to be a dockside wizard.',
            'Tie your wizard to the warning bell.',
        ])


if __name__ == '__main__':
    unittest.main()

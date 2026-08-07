import json
import os
import sys
import unittest
from unittest.mock import patch

from flask import Flask

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from models import (
    db,
    Campaign,
    CampaignAuditEvent,
    CampaignClock,
    CampaignSession,
    CampaignWorld,
    Character,
    CampaignMember,
    NPCActor,
    SessionDmTurn,
    SessionMessage,
    User,
)
from auth import generate_token
from services.post_turn_consistency import (
    PostTurnConsistencyIncident,
    reconcile_post_turn_state,
)
from routes.sessions import sessions_bp


def _graph_entities():
    return {
        'entities': [
            {'id': 'the_dock', 'type': 'location', 'name': 'The Dock', 'visibility': 'party_known'},
            {'id': 'river', 'type': 'location', 'name': 'The River', 'visibility': 'party_known'},
        ],
        'relations': [],
        'facts': [],
    }


def _world_state(location_id='the_dock', active_ids=('mira',)):
    return {
        'current_scene': {
            'location_id': location_id,
            'location_name': 'The Dock' if location_id == 'the_dock' else 'The River',
            'time_of_day': 'night',
            'active_npc_ids': list(active_ids),
            'immediate_tension': 'Mira lies on the dock, not breathing.',
        },
    }


class PostTurnConsistencyTest(unittest.TestCase):
    def setUp(self):
        self.env_patch = patch.dict(os.environ, {'GEMINI_EMBEDDINGS_ENABLED': 'false'}, clear=False)
        self.env_patch.start()
        self.app = Flask(__name__)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        self.app.config['SECRET_KEY'] = 'test-secret'
        self.app.config['JWT_EXPIRATION_HOURS'] = 24
        try:
            self.app.register_blueprint(sessions_bp)
        except AssertionError:
            pass
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

        self.user = User(username='player', email='player@example.com')
        self.user.set_password('password')
        db.session.add(self.user)
        db.session.flush()

        self.campaign = Campaign(name='Post-Turn Consistency', description='Test', user_id=self.user.id)
        db.session.add(self.campaign)
        db.session.flush()

        self.world = CampaignWorld(
            campaign_id=self.campaign.id,
            public_intro='{}',
            knowledge_graph=json.dumps(_graph_entities()),
            world_state=json.dumps(_world_state()),
            dm_private='{}',
            memory_revision=0,
        )
        db.session.add(self.world)

        self.session = CampaignSession(campaign_id=self.campaign.id)
        db.session.add(self.session)
        db.session.flush()

        mi = NPCActor(campaign_id=self.campaign.id, actor_id='mira', name='Mira', dossier='{}')
        db.session.add(mi)
        db.session.commit()

    def tearDown(self):
        db.session.rollback()
        db.drop_all()
        self.ctx.pop()
        self.env_patch.stop()

    def _clock(self, clock_id, name, segments, filled, status='active', summary=None, visibility='dm_private', commit=True):
        clock = CampaignClock(
            campaign_id=self.campaign.id,
            clock_id=clock_id,
            name=name,
            segments=segments,
            filled=filled,
            status=status,
            summary=summary,
            visibility=visibility,
        )
        db.session.add(clock)
        if commit:
            db.session.commit()
        return clock

    def _run_fixture(self, **kwargs):
        return reconcile_post_turn_state(
            self.campaign,
            self.session,
            player_message_id=kwargs.get('player_message_id', 1),
            dm_message_id=kwargs.get('dm_message_id', 2),
            trace_id='test:trace',
            parent_trace_id='test:parent',
            trace_label='test post-turn consistency',
        )

    def test_pipeline_finalizes_summary_against_committed_post_clock_state(self):
        """The running summary is authored from committed post-clock state, not
        from a pre-adjudication snapshot: the finalizer receives the committed
        clock value and its output is what the session presents."""
        from services.dm_turns import begin_session_dm_turn
        db.session.add(begin_session_dm_turn(
            self.campaign.id,
            self.session.id,
            1,
            'session_dm:session_1:message_1',
        ))
        self._clock('trapped_ferrymen', 'Trapped Ferrymen', 3, 1)
        self.session.running_summary = 'The Trapped Ferrymen clock sits at 1/3.'
        db.session.commit()

        captured = {}
        from routes.sessions import _run_session_memory_update

        def fake_finalize(summary_context, audit_context=None):
            captured['context'] = summary_context
            captured['called_after_clock_commit'] = (
                CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='trapped_ferrymen').one().filled == 2
            )
            return {'running_summary': 'The Trapped Ferrymen clock now sits at 2/3 and the party holds the gate.'}

        with patch('routes.sessions.get_session_memory_patch', return_value={
            'source_contract': 'compiled_session_memory_v2',
            'running_summary': 'The Trapped Ferrymen clock sits at 1/3.',
            'scene_patch': {},
            'upsert_graph_entities': [],
            'upsert_graph_relations': [],
            'upsert_graph_facts': [],
            'create_clocks': [],
            'retire_clocks': [],
            'update_npc_actors': [],
            'record_events': [],
        }), \
                patch('routes.sessions.get_session_clock_updates', return_value={
                    'create_clocks': [],
                    'advance_clocks': [{
                        'clock_id': 'trapped_ferrymen',
                        'delta': 1,
                        'reason': 'The visible exchange pressed the ferrymen onward.',
                        'evidence': ['The DM confirmed the ferrymen moved.'],
                    }],
                    'retire_clocks': [],
                    'no_change_explanations': [],
                }), \
                patch('routes.sessions.get_session_running_summary_finalize', side_effect=fake_finalize) as finalize_mock:
            _run_session_memory_update(
                campaign_id=self.campaign.id,
                session_id=self.session.id,
                user_id=self.user.id,
                player_message_id=1,
                player_content='We hold the gate.',
                ai_text='The ferrymen press against the gate.',
                hot_context={},
                parent_trace_id='test:trace',
                dm_message_id=2,
            )

        self.assertTrue(captured['called_after_clock_commit'])
        self.assertEqual(captured['context']['prior_running_summary'], 'The Trapped Ferrymen clock sits at 1/3.')
        trapped = next(
            clock for clock in captured['context']['active_clocks']
            if clock['clock_id'] == 'trapped_ferrymen'
        )
        self.assertEqual(trapped['filled'], 2)
        self.assertEqual(trapped['segments'], 3)

        turn = SessionDmTurn.query.filter_by(player_message_id=1).first()
        self.assertEqual(turn.post_turn_status, 'complete')
        self.assertEqual(turn.memory_status, 'complete')
        self.assertEqual(turn.clock_status, 'complete')
        session = db.session.get(CampaignSession, self.session.id)
        self.assertEqual(session.running_summary, 'The Trapped Ferrymen clock now sits at 2/3 and the party holds the gate.')

    def test_summary_finalize_failure_fails_closed(self):
        """If final summary generation fails, the turn must NOT be reported
        complete with the old summary still presented as current."""
        from services.dm_turns import begin_session_dm_turn
        db.session.add(begin_session_dm_turn(
            self.campaign.id,
            self.session.id,
            1,
            'session_dm:session_1:message_1',
        ))
        self._clock('trapped_ferrymen', 'Trapped Ferrymen', 3, 2)
        self.session.running_summary = 'The Trapped Ferrymen clock sits at 1/3.'
        db.session.commit()

        from routes.sessions import _run_session_memory_update
        with patch('routes.sessions.get_session_memory_patch', return_value={
            'source_contract': 'compiled_session_memory_v2',
            'running_summary': 'The Trapped Ferrymen clock sits at 1/3.',
            'scene_patch': {},
            'upsert_graph_entities': [],
            'upsert_graph_relations': [],
            'upsert_graph_facts': [],
            'create_clocks': [],
            'retire_clocks': [],
            'update_npc_actors': [],
            'record_events': [],
        }), \
                patch('routes.sessions.get_session_clock_updates', return_value={
                    'create_clocks': [],
                    'advance_clocks': [],
                    'retire_clocks': [],
                    'no_change_explanations': [],
                }), \
                patch('routes.sessions.get_session_running_summary_finalize', return_value=None):
            _run_session_memory_update(
                campaign_id=self.campaign.id,
                session_id=self.session.id,
                user_id=self.user.id,
                player_message_id=1,
                player_content='We hold the gate.',
                ai_text='The ferrymen press against the gate.',
                hot_context={},
                parent_trace_id='test:trace',
                dm_message_id=2,
            )

        turn = SessionDmTurn.query.filter_by(player_message_id=1).first()
        self.assertEqual(turn.post_turn_status, 'error')
        self.assertEqual(turn.memory_status, 'complete')
        self.assertEqual(turn.clock_status, 'complete')
        self.assertIn('summary', turn.error_text.lower())
        # Memory and clock commits are durable; only the summary finalization
        # is pending, so a retry can recover.
        clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='trapped_ferrymen').one()
        self.assertEqual(clock.filled, 2)
        self.assertTrue(
            CampaignAuditEvent.query.filter_by(campaign_id=self.campaign.id, event_type='post_turn_summary_finalize_error').first()
        )

    def test_summary_finalize_context_reflects_committed_clocks(self):
        from services.dm_tools import build_session_summary_finalize_context
        self._clock('trapped_ferrymen', 'Trapped Ferrymen', 3, 2)
        self._clock('mira_in_the_water', 'Mira in the Water', 4, 1, status='superseded')
        self.session.running_summary = 'Old summary.'
        db.session.commit()

        context = build_session_summary_finalize_context(
            self.campaign,
            self.session,
            'player says hi',
            'dm replies',
            player_message_id=1,
            dm_message_id=2,
        )

        self.assertEqual(context['prior_running_summary'], 'Old summary.')
        self.assertEqual(context['latest_player_message'], 'player says hi')
        active_ids = [clock['clock_id'] for clock in context['active_clocks']]
        self.assertIn('trapped_ferrymen', active_ids)
        trapped = next(clock for clock in context['active_clocks'] if clock['clock_id'] == 'trapped_ferrymen')
        self.assertEqual((trapped['filled'], trapped['segments']), (2, 3))
        resolved_ids = [clock['clock_id'] for clock in context['resolved_clocks']]
        self.assertIn('mira_in_the_water', resolved_ids)

    def test_no_conflict_clock_stays_active_and_consistent(self):
        self._clock('trapped_ferrymen', 'Trapped Ferrymen', 3, 1)
        db.session.commit()

        report = self._run_fixture()
        db.session.commit()

        self.assertEqual(report['clocks_superseded'], [])
        self.assertTrue(report['verified'])
        clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='trapped_ferrymen').one()
        self.assertEqual(clock.status, 'active')

    def test_subject_relocation_supersedes_active_clock(self):
        """A clock cannot remain actively located 'in the water' after committed
        scene state places its subject on the dock."""
        self._clock('mira_in_the_water', 'Mira in the Water', 4, 1, summary='Mira is being hauled toward the dock.')
        db.session.commit()

        report = self._run_fixture()
        db.session.commit()

        clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='mira_in_the_water').one()
        self.assertEqual(clock.status, 'superseded')
        self.assertIn('water', clock.summary.lower())
        self.assertIn('superseded', clock.summary.lower())
        self.assertEqual(report['clocks_superseded'][0]['kind'], 'subject_relocated')
        self.assertTrue(report['verified'])

    def test_condition_resolution_supersedes_danger_clock(self):
        """Stabilization or other resolution updates/retires the corresponding danger clock."""
        graph = json.loads(self.world.knowledge_graph)
        graph['facts'].append({
            'id': 'fact_mira_stabilized',
            'entity_ids': ['mira'],
            'text': 'Mira was stabilized and is now conscious.',
            'certainty': 'confirmed',
            'visibility': 'party_known',
        })
        self.world.knowledge_graph = json.dumps(graph)
        db.session.commit()

        self._clock('mira_drowning', 'Mira in Peril', 4, 2, summary='Mira is drowning and unconscious at 0 HP.')
        db.session.commit()

        report = self._run_fixture()
        db.session.commit()

        clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='mira_drowning').one()
        self.assertEqual(clock.status, 'superseded')
        self.assertEqual(report['clocks_superseded'][0]['kind'], 'condition_resolved')
        self.assertTrue(report['verified'])

    def test_unconscious_does_not_match_resolution_word_conscious(self):
        """A committed fact that Mira is *unconscious* must not be treated as
        evidence the danger condition resolved (substring 'conscious')."""
        graph = json.loads(self.world.knowledge_graph)
        graph['facts'].append({
            'id': 'fact_mira_still_unconscious',
            'entity_ids': ['mira'],
            'text': 'Mira is still unconscious and has not regained consciousness.',
            'certainty': 'confirmed',
            'visibility': 'party_known',
        })
        self.world.knowledge_graph = json.dumps(graph)
        db.session.commit()

        self._clock('mira_in_peril', 'Mira in Peril', 4, 2, summary='Mira is unconscious and drowning at 0 HP.')
        db.session.commit()

        report = self._run_fixture()
        db.session.commit()

        clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='mira_in_peril').one()
        self.assertEqual(clock.status, 'active')
        self.assertEqual(report['clocks_superseded'], [])
        self.assertTrue(report['verified'])

    def test_alive_but_still_drowning_is_not_resolution(self):
        """'alive but still drowning' contains a resolution word and a condition
        word, but the persistence marker makes it a contradiction, so the danger
        clock must remain active."""
        graph = json.loads(self.world.knowledge_graph)
        graph['facts'].append({
            'id': 'fact_mira_alive_still_drowning',
            'entity_ids': ['mira'],
            'text': 'Mira is alive but still drowning.',
            'certainty': 'confirmed',
            'visibility': 'party_known',
        })
        self.world.knowledge_graph = json.dumps(graph)
        # Keep Mira out of the visible scene cast so only the fact binds her.
        self.world.world_state = json.dumps({
            'current_scene': {
                'location_id': 'the_dock',
                'location_name': 'The Dock',
                'active_npc_ids': [],
            },
        })
        db.session.commit()

        self._clock('mira_in_peril', 'Mira in Peril', 4, 2, summary='Mira is drowning at 0 HP.')
        db.session.commit()

        report = self._run_fixture()
        db.session.commit()

        clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='mira_in_peril').one()
        self.assertEqual(clock.status, 'active')
        self.assertEqual(report['clocks_superseded'], [])
        self.assertTrue(report['verified'])

    def test_not_safe_is_not_resolution(self):
        """A negated resolution claim ('Mira is not safe') must not supersede an
        active danger clock."""
        graph = json.loads(self.world.knowledge_graph)
        graph['facts'].append({
            'id': 'fact_mira_not_safe',
            'entity_ids': ['mira'],
            'text': 'Mira is not safe yet.',
            'certainty': 'confirmed',
            'visibility': 'party_known',
        })
        self.world.knowledge_graph = json.dumps(graph)
        self.world.world_state = json.dumps({
            'current_scene': {
                'location_id': 'the_dock',
                'location_name': 'The Dock',
                'active_npc_ids': [],
            },
        })
        db.session.commit()

        self._clock('mira_in_peril', 'Mira in Peril', 4, 2, summary='Mira is drowning at 0 HP.')
        db.session.commit()

        report = self._run_fixture()
        db.session.commit()

        clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='mira_in_peril').one()
        self.assertEqual(clock.status, 'active')
        self.assertEqual(report['clocks_superseded'], [])
        self.assertTrue(report['verified'])

    def test_alive_but_drowning_is_not_resolution(self):
        """'alive but drowning' asserts the condition is ongoing even without a
        persistence marker, so the danger clock must remain active."""
        graph = json.loads(self.world.knowledge_graph)
        graph['facts'].append({
            'id': 'fact_mira_alive_but_drowning',
            'entity_ids': ['mira'],
            'text': 'Mira is alive but drowning.',
            'certainty': 'confirmed',
            'visibility': 'party_known',
        })
        self.world.knowledge_graph = json.dumps(graph)
        self.world.world_state = json.dumps({
            'current_scene': {
                'location_id': 'the_dock',
                'location_name': 'The Dock',
                'active_npc_ids': [],
            },
        })
        db.session.commit()

        self._clock('mira_in_peril', 'Mira in Peril', 4, 2, summary='Mira is drowning at 0 HP.')
        db.session.commit()

        report = self._run_fixture()
        db.session.commit()

        clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='mira_in_peril').one()
        self.assertEqual(clock.status, 'active')
        self.assertEqual(report['clocks_superseded'], [])
        self.assertTrue(report['verified'])

    def test_conscious_but_poisoned_is_not_resolution(self):
        """'conscious but poisoned' asserts the condition is ongoing even without
        a persistence marker, so the danger clock must remain active."""
        graph = json.loads(self.world.knowledge_graph)
        graph['facts'].append({
            'id': 'fact_mira_conscious_but_poisoned',
            'entity_ids': ['mira'],
            'text': 'Mira is conscious but poisoned.',
            'certainty': 'confirmed',
            'visibility': 'party_known',
        })
        self.world.knowledge_graph = json.dumps(graph)
        self.world.world_state = json.dumps({
            'current_scene': {
                'location_id': 'the_dock',
                'location_name': 'The Dock',
                'active_npc_ids': [],
            },
        })
        db.session.commit()

        self._clock('mira_in_peril', 'Mira in Peril', 4, 2, summary='Mira is poisoned at 0 HP.')
        db.session.commit()

        report = self._run_fixture()
        db.session.commit()

        clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='mira_in_peril').one()
        self.assertEqual(clock.status, 'active')
        self.assertEqual(report['clocks_superseded'], [])
        self.assertTrue(report['verified'])

    def test_affirmative_resolution_still_supersedes_condition_clock(self):
        """Positive control: an un-negated, uncontradicted resolution fact still
        supersedes the danger clock."""
        graph = json.loads(self.world.knowledge_graph)
        graph['facts'].append({
            'id': 'fact_mira_resolved',
            'entity_ids': ['mira'],
            'text': 'Mira was stabilized, revived, and is now conscious.',
            'certainty': 'confirmed',
            'visibility': 'party_known',
        })
        self.world.knowledge_graph = json.dumps(graph)
        self.world.world_state = json.dumps({
            'current_scene': {
                'location_id': 'the_dock',
                'location_name': 'The Dock',
                'active_npc_ids': [],
            },
        })
        db.session.commit()

        self._clock('mira_in_peril', 'Mira in Peril', 4, 2, summary='Mira is drowning at 0 HP.')
        db.session.commit()

        report = self._run_fixture()
        db.session.commit()

        clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='mira_in_peril').one()
        self.assertEqual(clock.status, 'superseded')
        self.assertEqual(report['clocks_superseded'][0]['kind'], 'condition_resolved')
        self.assertTrue(report['verified'])

    def test_was_unconscious_but_now_conscious_supersedes(self):
        """An explicit historical transition ('was unconscious but is now
        conscious') retires the danger clock."""
        graph = json.loads(self.world.knowledge_graph)
        graph['facts'].append({
            'id': 'fact_mira_transition',
            'entity_ids': ['mira'],
            'text': 'Mira was unconscious but is now conscious.',
            'certainty': 'confirmed',
            'visibility': 'party_known',
        })
        self.world.knowledge_graph = json.dumps(graph)
        self.world.world_state = json.dumps({
            'current_scene': {
                'location_id': 'the_dock',
                'location_name': 'The Dock',
                'active_npc_ids': [],
            },
        })
        db.session.commit()

        self._clock('mira_in_peril', 'Mira in Peril', 4, 2, summary='Mira is unconscious at 0 HP.')
        db.session.commit()

        report = self._run_fixture()
        db.session.commit()

        clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='mira_in_peril').one()
        self.assertEqual(clock.status, 'superseded')
        self.assertEqual(report['clocks_superseded'][0]['kind'], 'condition_resolved')
        self.assertTrue(report['verified'])

    def test_was_drowning_but_has_been_rescued_supersedes(self):
        """An explicit historical transition ('was drowning but has been
        rescued') retires the danger clock."""
        graph = json.loads(self.world.knowledge_graph)
        graph['facts'].append({
            'id': 'fact_mira_rescued',
            'entity_ids': ['mira'],
            'text': 'Mira was drowning but has been rescued.',
            'certainty': 'confirmed',
            'visibility': 'party_known',
        })
        self.world.knowledge_graph = json.dumps(graph)
        self.world.world_state = json.dumps({
            'current_scene': {
                'location_id': 'the_dock',
                'location_name': 'The Dock',
                'active_npc_ids': [],
            },
        })
        db.session.commit()

        self._clock('mira_in_peril', 'Mira in Peril', 4, 2, summary='Mira is drowning at 0 HP.')
        db.session.commit()

        report = self._run_fixture()
        db.session.commit()

        clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='mira_in_peril').one()
        self.assertEqual(clock.status, 'superseded')
        self.assertEqual(report['clocks_superseded'][0]['kind'], 'condition_resolved')
        self.assertTrue(report['verified'])

    def test_generic_location_word_does_not_match_inside_larger_word(self):
        """'sea' inside 'season' / 'road' inside 'broad' must not assert a
        generic location binding and falsely supersede a clock."""
        self._clock('mira_in_the_season', 'Mira in the Season', 4, 2, summary='Mira drifts with the tide.')
        db.session.commit()

        report = self._run_fixture()
        db.session.commit()

        clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='mira_in_the_season').one()
        self.assertEqual(clock.status, 'active')
        self.assertEqual(report['clocks_superseded'], [])
        self.assertTrue(report['verified'])

    def test_overlapping_npc_names_do_not_bind_wrong_subject(self):
        """A clock about 'Miranda' must not bind to the NPC 'Mira' whose name is
        a substring, so a conflicting location for Mira does not retire a clock
        that is actually about Miranda."""
        miram = NPCActor(campaign_id=self.campaign.id, actor_id='miranda', name='Miranda', dossier='{}')
        db.session.add(miram)
        db.session.commit()

        self._clock('miranda_in_the_water', 'Miranda in the Water', 4, 2)
        db.session.commit()

        report = self._run_fixture()
        db.session.commit()

        clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='miranda_in_the_water').one()
        self.assertEqual(clock.status, 'active')
        self.assertEqual(report['clocks_superseded'], [])
        self.assertTrue(report['verified'])

    def test_multiple_subjects_not_retired_on_arbitrary_first_match(self):
        """A clock naming more than one known subject must not be superseded
        based on an arbitrary first match. Here Mira has relocated to the dock
        (which alone would retire the clock) but Toren is still in danger, so
        the clock stays active."""
        toren = NPCActor(campaign_id=self.campaign.id, actor_id='toren', name='Toren', dossier='{}')
        db.session.add(toren)
        db.session.commit()

        self._clock(
            'mira_and_toren_in_the_water',
            'Mira and Toren in the Water',
            4,
            2,
            summary='Both are unconscious and being hauled toward the dock.',
        )
        db.session.commit()

        report = self._run_fixture()
        db.session.commit()

        clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='mira_and_toren_in_the_water').one()
        self.assertEqual(clock.status, 'active')
        self.assertEqual(report['clocks_superseded'], [])
        self.assertTrue(report['verified'])
        self.assertTrue(any(
            check['id'] == 'clock_subject_identity' and check['status'] == 'ambiguous'
            for check in report['checks']
        ))

    def test_party_known_clock_not_superseded_by_private_fact(self):
        """A party-known danger clock must not be superseded based on a
        dm_private resolution/location fact, and no party-visible surface may
        reveal the private fact."""
        graph = json.loads(self.world.knowledge_graph)
        graph['facts'].append({
            'id': 'fact_mira_private_stabilization',
            'entity_ids': ['mira'],
            'text': 'Mira was secretly stabilized by the dockmaster\'s private ritual.',
            'certainty': 'confirmed',
            'visibility': 'dm_private',
        })
        self.world.knowledge_graph = json.dumps(graph)
        # Mira is NOT in the visible scene cast, so the only binding for her is
        # the private fact.
        self.world.world_state = json.dumps({
            'current_scene': {
                'location_id': 'the_dock',
                'location_name': 'The Dock',
                'active_npc_ids': [],
            },
        })
        db.session.commit()

        self._clock(
            'mira_in_the_water',
            'Mira in the Water',
            4,
            2,
            summary='Mira is drowning and unconscious at 0 HP.',
            visibility='party_known',
        )
        db.session.commit()

        report = self._run_fixture()
        db.session.commit()

        clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='mira_in_the_water').one()
        self.assertEqual(clock.status, 'active')
        self.assertEqual(report['clocks_superseded'], [])
        self.assertTrue(report['verified'])

        from models import WorldEvent
        supersede_events = WorldEvent.query.filter_by(
            campaign_id=self.campaign.id,
            event_type='clock_superseded',
        ).all()
        self.assertEqual(supersede_events, [])
        for event in WorldEvent.query.filter_by(campaign_id=self.campaign.id).all():
            summary = event.summary or ''
            if event.visibility in {'public', 'party_known'}:
                self.assertNotIn('private ritual', summary)
                self.assertNotIn('secretly stabilized', summary)
        self.assertNotIn('private ritual', clock.summary)
        self.assertNotIn('secretly stabilized', clock.summary)

    def test_party_known_clock_superseded_by_party_fact(self):
        """Positive control: a party-visible fact about the same subject still
        supersedes the party-known clock, showing the gate is visibility-based."""
        graph = json.loads(self.world.knowledge_graph)
        graph['facts'].append({
            'id': 'fact_mira_pulled_ashore',
            'entity_ids': ['mira'],
            'text': 'Mira was pulled onto the dock by the crew.',
            'certainty': 'confirmed',
            'visibility': 'party_known',
        })
        self.world.knowledge_graph = json.dumps(graph)
        self.world.world_state = json.dumps({
            'current_scene': {
                'location_id': 'the_dock',
                'location_name': 'The Dock',
                'active_npc_ids': [],
            },
        })
        db.session.commit()

        self._clock(
            'mira_in_the_water',
            'Mira in the Water',
            4,
            2,
            summary='Mira is drowning and unconscious at 0 HP.',
            visibility='party_known',
        )
        db.session.commit()

        report = self._run_fixture()
        db.session.commit()

        clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='mira_in_the_water').one()
        self.assertEqual(clock.status, 'superseded')
        self.assertEqual(report['clocks_superseded'][0]['kind'], 'subject_relocated')
        self.assertTrue(report['verified'])

    def test_memory_failure_does_not_bump_revision_for_retryable_patch(self):
        """A memory failure must not advance world.memory_revision, so a stored
        failed patch carrying the pre-failure base_memory_revision remains
        retryable instead of tripping the stale check."""
        world = db.session.get(CampaignWorld, self.world.id)
        world.memory_revision = 3
        db.session.commit()

        from services.dm_turns import begin_session_dm_turn
        db.session.add(begin_session_dm_turn(
            self.campaign.id,
            self.session.id,
            1,
            'session_dm:session_1:message_1',
        ))
        db.session.commit()

        from routes.sessions import _run_session_memory_update
        with patch('routes.sessions.get_session_memory_patch', side_effect=RuntimeError('memory failed')):
            _run_session_memory_update(
                campaign_id=self.campaign.id,
                session_id=self.session.id,
                user_id=self.user.id,
                player_message_id=1,
                player_content='Hello',
                ai_text='Hi',
                hot_context={},
                parent_trace_id='test:trace',
                dm_message_id=2,
            )

        world = db.session.get(CampaignWorld, self.world.id)
        self.assertEqual(world.memory_revision, 3)

        turn = SessionDmTurn.query.filter_by(player_message_id=1).first()
        self.assertEqual(turn.post_turn_status, 'error')
        self.assertEqual(turn.post_turn_revision, 3)

        # The stored failed patch (base 3) is still applicable after recovery.
        from services.dm_tools import apply_compiled_session_memory_patch
        stored_patch = {
            'source_contract': 'compiled_session_memory_v2',
            'base_memory_revision': 3,
            'running_summary': 'The party recovered the lead at the dock.',
        }
        apply_compiled_session_memory_patch(self.campaign, self.session, stored_patch)
        db.session.commit()

        world = db.session.get(CampaignWorld, self.world.id)
        self.assertEqual(world.memory_revision, 4)
        self.assertEqual(db.session.get(CampaignSession, self.session.id).running_summary, 'The party recovered the lead at the dock.')

    def test_failure_recovery_order_apply_memory_then_clock_then_summary(self):
        """End-to-end failure→recovery ordering (#105): recover/apply the stored
        memory patch, replay skipped clock adjudication, then finalize the
        summary from the resulting committed state before declaring complete."""
        from services.dm_turns import begin_session_dm_turn
        world = db.session.get(CampaignWorld, self.world.id)
        world.memory_revision = 3
        db.session.commit()
        self._clock('trapped_ferrymen', 'Trapped Ferrymen', 3, 1)
        db.session.add(begin_session_dm_turn(
            self.campaign.id,
            self.session.id,
            1,
            'session_dm:session_1:message_1',
        ))
        db.session.commit()

        # 1) Simulate the memory failure that left the turn recoverable.
        from routes.sessions import _run_session_memory_update
        with patch('routes.sessions.get_session_memory_patch', side_effect=RuntimeError('memory failed')):
            _run_session_memory_update(
                campaign_id=self.campaign.id,
                session_id=self.session.id,
                user_id=self.user.id,
                player_message_id=1,
                player_content='We hold the gate.',
                ai_text='The ferrymen press against the gate.',
                hot_context={},
                parent_trace_id='test:trace',
                dm_message_id=2,
            )
        turn = SessionDmTurn.query.filter_by(player_message_id=1).first()
        self.assertEqual(turn.post_turn_status, 'error')
        self.assertEqual(db.session.get(CampaignWorld, self.world.id).memory_revision, 3)

        # 2) Recover: apply the stored memory patch (base 3).
        from services.dm_tools import apply_clock_adjudication, apply_compiled_session_memory_patch
        stored_patch = {
            'source_contract': 'compiled_session_memory_v2',
            'base_memory_revision': 3,
            'running_summary': 'The party recovered the lead at the dock.',
            'scene_patch': {'location_id': 'the_dock', 'location_name': 'The Dock'},
        }
        apply_compiled_session_memory_patch(self.campaign, self.session, stored_patch)
        db.session.commit()

        # 3) Replay the skipped clock adjudication.
        apply_clock_adjudication(self.campaign, {
            'create_clocks': [],
            'advance_clocks': [{
                'clock_id': 'trapped_ferrymen',
                'delta': 1,
                'reason': 'The ferrymen pressed onward.',
                'evidence': ['The DM confirmed movement.'],
            }],
            'retire_clocks': [],
            'no_change_explanations': [],
        })
        db.session.commit()
        clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='trapped_ferrymen').one()
        self.assertEqual(clock.filled, 2)

        # 4) Finalize the summary from the resulting committed state.
        from services.dm_tools import build_session_summary_finalize_context
        context = build_session_summary_finalize_context(
            self.campaign,
            self.session,
            'We hold the gate.',
            'The ferrymen press against the gate.',
            player_message_id=1,
            dm_message_id=2,
        )
        trapped = next(c for c in context['active_clocks'] if c['clock_id'] == 'trapped_ferrymen')
        self.assertEqual(trapped['filled'], 2)
        self.session.running_summary = 'The ferrymen press onward; the clock now sits at 2/3.'
        world = db.session.get(CampaignWorld, self.world.id)
        world.memory_revision = (world.memory_revision or 0) + 1
        db.session.commit()

        # 5) Declare complete with the correlated terminal revision.
        turn = SessionDmTurn.query.filter_by(player_message_id=1).first()
        from services.dm_turns import mark_session_dm_turn_post_turn_complete
        mark_session_dm_turn_post_turn_complete(
            1,
            dm_message_id=2,
            memory_status='complete',
            clock_status='complete',
            post_turn_revision=world.memory_revision,
        )
        db.session.commit()

        self.assertEqual(turn.post_turn_status, 'complete')
        self.assertEqual(turn.post_turn_revision, world.memory_revision)
        self.assertEqual(
            db.session.get(CampaignSession, self.session.id).running_summary,
            'The ferrymen press onward; the clock now sits at 2/3.',
        )

    def test_memory_failure_still_reconciles_durable_state(self):
        """Even when memory fails (turn reports error), deterministic clock
        repairs still bring durable clock state to one coherent state."""
        self._clock('trapped_ferrymen', 'Trapped Ferrymen', 3, 2)
        self._clock('mira_in_the_water', 'Mira in the Water', 4, 2, summary='Mira is unconscious at 0 HP and being hauled toward the dock.')
        db.session.commit()

        # Memory fails; clock processing is skipped. Reconciliation still runs
        # in the error path and repairs the durable surfaces.
        from services.dm_turns import begin_session_dm_turn
        db.session.add(begin_session_dm_turn(
            self.campaign.id,
            self.session.id,
            1,
            'session_dm:session_1:message_1',
        ))
        db.session.commit()

        from routes.sessions import _run_session_memory_update
        with patch('routes.sessions.get_session_memory_patch', side_effect=RuntimeError('memory failed')):
            _run_session_memory_update(
                campaign_id=self.campaign.id,
                session_id=self.session.id,
                user_id=self.user.id,
                player_message_id=1,
                player_content='Hello',
                ai_text='Hi',
                hot_context={},
                parent_trace_id='test:trace',
                dm_message_id=2,
            )

        turn = SessionDmTurn.query.filter_by(player_message_id=1).first()
        self.assertEqual(turn.post_turn_status, 'error')
        self.assertEqual(turn.memory_status, 'error')
        self.assertIsNotNone(turn.post_turn_revision)

        water_clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='mira_in_the_water').one()
        self.assertEqual(water_clock.status, 'superseded')

    def test_late_retried_adjudication_is_idempotent(self):
        self._clock('trapped_ferrymen', 'Trapped Ferrymen', 3, 2)
        self._clock('mira_in_the_water', 'Mira in the Water', 4, 2, summary='Mira is unconscious at 0 HP and being hauled toward the dock.')
        db.session.commit()

        first = self._run_fixture()
        db.session.commit()
        second = self._run_fixture()
        db.session.commit()

        self.assertTrue(first['clocks_superseded'])
        self.assertEqual(second['clocks_superseded'], [])
        # Re-running reconciliation still emits a fresh correlated revision.
        self.assertGreaterEqual(second['terminal_revision'], first['terminal_revision'])
        water_clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='mira_in_the_water').one()
        self.assertEqual(water_clock.status, 'superseded')

    def test_ambiguous_subject_location_raises_incident(self):
        graph = json.loads(self.world.knowledge_graph)
        graph['facts'].extend([
            {
                'id': 'fact_mira_dock',
                'entity_ids': ['mira'],
                'text': 'Mira was pulled onto the dock.',
                'certainty': 'confirmed',
                'visibility': 'party_known',
            },
            {
                'id': 'fact_mira_river',
                'entity_ids': ['mira'],
                'text': 'Mira was carried back into the river.',
                'certainty': 'confirmed',
                'visibility': 'party_known',
            },
        ])
        self.world.knowledge_graph = json.dumps(graph)
        self.world.world_state = json.dumps({
            'current_scene': {
                'location_id': 'the_dock',
                'location_name': 'The Dock',
                'active_npc_ids': [],
            },
        })
        db.session.commit()

        self._clock('mira_in_the_water', 'Mira in the Water', 4, 2)
        db.session.commit()

        with self.assertRaises(PostTurnConsistencyIncident) as ctx:
            self._run_fixture()
        self.assertIn('ambiguous', ctx.exception.summary)
        self.assertIsNotNone(ctx.exception.terminal_revision)
        self.assertIn('verified', ctx.exception.report)

    def test_route_sets_correlated_revision_on_complete_turn(self):
        token = generate_token(self.user.id)
        self._clock('trapped_ferrymen', 'Trapped Ferrymen', 3, 2)
        self.session.running_summary = 'The Trapped Ferrymen clock sits at 1/3.'
        db.session.commit()

        with patch('routes.sessions.get_session_dm_response_with_tools', return_value={
            'mode': 'speak',
            'content': 'The ferrymen press against the gate.',
            'parts': [{'type': 'narration', 'content': 'The ferrymen press against the gate.'}],
            'commit_action_ids': [],
        }), \
                patch('routes.sessions.get_session_memory_patch', return_value={
                    'source_contract': 'compiled_session_memory_v2',
                    'running_summary': 'The Trapped Ferrymen clock sits at 1/3.',
                    'scene_patch': {},
                    'upsert_graph_entities': [],
                    'upsert_graph_relations': [],
                    'upsert_graph_facts': [],
                    'create_clocks': [],
                    'retire_clocks': [],
                    'update_npc_actors': [],
                    'record_events': [],
                }), \
                patch('routes.sessions.get_session_clock_updates', return_value={
                    'create_clocks': [],
                    'advance_clocks': [],
                    'retire_clocks': [],
                    'no_change_explanations': [],
                }), \
                patch('routes.sessions.get_session_running_summary_finalize', return_value={
                    'running_summary': 'The Trapped Ferrymen clock sits at 2/3 and the party holds the gate.',
                }) as finalize_mock:
            response = self.client.post(
                f'/api/sessions/{self.session.id}/messages',
                json={'content': 'We hold the gate.', 'role': 'player'},
                headers={'Authorization': f'Bearer {token}'},
            )

        self.assertEqual(response.status_code, 201)
        finalize_mock.assert_called_once()
        player_msg = SessionMessage.query.filter_by(session_id=self.session.id, role='player').first()
        turn = SessionDmTurn.query.filter_by(player_message_id=player_msg.id).first()
        self.assertEqual(turn.post_turn_status, 'complete')
        self.assertIsNotNone(turn.post_turn_revision)
        world = db.session.get(CampaignWorld, self.world.id)
        self.assertEqual(turn.post_turn_revision, world.memory_revision)
        session = db.session.get(CampaignSession, self.session.id)
        self.assertEqual(session.running_summary, 'The Trapped Ferrymen clock sits at 2/3 and the party holds the gate.')


if __name__ == '__main__':
    unittest.main()

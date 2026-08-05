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
    AutomationRunAuditCycle,
    AutomationRunAuditorJob,
    AutomationRunEvent,
    AutomationRunProviderCall,
    AutomationSnapshot,
    Campaign,
    CampaignAuditEvent,
    EncounterMap,
    EncounterMapPlacement,
    CampaignMember,
    CampaignSession,
    CampaignWorld,
    Character,
    SheetProposal,
    SessionMessage,
    User,
    db,
)
from services.automation_auditor import (
    _auditor_user_prompt,
    aggregate_completed_auditor_jobs,
    execute_auditor_tool,
    request_auditor_decision_with_tools,
)
from services.character_service import update_character_relations


class AutomationRouteTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_root_path = app.root_path
        app.root_path = self.temp_dir.name

        with app.app_context():
            db.session.execute(db.text("PRAGMA foreign_keys=OFF"))
            db.drop_all()
            db.create_all()

            owner = User(username='owner', email='owner@example.com')
            owner.set_password('password')
            db.session.add(owner)
            db.session.flush()

            viewer = User(username='viewer', email='viewer@example.com')
            viewer.set_password('password')
            db.session.add(viewer)
            db.session.flush()

            campaign = Campaign(name='Automation Source', user_id=owner.id)
            db.session.add(campaign)
            db.session.flush()

            character = Character(
                user_id=owner.id,
                campaign_id=campaign.id,
                name='Seraphina Duskweaver',
                race='Tiefling',
                background='Charlatan',
            )
            db.session.add(character)
            db.session.flush()
            update_character_relations(character, {'classes': [{'class_name': 'Warlock', 'level': 5}]})

            db.session.add(CampaignMember(
                campaign_id=campaign.id,
                user_id=owner.id,
                role='player',
                selected_character_id=character.id,
                character_ready_at=character.created_at,
            ))

            session = CampaignSession(campaign_id=campaign.id, is_active=True)
            db.session.add(session)
            db.session.flush()
            db.session.add(SessionMessage(session_id=session.id, user_id=owner.id, role='player', content='I check the strange sigil.'))

            db.session.add(SheetProposal(
                session_id=session.id,
                character_id=character.id,
                reason='The sigil scorches your palm.',
                changes=[{'field': 'hp', 'before': 10, 'after': 9}],
                status='pending',
            ))

            encounter_map = EncounterMap(
                campaign_id=campaign.id,
                session_id=session.id,
                title='Mirror Dock',
                prompt='Wet planks over black water',
                image_filename='mirror-dock.png',
                labeled_image_filename='mirror-dock-labeled.png',
                model='gpt-image-1',
                size='1024x1024',
                quality='standard',
                grid_json='{"columns":8,"rows":8}',
                vtt_setup_json='{"map_summary":"Dock fight"}',
                encounter_state_json='{"round":2}',
                setup_status='ready',
            )
            db.session.add(encounter_map)
            db.session.flush()
            db.session.add(EncounterMapPlacement(
                encounter_map_id=encounter_map.id,
                actor_type='player',
                actor_id=str(character.id),
                label=character.name,
                grid_col=2,
                grid_row=3,
            ))

            db.session.add(CampaignAuditEvent(
                campaign_id=campaign.id,
                event_type='dm_silence_chosen',
                source='session_messages',
                actor='session_dm',
                summary='DM chose silence for the current beat.',
                payload='{"player_message_id":1,"decision":{"reason":"Hold tension"}}',
            ))

            db.session.add(CampaignWorld(
                campaign_id=campaign.id,
                public_intro='{"title":"Automation Source","starting_location":"Mirror Dock"}',
                knowledge_graph='{}',
                world_state='{"current_scene":{"location_name":"Mirror Dock","time_of_day":"night"}}',
                dm_private='{}',
            ))
            db.session.commit()

            self.owner_id = owner.id
            self.viewer_id = viewer.id
            self.campaign_id = campaign.id
            self.session_id = session.id
            self.token = generate_token(owner.id)
            self.viewer_token = generate_token(viewer.id)

        self.client = app.test_client()
        self.headers = {'Authorization': f'Bearer {self.token}'}
        self.viewer_headers = {'Authorization': f'Bearer {self.viewer_token}'}

    def tearDown(self):
        app.root_path = self.old_root_path
        with app.app_context():
            db.session.remove()
        self.temp_dir.cleanup()

    def _create_scorecard_run(self, criteria, *, name='Issue 76 Scorecard'):
        # Active templates require an explicit canonical category per criterion.
        # These scoring tests focus on severity/performance/completeness, so default
        # any category-less criterion to a canonical category.
        normalized_criteria = []
        for criterion in criteria:
            item = dict(criterion)
            if not (item.get('category') or '').strip():
                item['category'] = 'operational/runtime reliability'
            normalized_criteria.append(item)
        scorecard_response = self.client.post(
            '/api/automation/scorecards',
            headers=self.headers,
            json={'name': name, 'criteria': normalized_criteria},
        )
        self.assertEqual(scorecard_response.status_code, 201)
        scorecard_id = scorecard_response.get_json()['scorecard']['id']
        scenario_response = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={
                'source_campaign_id': self.campaign_id,
                'scorecard_template_id': scorecard_id,
            },
        )
        self.assertEqual(scenario_response.status_code, 201)
        scenario_id = scenario_response.get_json()['scenario']['id']
        snapshot_response = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/snapshots',
            headers=self.headers,
            json={},
        )
        self.assertEqual(snapshot_response.status_code, 201)
        snapshot_id = snapshot_response.get_json()['snapshot']['id']
        run_response = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': snapshot_id},
        )
        self.assertEqual(run_response.status_code, 201)
        return scenario_id, run_response.get_json()['run']['id']

    def _seed_scored_cycles(self, run_id, cycle_criteria, *, run_status='completed'):
        from services.automation_service import append_run_event, refresh_run_scorecard

        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            run.status = run_status
            for turn_number in range(1, 11):
                append_run_event(
                    run,
                    'turn_result',
                    {'action': 'speak', 'turn_number': turn_number},
                    dedupe_key=f'issue-76-turn:{run_id}:{turn_number}',
                    commit=False,
                    skip_workspace=True,
                )
            cycles = []
            for cycle_number, criteria in enumerate(cycle_criteria, start=1):
                normalized = []
                for criterion_id, status in criteria.items():
                    applicable = status != 'not_applicable'
                    normalized.append({
                        'criterion_id': criterion_id,
                        'status': status,
                        'applicability': {'applicable': applicable},
                    })
                cycle = AutomationRunAuditCycle(
                    run_id=run_id,
                    cycle_number=cycle_number,
                    phase='after_dm',
                    status='audited',
                    scorecard_json={'criteria': normalized},
                    scorecard_summary_json={},
                )
                db.session.add(cycle)
                cycles.append(cycle)
            db.session.commit()
            refresh_run_scorecard(run)
            return [cycle.id for cycle in cycles]

    def test_create_scenario_snapshot_run_and_claim_hidden_clone(self):
        scenario_response = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={'source_campaign_id': self.campaign_id, 'name': 'Night Dock Benchmark'},
        )
        self.assertEqual(scenario_response.status_code, 201)
        scenario_id = scenario_response.get_json()['scenario']['id']

        snapshot_response = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/snapshots',
            headers=self.headers,
            json={'label': 'Night Dock snapshot'},
        )
        self.assertEqual(snapshot_response.status_code, 201)
        snapshot_id = snapshot_response.get_json()['snapshot']['id']

        run_response = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': snapshot_id},
        )
        self.assertEqual(run_response.status_code, 201)
        run_id = run_response.get_json()['run']['id']

        claim_response = self.client.post(
            f'/api/automation/runs/{run_id}/claim',
            headers=self.headers,
            json={'worker_id': 'test-worker'},
        )
        self.assertEqual(claim_response.status_code, 200)
        claim_data = claim_response.get_json()
        self.assertEqual(claim_data['run']['status'], 'claimed')
        self.assertTrue(claim_data['derived_campaign']['is_automation_clone'])

        campaigns_response = self.client.get('/api/campaigns', headers=self.headers)
        self.assertEqual(campaigns_response.status_code, 200)
        campaign_ids = {campaign['id'] for campaign in campaigns_response.get_json()['campaigns']}
        self.assertEqual(campaign_ids, {self.campaign_id})

    def test_snapshot_audit_events_do_not_complete_clone_post_turn_status(self):
        with app.app_context():
            latest_session = CampaignSession.query.order_by(CampaignSession.id.desc()).first()
            latest_message = SessionMessage.query.order_by(SessionMessage.id.desc()).first()
            expected_derived_session_id = (latest_session.id if latest_session else 0) + 1
            cloned_message_count = SessionMessage.query.filter_by(session_id=self.session_id).count()
            expected_player_message_id = (latest_message.id if latest_message else 0) + cloned_message_count + 1
            stale_memory_trace_id = (
                f'session_memory_writer:session_{expected_derived_session_id}:message_{expected_player_message_id}'
            )
            stale_clock_trace_id = (
                f'session_clock_adjudicator:session_{expected_derived_session_id}:message_{expected_player_message_id}'
            )
            db.session.add_all([
                CampaignAuditEvent(
                    campaign_id=self.campaign_id,
                    event_type='memory_patch_applied',
                    source='dm_tools.memory',
                    actor='session_memory_writer',
                    trace_id=stale_memory_trace_id,
                    parent_trace_id=f'session_dm:session_{expected_derived_session_id}:message_{expected_player_message_id}',
                    summary='Historical memory patch from the source campaign.',
                    payload=json.dumps({'session_id': expected_derived_session_id, 'patch': {}, 'result': {}}),
                ),
                CampaignAuditEvent(
                    campaign_id=self.campaign_id,
                    event_type='clock_adjudication_applied',
                    source='session_clock',
                    actor='session_clock_adjudicator',
                    trace_id=stale_clock_trace_id,
                    parent_trace_id=f'session_dm:session_{expected_derived_session_id}:message_{expected_player_message_id}',
                    summary='Historical clock adjudication from the source campaign.',
                    payload=json.dumps({'updates': {}, 'result': {}}),
                ),
            ])
            db.session.commit()

        scenario_id = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={'source_campaign_id': self.campaign_id},
        ).get_json()['scenario']['id']
        snapshot = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/snapshots',
            headers=self.headers,
            json={},
        ).get_json()['snapshot']
        self.assertGreaterEqual(snapshot['metadata']['audit_event_count'], 2)
        run_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': snapshot['id']},
        ).get_json()['run']['id']
        self.client.post(
            f'/api/automation/runs/{run_id}/claim',
            headers=self.headers,
            json={'worker_id': 'worker-a'},
        )

        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            derived_session = CampaignSession.query.filter_by(
                campaign_id=run.derived_campaign_id,
                is_active=True,
            ).one()
            self.assertEqual(derived_session.id, expected_derived_session_id)
            cloned_stale_events = CampaignAuditEvent.query.filter(
                CampaignAuditEvent.campaign_id == run.derived_campaign_id,
                CampaignAuditEvent.trace_id.in_([stale_memory_trace_id, stale_clock_trace_id]),
            ).all()
            self.assertEqual(cloned_stale_events, [])

            player_message = SessionMessage(
                session_id=derived_session.id,
                user_id=self.owner_id,
                role='player',
                content='I test whether the fresh turn is still pending.',
            )
            dm_message = SessionMessage(
                session_id=derived_session.id,
                role='dm',
                content='The switchyard holds its breath.',
            )
            db.session.add_all([player_message, dm_message])
            db.session.flush()
            self.assertEqual(player_message.id, expected_player_message_id)
            dm_trace_id = f'session_dm:session_{derived_session.id}:message_{player_message.id}'
            db.session.add(CampaignAuditEvent(
                campaign_id=run.derived_campaign_id,
                event_type='dm_output_stored',
                source='session_messages',
                actor='session_dm',
                trace_id=dm_trace_id,
                summary='Stored visible session DM response.',
                payload=json.dumps({
                    'session_id': derived_session.id,
                    'player_message_id': player_message.id,
                    'dm_message_id': dm_message.id,
                }),
            ))
            db.session.commit()
            derived_session_id = derived_session.id
            player_message_id = player_message.id

        status_response = self.client.get(
            f'/api/sessions/{derived_session_id}/dm-turn-status?after_message_id={player_message_id}',
            headers=self.headers,
        )
        self.assertEqual(status_response.status_code, 200)
        status = status_response.get_json()
        self.assertEqual(status['status'], 'speak')
        self.assertFalse(status['post_turn_complete'])
        self.assertEqual(status['post_turn_status'], 'pending')
        self.assertEqual(status['memory_status'], 'pending')
        self.assertEqual(status['clock_status'], 'pending')

    def test_run_decision_posts_message_for_derived_run(self):
        scenario_id = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={'source_campaign_id': self.campaign_id},
        ).get_json()['scenario']['id']
        snapshot_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/snapshots',
            headers=self.headers,
            json={},
        ).get_json()['snapshot']['id']
        run_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': snapshot_id},
        ).get_json()['run']['id']
        claim_data = self.client.post(
            f'/api/automation/runs/{run_id}/claim',
            headers=self.headers,
            json={'worker_id': 'test-worker'},
        ).get_json()

        with patch('routes.automation.stream_manager.start_generation') as start_generation:
            response = self.client.post(
                f'/api/automation/runs/{run_id}/decisions',
                headers=self.headers,
                json={
                    'llm_player_id': claim_data['roster'][0]['llm_player_id'] or claim_data['roster'][0]['user_id'],
                    'user_id': claim_data['roster'][0]['user_id'],
                    'decision': {'action': 'speak', 'content': 'Seraphina studies the sigil in silence.'},
                    'worker_id': 'test-worker',
                    'lease_token': claim_data['lease_token'],
                },
            )

        self.assertEqual(response.status_code, 201)
        message = response.get_json()['message']
        self.assertEqual(message['role'], 'player')
        self.assertIn('Seraphina', message['content'])
        start_generation.assert_called_once()

    def test_snapshot_from_clone_session_materializes_session_history(self):
        scenario_id = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={'source_campaign_id': self.campaign_id},
        ).get_json()['scenario']['id']

        with app.app_context():
            # Create dummy snapshot for run FK constraint
            from models import AutomationSnapshot, AutomationRun
            dummy_snap = AutomationSnapshot(
                scenario_id=scenario_id,
                source_campaign_id=self.campaign_id,
                label="Dummy",
                summary="Dummy",
                snapshot_json={},
            )
            db.session.add(dummy_snap)
            db.session.flush()

            clone_campaign = Campaign(
                name='Automation Source [Run 9]',
                user_id=self.owner_id,
                status='automation_run',
                is_automation_clone=True,
                automation_source_campaign_id=self.campaign_id,
            )
            db.session.add(clone_campaign)
            db.session.flush()

            # Create mock run referencing the clone campaign
            mock_run = AutomationRun(
                scenario_id=scenario_id,
                snapshot_id=dummy_snap.id,
                user_id=self.owner_id,
                status='success',
                derived_campaign_id=clone_campaign.id
            )
            db.session.add(mock_run)

            clone_character = Character(
                user_id=self.owner_id,
                campaign_id=clone_campaign.id,
                name='Seraphina Duskweaver',
                race='Tiefling',
                background='Charlatan',
            )
            db.session.add(clone_character)
            db.session.flush()
            update_character_relations(clone_character, {'classes': [{'class_name': 'Warlock', 'level': 5}]})

            db.session.add(CampaignMember(
                campaign_id=clone_campaign.id,
                user_id=self.owner_id,
                role='player',
                selected_character_id=clone_character.id,
                character_ready_at=clone_character.created_at,
            ))

            clone_session = CampaignSession(campaign_id=clone_campaign.id, is_active=True)
            db.session.add(clone_session)
            db.session.flush()
            db.session.add(SessionMessage(
                session_id=clone_session.id,
                user_id=self.owner_id,
                role='player',
                content='I follow Mira deeper into the rail yard.',
            ))
            db.session.add(CampaignWorld(
                campaign_id=clone_campaign.id,
                public_intro='{"title":"Automation Source","starting_location":"Switchyard"}',
                knowledge_graph='{}',
                world_state='{"current_scene":{"location_name":"Hanging Switchyard","time_of_day":"night"}}',
                dm_private='{}',
            ))
            db.session.commit()
            clone_campaign_id = clone_campaign.id
            clone_session_id = clone_session.id

        snapshot_response = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/snapshots',
            headers=self.headers,
            json={'source_session_id': clone_session_id, 'label': 'Clone resume snapshot'},
        )
        self.assertEqual(snapshot_response.status_code, 201)
        snapshot = snapshot_response.get_json()['snapshot']
        self.assertEqual(snapshot['source_campaign_id'], clone_campaign_id)
        self.assertEqual(snapshot['source_session_id'], clone_session_id)
        self.assertEqual(snapshot['metadata']['message_count'], 1)

        run_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': snapshot['id']},
        ).get_json()['run']['id']

        claim_response = self.client.post(
            f'/api/automation/runs/{run_id}/claim',
            headers=self.headers,
            json={'worker_id': 'resume-worker'},
        )
        self.assertEqual(claim_response.status_code, 200)
        claim_payload = claim_response.get_json()
        self.assertIsNotNone(claim_payload['latest_session'])
        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            derived_session = CampaignSession.query.filter_by(campaign_id=run.derived_campaign_id).one()
            derived_messages = SessionMessage.query.filter_by(session_id=derived_session.id).order_by(SessionMessage.id.asc()).all()
        self.assertEqual(len(derived_messages), 1)
        self.assertEqual(derived_messages[0].content, 'I follow Mira deeper into the rail yard.')

    def test_claim_from_empty_source_campaign_does_not_create_fallback_session(self):
        with app.app_context():
            empty_campaign = Campaign(name='Empty Automation Source', user_id=self.owner_id)
            db.session.add(empty_campaign)
            db.session.flush()
            db.session.add(CampaignMember(
                campaign_id=empty_campaign.id,
                user_id=self.owner_id,
                role='player',
            ))
            db.session.commit()
            empty_campaign_id = empty_campaign.id

        scenario_id = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={'source_campaign_id': empty_campaign_id},
        ).get_json()['scenario']['id']
        snapshot_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/snapshots',
            headers=self.headers,
            json={},
        ).get_json()['snapshot']['id']
        run_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': snapshot_id},
        ).get_json()['run']['id']
        claim_response = self.client.post(
            f'/api/automation/runs/{run_id}/claim',
            headers=self.headers,
            json={'worker_id': 'empty-source-worker'},
        )

        self.assertEqual(claim_response.status_code, 200)
        res_data = claim_response.get_json()
        latest_session = res_data['latest_session']
        self.assertIsNone(latest_session)
        
        # Verify separate gameplay readiness preflight reporting
        gr = res_data.get('gameplay_readiness')
        self.assertIsNotNone(gr)
        self.assertFalse(gr['world_present'])
        self.assertFalse(gr['active_session_present'])
        self.assertFalse(gr['opening_dm_present'])
        self.assertFalse(gr['campaign_ready'])

        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            self.assertIsNone(CampaignSession.query.filter_by(campaign_id=run.derived_campaign_id).first())

    def test_zero_turn_run_scorecard_is_not_assessed(self):
        scenario_id = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={'source_campaign_id': self.campaign_id},
        ).get_json()['scenario']['id']
        snapshot_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/snapshots',
            headers=self.headers,
            json={},
        ).get_json()['snapshot']['id']
        run_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': snapshot_id},
        ).get_json()['run']['id']
        self.client.post(
            f'/api/automation/runs/{run_id}/claim',
            headers=self.headers,
            json={'worker_id': 'scorecard-worker'},
        )

        scorecard_response = self.client.get(f'/api/automation/runs/{run_id}/scorecard', headers=self.headers)
        self.assertEqual(scorecard_response.status_code, 200)
        payload = scorecard_response.get_json()
        self.assertEqual(payload['run']['scorecard_summary']['overall_status'], 'not_assessed')
        self.assertIsNone(payload['run']['scorecard_summary']['weighted_score'])
        self.assertEqual(payload['run']['scorecard_summary']['audited_cycle_count'], 0)
        self.assertEqual(payload['run']['scorecard_summary']['completed_turns'], 0)

    def test_run_37_cycle_average_preserves_worst_severity(self):
        criteria = [
            {'id': f'criterion_{index}', 'label': f'Criterion {index}'}
            for index in range(1, 9)
        ]
        _scenario_id, run_id = self._create_scorecard_run(
            criteria,
            name='Run 37 Memory Audit Scorebook',
        )
        run_37_statuses = [
            ['pass', 'pass', 'pass', 'pass', 'not_applicable', 'pass', 'pass', 'pass'],
            ['pass', 'pass', 'pass', 'pass', 'not_applicable', 'pass', 'pass', 'pass'],
            ['pass', 'pass', 'pass', 'pass', 'not_applicable', 'pass', 'pass', 'warn'],
            ['pass', 'pass', 'pass', 'pass', 'not_applicable', 'pass', 'pass', 'pass'],
            ['pass', 'warn', 'pass', 'pass', 'not_applicable', 'pass', 'fail', 'warn'],
            ['pass', 'warn', 'pass', 'pass', 'pass', 'pass', 'warn', 'warn'],
            ['pass', 'warn', 'pass', 'pass', 'not_applicable', 'pass', 'warn', 'warn'],
            ['pass', 'warn', 'pass', 'pass', 'not_applicable', 'pass', 'warn', 'warn'],
            ['pass', 'warn', 'pass', 'pass', 'not_applicable', 'pass', 'warn', 'fail'],
            ['pass', 'warn', 'warn', 'fail', 'not_applicable', 'pass', 'pass', 'fail'],
        ]
        self._seed_scored_cycles(
            run_id,
            [
                {
                    f'criterion_{index}': statuses[index - 1]
                    for index in range(1, 9)
                }
                for statuses in run_37_statuses
            ],
        )

        response = self.client.get(
            f'/api/automation/runs/{run_id}/scorecard',
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        summary = payload['run']['scorecard_summary']
        rows = {row['check_id']: row for row in payload['scorecard']}

        self.assertEqual(summary['scoring_model'], 'cycle_assessment_v2')
        self.assertEqual(summary['severity'], 'fail')
        self.assertEqual(summary['overall_status'], 'fail')
        self.assertEqual(summary['performance_score'], 0.8431)
        self.assertEqual(summary['weighted_score'], 0.8431)
        self.assertEqual(summary['score_numerator'], 129.0)
        self.assertEqual(summary['score_denominator'], 153.0)
        self.assertEqual(summary['assessment_count'], 77)
        self.assertEqual(summary['completeness'], 1.0)

        criterion_7 = rows['custom:criterion_7']
        self.assertEqual(criterion_7['status'], 'fail')
        self.assertEqual(criterion_7['details']['performance_score'], 0.7)
        self.assertEqual(criterion_7['details']['assessment_count'], 10)
        self.assertEqual(criterion_7['details']['score_numerator'], 14.0)
        self.assertEqual(criterion_7['details']['score_denominator'], 20)

    def test_uneven_assessment_counts_score_per_assessment_not_per_criterion(self):
        _scenario_id, run_id = self._create_scorecard_run([
            {'id': 'criterion_a', 'label': 'Criterion A', 'weight': 1},
            {'id': 'criterion_b', 'label': 'Criterion B', 'weight': 1},
        ])
        cycles = []
        for cycle_number in range(1, 11):
            cycle_criteria = {'criterion_a': 'pass'}
            if cycle_number == 10:
                cycle_criteria['criterion_b'] = 'fail'
            cycles.append(cycle_criteria)
        self._seed_scored_cycles(run_id, cycles)

        payload = self.client.get(
            f'/api/automation/runs/{run_id}/scorecard',
            headers=self.headers,
        ).get_json()
        summary = payload['run']['scorecard_summary']
        rows = {row['check_id']: row for row in payload['scorecard']}

        criterion_a = rows['custom:criterion_a']['details']
        criterion_b = rows['custom:criterion_b']['details']
        self.assertEqual(criterion_a['assessment_count'], 10)
        self.assertEqual(criterion_a['score_numerator'], 10.0)
        self.assertEqual(criterion_a['score_denominator'], 10)
        self.assertEqual(criterion_b['assessment_count'], 1)
        self.assertEqual(criterion_b['score_numerator'], 0.0)
        self.assertEqual(criterion_b['score_denominator'], 1)

        custom_numerator = criterion_a['score_numerator'] + criterion_b['score_numerator']
        custom_denominator = (
            criterion_a['score_denominator'] + criterion_b['score_denominator']
        )
        self.assertEqual(custom_numerator / custom_denominator, 10 / 11)

        default_rows = [
            row for row in payload['scorecard']
            if not str(row['check_id']).startswith('custom:')
        ]
        default_numerator = sum(row['details']['score_numerator'] for row in default_rows)
        default_denominator = sum(row['details']['score_denominator'] for row in default_rows)
        expected_score = (custom_numerator + default_numerator) / (
            custom_denominator + default_denominator
        )
        self.assertEqual(summary['performance_score'], round(expected_score, 4))
        self.assertEqual(summary['score_numerator'], round(custom_numerator + default_numerator, 4))
        self.assertEqual(summary['score_denominator'], custom_denominator + default_denominator)
        self.assertEqual(summary['assessment_count'], 11 + len(default_rows))
        # The nine cycles that skip criterion_b are missing applicable assessments:
        # they must lower run completeness without entering the score denominator.
        self.assertEqual(criterion_b['not_assessed_count'], 9)
        self.assertLess(summary['completeness'], 1.0)
        self.assertEqual(
            summary['completeness'],
            round(
                summary['assessment_count'] / summary['applicable_assessment_count'],
                4,
            ),
        )
        self.assertEqual(
            summary['applicable_assessment_count'],
            10 + criterion_b['applicable_assessment_count'] + len(default_rows),
        )

        self.assertEqual(rows['custom:criterion_a']['status'], 'pass')
        self.assertEqual(rows['custom:criterion_b']['status'], 'fail')
        self.assertEqual(summary['severity'], 'fail')

    def test_uneven_assessment_counts_respect_criterion_weights(self):
        _scenario_id, run_id = self._create_scorecard_run([
            {'id': 'weighted_a', 'label': 'Weighted A', 'weight': 3},
            {'id': 'weighted_b', 'label': 'Weighted B', 'weight': 1},
        ])
        self._seed_scored_cycles(
            run_id,
            [
                {'weighted_a': 'pass', 'weighted_b': 'fail'},
                {'weighted_a': 'pass'},
                {'weighted_a': 'pass'},
            ],
        )
        payload = self.client.get(
            f'/api/automation/runs/{run_id}/scorecard',
            headers=self.headers,
        ).get_json()
        rows = {row['check_id']: row for row in payload['scorecard']}
        weighted_a = rows['custom:weighted_a']['details']
        weighted_b = rows['custom:weighted_b']['details']

        self.assertEqual(weighted_a['assessment_count'], 3)
        self.assertEqual(weighted_a['score_numerator'], 9.0)
        self.assertEqual(weighted_a['score_denominator'], 9)
        self.assertEqual(weighted_b['assessment_count'], 1)
        self.assertEqual(weighted_b['score_numerator'], 0.0)
        self.assertEqual(weighted_b['score_denominator'], 1)

        custom_numerator = weighted_a['score_numerator'] + weighted_b['score_numerator']
        custom_denominator = (
            weighted_a['score_denominator'] + weighted_b['score_denominator']
        )
        self.assertEqual(custom_numerator / custom_denominator, 9 / 10)
        self.assertEqual(rows['custom:weighted_a']['status'], 'pass')
        self.assertEqual(rows['custom:weighted_b']['status'], 'fail')

    def test_missing_and_not_applicable_assessments_have_explicit_completeness(self):
        _scenario_id, run_id = self._create_scorecard_run([
            {
                'id': 'weighted_memory',
                'label': 'Weighted Memory',
                'weight': 3,
                'category': 'retrieval or memory use',
            },
            {
                'id': 'phase_only',
                'label': 'Phase Only',
                'weight': 5,
                'category': 'retrieval or memory use',
            },
        ])
        cycle_ids = self._seed_scored_cycles(
            run_id,
            [
                {'weighted_memory': 'pass', 'phase_only': 'not_applicable'},
                {'phase_only': 'not_applicable'},
            ],
            run_status='awaiting_audit',
        )
        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            legacy_cycle = db.session.get(AutomationRunAuditCycle, cycle_ids[0])
            legacy_scorecard = dict(legacy_cycle.scorecard_json or {})
            legacy_criteria = [
                dict(item)
                for item in (legacy_scorecard.get('criteria') or [])
            ]
            for item in legacy_criteria:
                if item.get('criterion_id') == 'phase_only':
                    item['status'] = 'not_assessed'
                    item['applicability'] = {'applicable': False}
            legacy_cycle.scorecard_json = {
                **legacy_scorecard,
                'criteria': legacy_criteria,
            }
            run.awaiting_audit_cycle_id = cycle_ids[-1]
            run.awaiting_audit_phase = 'after_dm'
            db.session.commit()

        run_payload = self.client.get(
            f'/api/automation/runs/{run_id}',
            headers=self.headers,
        ).get_json()
        scorecard_payload = self.client.get(
            f'/api/automation/runs/{run_id}/scorecard',
            headers=self.headers,
        ).get_json()
        bundle_payload = self.client.get(
            f'/api/automation/runs/{run_id}/audit-bundle',
            headers=self.headers,
        ).get_json()

        summary = scorecard_payload['run']['scorecard_summary']
        rows = {row['check_id']: row for row in scorecard_payload['scorecard']}
        weighted_memory = rows['custom:weighted_memory']['details']
        phase_only = rows['custom:phase_only']['details']

        self.assertEqual(weighted_memory['severity'], 'pass')
        self.assertEqual(weighted_memory['performance_score'], 1.0)
        self.assertEqual(weighted_memory['assessment_count'], 1)
        self.assertEqual(weighted_memory['not_assessed_count'], 1)
        self.assertEqual(weighted_memory['missing_assessment_count'], 1)
        self.assertEqual(weighted_memory['completeness'], 0.5)
        self.assertEqual(weighted_memory['score_denominator'], 3)
        self.assertEqual(phase_only['severity'], 'not_applicable')
        self.assertIsNone(phase_only['performance_score'])
        self.assertIsNone(phase_only['completeness'])
        self.assertEqual(phase_only['not_applicable_count'], 2)

        self.assertLess(summary['completeness'], 1.0)
        contract_fields = {
            'scoring_model',
            'severity',
            'performance_score',
            'score_numerator',
            'score_denominator',
            'assessment_count',
            'not_assessed_count',
            'not_applicable_count',
            'completeness',
        }
        self.assertTrue(contract_fields.issubset(summary))
        self.assertEqual(
            run_payload['run']['scorecard_summary'],
            summary,
        )
        self.assertEqual(
            bundle_payload['run']['scorecard_summary'],
            summary,
        )

    def test_baseline_comparison_reports_severity_and_performance_separately(self):
        scenario_id, baseline_run_id = self._create_scorecard_run([
            {
                'id': 'consistency',
                'label': 'Consistency',
                'weight': 4,
            },
        ])
        with app.app_context():
            baseline_run = db.session.get(AutomationRun, baseline_run_id)
            snapshot_id = baseline_run.snapshot_id
        current_response = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': snapshot_id},
        )
        self.assertEqual(current_response.status_code, 201)
        current_run_id = current_response.get_json()['run']['id']

        self._seed_scored_cycles(
            baseline_run_id,
            [
                {'consistency': 'pass'},
                {'consistency': 'fail'},
            ],
        )
        self._seed_scored_cycles(
            current_run_id,
            [
                {'consistency': 'pass'},
                {'consistency': 'pass'},
                {'consistency': 'fail'},
            ],
        )
        with app.app_context():
            current_run = db.session.get(AutomationRun, current_run_id)
            current_run.scenario.baseline_run_id = baseline_run_id
            db.session.commit()

        payload = self.client.get(
            f'/api/automation/runs/{current_run_id}/scorecard',
            headers=self.headers,
        ).get_json()
        comparison = next(
            item
            for item in payload['baseline_comparison']['comparisons']
            if item['check_id'] == 'custom:consistency'
        )

        self.assertEqual(comparison['baseline_severity'], 'fail')
        self.assertEqual(comparison['current_severity'], 'fail')
        self.assertEqual(comparison['severity_relationship'], 'unchanged')
        self.assertEqual(comparison['baseline_performance_score'], 0.5)
        self.assertEqual(comparison['current_performance_score'], 0.6667)
        self.assertEqual(comparison['performance_relationship'], 'better')
        self.assertEqual(comparison['relationship'], 'better')

    def test_run_scorecard_and_compare(self):
        scenario_id = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={'source_campaign_id': self.campaign_id},
        ).get_json()['scenario']['id']
        snapshot_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/snapshots',
            headers=self.headers,
            json={},
        ).get_json()['snapshot']['id']

        run_ids = []
        for _ in range(2):
            run_id = self.client.post(
                f'/api/automation/scenarios/{scenario_id}/runs',
                headers=self.headers,
                json={'snapshot_id': snapshot_id},
            ).get_json()['run']['id']
            self.client.post(f'/api/automation/runs/{run_id}/claim', headers=self.headers, json={'worker_id': 'worker'})
            self.client.post(f'/api/automation/runs/{run_id}/complete', headers=self.headers, json={'status': 'completed'})
            run_ids.append(run_id)

        scorecard_response = self.client.get(f'/api/automation/runs/{run_ids[0]}/scorecard', headers=self.headers)
        self.assertEqual(scorecard_response.status_code, 200)
        self.assertTrue(scorecard_response.get_json()['scorecard'])

        compare_response = self.client.post(
            '/api/automation/compare',
            headers=self.headers,
            json={'left_run_id': run_ids[0], 'right_run_id': run_ids[1]},
        )
        self.assertEqual(compare_response.status_code, 200)
        self.assertTrue(compare_response.get_json()['comparisons'])

    def test_other_users_can_read_and_mutate_shared_automation_workspace(self):
        scorecard_id = self.client.post(
            '/api/automation/scorecards',
            headers=self.headers,
            json={
                'name': 'Shared Audit',
                'criteria': [{'id': 'memory_quality', 'label': 'Memory Quality', 'category': 'retrieval or memory use'}],
            },
        ).get_json()['scorecard']['id']
        scenario_id = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={
                'source_campaign_id': self.campaign_id,
                'scorecard_template_id': scorecard_id,
                'name': 'Shared Scenario',
            },
        ).get_json()['scenario']['id']
        snapshot_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/snapshots',
            headers=self.headers,
            json={},
        ).get_json()['snapshot']['id']
        run_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': snapshot_id},
        ).get_json()['run']['id']
        self.client.post(
            f'/api/automation/runs/{run_id}/claim',
            headers=self.headers,
            json={'worker_id': 'worker-a'},
        )
        self.client.post(
            f'/api/automation/runs/{run_id}/complete',
            headers=self.headers,
            json={'status': 'completed'},
        )

        workspace_response = self.client.get('/api/automation', headers=self.viewer_headers)
        self.assertEqual(workspace_response.status_code, 200)
        workspace = workspace_response.get_json()
        self.assertEqual(workspace['scenarios'][0]['id'], scenario_id)
        self.assertEqual(workspace['scorecards'][0]['id'], scorecard_id)
        self.assertTrue(any(trend['scenario_id'] == scenario_id for trend in workspace['scenario_trends']))

        scenario_response = self.client.get(f'/api/automation/scenarios/{scenario_id}', headers=self.viewer_headers)
        self.assertEqual(scenario_response.status_code, 200)
        self.assertEqual(scenario_response.get_json()['scenario']['id'], scenario_id)
        self.assertTrue(scenario_response.get_json()['viewer_permissions']['manage_scenario'])

        runs_response = self.client.get(f'/api/automation/scenarios/{scenario_id}/runs', headers=self.viewer_headers)
        self.assertEqual(runs_response.status_code, 200)
        self.assertEqual(runs_response.get_json()['runs'][0]['id'], run_id)

        scorecard_response = self.client.get(f'/api/automation/scorecards/{scorecard_id}', headers=self.viewer_headers)
        self.assertEqual(scorecard_response.status_code, 200)
        self.assertEqual(scorecard_response.get_json()['scorecard']['id'], scorecard_id)

        run_response = self.client.get(f'/api/automation/runs/{run_id}', headers=self.viewer_headers)
        self.assertEqual(run_response.status_code, 200)
        self.assertEqual(run_response.get_json()['run']['id'], run_id)
        self.assertTrue(run_response.get_json()['viewer_permissions']['manage_run'])

        compare_response = self.client.post(
            '/api/automation/compare',
            headers=self.viewer_headers,
            json={'left_run_id': run_id, 'right_run_id': run_id},
        )
        self.assertEqual(compare_response.status_code, 200)

        update_scenario_response = self.client.put(
            f'/api/automation/scenarios/{scenario_id}',
            headers=self.viewer_headers,
            json={'name': 'Shared Scenario Updated'},
        )
        self.assertEqual(update_scenario_response.status_code, 200)

        create_run_response = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.viewer_headers,
            json={'snapshot_id': snapshot_id},
        )
        self.assertEqual(create_run_response.status_code, 201)
        second_run_id = create_run_response.get_json()['run']['id']

        stop_response = self.client.post(
            f'/api/automation/runs/{run_id}/stop',
            headers=self.viewer_headers,
            json={},
        )
        self.assertEqual(stop_response.status_code, 200)

        snapshot_response = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/snapshots',
            headers=self.viewer_headers,
            json={'label': 'Viewer snapshot'},
        )
        self.assertEqual(snapshot_response.status_code, 201)

        cleanup_response = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/cleanup',
            headers=self.viewer_headers,
            json={},
        )
        self.assertEqual(cleanup_response.status_code, 200)

        baseline_response = self.client.put(
            f'/api/automation/scenarios/{scenario_id}',
            headers=self.viewer_headers,
            json={'baseline_run_id': second_run_id},
        )
        self.assertEqual(baseline_response.status_code, 200)

    def test_claim_can_reclaim_expired_running_run(self):
        scenario_id = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={'source_campaign_id': self.campaign_id},
        ).get_json()['scenario']['id']
        snapshot_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/snapshots',
            headers=self.headers,
            json={},
        ).get_json()['snapshot']['id']
        run_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': snapshot_id},
        ).get_json()['run']['id']
        self.client.post(
            f'/api/automation/runs/{run_id}/claim',
            headers=self.headers,
            json={'worker_id': 'worker-a'},
        )

        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            run.status = 'running'
            run.worker_id = 'worker-a'
            run.lease_expires_at = utcnow() - timedelta(seconds=5)
            db.session.commit()

        reclaim_response = self.client.post(
            f'/api/automation/runs/{run_id}/claim',
            headers=self.headers,
            json={'worker_id': 'worker-b'},
        )
        self.assertEqual(reclaim_response.status_code, 200)
        reclaim_data = reclaim_response.get_json()
        self.assertTrue(reclaim_data['reclaimed'])
        self.assertEqual(reclaim_data['run']['worker_id'], 'worker-b')
        self.assertEqual(reclaim_data['run']['attempt_count'], 2)

    def test_event_append_is_idempotent_with_dedupe_key(self):
        scenario_id = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={'source_campaign_id': self.campaign_id},
        ).get_json()['scenario']['id']
        snapshot_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/snapshots',
            headers=self.headers,
            json={},
        ).get_json()['snapshot']['id']
        run_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': snapshot_id},
        ).get_json()['run']['id']
        claim = self.client.post(
            f'/api/automation/runs/{run_id}/claim',
            headers=self.headers,
            json={'worker_id': 'worker-a'},
        ).get_json()

        payload = {
            'event_type': 'turn_result',
            'payload': {'action': 'no_action'},
            'worker_id': 'worker-a',
            'lease_token': claim['lease_token'],
            'dedupe_key': 'turn-result-1',
        }
        first = self.client.post(f'/api/automation/runs/{run_id}/events', headers=self.headers, json=payload)
        second = self.client.post(f'/api/automation/runs/{run_id}/events', headers=self.headers, json=payload)

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        with app.app_context():
            rows = AutomationRunEvent.query.filter_by(run_id=run_id, dedupe_key='turn-result-1').all()
            self.assertEqual(len(rows), 1)

    def test_snapshot_materialization_preserves_proposals_and_encounter_state(self):
        scenario_id = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={'source_campaign_id': self.campaign_id},
        ).get_json()['scenario']['id']
        snapshot_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/snapshots',
            headers=self.headers,
            json={},
        ).get_json()['snapshot']['id']
        run_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': snapshot_id},
        ).get_json()['run']['id']
        claim = self.client.post(
            f'/api/automation/runs/{run_id}/claim',
            headers=self.headers,
            json={'worker_id': 'worker-a'},
        ).get_json()

        with app.app_context():
            derived_campaign_id = claim['derived_campaign']['id']
            derived_session = CampaignSession.query.filter_by(campaign_id=derived_campaign_id, is_active=True).first()
            proposals = SheetProposal.query.filter_by(session_id=derived_session.id).all()
            encounter_map = EncounterMap.query.filter_by(campaign_id=derived_campaign_id).first()

            self.assertEqual(len(proposals), 1)
            self.assertEqual(proposals[0].status, 'pending')
            self.assertIsNotNone(encounter_map)
            self.assertEqual(encounter_map.setup_status, 'ready')
            self.assertEqual(len(encounter_map.placements), 1)
            self.assertEqual(encounter_map.placements[0].grid_col, 2)
            self.assertEqual(encounter_map.placements[0].grid_row, 3)

    def test_provider_call_round_trip_and_replay_lookup(self):
        scenario_id = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={'source_campaign_id': self.campaign_id},
        ).get_json()['scenario']['id']
        snapshot_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/snapshots',
            headers=self.headers,
            json={},
        ).get_json()['snapshot']['id']
        run_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': snapshot_id},
        ).get_json()['run']['id']
        claim = self.client.post(
            f'/api/automation/runs/{run_id}/claim',
            headers=self.headers,
            json={'worker_id': 'worker-a'},
        ).get_json()

        create_response = self.client.post(
            f'/api/automation/runs/{run_id}/provider-calls',
            headers=self.headers,
            json={
                'dedupe_key': 'provider-call-1',
                'phase': 'overseer',
                'provider': 'openrouter',
                'model': 'gpt-test',
                'worker_id': 'worker-a',
                'lease_token': claim['lease_token'],
                'request': {'messages': [{'role': 'user', 'content': 'hi'}]},
                'response': {'id': 'resp_123'},
                'parsed_output': {'action': 'no_action'},
                'response_text': '{"action":"no_action"}',
            },
        )
        self.assertEqual(create_response.status_code, 201)

        replay_response = self.client.get(
            f'/api/automation/runs/{run_id}/provider-calls/replay?dedupe_key=provider-call-1',
            headers=self.headers,
        )
        self.assertEqual(replay_response.status_code, 200)
        replay_call = replay_response.get_json()['provider_call']
        self.assertEqual(replay_call['dedupe_key'], 'provider-call-1')
        self.assertEqual(replay_call['parsed_output']['action'], 'no_action')

    def test_scorecard_template_is_reused_and_snapshotted_on_run(self):
        scorecard_response = self.client.post(
            '/api/automation/scorecards',
            headers=self.headers,
            json={
                'name': 'DM Audit v1',
                'instructions': 'Use runtime truth, not vibes.',
                'criteria': [
                    {'id': 'memory_quality', 'label': 'Memory Quality', 'category': 'retrieval or memory use'},
                    {'id': 'story_consistency', 'label': 'Story Consistency', 'category': 'narrative quality'},
                ],
                'defaults': {'pause_phases': ['after_dm']},
            },
        )
        self.assertEqual(scorecard_response.status_code, 201)
        scorecard_id = scorecard_response.get_json()['scorecard']['id']

        scenario_id = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={
                'source_campaign_id': self.campaign_id,
                'scorecard_template_id': scorecard_id,
            },
        ).get_json()['scenario']['id']
        snapshot_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/snapshots',
            headers=self.headers,
            json={},
        ).get_json()['snapshot']['id']
        run_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': snapshot_id},
        ).get_json()['run']['id']

        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            self.assertEqual(run.scorecard_template_json['template_id'], scorecard_id)
            self.assertEqual(run.scorecard_template_json['criteria'][0]['id'], 'memory_quality')
            self.assertEqual(run.runner_config_json['audit_pause_phases'], ['after_dm'])

    def test_audit_config_pause_phases_are_copied_to_queued_run(self):
        scenario_id = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={
                'source_campaign_id': self.campaign_id,
                'audit_config': {'pause_phases': ['after_dm', 'after_player', 'after_dm', 'invalid']},
            },
        ).get_json()['scenario']['id']
        snapshot_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/snapshots',
            headers=self.headers,
            json={},
        ).get_json()['snapshot']['id']
        run_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': snapshot_id},
        ).get_json()['run']['id']

        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            self.assertEqual(run.runner_config_json['audit_pause_phases'], ['after_dm', 'after_player'])

    def test_runner_config_pause_phases_override_audit_config_alias(self):
        scenario_id = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={
                'source_campaign_id': self.campaign_id,
                'runner_config': {'audit_pause_phases': ['after_player']},
                'audit_config': {'audit_pause_phases': ['after_dm']},
            },
        ).get_json()['scenario']['id']
        snapshot_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/snapshots',
            headers=self.headers,
            json={},
        ).get_json()['snapshot']['id']
        run_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': snapshot_id},
        ).get_json()['run']['id']

        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            self.assertEqual(run.runner_config_json['audit_pause_phases'], ['after_player'])

    def test_pause_audit_continue_cycle_updates_custom_scorecard(self):
        scorecard_id = self.client.post(
            '/api/automation/scorecards',
            headers=self.headers,
            json={
                'name': 'DM Audit v1',
                'criteria': [
                    {'id': 'memory_quality', 'label': 'Memory Quality', 'category': 'retrieval or memory use'},
                    {'id': 'story_consistency', 'label': 'Story Consistency', 'category': 'narrative quality'},
                ],
            },
        ).get_json()['scorecard']['id']
        scenario_id = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={
                'source_campaign_id': self.campaign_id,
                'scorecard_template_id': scorecard_id,
            },
        ).get_json()['scenario']['id']
        snapshot_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/snapshots',
            headers=self.headers,
            json={},
        ).get_json()['snapshot']['id']
        run_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': snapshot_id},
        ).get_json()['run']['id']
        claim = self.client.post(
            f'/api/automation/runs/{run_id}/claim',
            headers=self.headers,
            json={'worker_id': 'worker-a'},
        ).get_json()

        pause_response = self.client.post(
            f'/api/automation/runs/{run_id}/pause',
            headers=self.headers,
            json={
                'worker_id': 'worker-a',
                'lease_token': claim['lease_token'],
                'phase': 'after_dm',
                'summary': 'Pause after DM turn',
                'dm_message_id': 99,
                'payload': {'turns_completed': 1},
            },
        )
        self.assertEqual(pause_response.status_code, 200)
        pause_payload = pause_response.get_json()
        cycle_id = pause_payload['audit_cycle']['id']
        self.assertEqual(pause_payload['run']['status'], 'awaiting_audit')
        self.assertTrue(pause_payload['worker_released'])
        self.assertFalse(pause_payload['run']['has_lease_token'])
        self.assertFalse(pause_payload['run']['claimable'])

        continue_before_audit = self.client.post(
            f'/api/automation/runs/{run_id}/continue',
            headers=self.headers,
            json={},
        )
        self.assertEqual(continue_before_audit.status_code, 409)

        audit_response = self.client.post(
            f'/api/automation/runs/{run_id}/audit-cycles/{cycle_id}/audit',
            headers=self.headers,
            json={
                'summary': 'DM kept scene continuity intact.',
                'notes': 'Checked transcript plus world state.',
                'scorecard': {
                    'overall_status': 'pass',
                    'overall_summary': 'Healthy cycle.',
                    'criteria': [
                        {'criterion_id': 'memory_quality', 'status': 'pass', 'summary': 'Memory stayed coherent.'},
                        {'criterion_id': 'story_consistency', 'status': 'warn', 'summary': 'Minor pacing wobble.'},
                    ],
                },
            },
        )
        self.assertEqual(audit_response.status_code, 200)
        self.assertTrue(any(row['check_id'] == 'custom:memory_quality' for row in audit_response.get_json()['scorecard']))

        continue_response = self.client.post(
            f'/api/automation/runs/{run_id}/continue',
            headers=self.headers,
            json={},
        )
        self.assertEqual(continue_response.status_code, 200)
        self.assertEqual(continue_response.get_json()['run']['status'], 'queued')
        self.assertTrue(continue_response.get_json()['run']['claimable'])

        scorecard_response = self.client.get(f'/api/automation/runs/{run_id}/scorecard', headers=self.headers)
        self.assertEqual(scorecard_response.status_code, 200)
        scorecards = {row['check_id']: row for row in scorecard_response.get_json()['scorecard']}
        self.assertEqual(scorecards['custom:memory_quality']['status'], 'pass')
        self.assertEqual(scorecards['custom:story_consistency']['status'], 'warn')
        self.assertEqual(scorecard_response.get_json()['run']['scorecard_summary']['audited_cycle_count'], 1)
        self.assertNotIn('budget_passed', scorecard_response.get_json()['run']['scorecard_summary'])
        self.assertNotIn('budgets', scorecard_response.get_json()['run']['scorecard_summary'])

        with app.app_context():
            cycle = db.session.get(AutomationRunAuditCycle, cycle_id)
            self.assertEqual(cycle.status, 'audited')
            self.assertEqual(cycle.scorecard_summary_json['criteria_assessed_count'], 2)
            self.assertEqual(cycle.scorecard_summary_json['criteria_not_assessed_count'], 0)

    def test_builtin_auditor_config_persists_on_run_creation(self):
        scenario_id = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={
                'source_campaign_id': self.campaign_id,
                'runner_config': {
                    'auditor_config': {
                        'mode': 'built_in',
                        'model': 'opencode-go/deepseek-v4-flash',
                        'count': 2,
                        'auto_continue': True,
                        'target_cycles': 3,
                    },
                },
            },
        ).get_json()['scenario']['id']
        snapshot_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/snapshots',
            headers=self.headers,
            json={},
        ).get_json()['snapshot']['id']
        run_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': snapshot_id},
        ).get_json()['run']['id']

        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            auditor_config = run.runner_config_json['auditor_config']
            self.assertEqual(auditor_config['mode'], 'built_in')
            self.assertEqual(auditor_config['model'], 'opencode-go/deepseek-v4-flash')
            self.assertEqual(auditor_config['count'], 2)
            self.assertTrue(auditor_config['auto_continue'])
            self.assertEqual(auditor_config['target_cycles'], 3)

    def test_auditor_tools_read_runtime_truth_without_mutating(self):
        scenario_id = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={'source_campaign_id': self.campaign_id},
        ).get_json()['scenario']['id']
        snapshot_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/snapshots',
            headers=self.headers,
            json={},
        ).get_json()['snapshot']['id']
        run_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': snapshot_id},
        ).get_json()['run']['id']
        self.client.post(
            f'/api/automation/runs/{run_id}/claim',
            headers=self.headers,
            json={'worker_id': 'worker-a'},
        )

        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            before_messages = SessionMessage.query.count()
            transcript = execute_auditor_tool(run, 'get_transcript', {'limit': 10})
            world_state = execute_auditor_tool(run, 'get_world_state', {})
            audit_events = execute_auditor_tool(run, 'get_audit_events', {'limit': 10})
            memory_results = execute_auditor_tool(run, 'search_campaign_memory', {'query': 'Mirror Dock', 'limit': 5})
            after_messages = SessionMessage.query.count()

            self.assertEqual(before_messages, after_messages)
            self.assertTrue(transcript['messages'])
            self.assertTrue(world_state['has_world'])
            self.assertIn('world_state', world_state)
            self.assertIn('events', audit_events)
            self.assertIn('matches', memory_results)

    def test_auditor_compact_tools_preserve_detail_drilldown(self):
        scenario_id = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={'source_campaign_id': self.campaign_id},
        ).get_json()['scenario']['id']
        snapshot_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/snapshots',
            headers=self.headers,
            json={},
        ).get_json()['snapshot']['id']
        run_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': snapshot_id},
        ).get_json()['run']['id']
        claim = self.client.post(
            f'/api/automation/runs/{run_id}/claim',
            headers=self.headers,
            json={'worker_id': 'worker-a'},
        ).get_json()
        cycle_id = self.client.post(
            f'/api/automation/runs/{run_id}/pause',
            headers=self.headers,
            json={
                'worker_id': 'worker-a',
                'lease_token': claim['lease_token'],
                'phase': 'after_dm',
                'summary': 'Pause after DM turn',
                'payload': {'turns_completed': 1},
            },
        ).get_json()['audit_cycle']['id']

        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            db.session.add(CampaignAuditEvent(
                campaign_id=run.derived_campaign_id,
                event_type='memory_patch_applied',
                source='dm_tools.memory',
                actor='session_memory_writer',
                summary='Applied clone memory patch.',
                payload='{"scene_patch":{"location_name":"Mirror Dock"},"facts":[{"id":"dock_fact"}]}',
            ))
            db.session.add(AutomationRunProviderCall(
                run_id=run_id,
                dedupe_key='test:provider:detail',
                phase='overseer',
                prompt_version_id='test_prompt',
                provider='opencode_go',
                model='deepseek-v4-flash',
                request_json={'messages': [{'role': 'user', 'content': 'Inspect the dock standoff.'}]},
                response_json={'choices': [{'message': {'content': 'Dock standoff summary.'}}]},
                parsed_output_json={'overall_status': 'warn'},
                response_text='Dock standoff summary.',
            ))
            db.session.add(AutomationRunEvent(
                run_id=run_id,
                event_type='player_decision',
                sequence_number=99,
                attempt_number=1,
                dedupe_key='test:run:event',
                payload_json={'speaker': 'Seraphina', 'decision': {'action': 'speak'}},
            ))
            db.session.add(AutomationRunAuditorJob(
                run_id=run_id,
                cycle_id=cycle_id,
                auditor_slot=7,
                status='completed',
                provider='opencode_go',
                model='deepseek-v4-flash',
                provider_call_id=1234,
                tool_call_count=4,
                submitted_scorecard_json={'overall_status': 'warn'},
                tool_trace_json=[{'tool_name': 'get_world_state', 'result': {'huge': True}}],
            ))
            db.session.commit()

            # Reset boundaries on the cycle so the test tool queries can fetch post-pause injected data
            cycle = db.session.get(AutomationRunAuditCycle, cycle_id)
            cycle.payload_json = {}
            db.session.commit()

            run_status = execute_auditor_tool(run, 'get_run_status', {})
            compact_events = execute_auditor_tool(run, 'get_audit_events', {'limit': 5})
            compact_provider_calls = execute_auditor_tool(run, 'get_provider_calls', {'limit': 5})
            compact_run_events = execute_auditor_tool(run, 'get_run_events', {'limit': 5})
            compact_snapshot = execute_auditor_tool(run, 'get_snapshot_manifest', {})
            evidence_packet = execute_auditor_tool(run, 'get_cycle_evidence_packet', {})
            blocked_bulk_events = execute_auditor_tool(run, 'get_audit_events', {'limit': 5, 'include_payload': True})
            blocked_bulk_provider_calls = execute_auditor_tool(run, 'get_provider_calls', {'limit': 5, 'include_artifacts': True})
            blocked_bulk_run_events = execute_auditor_tool(run, 'get_run_events', {'limit': 5, 'include_payload': True})
            blocked_bulk_snapshot = execute_auditor_tool(run, 'get_snapshot_manifest', {'include_payload': True})

            event_id = compact_events['events'][0]['id']
            provider_call_id = compact_provider_calls['provider_calls'][-1]['id']
            run_event_id = compact_run_events['events'][-1]['id']

            event_detail = execute_auditor_tool(run, 'get_audit_event_detail', {'event_id': event_id})
            escalated_event_detail = execute_auditor_tool(run, 'get_audit_event_detail', {'event_id': event_id, 'include_full_payload': True})
            provider_call_detail = execute_auditor_tool(run, 'get_provider_call_detail', {'provider_call_id': provider_call_id})
            run_event_detail = execute_auditor_tool(run, 'get_run_event_detail', {'event_id': run_event_id})
            selected_event_detail = execute_auditor_tool(
                run,
                'get_audit_event_detail',
                {'event_id': event_id, 'paths': ['payload.scene_patch.location_name', 'payload.facts[0].id']},
            )
            selected_provider_call_detail = execute_auditor_tool(
                run,
                'get_provider_call_detail',
                {
                    'provider_call_id': provider_call_id,
                    'request_paths': ['messages[0].content'],
                    'response_paths': ['choices[0].message.content'],
                    'parsed_output_paths': ['overall_status'],
                },
            )
            selected_run_event_detail = execute_auditor_tool(
                run,
                'get_run_event_detail',
                {'event_id': run_event_id, 'paths': ['payload.decision.action']},
            )
            selected_snapshot = execute_auditor_tool(
                run,
                'get_snapshot_manifest',
                {'sections': ['campaign'], 'paths': ['campaign.id']},
            )

            self.assertIn('payload_preview', compact_events['events'][0])
            self.assertNotIn('payload', compact_events['events'][0])
            self.assertIn('artifact_sizes', compact_provider_calls['provider_calls'][-1])
            self.assertNotIn('request', compact_provider_calls['provider_calls'][-1])
            self.assertIn('payload_preview', compact_run_events['events'][-1])
            self.assertNotIn('payload', compact_run_events['events'][-1])
            self.assertIn('metadata', compact_snapshot['snapshot'])
            self.assertNotIn('snapshot', compact_snapshot['snapshot'])
            self.assertEqual(run_status['run']['id'], run_id)
            self.assertEqual(run_status['current_audit_cycle']['id'], cycle_id)
            self.assertEqual(run_status['auditor_jobs'][0]['auditor_slot'], 7)
            self.assertNotIn('tool_trace', run_status['auditor_jobs'][0])
            self.assertNotIn('submitted_scorecard', run_status['auditor_jobs'][0])
            self.assertIn('Bulk audit-event payload fetch is disabled', blocked_bulk_events['error'])
            self.assertIn('Bulk provider artifacts are disabled', blocked_bulk_provider_calls['error'])
            self.assertIn('Bulk run-event payload fetch is disabled', blocked_bulk_run_events['error'])
            self.assertIn('Full snapshot payload fetch is disabled', blocked_bulk_snapshot['error'])

            self.assertEqual(evidence_packet['audit_cycle']['id'], cycle_id)
            self.assertTrue(evidence_packet['recent_audit_events'])
            self.assertTrue(evidence_packet['recent_provider_calls'])
            self.assertTrue(evidence_packet['recent_run_events'])
            self.assertIn('get_audit_event_detail', evidence_packet['follow_up_tools'])

            self.assertIn('escalation_required', event_detail)
            self.assertNotIn('payload', event_detail['event'])
            self.assertIn('payload.scene_patch', event_detail['suggested_paths'])
            self.assertIn('payload', escalated_event_detail['event'])
            self.assertIn('request', provider_call_detail['provider_call'])
            self.assertIn('payload', run_event_detail['event'])

            self.assertEqual(selected_event_detail['selected_paths']['payload.scene_patch.location_name'], 'Mirror Dock')
            self.assertEqual(selected_event_detail['selected_paths']['payload.facts[0].id'], 'dock_fact')
            self.assertNotIn('payload', selected_event_detail['event'])

            self.assertEqual(
                selected_provider_call_detail['selected_request_paths']['request.messages[0].content'],
                'Inspect the dock standoff.',
            )
            self.assertEqual(
                selected_provider_call_detail['selected_response_paths']['response.choices[0].message.content'],
                'Dock standoff summary.',
            )
            self.assertEqual(
                selected_provider_call_detail['selected_parsed_output_paths']['parsed_output.overall_status'],
                'warn',
            )
            self.assertNotIn('request', selected_provider_call_detail['provider_call'])

            self.assertEqual(selected_run_event_detail['selected_paths']['payload.decision.action'], 'speak')
            self.assertNotIn('payload', selected_run_event_detail['event'])

            self.assertIn('campaign', selected_snapshot['selected_sections'])
            self.assertEqual(selected_snapshot['selected_paths']['campaign.id'], self.campaign_id)
            self.assertNotIn('snapshot', selected_snapshot['snapshot'])

    def test_scorecard_template_evidence_requirements_persist_and_reach_auditor_prompt(self):
        scorecard = self.client.post(
            '/api/automation/scorecards',
            headers=self.headers,
            json={
                'name': 'Evidence Requirements Scorecard',
                'criteria': [
                    {
                        'id': 'scene_state',
                        'label': 'Scene State',
                        'description': 'Transcript and world state stay aligned.',
                        'category': 'durable state correctness',
                        'evidence_requirements': [
                            {
                                'surface': 'cycle_evidence_packet.scene_state_summary',
                                'reason': 'Primary scene alignment check.',
                                'priority': 'high',
                                'recommended_tools': ['get_cycle_evidence_packet'],
                            },
                            {
                                'surface': 'audit_event.payload.updates',
                                'reason': 'Verify patch-level scene mutations.',
                                'priority': 'medium',
                                'recommended_tools': ['get_audit_event_detail'],
                            },
                        ],
                    },
                ],
            },
        ).get_json()['scorecard']
        self.assertEqual(scorecard['criteria'][0]['evidence_requirements'][0]['surface'], 'cycle_evidence_packet.scene_state_summary')

        scenario_id = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={'source_campaign_id': self.campaign_id, 'scorecard_template_id': scorecard['id']},
        ).get_json()['scenario']['id']
        snapshot_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/snapshots',
            headers=self.headers,
            json={},
        ).get_json()['snapshot']['id']
        run_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': snapshot_id},
        ).get_json()['run']['id']
        claim = self.client.post(
            f'/api/automation/runs/{run_id}/claim',
            headers=self.headers,
            json={'worker_id': 'worker-a'},
        ).get_json()
        cycle_id = self.client.post(
            f'/api/automation/runs/{run_id}/pause',
            headers=self.headers,
            json={
                'worker_id': 'worker-a',
                'lease_token': claim['lease_token'],
                'phase': 'after_dm',
                'summary': 'Pause after DM turn',
            },
        ).get_json()['audit_cycle']['id']

        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            cycle = db.session.get(AutomationRunAuditCycle, cycle_id)
            prompt = json.loads(_auditor_user_prompt(run, cycle, 1, {'count': 1, 'required_tools': 'runtime_truth_full'}))
            self.assertIn('criterion_workflow', prompt)
            self.assertIn('nominate one primary evidence source per criterion', prompt['recommended_sequence'])
            self.assertIn('mark it not_assessed instead of pass', prompt['criterion_workflow'][2])
            self.assertEqual(prompt['final_response_contract']['overall_status'], 'pass|warn|fail|not_assessed|not_applicable')
            self.assertIn('primary_evidence', prompt['final_response_contract']['criteria'][0])
            self.assertEqual(prompt['final_response_contract']['criteria'][0]['status'], 'pass|warn|fail|not_assessed|not_applicable')
            self.assertEqual(
                prompt['criterion_evidence_requirements'][0]['evidence_requirements'][0]['surface'],
                'cycle_evidence_packet.scene_state_summary',
            )
            self.assertEqual(
                run.scorecard_template_json['criteria'][0]['evidence_requirements'][1]['recommended_tools'],
                ['get_audit_event_detail'],
            )

    def test_builtin_auditor_job_completes_paused_cycle_and_updates_scorecard(self):
        scorecard_id = self.client.post(
            '/api/automation/scorecards',
            headers=self.headers,
            json={
                'name': 'Built-In Auditor Scorecard',
                'criteria': [
                    {'id': 'memory_quality', 'label': 'Memory Quality', 'category': 'retrieval or memory use'},
                    {'id': 'scene_state', 'label': 'Scene State', 'category': 'durable state correctness'},
                ],
            },
        ).get_json()['scorecard']['id']
        scenario_id = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={
                'source_campaign_id': self.campaign_id,
                'scorecard_template_id': scorecard_id,
                'runner_config': {
                    'audit_pause_phases': ['after_dm'],
                    'auditor_config': {'mode': 'built_in', 'count': 1, 'auto_continue': False},
                },
            },
        ).get_json()['scenario']['id']
        snapshot_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/snapshots',
            headers=self.headers,
            json={},
        ).get_json()['snapshot']['id']
        run_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': snapshot_id},
        ).get_json()['run']['id']
        claim = self.client.post(
            f'/api/automation/runs/{run_id}/claim',
            headers=self.headers,
            json={'worker_id': 'worker-a'},
        ).get_json()
        pause = self.client.post(
            f'/api/automation/runs/{run_id}/pause',
            headers=self.headers,
            json={
                'worker_id': 'worker-a',
                'lease_token': claim['lease_token'],
                'phase': 'after_dm',
                'summary': 'Pause after DM turn',
                'payload': {'turns_completed': 1},
            },
        ).get_json()
        cycle_id = pause['audit_cycle']['id']

        def fake_auditor_decision(run, cycle, job, config, **_kwargs):
            from services.automation_service import persist_provider_call

            provider_call, _created = persist_provider_call(run, {
                'dedupe_key': f'auditor:{cycle.id}:slot:{job.auditor_slot}',
                'phase': 'auditor_decision',
                'prompt_version_id': 'test',
                'provider': 'opencode_go',
                'model': 'deepseek-v4-flash',
                'request': {'messages': []},
                'response': {'id': 'test-response'},
                'parsed_output': {'overall_status': 'warn'},
                'response_text': '{"overall_status":"warn"}',
            })
            return {
                'provider': 'opencode_go',
                'model': 'deepseek-v4-flash',
                'provider_call': provider_call,
                'tool_call_count': 3,
                'tool_trace': [{'tool_name': 'get_transcript'}],
                'scorecard': {
                    'overall_status': 'warn',
                    'overall_summary': 'Memory passed, scene state needs attention.',
                    'criteria': [
                        {'criterion_id': 'memory_quality', 'status': 'pass', 'summary': 'Memory held.', 'evidence': 'Transcript and world state agree.'},
                        {'criterion_id': 'scene_state', 'status': 'warn', 'summary': 'Scene drift risk.', 'evidence': 'Clock did not move.'},
                    ],
                    'tool_calls_used': ['get_transcript', 'get_world_state', 'get_clocks'],
                    'unresolved_evidence_gaps': [],
                },
            }

        with patch('services.automation_auditor.request_auditor_decision_with_tools', side_effect=fake_auditor_decision):
            response = self.client.post(
                f'/api/automation/runs/{run_id}/auditors/start',
                headers=self.headers,
                json={'sync': True},
            )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['completed'])
        self.assertEqual(payload['run']['status'], 'awaiting_audit')
        self.assertEqual(payload['auditor_jobs'][0]['status'], 'completed')
        self.assertEqual(payload['auditor_jobs'][0]['tool_call_count'], 3)

        with app.app_context():
            cycle = db.session.get(AutomationRunAuditCycle, cycle_id)
            job = AutomationRunAuditorJob.query.filter_by(run_id=run_id, cycle_id=cycle_id).one()
            provider_call = db.session.get(AutomationRunProviderCall, job.provider_call_id)
            self.assertEqual(cycle.status, 'audited')
            self.assertEqual(job.status, 'completed')
            self.assertEqual(provider_call.phase, 'auditor_decision')
            self.assertEqual(cycle.scorecard_json['tool_calls_used'], ['get_clocks', 'get_transcript', 'get_world_state'])

        scorecard_response = self.client.get(f'/api/automation/runs/{run_id}/scorecard', headers=self.headers)
        scorecards = {row['check_id']: row for row in scorecard_response.get_json()['scorecard']}
        self.assertEqual(scorecards['custom:memory_quality']['status'], 'pass')
        self.assertEqual(scorecards['custom:scene_state']['status'], 'warn')

    def test_canceled_in_flight_auditor_does_not_complete_cycle(self):
        scorecard_id = self.client.post(
            '/api/automation/scorecards',
            headers=self.headers,
            json={
                'name': 'Cancelable Auditor Scorecard',
                'criteria': [{'id': 'memory_quality', 'label': 'Memory Quality', 'category': 'retrieval or memory use'}],
            },
        ).get_json()['scorecard']['id']
        scenario_id = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={
                'source_campaign_id': self.campaign_id,
                'scorecard_template_id': scorecard_id,
                'runner_config': {
                    'audit_pause_phases': ['after_dm'],
                    'auditor_config': {'mode': 'built_in', 'count': 1, 'auto_continue': False},
                },
            },
        ).get_json()['scenario']['id']
        snapshot_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/snapshots',
            headers=self.headers,
            json={},
        ).get_json()['snapshot']['id']
        run_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': snapshot_id},
        ).get_json()['run']['id']
        claim = self.client.post(
            f'/api/automation/runs/{run_id}/claim',
            headers=self.headers,
            json={'worker_id': 'worker-a'},
        ).get_json()
        cycle_id = self.client.post(
            f'/api/automation/runs/{run_id}/pause',
            headers=self.headers,
            json={
                'worker_id': 'worker-a',
                'lease_token': claim['lease_token'],
                'phase': 'after_dm',
                'summary': 'Pause after DM turn',
            },
        ).get_json()['audit_cycle']['id']

        def fake_auditor_decision(run, cycle, job, config, **_kwargs):
            from services.automation_service import persist_provider_call

            provider_call, _created = persist_provider_call(run, {
                'dedupe_key': f'auditor:{cycle.id}:slot:{job.auditor_slot}',
                'phase': 'auditor_decision',
                'prompt_version_id': 'test',
                'provider': 'opencode_go',
                'model': 'deepseek-v4-flash',
                'request': {'messages': []},
                'response': {'id': 'test-response'},
                'parsed_output': {'overall_status': 'pass'},
                'response_text': '{"overall_status":"pass"}',
            })
            job.status = 'canceled'
            job.finished_at = utcnow()
            db.session.commit()
            return {
                'provider': 'opencode_go',
                'model': 'deepseek-v4-flash',
                'provider_call': provider_call,
                'tool_call_count': 1,
                'tool_trace': [{'tool_name': 'get_run_status'}],
                'scorecard': {
                    'overall_status': 'pass',
                    'overall_summary': 'Would have passed.',
                    'criteria': [
                        {'criterion_id': 'memory_quality', 'status': 'pass', 'summary': 'Stable.', 'evidence': 'Run status.'},
                    ],
                    'tool_calls_used': ['get_run_status'],
                    'unresolved_evidence_gaps': [],
                },
            }

        with patch('services.automation_auditor.request_auditor_decision_with_tools', side_effect=fake_auditor_decision):
            response = self.client.post(
                f'/api/automation/runs/{run_id}/auditors/start',
                headers=self.headers,
                json={'sync': True},
            )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertFalse(payload['completed'])
        self.assertEqual(payload['run']['status'], 'awaiting_audit')

        with app.app_context():
            cycle = db.session.get(AutomationRunAuditCycle, cycle_id)
            job = AutomationRunAuditorJob.query.filter_by(run_id=run_id, cycle_id=cycle_id).one()
            self.assertEqual(cycle.status, 'pending')
            self.assertEqual(job.status, 'canceled')
            self.assertIsNotNone(job.provider_call_id)
            self.assertEqual(job.tool_call_count, 1)

    def test_multi_auditor_aggregation_uses_worst_status_per_criterion(self):
        scorecard_id = self.client.post(
            '/api/automation/scorecards',
            headers=self.headers,
            json={
                'name': 'Multi Auditor Scorecard',
                'criteria': [{'id': 'memory_quality', 'label': 'Memory Quality', 'category': 'retrieval or memory use'}],
            },
        ).get_json()['scorecard']['id']
        scenario_id = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={'source_campaign_id': self.campaign_id, 'scorecard_template_id': scorecard_id},
        ).get_json()['scenario']['id']
        snapshot_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/snapshots',
            headers=self.headers,
            json={},
        ).get_json()['snapshot']['id']
        run_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': snapshot_id},
        ).get_json()['run']['id']
        claim = self.client.post(
            f'/api/automation/runs/{run_id}/claim',
            headers=self.headers,
            json={'worker_id': 'worker-a'},
        ).get_json()
        cycle_id = self.client.post(
            f'/api/automation/runs/{run_id}/pause',
            headers=self.headers,
            json={
                'worker_id': 'worker-a',
                'lease_token': claim['lease_token'],
                'phase': 'after_dm',
            },
        ).get_json()['audit_cycle']['id']

        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            cycle = db.session.get(AutomationRunAuditCycle, cycle_id)
            jobs = [
                AutomationRunAuditorJob(
                    run_id=run_id,
                    cycle_id=cycle_id,
                    auditor_slot=1,
                    status='completed',
                    submitted_scorecard_json={
                        'overall_status': 'pass',
                        'overall_summary': 'Looks good.',
                        'criteria': [{'criterion_id': 'memory_quality', 'status': 'pass', 'summary': 'Stable.', 'evidence': 'Transcript.'}],
                        'tool_calls_used': ['get_transcript'],
                    },
                ),
                AutomationRunAuditorJob(
                    run_id=run_id,
                    cycle_id=cycle_id,
                    auditor_slot=2,
                    status='completed',
                    submitted_scorecard_json={
                        'overall_status': 'warn',
                        'overall_summary': 'One gap.',
                        'criteria': [{'criterion_id': 'memory_quality', 'status': 'warn', 'summary': 'Missing clock evidence.', 'evidence': 'Clock table.'}],
                        'tool_calls_used': ['get_clocks'],
                        'unresolved_evidence_gaps': ['No provider-call replay checked.'],
                    },
                ),
            ]
            db.session.add_all(jobs)
            db.session.commit()

            aggregate = aggregate_completed_auditor_jobs(run, cycle, jobs)
            self.assertEqual(aggregate['overall_status'], 'warn')
            self.assertEqual(aggregate['criteria'][0]['status'], 'warn')
            self.assertEqual(aggregate['tool_calls_used'], ['get_clocks', 'get_transcript'])
            self.assertEqual(aggregate['unresolved_evidence_gaps'], ['No provider-call replay checked.'])

    def test_custom_scorecard_not_assessed_does_not_count_as_pass(self):
        scorecard_id = self.client.post(
            '/api/automation/scorecards',
            headers=self.headers,
            json={
                'name': 'Scoped Audit Scorecard',
                'criteria': [{'id': 'retrieval_relevance', 'label': 'Retrieval Relevance', 'category': 'retrieval or memory use'}],
            },
        ).get_json()['scorecard']['id']
        scenario_id = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={'source_campaign_id': self.campaign_id, 'scorecard_template_id': scorecard_id},
        ).get_json()['scenario']['id']
        snapshot_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/snapshots',
            headers=self.headers,
            json={},
        ).get_json()['snapshot']['id']
        run_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': snapshot_id},
        ).get_json()['run']['id']
        claim = self.client.post(
            f'/api/automation/runs/{run_id}/claim',
            headers=self.headers,
            json={'worker_id': 'worker-a'},
        ).get_json()
        cycle_id = self.client.post(
            f'/api/automation/runs/{run_id}/pause',
            headers=self.headers,
            json={
                'worker_id': 'worker-a',
                'lease_token': claim['lease_token'],
                'phase': 'after_dm',
                'summary': 'Pause after DM turn',
            },
        ).get_json()['audit_cycle']['id']

        audit_response = self.client.post(
            f'/api/automation/runs/{run_id}/audit-cycles/{cycle_id}/audit',
            headers=self.headers,
            json={
                'summary': 'Retrieval was not exercised this cycle.',
                'scorecard': {
                    'overall_status': 'pass',
                    'overall_summary': 'No retrieval happened.',
                    'criteria': [
                        {'criterion_id': 'retrieval_relevance', 'status': 'not_assessed', 'summary': 'No retrieval query occurred.'},
                    ],
                },
            },
        )
        self.assertEqual(audit_response.status_code, 200)

        scorecard_response = self.client.get(f'/api/automation/runs/{run_id}/scorecard', headers=self.headers)
        self.assertEqual(scorecard_response.status_code, 200)
        payload = scorecard_response.get_json()
        scorecards = {row['check_id']: row for row in payload['scorecard']}
        self.assertEqual(scorecards['custom:retrieval_relevance']['status'], 'not_assessed')
        self.assertEqual(scorecards['custom:retrieval_relevance']['details']['exercised_cycle_count'], 0)
        self.assertEqual(scorecards['custom:retrieval_relevance']['details']['not_assessed_cycle_count'], 1)

        with app.app_context():
            cycle = db.session.get(AutomationRunAuditCycle, cycle_id)
            self.assertEqual(cycle.scorecard_summary_json['overall_status'], 'not_assessed')
            self.assertEqual(cycle.scorecard_summary_json['criteria_assessed_count'], 0)
            self.assertEqual(cycle.scorecard_summary_json['criteria_not_assessed_count'], 1)

    def test_auditor_tool_loop_executes_tool_and_persists_artifact(self):
        scorecard_id = self.client.post(
            '/api/automation/scorecards',
            headers=self.headers,
            json={
                'name': 'Tool Loop Scorecard',
                'criteria': [{'id': 'memory_quality', 'label': 'Memory Quality', 'category': 'retrieval or memory use'}],
            },
        ).get_json()['scorecard']['id']
        scenario_id = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={
                'source_campaign_id': self.campaign_id,
                'scorecard_template_id': scorecard_id,
            },
        ).get_json()['scenario']['id']
        snapshot_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/snapshots',
            headers=self.headers,
            json={},
        ).get_json()['snapshot']['id']
        run_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': snapshot_id},
        ).get_json()['run']['id']
        claim = self.client.post(
            f'/api/automation/runs/{run_id}/claim',
            headers=self.headers,
            json={'worker_id': 'worker-a'},
        ).get_json()
        cycle_id = self.client.post(
            f'/api/automation/runs/{run_id}/pause',
            headers=self.headers,
            json={
                'worker_id': 'worker-a',
                'lease_token': claim['lease_token'],
                'phase': 'after_dm',
            },
        ).get_json()['audit_cycle']['id']

        from llm_providers import OpenRouterAdapter

        tool_response = OpenRouterAdapter().parse_response({
            'id': 'resp-tool',
            'choices': [{
                'message': {
                    'content': '',
                    'tool_calls': [{
                        'id': 'call_1',
                        'function': {'name': 'get_run_status', 'arguments': '{}'},
                    }],
                },
                'finish_reason': 'tool_calls',
            }],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15},
        })
        final_response = OpenRouterAdapter().parse_response({
            'id': 'resp-final',
            'choices': [{
                'message': {
                    'content': json.dumps({
                        'overall_status': 'pass',
                        'overall_summary': 'Tool-backed audit passed.',
                        'criteria': [{
                            'criterion_id': 'memory_quality',
                            'status': 'pass',
                            'summary': 'Memory evidence was available.',
                            'evidence': 'get_run_status returned the current audit cycle.',
                        }],
                        'tool_calls_used': ['get_run_status'],
                        'unresolved_evidence_gaps': [],
                    }),
                },
                'finish_reason': 'stop',
            }],
            'usage': {'prompt_tokens': 20, 'completion_tokens': 10, 'total_tokens': 30},
        })

        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            cycle = db.session.get(AutomationRunAuditCycle, cycle_id)
            job = AutomationRunAuditorJob(run_id=run_id, cycle_id=cycle_id, auditor_slot=1, status='queued')
            db.session.add(job)
            db.session.commit()

            with patch('services.automation_auditor._post_chat_normalized', side_effect=[tool_response, final_response]):
                result = request_auditor_decision_with_tools(
                    run,
                    cycle,
                    job,
                    {'mode': 'built_in', 'count': 1, 'model': 'opencode-go/deepseek-v4-flash'},
                )

            self.assertEqual(result['tool_call_count'], 1)
            self.assertEqual(result['tool_trace'][0]['tool_name'], 'get_run_status')
            self.assertEqual(result['scorecard']['overall_status'], 'pass')
            provider_call = result['provider_call']
            self.assertEqual(provider_call.phase, 'auditor_decision')
            self.assertEqual(provider_call.parsed_output_json['tool_calls_used'], ['get_run_status'])
            self.assertEqual(provider_call.usage_total_tokens, 45)

    def test_pause_endpoint_idempotency(self):
        scenario_id = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={'source_campaign_id': self.campaign_id, 'name': 'Benchmark Pause'},
        ).get_json()['scenario']['id']
        snapshot_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/snapshots',
            headers=self.headers,
            json={},
        ).get_json()['snapshot']['id']
        run_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': snapshot_id},
        ).get_json()['run']['id']
        claim = self.client.post(
            f'/api/automation/runs/{run_id}/claim',
            headers=self.headers,
            json={'worker_id': 'worker-pauser'},
        ).get_json()

        # 1. Duplicate after_player pause returns same cycle
        pause_1_resp = self.client.post(
            f'/api/automation/runs/{run_id}/pause',
            headers=self.headers,
            json={
                'worker_id': 'worker-pauser',
                'lease_token': claim['lease_token'],
                'phase': 'after_player',
                'player_message_id': 9901,
            },
        )
        self.assertEqual(pause_1_resp.status_code, 200)
        cycle_1 = pause_1_resp.get_json()['audit_cycle']

        pause_2_resp = self.client.post(
            f'/api/automation/runs/{run_id}/pause',
            headers=self.headers,
            json={
                'worker_id': 'worker-pauser',
                'lease_token': claim['lease_token'],
                'phase': 'after_player',
                'player_message_id': 9901,
            },
        )
        self.assertEqual(pause_2_resp.status_code, 200)
        cycle_2 = pause_2_resp.get_json()['audit_cycle']
        self.assertEqual(cycle_1['id'], cycle_2['id'])

        # Reset run status to running
        with app.app_context():
            from models import AutomationRun
            r = db.session.get(AutomationRun, run_id)
            r.status = 'running'
            r.awaiting_audit_cycle_id = None
            r.awaiting_audit_phase = None
            r.worker_id = 'worker-pauser'
            r.lease_token = claim['lease_token']
            r.lease_expires_at = utcnow() + timedelta(minutes=5)
            db.session.commit()

        # 2. Duplicate after_dm pause with same dm_message_id
        pause_3_resp = self.client.post(
            f'/api/automation/runs/{run_id}/pause',
            headers=self.headers,
            json={
                'worker_id': 'worker-pauser',
                'lease_token': claim['lease_token'],
                'phase': 'after_dm',
                'player_message_id': 9901,
                'dm_message_id': 9902,
            },
        )
        self.assertEqual(pause_3_resp.status_code, 200)
        cycle_3 = pause_3_resp.get_json()['audit_cycle']

        pause_4_resp = self.client.post(
            f'/api/automation/runs/{run_id}/pause',
            headers=self.headers,
            json={
                'worker_id': 'worker-pauser',
                'lease_token': claim['lease_token'],
                'phase': 'after_dm',
                'player_message_id': 9901,
                'dm_message_id': 9902,
            },
        )
        self.assertEqual(pause_4_resp.status_code, 200)
        cycle_4 = pause_4_resp.get_json()['audit_cycle']
        self.assertEqual(cycle_3['id'], cycle_4['id'])

        # Reset run status to running
        with app.app_context():
            from models import AutomationRun
            r = db.session.get(AutomationRun, run_id)
            r.status = 'running'
            r.awaiting_audit_cycle_id = None
            r.awaiting_audit_phase = None
            r.worker_id = 'worker-pauser'
            r.lease_token = claim['lease_token']
            r.lease_expires_at = utcnow() + timedelta(minutes=5)
            db.session.commit()

        # 3. Duplicate after_dm pause with same player_message_id but no dm_message_id (silent/empty)
        pause_5_resp = self.client.post(
            f'/api/automation/runs/{run_id}/pause',
            headers=self.headers,
            json={
                'worker_id': 'worker-pauser',
                'lease_token': claim['lease_token'],
                'phase': 'after_dm',
                'player_message_id': 9903,
            },
        )
        self.assertEqual(pause_5_resp.status_code, 200)
        cycle_5 = pause_5_resp.get_json()['audit_cycle']

        pause_6_resp = self.client.post(
            f'/api/automation/runs/{run_id}/pause',
            headers=self.headers,
            json={
                'worker_id': 'worker-pauser',
                'lease_token': claim['lease_token'],
                'phase': 'after_dm',
                'player_message_id': 9903,
            },
        )
        self.assertEqual(pause_6_resp.status_code, 200)
        cycle_6 = pause_6_resp.get_json()['audit_cycle']
        self.assertEqual(cycle_5['id'], cycle_6['id'])

        # Reset run status to running
        with app.app_context():
            from models import AutomationRun
            r = db.session.get(AutomationRun, run_id)
            r.status = 'running'
            r.awaiting_audit_cycle_id = None
            r.awaiting_audit_phase = None
            r.worker_id = 'worker-pauser'
            r.lease_token = claim['lease_token']
            r.lease_expires_at = utcnow() + timedelta(minutes=5)
            db.session.commit()

        # 4. Existing audited/skipped cycle does NOT force run back into awaiting_audit
        with app.app_context():
            from models import AutomationRunAuditCycle, AutomationRun
            c5 = db.session.get(AutomationRunAuditCycle, cycle_5['id'])
            c5.status = 'skipped'
            r = db.session.get(AutomationRun, run_id)
            r.status = 'running'
            r.awaiting_audit_cycle_id = None
            r.awaiting_audit_phase = None
            r.worker_id = 'worker-pauser'
            r.lease_token = claim['lease_token']
            r.lease_expires_at = utcnow() + timedelta(minutes=5)
            db.session.commit()

        pause_7_resp = self.client.post(
            f'/api/automation/runs/{run_id}/pause',
            headers=self.headers,
            json={
                'worker_id': 'worker-pauser',
                'lease_token': claim['lease_token'],
                'phase': 'after_dm',
                'player_message_id': 9903,
            },
        )
        self.assertEqual(pause_7_resp.status_code, 200)
        res_data = pause_7_resp.get_json()
        self.assertEqual(res_data['run']['status'], 'running')
        self.assertIsNone(res_data['run']['awaiting_audit_cycle_id'])
        self.assertFalse(res_data['paused'])

        # 5. Run already awaiting a different audit cycle returns 409
        with app.app_context():
            from models import AutomationRunAuditCycle, AutomationRun
            c1 = db.session.get(AutomationRunAuditCycle, cycle_1['id'])
            c1.status = 'pending'
            r = db.session.get(AutomationRun, run_id)
            r.status = 'awaiting_audit'
            r.awaiting_audit_cycle_id = c1.id
            r.awaiting_audit_phase = 'after_player'
            db.session.commit()

        pause_8_resp = self.client.post(
            f'/api/automation/runs/{run_id}/pause',
            headers=self.headers,
            json={
                'worker_id': 'worker-pauser',
                'lease_token': claim['lease_token'],
                'phase': 'after_dm',
                'player_message_id': 9904,
            },
        )
        self.assertEqual(pause_8_resp.status_code, 409)
        self.assertIn('already awaiting a different audit cycle', pause_8_resp.get_json()['error'])

    def test_p1_features_integration(self):
        scorecard = self.client.post(
            '/api/automation/scorecards',
            headers=self.headers,
            json={
                'name': 'P1 Testing Scorecard',
                'criteria': [
                    {'id': 'criterion_a', 'label': 'Criterion A', 'category': 'operational/runtime reliability'},
                    {'id': 'criterion_b', 'label': 'Criterion B', 'category': 'narrative quality'}
                ]
            }
        ).get_json()['scorecard']
        
        scenario_id = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={'source_campaign_id': self.campaign_id, 'scorecard_template_id': scorecard['id']},
        ).get_json()['scenario']['id']
        snapshot_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/snapshots',
            headers=self.headers,
            json={},
        ).get_json()['snapshot']['id']
        run_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': snapshot_id},
        ).get_json()['run']['id']
        claim = self.client.post(
            f'/api/automation/runs/{run_id}/claim',
            headers=self.headers,
            json={'worker_id': 'worker-p1'},
        ).get_json()
        cycle_id = self.client.post(
            f'/api/automation/runs/{run_id}/pause',
            headers=self.headers,
            json={
                'worker_id': 'worker-p1',
                'lease_token': claim['lease_token'],
                'phase': 'after_dm',
                'summary': 'Pause for P1 test',
            },
        ).get_json()['audit_cycle']['id']

        audit_payload = {
            'source': 'manual_auditor',
            'criteria': [
                {
                    'id': 'criterion_a',
                    'status': 'pass',
                    'summary': 'Criteria A passed',
                    'evidence_refs': [
                        {
                            'kind': 'session_message',
                            'id': 123,
                            'path': 'payload.some.path',
                            'summary': 'Nice message',
                            'visibility': 'public'
                        }
                    ],
                    'applicability': {
                        'applicable': True,
                        'reason': 'applicable for after_dm',
                        'phase': 'after_dm'
                    }
                },
                {
                    'id': 'criterion_b',
                    'status': 'not_assessed',
                    'summary': 'Criteria B is N/A',
                    'evidence_refs': [],
                    'applicability': {
                        'applicable': False,
                        'reason': 'not_applicable_for_phase',
                        'phase': 'after_dm'
                    },
                    'random_unknown_field': 'should_be_rejected'
                }
            ]
        }
        
        submit_resp = self.client.post(
            f'/api/automation/runs/{run_id}/audit-cycles/{cycle_id}/audit',
            headers=self.headers,
            json={'scorecard': audit_payload}
        )
        self.assertEqual(submit_resp.status_code, 200)
        
        with app.app_context():
            from models import AutomationRunAuditCycle, AutomationRun
            cycle = db.session.get(AutomationRunAuditCycle, cycle_id)
            crit_a = next(c for c in cycle.scorecard_json['criteria'] if c['criterion_id'] == 'criterion_a')
            self.assertEqual(crit_a['status'], 'pass')
            self.assertEqual(crit_a['evidence_refs'][0]['id'], 123)
            self.assertEqual(crit_a['evidence_refs'][0]['kind'], 'session_message')
            self.assertTrue(crit_a['applicability']['applicable'])
            
            crit_b = next(c for c in cycle.scorecard_json['criteria'] if c['criterion_id'] == 'criterion_b')
            self.assertEqual(crit_b['status'], 'not_applicable')
            self.assertFalse(crit_b['applicability']['applicable'])
            self.assertNotIn('random_unknown_field', crit_b)
            
            self.assertEqual(cycle.scorecard_summary_json['criteria_assessed_count'], 1)
            self.assertEqual(cycle.scorecard_summary_json['criteria_not_assessed_count'], 0)
            self.assertEqual(cycle.scorecard_summary_json['criteria_not_applicable_count'], 1)
            
            run = db.session.get(AutomationRun, run_id)
            from services.automation_service import refresh_run_scorecard
            scorecard_res = refresh_run_scorecard(run)
            crit_b_res = next(r for r in scorecard_res if r['check_id'] == 'custom:criterion_b')
            self.assertEqual(crit_b_res['status'], 'not_applicable')
            self.assertEqual(crit_b_res['details']['not_applicable_cycle_count'], 1)

        # Test get_private_candidates works when there is no CampaignWorld
        with app.app_context():
            from models import CampaignWorld, NPCActor
            from services.automation_auditor import get_private_candidates
            CampaignWorld.query.filter_by(campaign_id=self.campaign_id).delete()
            npc = NPCActor(
                campaign_id=self.campaign_id,
                actor_id='test_npc_unbound',
                name='NPC Unbound',
                dossier='{"secret_note": "UnboundSecretValue"}'
            )
            db.session.add(npc)
            db.session.commit()
            
            candidates = get_private_candidates(self.campaign_id)
            self.assertTrue(any(c['text'] == 'UnboundSecretValue' for c in candidates))

        bundle_resp = self.client.get(
            f'/api/automation/runs/{run_id}/audit-bundle',
            headers=self.headers
        )
        self.assertEqual(bundle_resp.status_code, 200)
        bundle = bundle_resp.get_json()
        self.assertEqual(bundle['manifest_version'], 'audit_bundle_v1')
        self.assertIn('evidence_gaps', bundle)
        self.assertIn('model_context_exposure', bundle)
        
        # Verify raw notes are redacted and not leaked in bundle
        npc_summaries = bundle['evidence_packet']['active_npc_summaries']
        for npc_item in npc_summaries:
            self.assertNotIn('private_notes_preview', npc_item)
            self.assertNotIn('UnboundSecretValue', str(npc_item))
            self.assertTrue(npc_item.get('has_private_notes'))
            self.assertIsNotNone(npc_item.get('private_notes_hash'))
        
        debug_resp = self.client.get(
            f'/api/automation/runs/{run_id}/debug-summary',
            headers=self.headers
        )
        self.assertEqual(debug_resp.status_code, 200)
        summary = debug_resp.get_json()
        self.assertEqual(summary['run_id'], run_id)
        self.assertIn('stuck_reasons', summary)
        self.assertFalse(summary['lease']['has_lease_token'])
        self.assertNotIn('lease_token', summary['lease'])

        # Test applicability parsing string booleans
        audit_payload_string_bool = {
            'source': 'manual_auditor',
            'criteria': [
                {
                    'id': 'criterion_a',
                    'status': 'pass',
                    'applicability': {
                        'applicable': 'false',
                        'reason': 'string false'
                    }
                },
                {
                    'id': 'criterion_b',
                    'status': 'not_assessed',
                    'applicability': {
                        'applicable': False,
                        'reason': 'string false'
                    }
                }
            ]
        }
        submit_resp_2 = self.client.post(
            f'/api/automation/runs/{run_id}/audit-cycles/{cycle_id}/audit',
            headers=self.headers,
            json={'scorecard': audit_payload_string_bool}
        )
        self.assertEqual(submit_resp_2.status_code, 200)
        with app.app_context():
            cycle_db = db.session.get(AutomationRunAuditCycle, cycle_id)
            crit_a = next(c for c in cycle_db.scorecard_json['criteria'] if c['criterion_id'] == 'criterion_a')
            self.assertFalse(crit_a['applicability']['applicable'])

        # Test empty/invalid evidence refs
        audit_payload_invalid_refs = {
            'source': 'manual_auditor',
            'criteria': [
                {
                    'id': 'criterion_a',
                    'status': 'pass',
                    'evidence_refs': [
                        {}, 
                        {'kind': 'session_message'}, 
                        {'kind': 'session_message', 'id': 999, 'visibility': 'invalid_vis'}
                    ]
                },
                {
                    'id': 'criterion_b',
                    'status': 'not_assessed',
                    'applicability': {'applicable': False}
                }
            ]
        }
        submit_resp_3 = self.client.post(
            f'/api/automation/runs/{run_id}/audit-cycles/{cycle_id}/audit',
            headers=self.headers,
            json={'scorecard': audit_payload_invalid_refs}
        )
        self.assertEqual(submit_resp_3.status_code, 200)
        with app.app_context():
            cycle_db = db.session.get(AutomationRunAuditCycle, cycle_id)
            crit_a = next(c for c in cycle_db.scorecard_json['criteria'] if c['criterion_id'] == 'criterion_a')
            self.assertEqual(len(crit_a['evidence_refs']), 1)
            self.assertEqual(crit_a['evidence_refs'][0]['visibility'], 'unknown')

        # Test empty criteria fallback
        audit_payload_empty_criteria = {
            'source': 'manual_auditor',
            'overall_status': 'fail',
            'criteria': []
        }
        submit_resp_4 = self.client.post(
            f'/api/automation/runs/{run_id}/audit-cycles/{cycle_id}/audit',
            headers=self.headers,
            json={'scorecard': audit_payload_empty_criteria}
        )
        self.assertEqual(submit_resp_4.status_code, 422)
        self.assertEqual(submit_resp_4.get_json()['error']['code'], 'empty_criteria')

        # Test missing status is rejected instead of defaulting to warn
        audit_payload_missing_status = {
            'source': 'manual_auditor',
            'criteria': [
                {'id': 'criterion_a'},
                {'id': 'criterion_b', 'status': 'pass'},
            ]
        }
        submit_resp_5 = self.client.post(
            f'/api/automation/runs/{run_id}/audit-cycles/{cycle_id}/audit',
            headers=self.headers,
            json={'scorecard': audit_payload_missing_status}
        )
        self.assertEqual(submit_resp_5.status_code, 422)
        submit_5_json = submit_resp_5.get_json()
        self.assertEqual(submit_5_json['error']['code'], 'missing_status')
        self.assertEqual(submit_5_json['error']['details']['criterion_id'], 'criterion_a')

        # Test falsey / non-string / unknown status values are rejected
        for bad_status in (False, [], '', 0, 'bogus'):
            audit_payload_bad_status = {
                'source': 'manual_auditor',
                'criteria': [
                    {'id': 'criterion_a', 'status': bad_status},
                    {'id': 'criterion_b', 'status': 'pass'},
                ]
            }
            submit_resp_bad = self.client.post(
                f'/api/automation/runs/{run_id}/audit-cycles/{cycle_id}/audit',
                headers=self.headers,
                json={'scorecard': audit_payload_bad_status}
            )
            self.assertEqual(
                submit_resp_bad.status_code,
                422,
                f'expected 422 for status {bad_status!r}',
            )
            self.assertEqual(submit_resp_bad.get_json()['error']['code'], 'invalid_status')

        # Test truthy non-string text fields are rejected as 422, not 500
        for field in ('summary', 'primary_evidence', 'evidence'):
            audit_payload_bad_text = {
                'source': 'manual_auditor',
                'criteria': [
                    {'id': 'criterion_a', 'status': 'pass', field: 123},
                    {'id': 'criterion_b', 'status': 'pass'},
                ]
            }
            submit_resp_text = self.client.post(
                f'/api/automation/runs/{run_id}/audit-cycles/{cycle_id}/audit',
                headers=self.headers,
                json={'scorecard': audit_payload_bad_text}
            )
            self.assertEqual(
                submit_resp_text.status_code,
                422,
                f'expected 422 for non-string {field}',
            )
            submit_text_json = submit_resp_text.get_json()
            self.assertEqual(submit_text_json['error']['code'], 'invalid_field_type')
            self.assertEqual(submit_text_json['error']['details']['field'], field)

    def test_fully_scored_cycle_count_ignores_malformed_stored_statuses(self):
        _scenario_id, run_id = self._create_scorecard_run([
            {'id': 'criterion_a', 'label': 'Criterion A'},
            {'id': 'criterion_b', 'label': 'Criterion B'},
        ])
        from services.automation_service import append_run_event, refresh_run_scorecard

        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            run.status = 'completed'
            for turn_number in range(1, 4):
                append_run_event(
                    run,
                    'turn_result',
                    {'action': 'speak', 'turn_number': turn_number},
                    dedupe_key=f'issue-80-count-turn:{run_id}:{turn_number}',
                    commit=False,
                    skip_workspace=True,
                )
            full_cycle = AutomationRunAuditCycle(
                run_id=run_id,
                cycle_number=1,
                phase='after_dm',
                status='audited',
                scorecard_json={'criteria': [
                    {'criterion_id': 'criterion_a', 'status': 'pass', 'applicability': {'applicable': True}},
                    {'criterion_id': 'criterion_b', 'status': 'warn', 'applicability': {'applicable': True}},
                ]},
                scorecard_summary_json={},
            )
            missing_status_cycle = AutomationRunAuditCycle(
                run_id=run_id,
                cycle_number=2,
                phase='after_dm',
                status='audited',
                scorecard_json={'criteria': [
                    {'criterion_id': 'criterion_a'},
                    {'criterion_id': 'criterion_b', 'status': 'pass', 'applicability': {'applicable': True}},
                ]},
                scorecard_summary_json={},
            )
            bad_row_cycle = AutomationRunAuditCycle(
                run_id=run_id,
                cycle_number=3,
                phase='after_dm',
                status='audited',
                scorecard_json={'criteria': [
                    'not-a-dict',
                    {'criterion_id': 'criterion_a', 'status': 'pass', 'applicability': {'applicable': True}},
                    {'criterion_id': 'criterion_b', 'status': 'pass', 'applicability': {'applicable': True}},
                ]},
                scorecard_summary_json={},
            )
            invalid_status_cycle = AutomationRunAuditCycle(
                run_id=run_id,
                cycle_number=4,
                phase='after_dm',
                status='audited',
                scorecard_json={'criteria': [
                    {'criterion_id': 'criterion_a', 'status': 'garbage', 'applicability': {'applicable': True}},
                    {'criterion_id': 'criterion_b', 'status': 'pass', 'applicability': {'applicable': True}},
                ]},
                scorecard_summary_json={},
            )
            db.session.add_all([full_cycle, missing_status_cycle, bad_row_cycle, invalid_status_cycle])
            db.session.commit()
            refresh_run_scorecard(run)

        response = self.client.get(f'/api/automation/runs/{run_id}/scorecard', headers=self.headers)
        self.assertEqual(response.status_code, 200)
        summary = response.get_json()['run']['scorecard_summary']
        self.assertEqual(summary['audited_cycle_count'], 4)
        self.assertEqual(summary['fully_scored_cycle_count'], 1)

    def test_replay_repairs_malformed_cycle_and_refreshes_counts(self):
        _scenario_id, run_id = self._create_scorecard_run([
            {'id': 'criterion_a', 'label': 'Criterion A'},
            {'id': 'criterion_b', 'label': 'Criterion B'},
        ])
        from services.automation_service import append_run_event, refresh_run_scorecard

        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            run.status = 'completed'
            for turn_number in range(1, 4):
                append_run_event(
                    run,
                    'turn_result',
                    {'action': 'speak', 'turn_number': turn_number},
                    dedupe_key=f'issue-80-replay-turn:{run_id}:{turn_number}',
                    commit=False,
                    skip_workspace=True,
                )
            cycle = AutomationRunAuditCycle(
                run_id=run_id,
                cycle_number=1,
                phase='after_dm',
                status='audited',
                scorecard_json={'criteria': [
                    {'criterion_id': 'criterion_a'},
                    {'criterion_id': 'criterion_b', 'status': 'pass', 'applicability': {'applicable': True}},
                ]},
                scorecard_summary_json={},
            )
            db.session.add(cycle)
            db.session.commit()
            cycle_id = cycle.id
            refresh_run_scorecard(run)

        response = self.client.get(f'/api/automation/runs/{run_id}/scorecard', headers=self.headers)
        summary = response.get_json()['run']['scorecard_summary']
        self.assertEqual(summary['audited_cycle_count'], 1)
        self.assertEqual(summary['fully_scored_cycle_count'], 0)

        replay_resp = self.client.post(
            f'/api/automation/runs/{run_id}/audit-cycles/{cycle_id}/replay',
            headers=self.headers,
            json={
                'scorecard': {
                    'source': 'repair_replay',
                    'criteria': [
                        {'id': 'criterion_a', 'status': 'pass', 'applicability': {'applicable': True}},
                        {'id': 'criterion_b', 'status': 'pass', 'applicability': {'applicable': True}},
                    ],
                },
            },
        )
        self.assertEqual(replay_resp.status_code, 200)
        self.assertTrue(replay_resp.get_json()['replayed'])

        response = self.client.get(f'/api/automation/runs/{run_id}/scorecard', headers=self.headers)
        summary = response.get_json()['run']['scorecard_summary']
        self.assertEqual(summary['audited_cycle_count'], 1)
        self.assertEqual(summary['fully_scored_cycle_count'], 1)

        with app.app_context():
            cycle_db = db.session.get(AutomationRunAuditCycle, cycle_id)
            repaired = {item['criterion_id']: item for item in cycle_db.scorecard_json['criteria']}
            self.assertEqual(repaired['criterion_a']['status'], 'pass')
            self.assertEqual(repaired['criterion_b']['status'], 'pass')

    def test_security_redaction_and_lease_token_safety(self):
        scorecard = self.client.post(
            '/api/automation/scorecards',
            headers=self.headers,
            json={
                'name': 'Security Testing Scorecard',
                'criteria': [{'id': 'criterion_sec', 'label': 'Security Criterion', 'category': 'safety/private-information handling'}]
            }
        ).get_json()['scorecard']
        
        scenario_id = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={'source_campaign_id': self.campaign_id, 'scorecard_template_id': scorecard['id']},
        ).get_json()['scenario']['id']
        
        snapshot_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/snapshots',
            headers=self.headers,
            json={},
        ).get_json()['snapshot']['id']
        
        run_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': snapshot_id},
        ).get_json()['run']['id']
        
        # Claim the run
        claim_resp = self.client.post(
            f'/api/automation/runs/{run_id}/claim',
            headers=self.headers,
            json={'worker_id': 'worker-security'},
        )
        self.assertEqual(claim_resp.status_code, 200)
        claim = claim_resp.get_json()
        
        # Verify root lease_token is present but NOT inside run dict
        self.assertIn('lease_token', claim)
        self.assertIsNotNone(claim['lease_token'])
        expected_token = claim['lease_token']
        self.assertNotIn('lease_token', claim['run'])
        self.assertTrue(claim['run']['has_lease_token'])

        # Query GET /api/automation/runs/<run_id> and verify no lease_token key exists
        run_resp = self.client.get(
            f'/api/automation/runs/{run_id}',
            headers=self.headers
        )
        self.assertEqual(run_resp.status_code, 200)
        run_json = run_resp.get_json()
        run_dict_serialized = json.dumps(run_json)
        self.assertNotIn(expected_token, run_dict_serialized)
        self.assertNotIn('"lease_token"', run_dict_serialized)
        self.assertTrue(run_json['run']['has_lease_token'])

        # Verify run_claimed event does not leak token
        events_resp = self.client.post(
            f'/api/automation/runs/{run_id}/auditor-tools/get_run_events',
            headers=self.headers,
            json={'args': {'include_payload': False}}
        )
        self.assertEqual(events_resp.status_code, 200)
        events_serialized = json.dumps(events_resp.get_json())
        self.assertNotIn(expected_token, events_serialized)
        self.assertNotIn('"lease_token"', events_serialized)

        # Pause to create a cycle so we can test audit bundle
        cycle_resp = self.client.post(
            f'/api/automation/runs/{run_id}/pause',
            headers=self.headers,
            json={
                'worker_id': 'worker-security',
                'lease_token': expected_token,
                'phase': 'after_dm',
                'summary': 'Pause for security audit',
            },
        )
        self.assertEqual(cycle_resp.status_code, 200)
        cycle_id = cycle_resp.get_json()['audit_cycle']['id']

        # Inject sensitive logs/events into DB to verify redaction utilities
        with app.app_context():
            from models import CampaignAuditEvent, AutomationRunProviderCall, AutomationRunEvent, AutomationRun
            run_db = db.session.get(AutomationRun, run_id)
            derived_campaign_id = run_db.derived_campaign_id
            
            # Audit event with API Key
            audit_ev = CampaignAuditEvent(
                campaign_id=derived_campaign_id,
                event_type='test_security',
                summary='test security event',
                payload=json.dumps({
                    'api_key': 'super-secret-key-123',
                    'client_secret': 'client-secret-999',
                    'usage_input_tokens': 150, # Should NOT be redacted
                    'token_count': 45
                })
            )
            db.session.add(audit_ev)
            
            # Provider call with passwords/tokens
            pc = AutomationRunProviderCall(
                run_id=run_id,
                dedupe_key='test-security-pc-1',
                phase='after_dm',
                request_json={
                    'authorization': 'Bearer some-auth-token-1234',
                    'api_key': 'provider-api-key-abc',
                    'usage_input_tokens': 100
                },
                response_json={
                    'access_token': 'oauth-access-token-xyz',
                    'usage_output_tokens': 200
                },
                parsed_output_json={
                    'secret_data': 'sensitive info',
                    'usage_total_tokens': 300
                }
            )
            db.session.add(pc)
            
            # Run event with password
            run_ev = AutomationRunEvent(
                run_id=run_id,
                event_type='user_action',
                sequence_number=10,
                dedupe_key='test-user-action-ev-10',
                payload_json={
                    'password': 'my-secure-password-789',
                    'normal_field': 'not-sensitive'
                }
            )
            db.session.add(run_ev)

            # Legacy Run event with lease_token
            legacy_run_ev = AutomationRunEvent(
                run_id=run_id,
                event_type='run_claimed',
                sequence_number=11,
                dedupe_key='test-run-claimed-ev-11',
                payload_json={
                    'lease_token': expected_token,
                    'normal_field': 'not-sensitive'
                }
            )
            db.session.add(legacy_run_ev)
            db.session.commit()

            # Reset boundaries on the cycle so the test tool queries can fetch post-pause injected data
            cycle = db.session.get(AutomationRunAuditCycle, cycle_id)
            cycle.payload_json = {}
            db.session.commit()
            
            audit_ev_id = audit_ev.id
            pc_id = pc.id
            run_ev_id = run_ev.id
            legacy_run_ev_id = legacy_run_ev.id

        # Verify get_audit_event_detail redacts secrets but preserves token counts
        audit_detail_resp = self.client.post(
            f'/api/automation/runs/{run_id}/auditor-tools/get_audit_event_detail',
            headers=self.headers,
            json={'args': {'event_id': audit_ev_id, 'paths': ['payload.api_key', 'payload.client_secret', 'payload.usage_input_tokens', 'payload.token_count']}}
        )
        self.assertEqual(audit_detail_resp.status_code, 200)
        audit_detail = audit_detail_resp.get_json()['result']
        self.assertEqual(audit_detail['selected_paths'].get('payload.api_key'), '[REDACTED]')
        self.assertEqual(audit_detail['selected_paths'].get('payload.client_secret'), '[REDACTED]')
        self.assertEqual(audit_detail['selected_paths'].get('payload.usage_input_tokens'), 150)
        self.assertEqual(audit_detail['selected_paths'].get('payload.token_count'), 45)

        # Assert get_audit_event_detail with include_full_payload=True
        audit_full_resp = self.client.post(
            f'/api/automation/runs/{run_id}/auditor-tools/get_audit_event_detail',
            headers=self.headers,
            json={'args': {'event_id': audit_ev_id, 'include_full_payload': True}}
        )
        self.assertEqual(audit_full_resp.status_code, 200)
        audit_full_data = audit_full_resp.get_json()['result']
        self.assertNotIn('super-secret-key-123', json.dumps(audit_full_data))
        self.assertNotIn('client-secret-999', json.dumps(audit_full_data))
        # Ensure secret keys are filtered from payload_keys in metadata
        self.assertNotIn('api_key', audit_full_data['event']['payload_keys'])
        self.assertNotIn('client_secret', audit_full_data['event']['payload_keys'])
        self.assertEqual(audit_full_data['event']['redacted_payload_key_count'], 2)
        self.assertTrue(audit_full_data['event']['has_redacted_payload_keys'])

        # Verify get_run_event_detail redacts secrets
        run_detail_resp = self.client.post(
            f'/api/automation/runs/{run_id}/auditor-tools/get_run_event_detail',
            headers=self.headers,
            json={'args': {'event_id': run_ev_id, 'paths': ['payload.password', 'payload.normal_field']}}
        )
        self.assertEqual(run_detail_resp.status_code, 200)
        run_detail = run_detail_resp.get_json()['result']
        self.assertEqual(run_detail['selected_paths'].get('payload.password'), '[REDACTED]')
        self.assertEqual(run_detail['selected_paths'].get('payload.normal_field'), 'not-sensitive')

        # Assert neither lease_token value nor key string "lease_token" appears in run events
        run_events_resp = self.client.post(
            f'/api/automation/runs/{run_id}/auditor-tools/get_run_events',
            headers=self.headers,
            json={'args': {'include_payload': False}}
        )
        self.assertEqual(run_events_resp.status_code, 200)
        run_events_serialized = json.dumps(run_events_resp.get_json())
        self.assertNotIn(expected_token, run_events_serialized)
        self.assertNotIn('"lease_token"', run_events_serialized)

        # Assert neither lease_token value nor key string "lease_token" appears in get_run_event_detail with no paths
        run_detail_no_paths_resp = self.client.post(
            f'/api/automation/runs/{run_id}/auditor-tools/get_run_event_detail',
            headers=self.headers,
            json={'args': {'event_id': legacy_run_ev_id}}
        )
        self.assertEqual(run_detail_no_paths_resp.status_code, 200)
        run_detail_no_paths_serialized = json.dumps(run_detail_no_paths_resp.get_json())
        self.assertNotIn(expected_token, run_detail_no_paths_serialized)
        self.assertNotIn('"lease_token"', run_detail_no_paths_serialized)

        # Assert neither lease_token value nor key string "lease_token" appears in get_run_event_detail with paths=["payload.lease_token"]
        run_detail_paths_resp = self.client.post(
            f'/api/automation/runs/{run_id}/auditor-tools/get_run_event_detail',
            headers=self.headers,
            json={'args': {'event_id': legacy_run_ev_id, 'paths': ['payload.lease_token']}}
        )
        self.assertEqual(run_detail_paths_resp.status_code, 200)
        run_detail_paths_serialized = json.dumps(run_detail_paths_resp.get_json())
        self.assertNotIn(expected_token, run_detail_paths_serialized)
        self.assertNotIn('"lease_token"', run_detail_paths_serialized)

        # Verify get_provider_call_detail redacts secrets
        pc_detail_resp = self.client.post(
            f'/api/automation/runs/{run_id}/auditor-tools/get_provider_call_detail',
            headers=self.headers,
            json={
                'args': {
                    'provider_call_id': pc_id,
                    'request_paths': ['authorization', 'api_key', 'usage_input_tokens'],
                    'response_paths': ['access_token', 'usage_output_tokens'],
                    'parsed_output_paths': ['secret_data', 'usage_total_tokens']
                }
            }
        )
        self.assertEqual(pc_detail_resp.status_code, 200)
        pc_detail = pc_detail_resp.get_json()['result']
        self.assertEqual(pc_detail['selected_request_paths'].get('request.authorization'), '[REDACTED]')
        self.assertEqual(pc_detail['selected_request_paths'].get('request.api_key'), '[REDACTED]')
        self.assertEqual(pc_detail['selected_request_paths'].get('request.usage_input_tokens'), 100)
        
        self.assertEqual(pc_detail['selected_response_paths'].get('response.access_token'), '[REDACTED]')
        self.assertEqual(pc_detail['selected_response_paths'].get('response.usage_output_tokens'), 200)
        
        self.assertEqual(pc_detail['selected_parsed_output_paths'].get('parsed_output.secret_data'), '[REDACTED]')
        self.assertEqual(pc_detail['selected_parsed_output_paths'].get('parsed_output.usage_total_tokens'), 300)

        # Assert get_provider_call_detail with no path args returns redacted artifacts and does not include raw secret values
        pc_no_paths_resp = self.client.post(
            f'/api/automation/runs/{run_id}/auditor-tools/get_provider_call_detail',
            headers=self.headers,
            json={'args': {'provider_call_id': pc_id}}
        )
        self.assertEqual(pc_no_paths_resp.status_code, 200)
        pc_no_paths_data = pc_no_paths_resp.get_json()['result']
        pc_no_paths_serialized = json.dumps(pc_no_paths_data)
        self.assertNotIn('Bearer some-auth-token-1234', pc_no_paths_serialized)
        self.assertNotIn('provider-api-key-abc', pc_no_paths_serialized)
        self.assertNotIn('oauth-access-token-xyz', pc_no_paths_serialized)
        self.assertNotIn('sensitive info', pc_no_paths_serialized)
        # Verify token counts are preserved
        self.assertEqual(pc_no_paths_data['provider_call']['request']['usage_input_tokens'], 100)
        self.assertEqual(pc_no_paths_data['provider_call']['response']['usage_output_tokens'], 200)
        self.assertEqual(pc_no_paths_data['provider_call']['parsed_output']['usage_total_tokens'], 300)

        # Verify GET /api/automation/runs/<run_id>/audit-bundle redacts all secrets
        bundle_resp = self.client.get(
            f'/api/automation/runs/{run_id}/audit-bundle',
            headers=self.headers
        )
        self.assertEqual(bundle_resp.status_code, 200)
        bundle_serialized = json.dumps(bundle_resp.get_json())
        self.assertNotIn('super-secret-key-123', bundle_serialized)
        self.assertNotIn('some-auth-token-1234', bundle_serialized)
        self.assertNotIn('my-secure-password-789', bundle_serialized)
        self.assertNotIn(expected_token, bundle_serialized)
        self.assertNotIn('"lease_token"', bundle_serialized)

        # Verify GET /api/automation/runs/<run_id>/debug-summary has no raw lease_token key
        debug_resp = self.client.get(
            f'/api/automation/runs/{run_id}/debug-summary',
            headers=self.headers
        )
        self.assertEqual(debug_resp.status_code, 200)
        debug_serialized = json.dumps(debug_resp.get_json())
        self.assertNotIn(expected_token, debug_serialized)
        self.assertNotIn('"lease_token"', debug_serialized)


    # ── Route-level credential enforcement tests ──────────────────

    def _claim_for_credential_tests(self):
        scenario_id = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={'source_campaign_id': self.campaign_id},
        ).get_json()['scenario']['id']
        snapshot_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/snapshots',
            headers=self.headers,
            json={},
        ).get_json()['snapshot']['id']
        run_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': snapshot_id},
        ).get_json()['run']['id']
        claim = self.client.post(
            f'/api/automation/runs/{run_id}/claim',
            headers=self.headers,
            json={'worker_id': 'cred-test-worker'},
        ).get_json()
        return run_id, claim['lease_token']

    def test_events_route_enforces_worker_credentials(self):
        run_id, token = self._claim_for_credential_tests()
        cases = [
            (
                'missing credentials',
                {'event_type': 'custom_event', 'payload': {}},
                409,
            ),
            (
                'wrong worker',
                {
                    'event_type': 'custom_event',
                    'payload': {},
                    'worker_id': 'wrong-worker',
                    'lease_token': token,
                },
                409,
            ),
            (
                'wrong token',
                {
                    'event_type': 'custom_event',
                    'payload': {},
                    'worker_id': 'cred-test-worker',
                    'lease_token': 'bad-token',
                },
                409,
            ),
            (
                'valid credentials',
                {
                    'event_type': 'custom_event',
                    'payload': {},
                    'worker_id': 'cred-test-worker',
                    'lease_token': token,
                },
                201,
            ),
        ]
        for label, payload, expected_status in cases:
            with self.subTest(label=label):
                resp = self.client.post(
                    f'/api/automation/runs/{run_id}/events',
                    headers=self.headers,
                    json=payload,
                )
                self.assertEqual(resp.status_code, expected_status)

    def test_provider_calls_route_enforces_worker_credentials(self):
        run_id, token = self._claim_for_credential_tests()
        base_payload = {
            'phase': 'after_dm',
            'request': {},
            'response': {},
            'parsed_output': {},
        }
        cases = [
            ({**base_payload, 'dedupe_key': 'test-pc-cred'}, 409),
            ({
                **base_payload,
                'dedupe_key': 'test-pc-cred-ok',
                'worker_id': 'cred-test-worker', 'lease_token': token,
            }, 201),
        ]
        for payload, expected_status in cases:
            with self.subTest(expected_status=expected_status):
                resp = self.client.post(
                    f'/api/automation/runs/{run_id}/provider-calls',
                    headers=self.headers,
                    json=payload,
                )
                self.assertEqual(resp.status_code, expected_status)

    def test_complete_route_enforces_worker_credentials(self):
        run_id, token = self._claim_for_credential_tests()
        cases = [
            ({}, 409),
            ({'worker_id': 'cred-test-worker', 'lease_token': token}, 200),
        ]
        for payload, expected_status in cases:
            with self.subTest(expected_status=expected_status):
                resp = self.client.post(
                    f'/api/automation/runs/{run_id}/complete',
                    headers=self.headers,
                    json=payload,
                )
                self.assertEqual(resp.status_code, expected_status)

    def test_complete_route_rejects_stale_completion_on_terminal_run(self):
        run_id, token = self._claim_for_credential_tests()
        first = self.client.post(
            f'/api/automation/runs/{run_id}/complete',
            headers=self.headers,
            json={
                'worker_id': 'cred-test-worker', 'lease_token': token,
                'status': 'failed',
                'dedupe_key': f'run_completed:{run_id}:dm-timeout:post_turn',
            },
        )
        self.assertEqual(first.status_code, 200)

        second = self.client.post(
            f'/api/automation/runs/{run_id}/complete',
            headers=self.headers,
            json={
                'worker_id': 'cred-test-worker', 'lease_token': token,
                'status': 'completed',
                'dedupe_key': f'run_completed:{run_id}:late-write',
            },
        )
        self.assertEqual(second.status_code, 409)

        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            self.assertEqual(run.status, 'failed')
            completions = AutomationRunEvent.query.filter_by(run_id=run_id, event_type='run_completed').all()
            self.assertEqual(len(completions), 1)

    def test_complete_route_allows_idempotent_retry_with_same_dedupe_key(self):
        run_id, token = self._claim_for_credential_tests()
        dedupe_key = f'run_completed:{run_id}:dm-timeout:post_turn'
        first = self.client.post(
            f'/api/automation/runs/{run_id}/complete',
            headers=self.headers,
            json={
                'worker_id': 'cred-test-worker', 'lease_token': token,
                'status': 'failed',
                'dedupe_key': dedupe_key,
            },
        )
        self.assertEqual(first.status_code, 200)

        retry = self.client.post(
            f'/api/automation/runs/{run_id}/complete',
            headers=self.headers,
            json={
                'worker_id': 'cred-test-worker', 'lease_token': token,
                'status': 'failed',
                'dedupe_key': dedupe_key,
            },
        )
        self.assertEqual(retry.status_code, 200)

        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            self.assertEqual(run.status, 'failed')
            completions = AutomationRunEvent.query.filter_by(run_id=run_id, event_type='run_completed').all()
            self.assertEqual(len(completions), 1)

    def test_decisions_route_rejects_missing_credentials(self):
        run_id, token = self._claim_for_credential_tests()
        resp = self.client.post(
            f'/api/automation/runs/{run_id}/decisions',
            headers=self.headers,
            json={
                'llm_player_id': 1,
                'user_id': self.owner_id,
                'decision': {'action': 'no_action'},
                'dedupe_key': 'test-decision-cred-missing',
            },
        )
        self.assertEqual(resp.status_code, 409)

    def test_decisions_route_accepts_valid_credentials(self):
        run_id, token = self._claim_for_credential_tests()
        # Need a valid roster entry for the decision
        scenario_id = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={'source_campaign_id': self.campaign_id},
        ).get_json()['scenario']['id']
        snapshot_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/snapshots',
            headers=self.headers,
            json={},
        ).get_json()['snapshot']['id']
        run2_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': snapshot_id},
        ).get_json()['run']['id']
        claim = self.client.post(
            f'/api/automation/runs/{run2_id}/claim',
            headers=self.headers,
            json={'worker_id': 'cred-test-worker'},
        ).get_json()
        token2 = claim['lease_token']
        roster_entry = claim['roster'][0]
        resp = self.client.post(
            f'/api/automation/runs/{run2_id}/decisions',
            headers=self.headers,
            json={
                'llm_player_id': roster_entry.get('llm_player_id') or roster_entry['user_id'],
                'user_id': roster_entry['user_id'],
                'decision': {'action': 'no_action'},
                'dedupe_key': 'test-decision-cred-ok',
                'worker_id': 'cred-test-worker',
                'lease_token': token2,
            },
        )
        self.assertEqual(resp.status_code, 200)

    def test_events_route_rejects_status_bypasses(self):
        run_id, token = self._claim_for_credential_tests()
        for status in ('completed', 'queued'):
            with self.subTest(status=status):
                resp = self.client.post(
                    f'/api/automation/runs/{run_id}/events',
                    headers=self.headers,
                    json={'event_type': 'custom', 'status': status},
                )
                self.assertEqual(resp.status_code, 409)

    def test_post_completion_credential_free_mutations_rejected(self):
        run_id, token = self._claim_for_credential_tests()
        complete_resp = self.client.post(
            f'/api/automation/runs/{run_id}/complete',
            headers=self.headers,
            json={'worker_id': 'cred-test-worker', 'lease_token': token},
        )
        self.assertEqual(complete_resp.status_code, 200)

        events_resp = self.client.post(
            f'/api/automation/runs/{run_id}/events',
            headers=self.headers,
            json={'event_type': 'custom'},
        )
        self.assertEqual(events_resp.status_code, 409)

        pc_resp = self.client.post(
            f'/api/automation/runs/{run_id}/provider-calls',
            headers=self.headers,
            json={'dedupe_key': 'pc-after-complete', 'phase': 'after_dm', 'request': {}, 'response': {}, 'parsed_output': {}},
        )
        self.assertEqual(pc_resp.status_code, 409)

    def test_summary_detects_missing_after_dm_when_no_dm_turn_status(self):
        """When a player_decision has an after_player audit cycle but no
        corresponding after_dm cycle and no dm_turn_status event was recorded,
        get_audit_pause_summary should flag the missing after_dm pause."""
        scenario_id = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={'source_campaign_id': self.campaign_id, 'name': 'Summary Missing After DM'},
        ).get_json()['scenario']['id']
        snapshot_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/snapshots',
            headers=self.headers,
            json={},
        ).get_json()['snapshot']['id']
        run_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': snapshot_id, 'runner_config': {'audit_pause_phases': ['after_dm', 'after_player']}},
        ).get_json()['run']['id']

        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            from datetime import datetime, timedelta, timezone

            t1 = datetime.now(timezone.utc)
            t2 = t1 + timedelta(seconds=1)
            # Player decision event for message 410
            db.session.add(AutomationRunEvent(
                run_id=run_id, event_type='player_decision', sequence_number=1001,
                attempt_number=1, dedupe_key='summ:pd:1001',
                payload_json={'posted_message_id': 410, 'decision': {'action': 'speak'}},
                created_at=t1,
            ))
            # After_player audit cycle for message 410
            ap_cycle = AutomationRunAuditCycle(
                run_id=run_id, cycle_number=1, phase='after_player',
                status='completed', player_message_id=410,
                created_at=t1, updated_at=t1,
            )
            db.session.add(ap_cycle)
            db.session.flush()
            # Second player decision (no after_dm in between)
            db.session.add(AutomationRunEvent(
                run_id=run_id, event_type='player_decision', sequence_number=1002,
                attempt_number=1, dedupe_key='summ:pd:1002',
                payload_json={'posted_message_id': 411, 'decision': {'action': 'speak'}},
                created_at=t2,
            ))
            db.session.commit()

            summary = run.get_audit_pause_summary()

        self.assertTrue(summary['any_configured_pause_skipped'])
        after_dm_skipped = [s for s in summary['skipped_pauses'] if s['phase'] == 'after_dm']
        self.assertEqual(len(after_dm_skipped), 1)
        self.assertEqual(after_dm_skipped[0]['message_id'], 410)
        self.assertIn('resumed player loop', after_dm_skipped[0]['reason'])

    def test_roster_provisioning(self):
        # 1. Provision a player roster entry
        provision_resp = self.client.post(
            f'/api/automation/source-campaigns/{self.campaign_id}/roster',
            headers=self.headers,
            json={
                'entries': [
                    {
                        'label': 'Test Aria',
                        'member_role': 'player',
                        'character': {
                            'name': 'Aria Stonepath',
                            'race': 'Dwarf',
                            'total_level': 3,
                            'max_hp': 28,
                            'current_hp': 28,
                            'armor_class': 17,
                        }
                    }
                ]
            }
        )
        self.assertEqual(provision_resp.status_code, 201)
        res_data = provision_resp.get_json()
        self.assertEqual(len(res_data['entries']), 1)
        entry = res_data['entries'][0]
        self.assertEqual(entry['label'], 'Test Aria')
        self.assertEqual(entry['member_role'], 'player')
        self.assertEqual(entry['character_name'], 'Aria Stonepath')
        self.assertIsNotNone(entry['user_id'])
        self.assertIsNotNone(entry['llm_player_id'])
        self.assertIsNotNone(entry['api_key'])

        first_user_id = entry['user_id']
        first_llm_id = entry['llm_player_id']

        # 2. Provision again with the same label to verify it resolves/reuses the player identity (but does not return secret api_key again)
        re_provision_resp = self.client.post(
            f'/api/automation/source-campaigns/{self.campaign_id}/roster',
            headers=self.headers,
            json={
                'entries': [
                    {
                        'label': 'Test Aria',
                        'member_role': 'player',
                        'character': {
                            'name': 'Aria Stonepath V2',
                            'race': 'Dwarf',
                        }
                    }
                ]
            }
        )
        self.assertEqual(re_provision_resp.status_code, 201)
        res_data2 = re_provision_resp.get_json()
        entry2 = res_data2['entries'][0]
        self.assertEqual(entry2['user_id'], first_user_id)
        self.assertEqual(entry2['llm_player_id'], first_llm_id)
        self.assertEqual(entry2['character_name'], 'Aria Stonepath V2')
        self.assertNotIn('api_key', entry2)

        # 3. Verify validation rollback on invalid entry
        rollback_resp = self.client.post(
            f'/api/automation/source-campaigns/{self.campaign_id}/roster',
            headers=self.headers,
            json={
                'entries': [
                    {
                        'label': 'Valid Label',
                        'character': {'name': 'Rollback Char', 'race': 'Human'}
                    },
                    {
                        # missing label, triggers ValueError
                        'label': '',
                        'character': {'name': 'Invalid Label', 'race': 'Human'}
                    }
                ]
            }
        )
        self.assertEqual(rollback_resp.status_code, 400)
        # Verify first entry was rolled back and not committed (Unique label check or User check)
        with app.app_context():
            rolled_back_user = User.query.filter_by(username='Valid Label').first()
            self.assertIsNone(rolled_back_user)

        # 4. Verify parameter validation checks
        # Test: Invalid role (spectator)
        resp = self.client.post(
            f'/api/automation/source-campaigns/{self.campaign_id}/roster',
            headers=self.headers,
            json={'entries': [{'label': 'Spectator Label', 'member_role': 'spectator', 'character': {'name': 'Name', 'race': 'Elf'}}]}
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("must be 'player'", resp.get_json()['error'])

        # Test: Missing character
        resp = self.client.post(
            f'/api/automation/source-campaigns/{self.campaign_id}/roster',
            headers=self.headers,
            json={'entries': [{'label': 'No Char Label'}]}
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("character must be an object", resp.get_json()['error'])

        # Test: Missing character name
        resp = self.client.post(
            f'/api/automation/source-campaigns/{self.campaign_id}/roster',
            headers=self.headers,
            json={'entries': [{'label': 'No Name Label', 'character': {'race': 'Elf'}}]}
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("character.name must be a non-empty string", resp.get_json()['error'])

        # Test: Missing character race
        resp = self.client.post(
            f'/api/automation/source-campaigns/{self.campaign_id}/roster',
            headers=self.headers,
            json={'entries': [{'label': 'No Race Label', 'character': {'name': 'ElfName'}}]}
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("character.race must be a non-empty string", resp.get_json()['error'])

        # Test: Non-dict entry object
        resp = self.client.post(
            f'/api/automation/source-campaigns/{self.campaign_id}/roster',
            headers=self.headers,
            json={'entries': ["not-an-object"]}
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("must be an object", resp.get_json()['error'])

        # Test: Non-string label
        resp = self.client.post(
            f'/api/automation/source-campaigns/{self.campaign_id}/roster',
            headers=self.headers,
            json={'entries': [{'label': 1234, 'character': {'name': 'Name', 'race': 'Elf'}}]}
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("label must be a non-empty string", resp.get_json()['error'])

        # Test: Whitespace label
        resp = self.client.post(
            f'/api/automation/source-campaigns/{self.campaign_id}/roster',
            headers=self.headers,
            json={'entries': [{'label': '   ', 'character': {'name': 'Name', 'race': 'Elf'}}]}
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("label must be a non-empty string", resp.get_json()['error'])

        # Test: Empty entries
        resp = self.client.post(
            f'/api/automation/source-campaigns/{self.campaign_id}/roster',
            headers=self.headers,
            json={'entries': []}
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("must be a non-empty list", resp.get_json()['error'])

        # Test: Duplicate labels in request
        resp = self.client.post(
            f'/api/automation/source-campaigns/{self.campaign_id}/roster',
            headers=self.headers,
            json={
                'entries': [
                    {'label': 'DupLabel', 'character': {'name': 'Name 1', 'race': 'Elf'}},
                    {'label': 'DupLabel', 'character': {'name': 'Name 2', 'race': 'Dwarf'}},
                ]
            }
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("is duplicated in request", resp.get_json()['error'])

    def test_explicit_roster_scenario_validation(self):
        # Provision a player
        provision_resp = self.client.post(
            f'/api/automation/source-campaigns/{self.campaign_id}/roster',
            headers=self.headers,
            json={
                'entries': [
                    {
                        'label': 'Valeria',
                        'member_role': 'player',
                        'character': {'name': 'Valeria Shadow', 'race': 'Elf'}
                    }
                ]
            }
        ).get_json()
        entry = provision_resp['entries'][0]

        # 1. Create a scenario with valid explicit roster
        scenario_resp = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={
                'name': 'Explicit Roster Scenario',
                'source_campaign_id': self.campaign_id,
                'roster': [
                    {
                        'user_id': entry['user_id'],
                        'character_id': entry['character_id'],
                        'llm_player_id': entry['llm_player_id']
                    }
                ]
            }
        )
        self.assertEqual(scenario_resp.status_code, 201)
        scenario_data = scenario_resp.get_json()['scenario']
        self.assertEqual(len(scenario_data['roster']), 1)
        self.assertEqual(scenario_data['roster'][0]['user_id'], entry['user_id'])
        self.assertEqual(scenario_data['roster'][0]['character_id'], entry['character_id'])

        # 2. Reject spectator entry
        # Temporarily make the member a spectator
        with app.app_context():
            member = CampaignMember.query.filter_by(campaign_id=self.campaign_id, user_id=entry['user_id']).first()
            member.role = 'spectator'
            db.session.commit()

        spectator_resp = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={
                'name': 'Spectator Rejected Scenario',
                'source_campaign_id': self.campaign_id,
                'roster': [
                    {
                        'user_id': entry['user_id'],
                        'character_id': entry['character_id'],
                        'llm_player_id': entry['llm_player_id']
                    }
                ]
            }
        )
        self.assertEqual(spectator_resp.status_code, 400)
        self.assertIn('is a spectator', spectator_resp.get_json()['error'])

        # Restore role
        with app.app_context():
            member = CampaignMember.query.filter_by(campaign_id=self.campaign_id, user_id=entry['user_id']).first()
            member.role = 'player'
            db.session.commit()

        # 3. Reject mismatching selected character
        mismatch_resp = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={
                'name': 'Mismatch Character Scenario',
                'source_campaign_id': self.campaign_id,
                'roster': [
                    {
                        'user_id': entry['user_id'],
                        'character_id': 99999, # invalid/mismatch
                        'llm_player_id': entry['llm_player_id']
                    }
                ]
            }
        )
        self.assertEqual(mismatch_resp.status_code, 400)
        self.assertIn('does not belong to campaign', mismatch_resp.get_json()['error'])

        # 4. Reject malformed explicit roster entries (non-dictionary)
        malformed_resp = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={
                'name': 'Malformed Roster Scenario',
                'source_campaign_id': self.campaign_id,
                'roster': ["not-an-object"]
            }
        )
        self.assertEqual(malformed_resp.status_code, 400)
        self.assertIn('must be an object', malformed_resp.get_json()['error'])

        # 5. Reject null/None roster entries
        null_resp = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={
                'name': 'Null Roster Scenario',
                'source_campaign_id': self.campaign_id,
                'roster': [None]
            }
        )
        self.assertEqual(null_resp.status_code, 400)
        self.assertIn('must be an object', null_resp.get_json()['error'])

    def test_scenario_roster_immutability(self):
        # Provision a player
        provision_resp = self.client.post(
            f'/api/automation/source-campaigns/{self.campaign_id}/roster',
            headers=self.headers,
            json={
                'entries': [
                    {
                        'label': 'Immutable Test',
                        'member_role': 'player',
                        'character': {'name': 'Original Name', 'race': 'Elf'}
                    }
                ]
            }
        ).get_json()
        entry = provision_resp['entries'][0]

        # Create scenario
        scenario_id = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={
                'name': 'Immutable Roster Scenario',
                'source_campaign_id': self.campaign_id,
                'roster': [
                    {
                        'user_id': entry['user_id'],
                        'character_id': entry['character_id'],
                        'llm_player_id': entry['llm_player_id']
                    }
                ]
            }
        ).get_json()['scenario']['id']

        # Mutate the source character's name in db
        with app.app_context():
            char = db.session.get(Character, entry['character_id'])
            char.name = 'Mutated Character Name'
            db.session.commit()

        # Get scenario and verify its roster still has "Original Name"
        get_scenario = self.client.get(f'/api/automation/scenarios/{scenario_id}', headers=self.headers).get_json()['scenario']
        self.assertEqual(get_scenario['roster'][0]['character_name'], 'Original Name')

        # Capture snapshot for scenario
        snapshot_resp = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/snapshots',
            headers=self.headers,
            json={}
        )
        self.assertEqual(snapshot_resp.status_code, 201)
        snap_id = snapshot_resp.get_json()['snapshot']['id']

        with app.app_context():
            snapshot = db.session.get(AutomationSnapshot, snap_id)
            snapshot_roster = snapshot.snapshot_json['roster']
            self.assertEqual(snapshot_roster[0]['character_name'], 'Original Name')


    def test_automation_readiness_scenarios(self):
        from models import Campaign, CampaignMember, Character, AutomationRun, AutomationSnapshot, User
        from services.planning_service import can_start_session, all_members_ready, party_is_full

        with app.app_context():
            owner = db.session.get(User, self.owner_id)

            # Test regular campaign behaves according to interactive rules
            regular_campaign = Campaign(
                name="Regular Campaign",
                user_id=owner.id,
                settings=json.dumps({'required_players': 2})
            )
            db.session.add(regular_campaign)
            db.session.flush()

            owner_member = CampaignMember(campaign_id=regular_campaign.id, user_id=owner.id, role='player')
            db.session.add(owner_member)
            db.session.flush()

            self.assertFalse(party_is_full(regular_campaign, [owner_member]))
            ready, details = can_start_session(regular_campaign)
            self.assertFalse(ready)
            self.assertEqual(details['readiness_mode'], 'interactive')

            # Setup automation scenario players
            u1 = User(username='auto_player_1', email='ap1@test.com')
            u1.set_password('password')
            u2 = User(username='auto_player_2', email='ap2@test.com')
            u2.set_password('password')
            db.session.add_all([u1, u2])
            db.session.flush()

            clone_campaign = Campaign(
                name="Automation Campaign [Clone]",
                user_id=owner.id,
                is_automation_clone=True,
                automation_source_run_id=9999,
                settings=json.dumps({'required_players': 2})
            )
            db.session.add(clone_campaign)
            db.session.flush()

            c1 = Character(user_id=u1.id, name="Hero 1", race="Human", campaign_id=clone_campaign.id)
            c2 = Character(user_id=u2.id, name="Hero 2", race="Elf", campaign_id=clone_campaign.id)
            db.session.add_all([c1, c2])
            db.session.flush()

            frozen_roster = [
                {'user_id': u1.id, 'character_id': c1.id, 'character_name': c1.name},
                {'user_id': u2.id, 'character_id': c2.id, 'character_name': c2.name}
            ]

            snapshot = AutomationSnapshot(
                scenario_id=1,
                source_campaign_id=1,
                label='test snapshot',
                snapshot_json={'roster': frozen_roster}
            )
            db.session.add(snapshot)
            db.session.flush()

            run = AutomationRun(
                scenario_id=1,
                snapshot_id=snapshot.id,
                user_id=owner.id,
                status='queued'
            )
            db.session.add(run)
            db.session.flush()

            clone_campaign.automation_source_run_id = run.id
            run.derived_campaign_id = clone_campaign.id

            owner_cloned = CampaignMember(
                campaign_id=clone_campaign.id,
                user_id=owner.id,
                role='player',
                selected_character_id=None,
                character_ready_at=None
            )
            m1 = CampaignMember(
                campaign_id=clone_campaign.id,
                user_id=u1.id,
                role='player',
                selected_character_id=c1.id,
                character_ready_at=utcnow()
            )
            m2 = CampaignMember(
                campaign_id=clone_campaign.id,
                user_id=u2.id,
                role='player',
                selected_character_id=c2.id,
                character_ready_at=utcnow()
            )
            db.session.add_all([owner_cloned, m1, m2])
            db.session.commit()

            # Case 1: Unready owner does not block (ready is True)
            members = [owner_cloned, m1, m2]
            self.assertTrue(party_is_full(clone_campaign, members))
            self.assertTrue(all_members_ready(clone_campaign, members))
            ready, details = can_start_session(clone_campaign)
            self.assertTrue(ready)
            self.assertEqual(details['readiness_mode'], 'automation_roster')
            self.assertEqual(details['required_players'], 2)
            self.assertEqual(details['ready_players'], 2)
            self.assertEqual(details['readiness_user_ids'], [u1.id, u2.id])
            self.assertEqual(details['missing_readiness_user_ids'], [])

            # Case 2: Non-roster owner does not count
            owner_cloned.selected_character_id = 999
            owner_cloned.character_ready_at = utcnow()
            m2.selected_character_id = None
            m2.character_ready_at = None
            db.session.commit()

            ready, details = can_start_session(clone_campaign)
            self.assertFalse(ready)
            self.assertEqual(details['ready_players'], 1)

            # Case 3: Unready roster member blocks
            owner_cloned.selected_character_id = None
            owner_cloned.character_ready_at = None
            db.session.commit()
            ready, details = can_start_session(clone_campaign)
            self.assertFalse(ready)

            # Case 4: Every roster member is required
            u3 = User(username='auto_player_3', email='ap3@test.com')
            u3.set_password('password')
            db.session.add(u3)
            db.session.flush()
            m2.selected_character_id = c2.id
            m2.character_ready_at = utcnow()
            
            frozen_roster_3 = [
                {'user_id': u1.id, 'character_id': c1.id, 'character_name': c1.name},
                {'user_id': u2.id, 'character_id': c2.id, 'character_name': c2.name},
                {'user_id': u3.id, 'character_id': None, 'character_name': 'Hero 3'}
            ]
            snapshot.snapshot_json = {'roster': frozen_roster_3}
            m3 = CampaignMember(
                campaign_id=clone_campaign.id,
                user_id=u3.id,
                role='player',
                selected_character_id=None,
                character_ready_at=None
            )
            db.session.add(m3)
            db.session.commit()

            ready, details = can_start_session(clone_campaign)
            self.assertFalse(ready)
            self.assertEqual(details['required_players'], 3)
            self.assertEqual(details['ready_players'], 2)

            # Case 5: Missing cloned roster member fails closed
            db.session.delete(m3)
            db.session.commit()
            self.assertFalse(party_is_full(clone_campaign, [owner_cloned, m1, m2]))
            ready, details = can_start_session(clone_campaign)
            self.assertFalse(ready)
            self.assertIn(u3.id, details['missing_readiness_user_ids'])

            # Case 6: Non-roster members are ignored
            snapshot.snapshot_json = {'roster': frozen_roster}
            extra_user = User(username='extra_player', email='ep@test.com')
            extra_user.set_password('password')
            db.session.add(extra_user)
            db.session.flush()
            m_extra = CampaignMember(
                campaign_id=clone_campaign.id,
                user_id=extra_user.id,
                role='player',
                selected_character_id=None,
                character_ready_at=None
            )
            db.session.add(m_extra)
            db.session.commit()

            self.assertTrue(party_is_full(clone_campaign, [owner_cloned, m1, m2, m_extra]))
            ready, details = can_start_session(clone_campaign)
            self.assertTrue(ready)
            self.assertEqual(details['required_players'], 2)
            self.assertEqual(details['ready_players'], 2)
            
            db.session.rollback()

    def test_delete_scenario_and_run(self):
        scenario_response = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={'source_campaign_id': self.campaign_id, 'name': 'To Delete'},
        )
        self.assertEqual(scenario_response.status_code, 201)
        scenario_id = scenario_response.get_json()['scenario']['id']

        snapshot_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/snapshots',
            headers=self.headers,
            json={},
        ).get_json()['snapshot']['id']
        
        run_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': snapshot_id},
        ).get_json()['run']['id']

        run_response = self.client.get(f'/api/automation/runs/{run_id}', headers=self.headers)
        self.assertEqual(run_response.status_code, 200)

        delete_run_response = self.client.delete(f'/api/automation/runs/{run_id}', headers=self.headers)
        self.assertEqual(delete_run_response.status_code, 200)
        self.assertTrue(delete_run_response.get_json()['ok'])

        run_response = self.client.get(f'/api/automation/runs/{run_id}', headers=self.headers)
        self.assertEqual(run_response.status_code, 404)

        run_id2 = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': snapshot_id},
        ).get_json()['run']['id']

        delete_scenario_response = self.client.delete(f'/api/automation/scenarios/{scenario_id}', headers=self.headers)
        self.assertEqual(delete_scenario_response.status_code, 200)
        self.assertTrue(delete_scenario_response.get_json()['ok'])

        self.assertEqual(self.client.get(f'/api/automation/scenarios/{scenario_id}', headers=self.headers).status_code, 404)
        self.assertEqual(self.client.get(f'/api/automation/runs/{run_id2}', headers=self.headers).status_code, 404)

    def test_delete_snapshot(self):
        # Enable SQLite foreign key constraints explicitly for this test
        with app.app_context():
            db.session.execute(db.text("PRAGMA foreign_keys=ON"))

        scenario_response = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={'source_campaign_id': self.campaign_id, 'name': 'To Delete Snapshot'},
        )
        self.assertEqual(scenario_response.status_code, 201)
        scenario_id = scenario_response.get_json()['scenario']['id']

        try:
            # Seed the source campaign with standard child rows before snapshotting
            with app.app_context():
                from models import (
                    LootBox, CampaignShop, CampaignMemoryEmbedding, NPCActor, CampaignClock, WorldEvent, Campaign
                )
                # Add world event
                db.session.add(WorldEvent(
                    campaign_id=self.campaign_id,
                    event_type='test',
                    summary='A test world event',
                    payload='{}',
                ))
                # Add memory embedding
                db.session.add(CampaignMemoryEmbedding(
                    campaign_id=self.campaign_id,
                    item_type='note',
                    item_id='note-1',
                    canonical_text='Some text content',
                    text_hash='hash',
                    embedding_model='text-embedding-ada-002',
                    embedding_dimensions=2,
                    embedding_json='[0.1, 0.2]',
                ))
                # Add npc, clock, shop, lootbox
                db.session.add(NPCActor(campaign_id=self.campaign_id, actor_id='npc-1', name='NPC Actor', dossier='Test dossier'))
                db.session.add(CampaignClock(campaign_id=self.campaign_id, clock_id='clock-1', name='Clock', segments=8, filled=4))
                db.session.add(CampaignShop(campaign_id=self.campaign_id, name='Shop', items_json='[]'))
                db.session.add(LootBox(campaign_id=self.campaign_id, name='LootBox', items_json='[]', currency_json='{}'))
                db.session.commit()

            # Create the snapshot
            snapshot_response = self.client.post(
                f'/api/automation/scenarios/{scenario_id}/snapshots',
                headers=self.headers,
                json={},
            )
            self.assertEqual(snapshot_response.status_code, 201)
            snapshot_id = snapshot_response.get_json()['snapshot']['id']
            
            # Create the run
            run_response = self.client.post(
                f'/api/automation/scenarios/{scenario_id}/runs',
                headers=self.headers,
                json={'snapshot_id': snapshot_id},
            )
            self.assertEqual(run_response.status_code, 201)
            run_id = run_response.get_json()['run']['id']

            # Seed scenario baseline run, cycle, and audit attempt
            with app.app_context():
                from models import AutomationScenario, AutomationRunAuditCycle, AutomationRunAuditAttempt
                scen = db.session.get(AutomationScenario, scenario_id)
                scen.baseline_run_id = run_id
                
                cycle = AutomationRunAuditCycle(
                    run_id=run_id,
                    cycle_number=1,
                    phase='after_dm',
                    status='audited',
                    summary='Test cycle',
                )
                db.session.add(cycle)
                db.session.flush()
                
                attempt = AutomationRunAuditAttempt(
                    run_id=run_id,
                    cycle_id=cycle.id,
                    cycle_number=1,
                    phase='after_dm',
                    status='success',
                )
                db.session.add(attempt)
                db.session.commit()

            # Setup physical map files in the instance/encounter_maps directory
            from services.encounter_map_service import encounter_map_storage_dir
            storage_dir = encounter_map_storage_dir()
            storage_dir.mkdir(parents=True, exist_ok=True)
            
            shared_map_file = storage_dir / 'mirror-dock.png'
            shared_map_labeled = storage_dir / 'mirror-dock-labeled.png'
            clone_only_file = storage_dir / 'clone-only.png'
            
            shared_map_file.write_text('shared')
            shared_map_labeled.write_text('shared_labeled')
            clone_only_file.write_text('clone_only')

            # Materialize run campaign
            with app.app_context():
                from services.automation_service import materialize_run_campaign
                from models import (
                    AutomationRun, Campaign, CampaignMonster, CampaignAuditEvent,
                    SessionDmTurn, CampaignMemoryRun, CampaignMemoryLog, EncounterMap
                )
                run = db.session.get(AutomationRun, run_id)
                clone_campaign, character_map, preflight = materialize_run_campaign(run)
                db.session.commit()
                
                clone_campaign_id = clone_campaign.id
                
                # Setup clone-only encounter map image
                clone_map = EncounterMap.query.filter_by(campaign_id=clone_campaign_id).first()
                if clone_map:
                    clone_map.image_filename = 'clone-only.png'
                
                # Seed runtime-only rows
                db.session.add(CampaignMonster(
                    campaign_id=clone_campaign_id,
                    monster_id="monster-1",
                    name="Campaign Monster",
                    stat_block="{}",
                ))
                db.session.add(CampaignAuditEvent(
                    campaign_id=clone_campaign_id,
                    event_type='dm_silence_chosen',
                    source='session_messages',
                    actor='session_dm',
                    summary='Clone audit event',
                ))
                
                clone_session = clone_campaign.sessions[0]
                clone_session_id = clone_session.id
                clone_msg = clone_session.messages[0]
                
                db.session.add(SessionDmTurn(
                    campaign_id=clone_campaign_id,
                    session_id=clone_session.id,
                    player_message_id=clone_msg.id,
                    status='pending',
                ))
                db.session.add(CampaignMemoryRun(
                    memory_run_id='run-clone-1',
                    campaign_id=clone_campaign_id,
                ))
                db.session.add(CampaignMemoryLog(
                    campaign_id=clone_campaign_id,
                    memory_run_id='run-clone-1',
                    operation='create',
                ))
                db.session.commit()

            # Delete the snapshot
            delete_snapshot_response = self.client.delete(f'/api/automation/snapshots/{snapshot_id}', headers=self.headers)
            self.assertEqual(delete_snapshot_response.status_code, 200)
            self.assertTrue(delete_snapshot_response.get_json()['ok'])
        finally:
            with app.app_context():
                db.session.execute(db.text("PRAGMA foreign_keys=OFF"))

        # Verify all cascade-deleted tables are clean
        with app.app_context():
            from models import (
                AutomationSnapshot, AutomationRun, AutomationScenario, AutomationRunAuditCycle, AutomationRunAuditAttempt, Campaign,
                LLMPlayer, LootBox, SessionDmTurn, CampaignShop, CampaignMemoryRun, CampaignMemoryLog, SheetProposal,
                Character, CharacterClass, CharacterSkill, CharacterSavingThrow, CharacterProficiency, CharacterFeature,
                CharacterWeapon, CharacterEquipment, CharacterSpell, CharacterNote, CharacterResource, CharacterCompanion, CharacterCondition,
                CampaignMonster, CampaignAuditEvent, EncounterMap
            )
            # Run-owned rows are gone
            self.assertIsNone(db.session.get(AutomationSnapshot, snapshot_id))
            self.assertIsNone(db.session.get(AutomationRun, run_id))
            
            # Clone-owned campaigns are gone
            self.assertIsNone(db.session.get(Campaign, clone_campaign_id))
            
            # Baseline reference is nullified on scenario
            scen = db.session.get(AutomationScenario, scenario_id)
            self.assertIsNone(scen.baseline_run_id)
            
            # Derived campaign runtime-only table checks
            self.assertEqual(CampaignMonster.query.filter_by(campaign_id=clone_campaign_id).count(), 0)
            self.assertEqual(CampaignAuditEvent.query.filter_by(campaign_id=clone_campaign_id).count(), 0)
            self.assertEqual(SessionDmTurn.query.filter_by(campaign_id=clone_campaign_id).count(), 0)
            self.assertEqual(CampaignMemoryRun.query.filter_by(campaign_id=clone_campaign_id).count(), 0)
            self.assertEqual(CampaignMemoryLog.query.filter_by(campaign_id=clone_campaign_id).count(), 0)
            self.assertEqual(SheetProposal.query.filter_by(session_id=clone_session_id).count(), 0)
            self.assertEqual(AutomationRunAuditCycle.query.filter_by(run_id=run_id).count(), 0)
            self.assertEqual(AutomationRunAuditAttempt.query.filter_by(run_id=run_id).count(), 0)
            
            # Cloned characters and their classes/skills/saving throws sub-relations are gone
            cloned_character_ids = [k for k in character_map.values()]
            if cloned_character_ids:
                self.assertEqual(Character.query.filter(Character.id.in_(cloned_character_ids)).count(), 0)
                self.assertEqual(CharacterClass.query.filter(CharacterClass.character_id.in_(cloned_character_ids)).count(), 0)
                self.assertEqual(CharacterSkill.query.filter(CharacterSkill.character_id.in_(cloned_character_ids)).count(), 0)
                self.assertEqual(CharacterSavingThrow.query.filter(CharacterSavingThrow.character_id.in_(cloned_character_ids)).count(), 0)

            # Source campaign and characters still exist
            self.assertIsNotNone(db.session.get(Campaign, self.campaign_id))
            source_char = Character.query.filter_by(campaign_id=self.campaign_id).first()
            self.assertIsNotNone(source_char)
            self.assertEqual(CharacterClass.query.filter_by(character_id=source_char.id).count(), 1)
            
            # Files verification
            self.assertTrue(shared_map_file.exists())
            self.assertTrue(shared_map_labeled.exists())
            self.assertFalse(clone_only_file.exists())

    def test_cleanup_scenario_retention_delete(self):
        with app.app_context():
            db.session.execute(db.text("PRAGMA foreign_keys=ON"))
        try:
            scenario_response = self.client.post(
                '/api/automation/scenarios',
                headers=self.headers,
                json={'source_campaign_id': self.campaign_id, 'name': 'To Clean Scenario'},
            )
            scenario_id = scenario_response.get_json()['scenario']['id']

            snapshot_id = self.client.post(
                f'/api/automation/scenarios/{scenario_id}/snapshots',
                headers=self.headers,
                json={},
            ).get_json()['snapshot']['id']
            
            run_response = self.client.post(
                f'/api/automation/scenarios/{scenario_id}/runs',
                headers=self.headers,
                json={'snapshot_id': snapshot_id},
            )
            run_id = run_response.get_json()['run']['id']

            # Materialize clone
            with app.app_context():
                from services.automation_service import materialize_run_campaign
                from models import AutomationRun, Campaign
                from datetime import datetime, UTC
                run = db.session.get(AutomationRun, run_id)
                run.status = 'completed'
                run.finished_at = datetime.now(UTC)
                clone, _, _ = materialize_run_campaign(run)
                db.session.commit()
                clone_id = clone.id

            # Perform retention delete via cleanup endpoint
            cleanup_response = self.client.post(
                f'/api/automation/scenarios/{scenario_id}/cleanup',
                headers=self.headers,
                json={'action': 'delete', 'older_than_days': 0, 'keep_recent_runs': 0},
            )
            self.assertEqual(cleanup_response.status_code, 200)

            # Verify database was cleaned up
            with app.app_context():
                self.assertIsNone(db.session.get(Campaign, clone_id))
        finally:
            with app.app_context():
                db.session.execute(db.text("PRAGMA foreign_keys=OFF"))

    def test_delete_source_campaign_with_scenario_fails(self):
        # Create scenario referencing self.campaign_id
        self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={'source_campaign_id': self.campaign_id, 'name': 'Referencing Scenario'},
        )

        # Deleting source campaign should fail with 400 because scenario references it
        delete_response = self.client.delete(f'/api/campaigns/{self.campaign_id}', headers=self.headers)
        self.assertEqual(delete_response.status_code, 400)
        self.assertIn('references it', delete_response.get_json()['error'])

    def test_delete_campaign_clone_directly_nullifies_run_reference(self):
        with app.app_context():
            db.session.execute(db.text("PRAGMA foreign_keys=ON"))
        try:
            scenario_response = self.client.post(
                '/api/automation/scenarios',
                headers=self.headers,
                json={'source_campaign_id': self.campaign_id, 'name': 'To Clean Scenario'},
            )
            scenario_id = scenario_response.get_json()['scenario']['id']

            snapshot_id = self.client.post(
                f'/api/automation/scenarios/{scenario_id}/snapshots',
                headers=self.headers,
                json={},
            ).get_json()['snapshot']['id']
            
            run_response = self.client.post(
                f'/api/automation/scenarios/{scenario_id}/runs',
                headers=self.headers,
                json={'snapshot_id': snapshot_id},
            )
            run_id = run_response.get_json()['run']['id']

            # Materialize clone
            with app.app_context():
                from services.automation_service import materialize_run_campaign
                from models import AutomationRun, Campaign
                from services.campaign_cleanup import delete_campaign_graph
                run = db.session.get(AutomationRun, run_id)
                clone, _, _ = materialize_run_campaign(run)
                db.session.commit()
                clone_id = clone.id

                # Delete the campaign clone directly using delete_campaign_graph helper
                delete_campaign_graph([clone_id], character_policy='delete')
                db.session.commit()

                # Verify run's derived campaign ID is nullified
                run = db.session.get(AutomationRun, run_id)
                self.assertIsNone(run.derived_campaign_id)
        finally:
            with app.app_context():
                db.session.execute(db.text("PRAGMA foreign_keys=OFF"))

    def test_custom_criteria_scorecard_and_category_breakdowns(self):
        scorecard_id = self.client.post(
            '/api/automation/scorecards',
            headers=self.headers,
            json={
                'name': 'Category breakdown scorecard',
                'criteria': [
                    {'id': 'memory_quality', 'label': 'Memory Quality', 'weight': 3, 'category': 'retrieval or memory use'},
                    {'id': 'story_consistency', 'label': 'Story Consistency', 'weight': 2, 'category': 'narrative quality'},
                    {'id': 'state_correctness', 'label': 'State Correctness', 'weight': 1, 'category': 'durable state correctness'},
                ],
            },
        ).get_json()['scorecard']['id']
        scenario_id = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={
                'source_campaign_id': self.campaign_id,
                'scorecard_template_id': scorecard_id,
            },
        ).get_json()['scenario']['id']
        snapshot_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/snapshots',
            headers=self.headers,
            json={},
        ).get_json()['snapshot']['id']
        run_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': snapshot_id},
        ).get_json()['run']['id']
        claim = self.client.post(
            f'/api/automation/runs/{run_id}/claim',
            headers=self.headers,
            json={'worker_id': 'worker-a'},
        ).get_json()
        cycle_id = self.client.post(
            f'/api/automation/runs/{run_id}/pause',
            headers=self.headers,
            json={
                'worker_id': 'worker-a',
                'lease_token': claim['lease_token'],
                'phase': 'after_dm',
                'summary': 'Pause after DM turn',
                'dm_message_id': 99,
                'payload': {'turns_completed': 1},
            },
        ).get_json()['audit_cycle']['id']

        # 1. Before submitting audit, refresh run scorecard. Missing results should be not_assessed.
        with app.app_context():
            from models import AutomationRun
            from services.automation_service import refresh_run_scorecard
            run = db.session.get(AutomationRun, run_id)
            res = refresh_run_scorecard(run)
            crit_mem = next(c for c in res if c['check_id'] == 'custom:memory_quality')
            self.assertEqual(crit_mem['status'], 'not_assessed')
            self.assertEqual(crit_mem['details']['weight'], 3)
            self.assertEqual(crit_mem['details']['category'], 'retrieval or memory use')

            # Aggregate score should exclude not_assessed
            self.assertIsNone(run.scorecard_summary_json['weighted_score'])
            
            # The category breakdown for custom categories without assessments should be not_assessed or not_applicable
            bd = run.scorecard_summary_json['category_breakdown']
            self.assertEqual(bd['retrieval or memory use']['status'], 'not_assessed')
            self.assertIsNone(bd['retrieval or memory use']['score'])
            self.assertEqual(bd['safety/private-information handling']['status'], 'not_applicable')
            self.assertIsNone(bd['safety/private-information handling']['score'])

        # 2. Submit audit cycle scorecard with custom ratings
        self.client.post(
            f'/api/automation/runs/{run_id}/audit-cycles/{cycle_id}/audit',
            headers=self.headers,
            json={
                'scorecard': {
                    'summary': 'Done audit',
                    'notes': 'Good notes',
                    'criteria': [
                        {
                            'id': 'memory_quality',
                            'status': 'pass',
                            'evidence_refs': [],
                            'applicability': {'applicable': True, 'reason': 'ok'}
                        },
                        {
                            'id': 'story_consistency',
                            'status': 'warn',
                            'evidence_refs': [],
                            'applicability': {'applicable': True, 'reason': 'ok'}
                        },
                        {
                            'id': 'state_correctness',
                            'status': 'fail',
                            'evidence_refs': [],
                            'applicability': {'applicable': True, 'reason': 'ok'}
                        }
                    ]
                }
            }
        )

        # 3. Refresh and verify scoring and breakdowns
        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            res = refresh_run_scorecard(run)
            
            # Assert overall status is fail because of state_correctness fail
            self.assertEqual(run.scorecard_summary_json['overall_status'], 'fail')

            # Category breakdowns
            bd = run.scorecard_summary_json['category_breakdown']
            
            # memory quality: pass (weight 3) -> 100%
            self.assertEqual(bd['retrieval or memory use']['status'], 'pass')
            self.assertEqual(bd['retrieval or memory use']['score'], 1.0)
            
            # story consistency: warn (weight 2) -> 50%
            self.assertEqual(bd['narrative quality']['status'], 'warn')
            self.assertEqual(bd['narrative quality']['score'], 0.75)
            
            # state correctness: fail (weight 1) -> 0%
            self.assertEqual(bd['durable state correctness']['status'], 'fail')
            self.assertEqual(bd['durable state correctness']['score'], 0.0)

            # safety: not_applicable
            self.assertEqual(bd['safety/private-information handling']['status'], 'not_applicable')
            self.assertIsNone(bd['safety/private-information handling']['score'])

            # Verify that total weighted score calculation matches
            total_w = 0
            pass_w = 0
            for c in res:
                s = c['status']
                w = c['details']['weight']
                if s == 'pass':
                    total_w += w
                    pass_w += w
                elif s == 'warn':
                    total_w += w
                    pass_w += 0.5 * w
                elif s == 'fail':
                    total_w += w
                    pass_w += 0.0 * w
            
            self.assertAlmostEqual(run.scorecard_summary_json['weighted_score'], pass_w / total_w, places=4)

        # 4. Verify route-level / cross-surface consistency
        run_api_resp = self.client.get(f'/api/automation/runs/{run_id}', headers=self.headers)
        self.assertEqual(run_api_resp.status_code, 200)
        run_api_data = run_api_resp.get_json()['run']
        with app.app_context():
            from models import AutomationRun
            from services.automation_service import refresh_run_scorecard
            run = db.session.get(AutomationRun, run_id)
            self.assertEqual(run_api_data['scorecard_summary']['weighted_score'], run.scorecard_summary_json['weighted_score'])
            self.assertEqual(run_api_data['scorecard_summary']['category_breakdown'], run.scorecard_summary_json['category_breakdown'])

        scorecard_api_resp = self.client.get(f'/api/automation/runs/{run_id}/scorecard', headers=self.headers)
        self.assertEqual(scorecard_api_resp.status_code, 200)
        scorecard_api_data = scorecard_api_resp.get_json()
        self.assertEqual(scorecard_api_data['run']['scorecard_summary']['weighted_score'], run.scorecard_summary_json['weighted_score'])
        self.assertEqual(scorecard_api_data['run']['scorecard_summary']['category_breakdown'], run.scorecard_summary_json['category_breakdown'])

        bundle_api_resp = self.client.get(f'/api/automation/runs/{run_id}/audit-bundle', headers=self.headers)
        self.assertEqual(bundle_api_resp.status_code, 200)
        bundle_api_data = bundle_api_resp.get_json()
        self.assertEqual(bundle_api_data['run']['scorecard_summary']['weighted_score'], run.scorecard_summary_json['weighted_score'])
        self.assertEqual(bundle_api_data['run']['scorecard_summary']['category_breakdown'], run.scorecard_summary_json['category_breakdown'])

        # 5. Verify unclassified/unknown category criterion is excluded from breakdowns but included in score.
        # A schema v1 legacy snapshot carries an invalid explicit category that predates strict
        # template validation; it must remain readable via the scoring normalization path.
        with app.app_context():
            from models import AutomationRun
            run = db.session.get(AutomationRun, run_id)
            run.scorecard_template_json = {
                'template_id': 0,
                'schema_version': 1,
                'name': 'Unknown category scorecard',
                'criteria': [
                    {'id': 'unclassified_crit', 'label': 'Unclassified', 'weight': 2, 'category': 'unknown-cat-name-xyz'},
                ],
                'defaults': {},
            }
            db.session.commit()
            res2 = refresh_run_scorecard(run)
            bd2 = run.scorecard_summary_json['category_breakdown']
            # "unknown-cat-name-xyz" should not be in the breakdown keys (only the 5 named categories)
            self.assertNotIn('unknown-cat-name-xyz', bd2)
            # Make sure it didn't fallback to narrative quality
            self.assertEqual(bd2['narrative quality']['status'], 'pass')
            # The invalid explicit category is surfaced as a configuration error.
            config = run.scorecard_summary_json['scorecard_configuration']
            self.assertFalse(config['valid'])
            self.assertEqual(config['uncategorized_criterion_count'], 1)
            self.assertEqual(config['invalid_criteria'][0]['criterion_id'], 'unclassified_crit')

        # 6. Verify missing built-in metric (value is None) evaluates to not_assessed
        with app.app_context():
            from services.automation_service import _evaluate_check
            self.assertEqual(_evaluate_check(None, {'id': 'test_none', 'kind': 'number'}), 'not_assessed')
            self.assertEqual(_evaluate_check(None, {'id': 'test_none_enum', 'kind': 'enum'}), 'not_assessed')

        # 7. Verify criteria weights are capped at 100 and score is clipped below 1.0 if warning/failure exists
        scorecard_id3 = self.client.post(
            '/api/automation/scorecards',
            headers=self.headers,
            json={
                'name': 'Capped and skewed scorecard',
                'criteria': [
                    {'id': 'crit_huge', 'label': 'Huge Weight', 'weight': 20000, 'category': 'narrative quality'},
                    {'id': 'crit_small', 'label': 'Small Weight', 'weight': 1, 'category': 'narrative quality'},
                ],
            },
        ).get_json()['scorecard']['id']
        
        with app.app_context():
            from models import AutomationScorecardTemplate
            tmpl = db.session.get(AutomationScorecardTemplate, scorecard_id3)
            crit_huge = next(c for c in tmpl.criteria_json if c['id'] == 'crit_huge')
            # Verify weight was capped to 100
            self.assertEqual(crit_huge['weight'], 100)

            # Test precision clipping logic by constructing a run with near-perfect but imperfect results
            run = db.session.get(AutomationRun, run_id)
            run.scorecard_template_json = tmpl.snapshot()
            db.session.commit()
            
        # Resume, claim, and submit cycle feedback with pass on huge weight and fail on small weight
        self.client.post(f'/api/automation/runs/{run_id}/continue', headers=self.headers, json={})
        claim2 = self.client.post(
            f'/api/automation/runs/{run_id}/claim',
            headers=self.headers,
            json={'worker_id': 'worker-a'},
        ).get_json()
        
        cycle_id2 = self.client.post(
            f'/api/automation/runs/{run_id}/pause',
            headers=self.headers,
            json={
                'worker_id': 'worker-a',
                'lease_token': claim2['lease_token'],
                'phase': 'after_dm',
                'summary': 'Pause 2',
                'dm_message_id': 100,
                'payload': {'turns_completed': 2},
            },
        ).get_json()['audit_cycle']['id']
        
        # Submit audit cycle with pass (huge) and fail (small)
        self.client.post(
            f'/api/automation/runs/{run_id}/audit-cycles/{cycle_id2}/audit',
            headers=self.headers,
            json={
                'scorecard': {
                    'summary': 'Done audit 2',
                    'criteria': [
                        {'id': 'crit_huge', 'status': 'pass', 'applicability': {'applicable': True}},
                        {'id': 'crit_small', 'status': 'fail', 'applicability': {'applicable': True}},
                    ]
                }
            }
        )
        
        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            refresh_run_scorecard(run)
            # Mathematical score: 100 / 101 = 0.990099... -> rounds to 0.9901
            self.assertEqual(run.scorecard_summary_json['weighted_score'], 0.9688)
            
        # 8. Verify ingestion normalization for not_applicable / legacy N/A
        # Resume, claim, and pause for cycle 3
        self.client.post(f'/api/automation/runs/{run_id}/continue', headers=self.headers, json={})
        claim3 = self.client.post(
            f'/api/automation/runs/{run_id}/claim',
            headers=self.headers,
            json={'worker_id': 'worker-a'},
        ).get_json()
        
        cycle_id3 = self.client.post(
            f'/api/automation/runs/{run_id}/pause',
            headers=self.headers,
            json={
                'worker_id': 'worker-a',
                'lease_token': claim3['lease_token'],
                'phase': 'after_dm',
                'summary': 'Pause 3',
                'dm_message_id': 101,
                'payload': {'turns_completed': 3},
            },
        ).get_json()['audit_cycle']['id']
        
        # Submit audit with mismatched and legacy N/A inputs
        self.client.post(
            f'/api/automation/runs/{run_id}/audit-cycles/{cycle_id3}/audit',
            headers=self.headers,
            json={
                'scorecard': {
                    'summary': 'Done audit 3',
                    'criteria': [
                        # Mismatched: status pass, applicable False -> normalized to not_applicable / False
                        {'id': 'crit_huge', 'status': 'pass', 'applicability': {'applicable': False}},
                        # Legacy N/A: status not_assessed, applicable False -> normalized to not_applicable / False
                        {'id': 'crit_small', 'status': 'not_assessed', 'applicability': {'applicable': False}},
                    ]
                }
            }
        )
        
        with app.app_context():
            from models import AutomationRunAuditCycle
            c3 = db.session.get(AutomationRunAuditCycle, cycle_id3)
            crit_h = next(c for c in c3.scorecard_json['criteria'] if c['criterion_id'] == 'crit_huge')
            crit_s = next(c for c in c3.scorecard_json['criteria'] if c['criterion_id'] == 'crit_small')
            
            # Verify normalized status and applicability
            self.assertEqual(crit_h['status'], 'not_applicable')
            self.assertEqual(crit_h['applicability']['applicable'], False)
            self.assertEqual(crit_s['status'], 'not_applicable')
            self.assertEqual(crit_s['applicability']['applicable'], False)
            
            # Verify cycle overall status is not_applicable since all are not_applicable
            self.assertEqual(c3.scorecard_summary_json['overall_status'], 'not_applicable')

    def test_builtin_auditor_not_applicable_canonical_flow(self):
        scorecard_id = self.client.post(
            '/api/automation/scorecards',
            headers=self.headers,
            json={
                'name': 'Built-In Auditor N/A Scorecard',
                'criteria': [
                    {'id': 'memory_quality', 'label': 'Memory Quality', 'weight': 2, 'category': 'retrieval or memory use'},
                    {'id': 'scene_mood', 'label': 'Scene Mood', 'weight': 3, 'category': 'narrative quality'},
                ],
            },
        ).get_json()['scorecard']['id']
        scenario_id = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={
                'source_campaign_id': self.campaign_id,
                'scorecard_template_id': scorecard_id,
                'runner_config': {
                    'audit_pause_phases': ['after_dm'],
                    'auditor_config': {'mode': 'built_in', 'count': 1, 'auto_continue': False},
                },
            },
        ).get_json()['scenario']['id']
        snapshot_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/snapshots',
            headers=self.headers,
            json={},
        ).get_json()['snapshot']['id']
        run_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': snapshot_id},
        ).get_json()['run']['id']
        claim = self.client.post(
            f'/api/automation/runs/{run_id}/claim',
            headers=self.headers,
            json={'worker_id': 'worker-a'},
        ).get_json()
        cycle_id = self.client.post(
            f'/api/automation/runs/{run_id}/pause',
            headers=self.headers,
            json={
                'worker_id': 'worker-a',
                'lease_token': claim['lease_token'],
                'phase': 'after_dm',
                'summary': 'Pause after DM turn',
                'payload': {'turns_completed': 1},
            },
        ).get_json()['audit_cycle']['id']

        def fake_auditor_decision(run, cycle, job, config, **_kwargs):
            from services.automation_service import persist_provider_call

            provider_call, _created = persist_provider_call(run, {
                'dedupe_key': f'auditor:{cycle.id}:slot:{job.auditor_slot}',
                'phase': 'auditor_decision',
                'prompt_version_id': 'test',
                'provider': 'opencode_go',
                'model': 'deepseek-v4-flash',
                'request': {'messages': []},
                'response': {'id': 'test-response'},
                'parsed_output': {'overall_status': 'pass'},
                'response_text': '{"overall_status":"pass"}',
            })
            return {
                'provider': 'opencode_go',
                'model': 'deepseek-v4-flash',
                'provider_call': provider_call,
                'tool_call_count': 1,
                'tool_trace': [{'tool_name': 'get_transcript'}],
                'scorecard': {
                    'overall_status': 'pass',
                    'overall_summary': 'Memory held; scene mood not applicable this phase.',
                    'criteria': [
                        {'criterion_id': 'memory_quality', 'status': 'pass', 'summary': 'Memory held.', 'evidence': 'Transcript and world state agree.'},
                        {'criterion_id': 'scene_mood', 'status': 'not_applicable', 'summary': 'No mood beats this phase.', 'evidence': 'Phase exposes no mood signal.'},
                    ],
                    'tool_calls_used': ['get_transcript'],
                    'unresolved_evidence_gaps': [],
                },
            }

        with patch('services.automation_auditor.request_auditor_decision_with_tools', side_effect=fake_auditor_decision):
            response = self.client.post(
                f'/api/automation/runs/{run_id}/auditors/start',
                headers=self.headers,
                json={'sync': True},
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['completed'])

        with app.app_context():
            cycle = db.session.get(AutomationRunAuditCycle, cycle_id)
            self.assertEqual(cycle.status, 'audited')
            crit_na = next(c for c in cycle.scorecard_json['criteria'] if c['criterion_id'] == 'scene_mood')
            # The built-in auditor's not_applicable status must survive normalization and be
            # canonicalized exactly like the manual path (status + applicability agree).
            self.assertEqual(crit_na['status'], 'not_applicable')
            self.assertFalse(crit_na['applicability']['applicable'])
            self.assertEqual(cycle.scorecard_summary_json['criteria_assessed_count'], 1)
            self.assertEqual(cycle.scorecard_summary_json['criteria_not_applicable_count'], 1)
            self.assertEqual(cycle.scorecard_summary_json['criteria_not_assessed_count'], 0)

        scorecard_response = self.client.get(f'/api/automation/runs/{run_id}/scorecard', headers=self.headers)
        self.assertEqual(scorecard_response.status_code, 200)
        scorecard_payload = scorecard_response.get_json()
        rows = {row['check_id']: row for row in scorecard_payload['scorecard']}
        self.assertEqual(rows['custom:scene_mood']['status'], 'not_applicable')
        self.assertEqual(rows['custom:scene_mood']['details']['not_applicable_cycle_count'], 1)
        self.assertEqual(rows['custom:memory_quality']['status'], 'pass')

        # The not_applicable criterion must be excluded from weighting: recompute the
        # expected weighted score from pass/warn/fail rows only and compare.
        weighted_total = 0
        weighted_pass = 0
        for row in scorecard_payload['scorecard']:
            status = row['status']
            weight = row['details']['weight']
            if status == 'pass':
                weighted_total += weight
                weighted_pass += weight
            elif status == 'warn':
                weighted_total += weight
                weighted_pass += 0.5 * weight
            elif status == 'fail':
                weighted_total += weight
        expected_score = round(weighted_pass / weighted_total, 4)
        self.assertEqual(scorecard_payload['run']['scorecard_summary']['weighted_score'], expected_score)
        # scene_mood (weight 3) counted as fail/warn would drag the score below this value.
        # The narrative category also contains the built-in dm_silence/dm_empty checks (both
        # pass here); scene_mood must be excluded from the category weighting, so the
        # category score stays a perfect 1.0 instead of dropping below it.
        self.assertEqual(scorecard_payload['run']['scorecard_summary']['category_breakdown']['narrative quality']['status'], 'pass')
        self.assertEqual(scorecard_payload['run']['scorecard_summary']['category_breakdown']['narrative quality']['score'], 1.0)

    def test_missing_builtin_metric_and_uncategorized_custom_mixed_scorecard(self):
        scorecard_id = self.client.post(
            '/api/automation/scorecards',
            headers=self.headers,
            json={
                'name': 'Mixed Missing Metric Scorecard',
                'criteria': [
                    {'id': 'narrative_probe', 'label': 'Narrative Probe', 'weight': 2, 'category': 'narrative quality'},
                ],
            },
        ).get_json()['scorecard']['id']
        scenario_id = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={
                'source_campaign_id': self.campaign_id,
                'scorecard_template_id': scorecard_id,
                'audit_config': {
                    'checks': [
                        {'id': 'known_errors', 'metric': 'error_count', 'kind': 'number', 'pass_if': {'lte': 0}, 'fail_if': {'gt': 0}, 'weight': 2, 'better_direction': 'lower'},
                        {'id': 'missing_numeric', 'metric': 'nonexistent_numeric_metric', 'kind': 'number', 'pass_if': {'lte': 0}, 'fail_if': {'gt': 0}, 'weight': 7},
                        {'id': 'missing_enum', 'metric': 'nonexistent_enum_metric', 'kind': 'enum', 'pass_values': ['ok'], 'fail_values': ['bad'], 'weight': 5},
                    ],
                },
            },
        ).get_json()['scenario']['id']
        snapshot_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/snapshots',
            headers=self.headers,
            json={},
        ).get_json()['snapshot']['id']
        run_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': snapshot_id},
        ).get_json()['run']['id']
        with app.app_context():
            # Simulate a historical template snapshot (schema v1) that predates strict
            # category validation: the uncategorized criterion has no explicit category.
            # Legacy snapshots must remain readable through the scoring normalization path.
            run = db.session.get(AutomationRun, run_id)
            run.scorecard_template_json = {
                'template_id': scorecard_id,
                'schema_version': 1,
                'name': 'Mixed Missing Metric Scorecard',
                'criteria': [
                    {'id': 'narrative_probe', 'label': 'Narrative Probe', 'weight': 2, 'category': 'narrative quality'},
                    {'id': 'uncategorized_probe', 'label': 'Uncategorized Probe', 'weight': 2},
                ],
                'defaults': {},
            }
            db.session.commit()
        claim = self.client.post(
            f'/api/automation/runs/{run_id}/claim',
            headers=self.headers,
            json={'worker_id': 'worker-a'},
        ).get_json()
        cycle_id = self.client.post(
            f'/api/automation/runs/{run_id}/pause',
            headers=self.headers,
            json={
                'worker_id': 'worker-a',
                'lease_token': claim['lease_token'],
                'phase': 'after_dm',
                'summary': 'Pause after DM turn',
                'payload': {'turns_completed': 1},
            },
        ).get_json()['audit_cycle']['id']
        audit_resp = self.client.post(
            f'/api/automation/runs/{run_id}/audit-cycles/{cycle_id}/audit',
            headers=self.headers,
            json={
                'scorecard': {
                    'summary': 'Mixed audit',
                    'criteria': [
                        {'id': 'narrative_probe', 'status': 'pass', 'applicability': {'applicable': True}},
                        {'id': 'uncategorized_probe', 'status': 'warn', 'applicability': {'applicable': True}},
                    ],
                },
            },
        )
        self.assertEqual(audit_resp.status_code, 200)

        with app.app_context():
            from services.automation_service import refresh_run_scorecard
            run = db.session.get(AutomationRun, run_id)
            results = refresh_run_scorecard(run)
            by_id = {row['check_id']: row for row in results}

            # Missing built-in metrics follow the same rule as missing custom results:
            # explicit not_assessed, no TypeError, no invented warning.
            self.assertEqual(by_id['missing_numeric']['status'], 'not_assessed')
            self.assertEqual(by_id['missing_enum']['status'], 'not_assessed')
            self.assertEqual(by_id['known_errors']['status'], 'pass')

            # The uncategorized custom criterion stays uncategorized (no narrative fallback).
            self.assertEqual(by_id['custom:uncategorized_probe']['details']['category'], 'uncategorized')
            self.assertFalse(run.scorecard_summary_json['scorecard_configuration']['valid'])
            self.assertEqual(by_id['custom:uncategorized_probe']['status'], 'warn')

            # Weighted score excludes not_assessed rows (weights 7 and 5) and counts only
            # known_errors (pass, w2), narrative_probe (pass, w2), uncategorized_probe (warn, w2).
            self.assertEqual(run.scorecard_summary_json['weighted_score'], round(5 / 6, 4))

            breakdown = run.scorecard_summary_json['category_breakdown']
            self.assertEqual(sorted(breakdown.keys()), sorted([
                'operational/runtime reliability',
                'narrative quality',
                'durable state correctness',
                'retrieval or memory use',
                'safety/private-information handling',
                'uncategorized',
            ]))
            # Narrative category contains only narrative_probe; if the uncategorized warn had
            # leaked in, the score would be 0.75 instead of 1.0.
            self.assertEqual(breakdown['narrative quality']['status'], 'pass')
            self.assertEqual(breakdown['narrative quality']['score'], 1.0)
            self.assertEqual(breakdown['operational/runtime reliability']['status'], 'pass')
            self.assertEqual(breakdown['operational/runtime reliability']['score'], 1.0)
            self.assertEqual(breakdown['safety/private-information handling']['status'], 'not_applicable')
            self.assertIsNone(breakdown['safety/private-information handling']['score'])

            expected_weighted = run.scorecard_summary_json['weighted_score']
            expected_breakdown = run.scorecard_summary_json['category_breakdown']

        # Cross-surface consistency: run API (UI watch payload), scorecard endpoint, and
        # audit bundle all expose the same weighted score and category breakdown.
        run_api_data = self.client.get(f'/api/automation/runs/{run_id}', headers=self.headers).get_json()
        self.assertEqual(run_api_data['run']['scorecard_summary']['weighted_score'], expected_weighted)
        self.assertEqual(run_api_data['run']['scorecard_summary']['category_breakdown'], expected_breakdown)

        scorecard_api_data = self.client.get(f'/api/automation/runs/{run_id}/scorecard', headers=self.headers).get_json()
        self.assertEqual(scorecard_api_data['run']['scorecard_summary']['weighted_score'], expected_weighted)
        self.assertEqual(scorecard_api_data['run']['scorecard_summary']['category_breakdown'], expected_breakdown)

        bundle_api_data = self.client.get(f'/api/automation/runs/{run_id}/audit-bundle', headers=self.headers).get_json()
        self.assertEqual(bundle_api_data['run']['scorecard_summary']['weighted_score'], expected_weighted)
        self.assertEqual(bundle_api_data['run']['scorecard_summary']['category_breakdown'], expected_breakdown)

    def test_builtin_auditor_omitted_criterion_is_not_assessed(self):
        scorecard_id = self.client.post(
            '/api/automation/scorecards',
            headers=self.headers,
            json={
                'name': 'Partial Auditor Scorecard',
                'criteria': [
                    {'id': 'criterion_a', 'label': 'Criterion A', 'weight': 2, 'category': 'narrative quality'},
                    {'id': 'criterion_b', 'label': 'Criterion B', 'weight': 3, 'category': 'durable state correctness'},
                ],
            },
        ).get_json()['scorecard']['id']
        scenario_id = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={'source_campaign_id': self.campaign_id, 'scorecard_template_id': scorecard_id},
        ).get_json()['scenario']['id']
        snapshot_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/snapshots',
            headers=self.headers,
            json={},
        ).get_json()['snapshot']['id']
        run_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': snapshot_id},
        ).get_json()['run']['id']
        claim = self.client.post(
            f'/api/automation/runs/{run_id}/claim',
            headers=self.headers,
            json={'worker_id': 'worker-a'},
        ).get_json()
        cycle_id = self.client.post(
            f'/api/automation/runs/{run_id}/pause',
            headers=self.headers,
            json={
                'worker_id': 'worker-a',
                'lease_token': claim['lease_token'],
                'phase': 'after_dm',
                'summary': 'Pause after DM turn',
                'payload': {'turns_completed': 1},
            },
        ).get_json()['audit_cycle']['id']

        with app.app_context():
            from services.automation_auditor import _normalize_final_scorecard
            from services.automation_service import refresh_run_scorecard, submit_audit_cycle_feedback
            run = db.session.get(AutomationRun, run_id)
            cycle = db.session.get(AutomationRunAuditCycle, cycle_id)

            # The auditor's final JSON only covers one of the two template criteria.
            normalized = _normalize_final_scorecard(
                {
                    'overall_summary': 'Partial audit.',
                    'criteria': [
                        {'criterion_id': 'criterion_a', 'status': 'pass', 'summary': 'Stable.', 'evidence': 'Transcript.'},
                    ],
                },
                run,
            )
            normalized_statuses = {item['criterion_id']: item['status'] for item in normalized['criteria']}
            self.assertEqual(normalized_statuses['criterion_a'], 'pass')
            # Missing results use not_assessed (excluded from weighting), not half-credit warn.
            self.assertEqual(normalized_statuses['criterion_b'], 'not_assessed')

            # The aggregate path applies the same rule to a job scorecard missing the criterion.
            job = AutomationRunAuditorJob(
                run_id=run_id,
                cycle_id=cycle_id,
                auditor_slot=1,
                status='completed',
                submitted_scorecard_json={
                    'overall_status': 'pass',
                    'overall_summary': 'Partial audit.',
                    'criteria': [{'criterion_id': 'criterion_a', 'status': 'pass', 'summary': 'Stable.', 'evidence': 'Transcript.'}],
                    'tool_calls_used': ['get_transcript'],
                },
            )
            db.session.add(job)
            db.session.commit()
            aggregate = aggregate_completed_auditor_jobs(run, cycle, [job])
            aggregate_statuses = {item['criterion_id']: item['status'] for item in aggregate['criteria']}
            self.assertEqual(aggregate_statuses['criterion_b'], 'not_assessed')

            submit_audit_cycle_feedback(cycle, summary='Built-in aggregate', scorecard=aggregate)
            results = refresh_run_scorecard(run)
            by_id = {row['check_id']: row for row in results}
            self.assertEqual(by_id['custom:criterion_a']['status'], 'pass')
            self.assertEqual(by_id['custom:criterion_b']['status'], 'not_assessed')

            # criterion_b (weight 3) must be excluded from the weighted score: recompute
            # from pass/warn/fail rows only and compare against the persisted summary.
            weighted_total = 0
            weighted_pass = 0
            for row in results:
                status = row['status']
                weight = row['details']['weight']
                if status == 'pass':
                    weighted_total += weight
                    weighted_pass += weight
                elif status == 'warn':
                    weighted_total += weight
                    weighted_pass += 0.5 * weight
                elif status == 'fail':
                    weighted_total += weight
            self.assertEqual(
                run.scorecard_summary_json['weighted_score'],
                round(weighted_pass / weighted_total, 4),
            )

    def test_category_status_ignores_not_assessed_criteria(self):
        scorecard_id = self.client.post(
            '/api/automation/scorecards',
            headers=self.headers,
            json={
                'name': 'Category Precedence Scorecard',
                'criteria': [
                    {'id': 'narrative_done', 'label': 'Narrative Done', 'weight': 2, 'category': 'narrative quality'},
                    {'id': 'narrative_pending', 'label': 'Narrative Pending', 'weight': 3, 'category': 'narrative quality'},
                ],
            },
        ).get_json()['scorecard']['id']
        scenario_id = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={'source_campaign_id': self.campaign_id, 'scorecard_template_id': scorecard_id},
        ).get_json()['scenario']['id']
        snapshot_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/snapshots',
            headers=self.headers,
            json={},
        ).get_json()['snapshot']['id']
        run_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': snapshot_id},
        ).get_json()['run']['id']
        claim = self.client.post(
            f'/api/automation/runs/{run_id}/claim',
            headers=self.headers,
            json={'worker_id': 'worker-a'},
        ).get_json()
        cycle_id = self.client.post(
            f'/api/automation/runs/{run_id}/pause',
            headers=self.headers,
            json={
                'worker_id': 'worker-a',
                'lease_token': claim['lease_token'],
                'phase': 'after_dm',
                'summary': 'Pause after DM turn',
                'payload': {'turns_completed': 1},
            },
        ).get_json()['audit_cycle']['id']
        audit_resp = self.client.post(
            f'/api/automation/runs/{run_id}/audit-cycles/{cycle_id}/audit',
            headers=self.headers,
            json={
                'scorecard': {
                    'summary': 'Partial manual audit',
                    'criteria': [
                        {'id': 'narrative_done', 'status': 'pass', 'applicability': {'applicable': True}},
                        {'id': 'narrative_pending', 'status': 'not_assessed', 'applicability': {'applicable': True}},
                    ],
                },
            },
        )
        self.assertEqual(audit_resp.status_code, 200)

        with app.app_context():
            from services.automation_service import refresh_run_scorecard
            run = db.session.get(AutomationRun, run_id)
            refresh_run_scorecard(run)
            # One pass plus one still-unassessed criterion in the same category: not_assessed
            # is excluded from both the category status and its weighted score.
            narrative = run.scorecard_summary_json['category_breakdown']['narrative quality']
            self.assertEqual(narrative['status'], 'pass')
            self.assertEqual(narrative['score'], 1.0)

    def test_reconciliation_lease_expiry_and_reclaim(self):
        run_id, token = self._claim_for_credential_tests()
        start_time = utcnow()
        deadline = start_time + timedelta(seconds=30)

        resp = self.client.post(
            f'/api/automation/runs/{run_id}/events',
            headers=self.headers,
            json={
                'worker_id': 'cred-test-worker',
                'lease_token': token,
                'event_type': 'dm_turn_reconciliation_started',
                'status': 'reconciling',
                'reconciliation_player_message_id': 'msg-123',
                'reconciliation_timeout_phase': 'post_turn',
                'reconciliation_timeout_error': 'dm_post_turn_timeout',
                'reconciliation_started_at': start_time.isoformat(),
                'reconciliation_deadline': deadline.isoformat(),
            }
        )
        self.assertEqual(resp.status_code, 201)

        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            self.assertEqual(run.status, 'reconciling')
            self.assertEqual(run.reconciliation_player_message_id, 'msg-123')
            self.assertEqual(run.reconciliation_timeout_phase, 'post_turn')
            self.assertEqual(run.reconciliation_timeout_error, 'dm_post_turn_timeout')
            self.assertEqual(run.reconciliation_started_at, start_time)
            self.assertEqual(run.reconciliation_deadline, deadline)

            # Expire lease
            run.lease_expires_at = utcnow() - timedelta(seconds=5)
            db.session.commit()

        reclaim_resp = self.client.post(
            f'/api/automation/runs/{run_id}/claim',
            headers=self.headers,
            json={'worker_id': 'new-worker-id'},
        )
        self.assertEqual(reclaim_resp.status_code, 200)

        reclaim_json = reclaim_resp.get_json()
        reclaim_run = reclaim_json['run']
        self.assertEqual(reclaim_run['status'], 'claimed')
        self.assertEqual(reclaim_run['reconciliation_player_message_id'], 'msg-123')
        self.assertEqual(reclaim_run['reconciliation_timeout_phase'], 'post_turn')
        self.assertEqual(reclaim_run['reconciliation_timeout_error'], 'dm_post_turn_timeout')
        self.assertEqual(reclaim_run['reconciliation_deadline'], deadline.isoformat())

    def test_reconciliation_deadline_survives_repeated_reclaim_after_run_started(self):
        run_id, token = self._claim_for_credential_tests()
        start_time = utcnow()
        deadline = start_time + timedelta(seconds=30)

        resp = self.client.post(
            f'/api/automation/runs/{run_id}/events',
            headers=self.headers,
            json={
                'worker_id': 'cred-test-worker',
                'lease_token': token,
                'event_type': 'dm_turn_reconciliation_started',
                'status': 'reconciling',
                'reconciliation_player_message_id': 'msg-456',
                'reconciliation_timeout_phase': 'visible',
                'reconciliation_timeout_error': 'dm_visible_response_timeout',
                'reconciliation_started_at': start_time.isoformat(),
                'reconciliation_deadline': deadline.isoformat(),
            }
        )
        self.assertEqual(resp.status_code, 201)

        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            run.lease_expires_at = utcnow() - timedelta(seconds=5)
            db.session.commit()

        reclaim_resp = self.client.post(
            f'/api/automation/runs/{run_id}/claim',
            headers=self.headers,
            json={'worker_id': 'worker-a'},
        )
        self.assertEqual(reclaim_resp.status_code, 200)
        reclaim_json = reclaim_resp.get_json()
        new_token = reclaim_json['lease_token']

        run_started_resp = self.client.post(
            f'/api/automation/runs/{run_id}/events',
            headers=self.headers,
            json={
                'worker_id': 'worker-a',
                'lease_token': new_token,
                'event_type': 'run_started',
                'status': 'running',
                'reconciliation_player_message_id': 'msg-456',
                'reconciliation_timeout_phase': 'visible',
                'reconciliation_timeout_error': 'dm_visible_response_timeout',
                'reconciliation_started_at': start_time.isoformat(),
                'reconciliation_deadline': deadline.isoformat(),
            }
        )
        self.assertEqual(run_started_resp.status_code, 201)

        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            self.assertEqual(run.status, 'running')
            self.assertEqual(run.reconciliation_player_message_id, 'msg-456')
            self.assertEqual(run.reconciliation_deadline, deadline)
            self.assertEqual(run.reconciliation_started_at, start_time)

            run.lease_expires_at = utcnow() - timedelta(seconds=5)
            db.session.commit()

        reclaim2_resp = self.client.post(
            f'/api/automation/runs/{run_id}/claim',
            headers=self.headers,
            json={'worker_id': 'worker-b'},
        )
        self.assertEqual(reclaim2_resp.status_code, 200)
        reclaim2_run = reclaim2_resp.get_json()['run']
        self.assertEqual(reclaim2_run['status'], 'claimed')
        self.assertEqual(reclaim2_run['reconciliation_player_message_id'], 'msg-456')
        self.assertEqual(reclaim2_run['reconciliation_timeout_phase'], 'visible')
        self.assertEqual(reclaim2_run['reconciliation_deadline'], deadline.isoformat())

        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            run.lease_expires_at = utcnow() - timedelta(seconds=5)
            db.session.commit()

        reclaim3_resp = self.client.post(
            f'/api/automation/runs/{run_id}/claim',
            headers=self.headers,
            json={'worker_id': 'worker-c'},
        )
        self.assertEqual(reclaim3_resp.status_code, 200)
        reclaim3_run = reclaim3_resp.get_json()['run']
        self.assertEqual(reclaim3_run['reconciliation_deadline'], deadline.isoformat())

    def test_scorecard_template_creation_requires_explicit_categories(self):
        # Missing explicit category is rejected.
        missing = self.client.post(
            '/api/automation/scorecards',
            headers=self.headers,
            json={
                'name': 'Missing Category',
                'criteria': [{'id': 'memory_quality', 'label': 'Memory Quality'}],
            },
        )
        self.assertEqual(missing.status_code, 400)
        self.assertIn('explicit category', missing.get_json()['error'])

        # Invalid explicit category is rejected instead of being silently repaired.
        invalid = self.client.post(
            '/api/automation/scorecards',
            headers=self.headers,
            json={
                'name': 'Invalid Category',
                'criteria': [{'id': 'memory_quality', 'label': 'Memory Quality', 'category': 'not-a-real-category'}],
            },
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertIn('invalid category', invalid.get_json()['error'])

        # Alias values are accepted and normalized to the canonical name.
        alias = self.client.post(
            '/api/automation/scorecards',
            headers=self.headers,
            json={
                'name': 'Alias Category',
                'criteria': [{'id': 'memory_quality', 'label': 'Memory Quality', 'category': 'memory'}],
            },
        )
        self.assertEqual(alias.status_code, 201)
        self.assertEqual(alias.get_json()['scorecard']['criteria'][0]['category'], 'retrieval or memory use')

        # Canonical values are accepted unchanged.
        canonical = self.client.post(
            '/api/automation/scorecards',
            headers=self.headers,
            json={
                'name': 'Canonical Category',
                'criteria': [{'id': 'story_consistency', 'label': 'Story Consistency', 'category': 'narrative quality'}],
            },
        )
        self.assertEqual(canonical.status_code, 201)
        self.assertEqual(canonical.get_json()['scorecard']['criteria'][0]['category'], 'narrative quality')

    def test_scorecard_template_update_requires_explicit_categories(self):
        created = self.client.post(
            '/api/automation/scorecards',
            headers=self.headers,
            json={
                'name': 'Update Target',
                'criteria': [{'id': 'memory_quality', 'label': 'Memory Quality', 'category': 'retrieval or memory use'}],
            },
        ).get_json()['scorecard']
        scorecard_id = created['id']

        missing = self.client.put(
            f'/api/automation/scorecards/{scorecard_id}',
            headers=self.headers,
            json={
                'name': 'Update Target',
                'criteria': [{'id': 'memory_quality', 'label': 'Memory Quality'}],
            },
        )
        self.assertEqual(missing.status_code, 400)
        self.assertIn('explicit category', missing.get_json()['error'])

        invalid = self.client.put(
            f'/api/automation/scorecards/{scorecard_id}',
            headers=self.headers,
            json={
                'name': 'Update Target',
                'criteria': [{'id': 'memory_quality', 'label': 'Memory Quality', 'category': 'bogus'}],
            },
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertIn('invalid category', invalid.get_json()['error'])

        alias = self.client.put(
            f'/api/automation/scorecards/{scorecard_id}',
            headers=self.headers,
            json={
                'name': 'Update Target',
                'criteria': [{'id': 'story_consistency', 'label': 'Story Consistency', 'category': 'narrative'}],
            },
        )
        self.assertEqual(alias.status_code, 200)
        self.assertEqual(alias.get_json()['scorecard']['criteria'][0]['category'], 'narrative quality')

        canonical = self.client.put(
            f'/api/automation/scorecards/{scorecard_id}',
            headers=self.headers,
            json={
                'name': 'Update Target',
                'criteria': [{'id': 'scene_state', 'label': 'Scene State', 'category': 'durable state correctness'}],
            },
        )
        self.assertEqual(canonical.status_code, 200)
        self.assertEqual(canonical.get_json()['scorecard']['criteria'][0]['category'], 'durable state correctness')

    def test_scorecard_template_activation_requires_valid_categories(self):
        valid = self.client.post(
            '/api/automation/scorecards',
            headers=self.headers,
            json={
                'name': 'Activatable',
                'criteria': [{'id': 'memory_quality', 'label': 'Memory Quality', 'category': 'retrieval or memory use'}],
            },
        ).get_json()['scorecard']
        ok = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={'source_campaign_id': self.campaign_id, 'scorecard_template_id': valid['id']},
        )
        self.assertEqual(ok.status_code, 201)
        scenario_id = ok.get_json()['scenario']['id']

        with app.app_context():
            from models import AutomationScorecardTemplate
            legacy = AutomationScorecardTemplate(
                user_id=self.owner_id,
                name='Legacy Invalid',
                criteria_json=[
                    {'id': 'memory_quality', 'label': 'Memory Quality', 'weight': 2},
                    {'id': 'unknown_crit', 'label': 'Unknown', 'weight': 2, 'category': 'not-a-real-category'},
                ],
                defaults_json={},
            )
            db.session.add(legacy)
            db.session.commit()
            legacy_id = legacy.id

        rejected = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={'source_campaign_id': self.campaign_id, 'scorecard_template_id': legacy_id},
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertIn('cannot be activated', rejected.get_json()['error'])

        update_rejected = self.client.put(
            f'/api/automation/scenarios/{scenario_id}',
            headers=self.headers,
            json={'scorecard_template_id': legacy_id},
        )
        self.assertEqual(update_rejected.status_code, 400)
        self.assertIn('cannot be activated', update_rejected.get_json()['error'])

        detach = self.client.put(
            f'/api/automation/scenarios/{scenario_id}',
            headers=self.headers,
            json={'scorecard_template_id': None},
        )
        self.assertEqual(detach.status_code, 200)

    def test_legacy_scorecard_template_upgrade_assigns_canonical_categories(self):
        # Reproduce the deployed pre-v2 Memory Audit Scorebook: generic criterion
        # ids with the real stored descriptions but no category. The startup repair
        # must assign the explicit per-criterion categories those descriptions imply.
        deployed_criteria = [
            {'id': 'criterion_1', 'label': 'Criterion 1', 'weight': 2,
             'description': 'Established facts remain stable and are recalled accurately when relevant.'},
            {'id': 'criterion_2', 'label': 'Criterion 2', 'weight': 2,
             'description': 'Secret or unrevealed information is not exposed to players without in-world justification.'},
            {'id': 'criterion_3', 'label': 'Criterion 3', 'weight': 2,
             'description': 'DM output remains consistent with the current location, participants, objectives, and immediate prior events.'},
            {'id': 'criterion_4', 'label': 'Criterion 4', 'weight': 2,
             'description': 'NPC and character names, roles, traits, relationships, and ownership remain consistent.'},
            {'id': 'criterion_5', 'label': 'Criterion 5', 'weight': 2,
             'description': 'Retrieved campaign memory is pertinent, timely, and not contradicted by stronger current evidence.'},
            {'id': 'criterion_6', 'label': 'Criterion 6', 'weight': 2,
             'description': 'Cycle and campaign summaries preserve material facts, decisions, unresolved threads, and consequences without invention.'},
            {'id': 'criterion_7', 'label': 'Criterion 7', 'weight': 2,
             'description': 'World clocks, deadlines, elapsed time, and triggered consequences align with recorded state and narration.'},
            {'id': 'criterion_8', 'label': 'Criterion 8', 'weight': 2,
             'description': 'Transcript, world state, characters, NPCs, clocks, and campaign memory do not materially diverge.'},
        ]
        expected_categories = {
            'criterion_1': 'durable state correctness',
            'criterion_2': 'safety/private-information handling',
            'criterion_3': 'durable state correctness',
            'criterion_4': 'durable state correctness',
            'criterion_5': 'retrieval or memory use',
            'criterion_6': 'retrieval or memory use',
            'criterion_7': 'durable state correctness',
            'criterion_8': 'durable state correctness',
        }
        with app.app_context():
            from models import AutomationScorecardTemplate
            scorebook = AutomationScorecardTemplate(
                user_id=self.owner_id,
                name='Memory Audit Scorebook',
                criteria_json=[dict(criterion) for criterion in deployed_criteria],
                defaults_json={},
            )
            # An unrelated legacy template with a generic id must NOT be assigned a
            # fabricated category; it stays invalid instead of getting a wrong one.
            unrelated = AutomationScorecardTemplate(
                user_id=self.owner_id,
                name='Unrelated Legacy',
                criteria_json=[{'id': 'generic_probe', 'label': 'Generic Probe', 'weight': 2}],
                defaults_json={},
            )
            db.session.add_all([scorebook, unrelated])
            db.session.commit()
            scorebook_id = scorebook.id
            unrelated_id = unrelated.id

        with app.app_context():
            from models import AutomationScorecardTemplate
            from services.automation_service import (
                assert_scorecard_template_activatable,
                scorecard_configuration,
                upgrade_legacy_scorecard_template_categories,
            )
            upgraded = upgrade_legacy_scorecard_template_categories()
            self.assertEqual(upgraded, 1)
            # Idempotent: a second pass makes no further changes.
            self.assertEqual(upgrade_legacy_scorecard_template_categories(), 0)

            scorebook = db.session.get(AutomationScorecardTemplate, scorebook_id)
            categories = {criterion['id']: criterion['category'] for criterion in scorebook.criteria_json}
            self.assertEqual(categories, expected_categories)
            # Descriptions survive the repair untouched.
            self.assertEqual(scorebook.criteria_json[1]['description'], deployed_criteria[1]['description'])
            self.assertTrue(scorecard_configuration(scorebook.snapshot())['valid'])
            assert_scorecard_template_activatable(scorebook)

            unrelated = db.session.get(AutomationScorecardTemplate, unrelated_id)
            self.assertIsNone(unrelated.criteria_json[0].get('category'))
            self.assertFalse(scorecard_configuration(unrelated.snapshot())['valid'])

        ok = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={'source_campaign_id': self.campaign_id, 'scorecard_template_id': scorebook_id},
        )
        self.assertEqual(ok.status_code, 201)

    def test_scorecard_configuration_signal_consistent_across_exports(self):
        scorecard_id = self.client.post(
            '/api/automation/scorecards',
            headers=self.headers,
            json={
                'name': 'Signal Consistency',
                'criteria': [{'id': 'memory_quality', 'label': 'Memory Quality', 'category': 'retrieval or memory use'}],
            },
        ).get_json()['scorecard']['id']
        scenario_id = self.client.post(
            '/api/automation/scenarios',
            headers=self.headers,
            json={'source_campaign_id': self.campaign_id, 'scorecard_template_id': scorecard_id},
        ).get_json()['scenario']['id']
        snapshot_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/snapshots',
            headers=self.headers,
            json={},
        ).get_json()['snapshot']['id']
        run_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': snapshot_id},
        ).get_json()['run']['id']
        with app.app_context():
            from models import AutomationRun
            run = db.session.get(AutomationRun, run_id)
            # Legacy snapshot (schema v1) carrying an invalid explicit category; must remain
            # readable through the scoring normalization path and surface a config error.
            run.scorecard_template_json = {
                'template_id': scorecard_id,
                'schema_version': 1,
                'name': 'Signal Consistency',
                'criteria': [
                    {'id': 'memory_quality', 'label': 'Memory Quality', 'weight': 2, 'category': 'retrieval or memory use'},
                    {'id': 'unknown_crit', 'label': 'Unknown', 'weight': 2, 'category': 'not-a-real-category'},
                ],
                'defaults': {},
            }
            db.session.commit()
        claim = self.client.post(
            f'/api/automation/runs/{run_id}/claim',
            headers=self.headers,
            json={'worker_id': 'worker-a'},
        ).get_json()
        cycle_id = self.client.post(
            f'/api/automation/runs/{run_id}/pause',
            headers=self.headers,
            json={
                'worker_id': 'worker-a',
                'lease_token': claim['lease_token'],
                'phase': 'after_dm',
                'summary': 'Pause after DM turn',
                'payload': {'turns_completed': 1},
            },
        ).get_json()['audit_cycle']['id']

        with app.app_context():
            from services.automation_service import refresh_run_scorecard
            run = db.session.get(AutomationRun, run_id)
            results = refresh_run_scorecard(run)
            by_id = {row['check_id']: row for row in results}
            expected = run.scorecard_summary_json['scorecard_configuration']
            expected_breakdown = run.scorecard_summary_json['category_breakdown']
            expected_score = run.scorecard_summary_json['weighted_score']
            # Scoring resolves membership via get_criterion_category(): the canonical explicit
            # category is kept and the invalid explicit value becomes 'uncategorized'.
            self.assertEqual(by_id['custom:memory_quality']['details']['category'], 'retrieval or memory use')
            self.assertEqual(by_id['custom:unknown_crit']['details']['category'], 'uncategorized')
        self.assertFalse(expected['valid'])
        self.assertEqual(expected['uncategorized_criterion_count'], 1)
        self.assertEqual(expected['invalid_criteria'][0]['criterion_id'], 'unknown_crit')

        watch = self.client.get(f'/api/automation/runs/{run_id}', headers=self.headers).get_json()
        watch_summary = watch['run']['scorecard_summary']
        self.assertEqual(watch_summary['scorecard_configuration'], expected)
        self.assertEqual(watch_summary['category_breakdown'], expected_breakdown)
        self.assertEqual(watch_summary['weighted_score'], expected_score)

        scorecard_endpoint = self.client.get(f'/api/automation/runs/{run_id}/scorecard', headers=self.headers).get_json()
        endpoint_summary = scorecard_endpoint['run']['scorecard_summary']
        self.assertEqual(endpoint_summary['scorecard_configuration'], expected)
        self.assertEqual(endpoint_summary['category_breakdown'], expected_breakdown)
        self.assertEqual(endpoint_summary['weighted_score'], expected_score)
        endpoint_rows = {row['check_id']: row for row in scorecard_endpoint['scorecard']}
        self.assertEqual(endpoint_rows['custom:memory_quality']['details']['category'], 'retrieval or memory use')
        self.assertEqual(endpoint_rows['custom:unknown_crit']['details']['category'], 'uncategorized')

        bundle = self.client.get(f'/api/automation/runs/{run_id}/audit-bundle', headers=self.headers).get_json()
        self.assertEqual(bundle['scorecard_template']['configuration'], expected)
        bundle_categories = {entry['id']: entry for entry in bundle['scorecard_template']['criteria']}
        # The audit bundle exports the resolved category (same membership used by scoring)
        # alongside the raw stored value.
        self.assertEqual(bundle_categories['memory_quality']['category'], 'retrieval or memory use')
        self.assertEqual(bundle_categories['memory_quality']['raw_category'], 'retrieval or memory use')
        self.assertEqual(bundle_categories['unknown_crit']['category'], 'uncategorized')
        self.assertEqual(bundle_categories['unknown_crit']['raw_category'], 'not-a-real-category')


if __name__ == '__main__':
    unittest.main()

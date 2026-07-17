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
                'criteria': [{'id': 'memory_quality', 'label': 'Memory Quality'}],
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
                    {'id': 'memory_quality', 'label': 'Memory Quality'},
                    {'id': 'story_consistency', 'label': 'Story Consistency'},
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
                    {'id': 'memory_quality', 'label': 'Memory Quality'},
                    {'id': 'story_consistency', 'label': 'Story Consistency'},
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
            self.assertEqual(prompt['final_response_contract']['overall_status'], 'pass|warn|fail|not_assessed')
            self.assertIn('primary_evidence', prompt['final_response_contract']['criteria'][0])
            self.assertEqual(prompt['final_response_contract']['criteria'][0]['status'], 'pass|warn|fail|not_assessed')
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
                    {'id': 'memory_quality', 'label': 'Memory Quality'},
                    {'id': 'scene_state', 'label': 'Scene State'},
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
                'criteria': [{'id': 'memory_quality', 'label': 'Memory Quality'}],
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
                'criteria': [{'id': 'memory_quality', 'label': 'Memory Quality'}],
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
                'criteria': [{'id': 'retrieval_relevance', 'label': 'Retrieval Relevance'}],
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
                'criteria': [{'id': 'memory_quality', 'label': 'Memory Quality'}],
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

        tool_response = {
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
        }
        final_response = {
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
        }

        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            cycle = db.session.get(AutomationRunAuditCycle, cycle_id)
            job = AutomationRunAuditorJob(run_id=run_id, cycle_id=cycle_id, auditor_slot=1, status='queued')
            db.session.add(job)
            db.session.commit()

            with patch('services.automation_auditor._post_chat_response', side_effect=[tool_response, final_response]):
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
                    {'id': 'criterion_a', 'label': 'Criterion A'},
                    {'id': 'criterion_b', 'label': 'Criterion B'}
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
            self.assertEqual(crit_b['status'], 'not_assessed')
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
        self.assertEqual(submit_resp_4.status_code, 200)
        with app.app_context():
            cycle_db = db.session.get(AutomationRunAuditCycle, cycle_id)
            self.assertEqual(cycle_db.scorecard_summary_json['overall_status'], 'fail')

    def test_security_redaction_and_lease_token_safety(self):
        scorecard = self.client.post(
            '/api/automation/scorecards',
            headers=self.headers,
            json={
                'name': 'Security Testing Scorecard',
                'criteria': [{'id': 'criterion_sec', 'label': 'Security Criterion'}]
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

    def test_events_route_rejects_missing_credentials(self):
        run_id, token = self._claim_for_credential_tests()
        resp = self.client.post(
            f'/api/automation/runs/{run_id}/events',
            headers=self.headers,
            json={'event_type': 'custom_event', 'payload': {}},
        )
        self.assertEqual(resp.status_code, 409)

    def test_events_route_rejects_wrong_worker_id(self):
        run_id, token = self._claim_for_credential_tests()
        resp = self.client.post(
            f'/api/automation/runs/{run_id}/events',
            headers=self.headers,
            json={
                'event_type': 'custom_event', 'payload': {},
                'worker_id': 'wrong-worker', 'lease_token': token,
            },
        )
        self.assertEqual(resp.status_code, 409)

    def test_events_route_rejects_wrong_lease_token(self):
        run_id, token = self._claim_for_credential_tests()
        resp = self.client.post(
            f'/api/automation/runs/{run_id}/events',
            headers=self.headers,
            json={
                'event_type': 'custom_event', 'payload': {},
                'worker_id': 'cred-test-worker', 'lease_token': 'bad-token',
            },
        )
        self.assertEqual(resp.status_code, 409)

    def test_events_route_accepts_valid_credentials(self):
        run_id, token = self._claim_for_credential_tests()
        resp = self.client.post(
            f'/api/automation/runs/{run_id}/events',
            headers=self.headers,
            json={
                'event_type': 'custom_event', 'payload': {},
                'worker_id': 'cred-test-worker', 'lease_token': token,
            },
        )
        self.assertIn(resp.status_code, {200, 201})

    def test_provider_calls_route_rejects_missing_credentials(self):
        run_id, token = self._claim_for_credential_tests()
        resp = self.client.post(
            f'/api/automation/runs/{run_id}/provider-calls',
            headers=self.headers,
            json={'dedupe_key': 'test-pc-cred', 'phase': 'after_dm', 'request': {}, 'response': {}, 'parsed_output': {}},
        )
        self.assertEqual(resp.status_code, 409)

    def test_provider_calls_route_accepts_valid_credentials(self):
        run_id, token = self._claim_for_credential_tests()
        resp = self.client.post(
            f'/api/automation/runs/{run_id}/provider-calls',
            headers=self.headers,
            json={
                'dedupe_key': 'test-pc-cred-ok', 'phase': 'after_dm',
                'request': {}, 'response': {}, 'parsed_output': {},
                'worker_id': 'cred-test-worker', 'lease_token': token,
            },
        )
        self.assertEqual(resp.status_code, 201)

    def test_complete_route_rejects_missing_credentials(self):
        run_id, token = self._claim_for_credential_tests()
        resp = self.client.post(
            f'/api/automation/runs/{run_id}/complete',
            headers=self.headers,
            json={},
        )
        self.assertEqual(resp.status_code, 409)

    def test_complete_route_accepts_valid_credentials(self):
        run_id, token = self._claim_for_credential_tests()
        resp = self.client.post(
            f'/api/automation/runs/{run_id}/complete',
            headers=self.headers,
            json={
                'worker_id': 'cred-test-worker', 'lease_token': token,
            },
        )
        self.assertEqual(resp.status_code, 200)

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

    def test_events_route_rejects_status_completed_bypass(self):
        run_id, token = self._claim_for_credential_tests()
        resp = self.client.post(
            f'/api/automation/runs/{run_id}/events',
            headers=self.headers,
            json={'event_type': 'custom', 'status': 'completed'},
        )
        self.assertEqual(resp.status_code, 409)

    def test_events_route_rejects_status_queued_bypass(self):
        run_id, token = self._claim_for_credential_tests()
        resp = self.client.post(
            f'/api/automation/runs/{run_id}/events',
            headers=self.headers,
            json={'event_type': 'custom', 'status': 'queued'},
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

        # 5. Verify unclassified/unknown category criterion is excluded from breakdowns but included in score
        scorecard_id2 = self.client.post(
            '/api/automation/scorecards',
            headers=self.headers,
            json={
                'name': 'Unknown category scorecard',
                'criteria': [
                    {'id': 'unclassified_crit', 'label': 'Unclassified', 'weight': 2, 'category': 'unknown-cat-name-xyz'},
                ],
            },
        ).get_json()['scorecard']['id']
        
        with app.app_context():
            from models import AutomationScorecardTemplate
            run = db.session.get(AutomationRun, run_id)
            run.scorecard_template_json = db.session.get(AutomationScorecardTemplate, scorecard_id2).snapshot()
            db.session.commit()
            
            res2 = refresh_run_scorecard(run)
            bd2 = run.scorecard_summary_json['category_breakdown']
            # "unknown-cat-name-xyz" should not be in the breakdown keys (only the 5 named categories)
            self.assertNotIn('unknown-cat-name-xyz', bd2)
            # Make sure it didn't fallback to narrative quality
            self.assertEqual(bd2['narrative quality']['status'], 'pass')

        # 6. Verify missing built-in metric (value is None) evaluates to not_assessed
        with app.app_context():
            from services.automation_service import _evaluate_check
            self.assertEqual(_evaluate_check(None, {'id': 'test_none', 'kind': 'number'}), 'not_assessed')
            self.assertEqual(_evaluate_check(None, {'id': 'test_none_enum', 'kind': 'enum'}), 'not_assessed')


if __name__ == '__main__':
    unittest.main()

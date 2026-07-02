import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ['DATABASE_URL'] = 'sqlite:///:memory:'

from app import app
from auth import generate_token
from models import (
    AutomationRun,
    AutomationRunAuditCycle,
    AutomationRunEvent,
    AutomationScorecardTemplate,
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
from services.character_service import update_character_relations


class AutomationRouteTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_root_path = app.root_path
        app.root_path = self.temp_dir.name

        with app.app_context():
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
            clone_campaign = Campaign(
                name='Automation Source [Run 9]',
                user_id=self.owner_id,
                status='automation_run',
                is_automation_clone=True,
                automation_source_campaign_id=self.campaign_id,
            )
            db.session.add(clone_campaign)
            db.session.flush()

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
            run.lease_expires_at = datetime.utcnow() - timedelta(seconds=5)
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
        cycle_id = pause_response.get_json()['audit_cycle']['id']
        self.assertEqual(pause_response.get_json()['run']['status'], 'awaiting_audit')

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
        self.assertEqual(continue_response.get_json()['run']['status'], 'running')

        scorecard_response = self.client.get(f'/api/automation/runs/{run_id}/scorecard', headers=self.headers)
        self.assertEqual(scorecard_response.status_code, 200)
        scorecards = {row['check_id']: row for row in scorecard_response.get_json()['scorecard']}
        self.assertEqual(scorecards['custom:memory_quality']['status'], 'pass')
        self.assertEqual(scorecards['custom:story_consistency']['status'], 'warn')
        self.assertEqual(scorecard_response.get_json()['run']['scorecard_summary']['audited_cycle_count'], 1)

        with app.app_context():
            cycle = db.session.get(AutomationRunAuditCycle, cycle_id)
            self.assertEqual(cycle.status, 'audited')


if __name__ == '__main__':
    unittest.main()

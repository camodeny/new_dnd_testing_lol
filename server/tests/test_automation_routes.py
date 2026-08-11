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
    SessionDmTurn,
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

    def test_post_turn_failures_are_correlated_once_across_metrics_and_incidents(self):
        _scenario_id, run_id = self._create_scorecard_run([{'id': 'runtime_truth', 'label': 'Runtime truth'}])
        from services.automation_service import append_run_event, refresh_run_scorecard

        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            run.status = 'completed'
            run.derived_campaign_id = self.campaign_id
            session = CampaignSession.query.filter_by(campaign_id=run.derived_campaign_id).first()
            if session is None:
                session = CampaignSession(campaign_id=run.derived_campaign_id, is_active=True)
                db.session.add(session)
                db.session.flush()

            for index in range(3):
                player_message = SessionMessage(
                    session_id=session.id,
                    user_id=run.user_id,
                    role='player',
                    content=f'Post-turn failure {index}',
                )
                db.session.add(player_message)
                db.session.flush()
                trace_id = f'session_dm:session_{session.id}:message_{player_message.id}'
                db.session.add(SessionDmTurn(
                    campaign_id=run.derived_campaign_id,
                    session_id=session.id,
                    player_message_id=player_message.id,
                    trace_id=trace_id,
                    status='speak',
                    post_turn_status='error',
                    memory_status='error',
                    clock_status='skipped',
                    error_text='Missing relation endpoint.',
                ))
                db.session.add(CampaignAuditEvent(
                    campaign_id=run.derived_campaign_id,
                    event_type='memory_update_error',
                    summary='Post-turn memory update failed.',
                    payload=json.dumps({'player_message_id': player_message.id}),
                    trace_id=f'session_memory_writer:session_{session.id}:message_{player_message.id}',
                    parent_trace_id=trace_id,
                ))
                append_run_event(
                    run,
                    'dm_turn_status',
                    {
                        'player_message_id': player_message.id,
                        'status': 'speak',
                        'post_turn_status': 'error',
                        'memory_status': 'error',
                        'clock_status': 'skipped',
                        'post_turn_error': 'Missing relation endpoint.',
                    },
                    dedupe_key=f'issue-99-dm-status:{player_message.id}',
                    commit=False,
                    skip_workspace=True,
                )
                append_run_event(
                    run,
                    'turn_result',
                    {'action': 'speak', 'player_message_id': player_message.id},
                    dedupe_key=f'issue-99-turn:{player_message.id}',
                    commit=False,
                    skip_workspace=True,
                )
            db.session.commit()
            results = refresh_run_scorecard(run)
            summary = run.scorecard_summary_json

            self.assertEqual(summary['error_count'], 3)
            self.assertEqual(summary['error_counts_by_kind'], {'memory': 3})
            self.assertEqual(summary['unrecovered_error_count'], 3)
            self.assertEqual(summary['recovered_error_count'], 0)
            self.assertEqual(len(summary['automation_errors']), 3)
            self.assertEqual(len(summary['incidents']), 3)
            self.assertTrue(all(item['evidence_refs'] for item in summary['automation_errors']))
            error_check = next(item for item in results if item['check_id'] == 'error_count')
            self.assertEqual(error_check['status'], 'fail')
            self.assertEqual(error_check['details']['metric_value'], 3)

        response = self.client.get(f'/api/automation/runs/{run_id}/scorecard', headers=self.headers)
        self.assertEqual(response.status_code, 200)
        api_summary = response.get_json()['run']['scorecard_summary']
        self.assertEqual(api_summary['error_count'], 3)
        self.assertEqual(api_summary['error_counts_by_kind'], {'memory': 3})
        self.assertEqual(len(api_summary['incidents']), 3)



    def test_trace_only_memory_update_error_is_correlated_without_raising(self):
        _scenario_id, run_id = self._create_scorecard_run([{'id': 'runtime_truth', 'label': 'Runtime truth'}])
        from services.automation_service import refresh_run_scorecard

        with app.app_context():
            run = db.session.get(AutomationRun, run_id)
            run.status = 'completed'
            run.derived_campaign_id = self.campaign_id
            # Regression for Run 42: a trace-only post-turn error event whose
            # payload omits player_message_id must correlate via the trace id
            # without raising (the missing `re` import surfaced here).
            db.session.add(CampaignAuditEvent(
                campaign_id=run.derived_campaign_id,
                event_type='memory_update_error',
                summary='Post-turn memory update failed.',
                payload=json.dumps({'kind': 'trace_only'}),
                trace_id='session_memory_writer:session_1:message_4242',
                parent_trace_id='session_dm:session_1:message_4242',
            ))
            db.session.commit()
            results = refresh_run_scorecard(run)
            summary = run.scorecard_summary_json

            self.assertEqual(summary['error_counts_by_kind'], {'memory': 1})
            self.assertEqual(summary['unrecovered_error_count'], 1)
            correlated = summary['automation_errors'][0]['correlation_key']
            self.assertTrue(correlated.startswith('player_message:'))
            self.assertEqual(summary['automation_errors'][0]['player_message_id'], 4242)
            error_check = next(item for item in results if item['check_id'] == 'error_count')
            self.assertEqual(error_check['status'], 'fail')

        response = self.client.get(f'/api/automation/runs/{run_id}/scorecard', headers=self.headers)
        self.assertEqual(response.status_code, 200)
        api_summary = response.get_json()['run']['scorecard_summary']
        self.assertEqual(api_summary['error_counts_by_kind'], {'memory': 1})
        self.assertEqual(api_summary['automation_errors'][0]['player_message_id'], 4242)


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

    def test_cycle_average_preserves_worst_severity(self):
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

        # Derived recomputation happens on the scorecard/evidence endpoints;
        # control-plane reads serve the last committed aggregate state. Recompute
        # first so all surfaces observe the same canonical snapshot.
        scorecard_payload = self.client.get(
            f'/api/automation/runs/{run_id}/scorecard',
            headers=self.headers,
        ).get_json()
        run_payload = self.client.get(
            f'/api/automation/runs/{run_id}',
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
            audit_event = CampaignAuditEvent(
                campaign_id=run.derived_campaign_id,
                event_type='memory_patch_applied',
                source='dm_tools.memory',
                actor='session_memory_writer',
                summary='Applied clone memory patch.',
                payload='{"scene_patch":{"location_name":"Mirror Dock"},"facts":[{"id":"dock_fact"}]}',
            )
            provider_call = AutomationRunProviderCall(
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
            )
            run_event = AutomationRunEvent(
                run_id=run_id,
                event_type='player_decision',
                sequence_number=99,
                attempt_number=1,
                dedupe_key='test:run:event',
                payload_json={'speaker': 'Seraphina', 'decision': {'action': 'speak'}},
            )
            db.session.add_all([audit_event, provider_call, run_event])
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

            # Advance the explicit snapshot boundary to include these fixtures.
            cycle = db.session.get(AutomationRunAuditCycle, cycle_id)
            payload = dict(cycle.payload_json or {})
            payload.update({
                'boundary_audit_event_id': audit_event.id,
                'boundary_provider_call_id': provider_call.id,
                'boundary_run_event_id': run_event.id,
            })
            cycle.payload_json = payload
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

            # Advance the explicit snapshot boundary to include these fixtures.
            cycle = db.session.get(AutomationRunAuditCycle, cycle_id)
            payload = dict(cycle.payload_json or {})
            payload.update({
                'boundary_audit_event_id': audit_ev.id,
                'boundary_provider_call_id': pc.id,
                'boundary_run_event_id': legacy_run_ev.id,
            })
            cycle.payload_json = payload
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







    def _claim_proposal_run(self, worker_id='proposal-test-worker'):
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
            json={'worker_id': worker_id},
        ).get_json()
        return run_id, claim





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


    def test_retry_taxonomy_is_complete_deduplicated_and_cross_surface_consistent(self):
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
            json={'worker_id': 'run-37-worker'},
        )

        with app.app_context():
            from services.automation_service import refresh_run_scorecard

            run = db.session.get(AutomationRun, run_id)
            run.status = 'completed'
            db.session.add(AutomationRunEvent(
                run_id=run.id,
                event_type='turn_result',
                sequence_number=(run.last_event_sequence or 0) + 1,
                attempt_number=1,
                dedupe_key='run-37:turn-result',
                payload_json={'action': 'speak', 'turn_number': 1},
            ))
            cycle = AutomationRunAuditCycle(
                run_id=run.id,
                cycle_number=1,
                phase='after_dm',
                status='audited',
                payload_json={},
                scorecard_json={},
            )
            db.session.add(cycle)
            db.session.flush()
            run.awaiting_audit_cycle_id = cycle.id
            run.awaiting_audit_phase = 'after_dm'

            # Run 37 fixture: one finalizer contract guard retry on each of ten turns.
            for turn in range(1, 11):
                db.session.add(CampaignAuditEvent(
                    campaign_id=run.derived_campaign_id,
                    event_type='finalizer_contract_guard_retry',
                    source='session_dm.guard',
                    actor='session_dm_guard',
                    trace_id=f'run-37:turn:{turn}:finalizer_contract_guard',
                    parent_trace_id=f'run-37:turn:{turn}',
                    summary='Finalizer contract repair.',
                    payload=json.dumps({'turn_number': turn, 'attempt': 1, 'provider_call_id': 1000 + turn}),
                ))
            # The tenth repair was exhausted; the shared trace correlates its outcome.
            db.session.add(CampaignAuditEvent(
                campaign_id=run.derived_campaign_id,
                event_type='finalizer_contract_guard_blocked',
                source='session_dm.guard',
                actor='session_dm_guard',
                trace_id='run-37:turn:10:finalizer_contract_guard',
                parent_trace_id='run-37:turn:10',
                summary='Finalizer contract repair exhausted.',
                payload='{"turn_number":10,"outcome":"exhausted"}',
            ))
            mixed_events = [
                ('model_retry', 'provider:1', {'turn_number': 2, 'attempt': 1, 'next_attempt': 2}),
                ('private_output_guard_retry', 'guard:success', {'turn_number': 3, 'attempt': 1}),
                ('private_output_guard_retry', 'guard:exhausted', {'turn_number': 3, 'attempt': 2}),
                ('private_output_guard_blocked', 'guard:exhausted', {'turn_number': 3, 'outcome': 'exhausted'}),
                ('tool_call_repair', 'tool:1', {'turn_number': 4, 'attempt': 1, 'outcome': 'repaired'}),
                ('model_request', 'other:1', {'turn_number': 5, 'operation': 'planning_dm_response_blank_retry'}),
            ]
            for event_type, trace_id, payload in mixed_events:
                db.session.add(CampaignAuditEvent(
                    campaign_id=run.derived_campaign_id,
                    event_type=event_type,
                    source='test',
                    actor='session_dm',
                    trace_id=trace_id,
                    parent_trace_id=f'{trace_id}:parent',
                    summary=event_type,
                    payload=json.dumps(payload),
                ))
            db.session.add(AutomationRunProviderCall(
                run_id=run.id,
                dedupe_key='run-37:parse-repairs',
                phase='session_dm',
                provider='test',
                model='test-model',
                parse_repair_attempts=3,
                request_json={'turn_number': 6, 'trace_id': 'parse:1'},
                response_json={},
                parsed_output_json={},
            ))
            db.session.commit()

            first = refresh_run_scorecard(run)
            second = refresh_run_scorecard(run)
            first_retry = next(row for row in first if row['check_id'] == 'model_retry_count')
            second_retry = next(row for row in second if row['check_id'] == 'model_retry_count')
            expected_counts = {
                'provider_retry': 1,
                'parse_repair': 3,
                'resolver_contract_repair': 0,
                'contract_guard_retry': 10,
                'tool_repair': 1,
                'guard_retry': 2,
                'other_model_reinvocation': 1,
            }
            self.assertEqual(first_retry['details']['metric_value'], 18)
            self.assertEqual(second_retry['details']['metric_value'], 18)
            self.assertEqual(first_retry['details']['retry_metrics']['counts'], expected_counts)
            self.assertEqual(sum(expected_counts.values()), first_retry['details']['retry_metrics']['total'])
            self.assertEqual(first_retry['status'], 'fail')
            self.assertIn('18 model retries or repair re-invocations', first_retry['summary'])
            correlations = first_retry['details']['retry_metrics']['correlations']
            self.assertEqual(len({item['source_key'] for item in correlations}), 18)
            self.assertTrue(any(item['outcome'] == 'repaired' for item in correlations))
            self.assertTrue(any(item['outcome'] == 'exhausted' for item in correlations))
            self.assertTrue(all('turn' in item and 'provider_call_id' in item and 'attempt' in item for item in correlations))

        baseline_run_id = self.client.post(
            f'/api/automation/scenarios/{scenario_id}/runs',
            headers=self.headers,
            json={'snapshot_id': snapshot_id},
        ).get_json()['run']['id']
        with app.app_context():
            from services.automation_service import refresh_run_scorecard

            run = db.session.get(AutomationRun, run_id)
            baseline_run = db.session.get(AutomationRun, baseline_run_id)
            baseline_run.status = 'completed'
            db.session.add(AutomationRunEvent(
                run_id=baseline_run.id,
                event_type='turn_result',
                sequence_number=(baseline_run.last_event_sequence or 0) + 1,
                attempt_number=1,
                dedupe_key='run-37:baseline-turn-result',
                payload_json={'action': 'speak', 'turn_number': 1},
            ))
            run.scenario.baseline_run_id = baseline_run.id
            db.session.commit()
            refresh_run_scorecard(run)

        watch = self.client.get(f'/api/automation/runs/{run_id}', headers=self.headers).get_json()
        scorecard = self.client.get(f'/api/automation/runs/{run_id}/scorecard', headers=self.headers).get_json()
        bundle = self.client.get(f'/api/automation/runs/{run_id}/audit-bundle', headers=self.headers).get_json()
        self.assertEqual(watch['retry_metrics']['counts'], expected_counts)
        self.assertEqual(watch['run']['scorecard_summary']['retry_metrics']['total'], 18)
        scorecard_retry = next(row for row in scorecard['scorecard'] if row['check_id'] == 'model_retry_count')
        self.assertEqual(scorecard_retry['details']['retry_metrics']['counts'], expected_counts)
        self.assertEqual(bundle['retry_metrics']['counts'], expected_counts)
        self.assertEqual(bundle['evidence_packet']['retry_metrics']['total'], 18)
        baseline_retry = next(
            row for row in watch['baseline_comparison']['comparisons']
            if row['check_id'] == 'model_retry_count'
        )
        self.assertEqual(baseline_retry['baseline_retry_metrics']['total'], 0)
        self.assertEqual(baseline_retry['current_retry_metrics']['counts'], expected_counts)
        retry_incident = next(item for item in watch['incidents'] if item['incident_type'] == 'retry_storm')
        self.assertEqual(retry_incident['count'], 18)
        self.assertEqual(retry_incident['retry_counts'], expected_counts)


if __name__ == '__main__':
    unittest.main()

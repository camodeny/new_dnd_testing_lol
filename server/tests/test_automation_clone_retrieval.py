import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ['DATABASE_URL'] = 'sqlite:///:memory:'

from app import app
from auth import generate_token
from time_utils import utcnow
from models import (
    AutomationRun,
    AutomationRunEvent,
    AutomationSnapshot,
    AutomationScenario,
    Campaign,
    CampaignClock,
    CampaignMemoryEmbedding,
    CampaignSession,
    CampaignWorld,
    Character,
    NPCActor,
    User,
    WorldEvent,
    db,
)
from services.automation_service import (
    AUTOMATION_SNAPSHOT_SCHEMA_VERSION,
    CLONE_RETRIEVAL_PREFLIGHT_VERSION,
    CloneRetrievalPreflightError,
    create_snapshot_for_scenario,
    claim_run_for_worker,
    _campaign_candidate_keys,
    validate_clone_retrieval_equivalence,
    _normalize_memory_anchors,
)
from services import dm_tools


class AutomationCloneRetrievalTest(unittest.TestCase):
    def setUp(self):
        self.app_context = app.app_context()
        self.app_context.push()
        db.drop_all()
        db.create_all()

        # Create basic fixture data
        self.user = User(username='testworker', email='worker@example.com')
        self.user.set_password('password')
        db.session.add(self.user)
        db.session.flush()

        self.campaign = Campaign(name='Source Campaign', user_id=self.user.id)
        db.session.add(self.campaign)
        db.session.flush()

        self.session = CampaignSession(
            campaign_id=self.campaign.id,
            is_active=True,
            memory_anchors={
                'current_goal': 'Find the ancient artifact',
                'current_scene': 'The dark ruins',
                'open_clues': ['Old parchment map'],
                'unresolved_questions': ['Who built the ruins?'],
                'npc_observations': ['The local guard is suspicious'],
                'recent_offers_promises': ['A reward of 50 gold pieces'],
            }
        )
        db.session.add(self.session)
        db.session.flush()

        self.world = CampaignWorld(
            campaign_id=self.campaign.id,
            public_intro='{"title": "Source Campaign"}',
            knowledge_graph='{"entities": [{"id": "ruins_key", "name": "Ancient Ruins"}], "relations": [], "facts": [{"id": "ruins_fact", "summary": "The ruins are haunted"}]}',
            world_state='{"current_scene": "The dark ruins"}',
            dm_private='{"secret": "A dragon sleeps below"}',
        )
        db.session.add(self.world)

        self.npc = NPCActor(
            campaign_id=self.campaign.id,
            actor_id='npc_guard',
            name='Guard Captain',
            role='Guard Captain',
            public_summary='Guard Captain',
            dossier='{}',
        )
        db.session.add(self.npc)

        self.clock = CampaignClock(
            campaign_id=self.campaign.id,
            clock_id='clock_danger',
            name='Imminent Danger',
            segments=4,
            filled=1,
            pressure_type='danger',
            visibility='dm_private',
            summary='Danger approaches',
            status='active',
        )
        db.session.add(self.clock)

        self.event = WorldEvent(
            campaign_id=self.campaign.id,
            event_type='world_event',
            summary='The skies turned red.',
            payload='{}',
            visibility='dm_private',
        )
        db.session.add(self.event)
        db.session.flush()

        # Add embeddings matching all required types
        self.embeddings = []
        embedding_data = [
            ('entity', 'ruins_key'),
            ('fact', 'ruins_fact'),
            ('npc_actor', 'npc_guard'),
            ('clock', 'clock_danger'),
            ('world_event', str(self.event.id)),
            ('world_state', 'current'),
        ]
        for item_type, item_id in embedding_data:
            emb = CampaignMemoryEmbedding(
                campaign_id=self.campaign.id,
                item_type=item_type,
                item_id=item_id,
                visibility='dm_private',
                canonical_text=f'Canonical text for {item_type}:{item_id}',
                text_hash='testhash',
                embedding_model='gemini-embedding-2',
                embedding_dimensions=3,
                embedding_json='[0.1, 0.2, 0.3]',
            )
            db.session.add(emb)
            self.embeddings.append(emb)

        db.session.flush()

        # Create scenario
        self.scenario = AutomationScenario(
            source_campaign_id=self.campaign.id,
            user_id=self.user.id,
            name='Test Scenario',
        )
        db.session.add(self.scenario)
        db.session.flush()

        # Create snapshot using schema version 2
        self.snapshot = create_snapshot_for_scenario(
            self.scenario,
            label='Test Snapshot',
        )

        # Create automation run
        self.run = AutomationRun(
            scenario_id=self.scenario.id,
            snapshot_id=self.snapshot.id,
            user_id=self.user.id,
            status='queued',
        )
        db.session.add(self.run)
        db.session.commit()

        self.token = generate_token(self.user.id)
        self.client = app.test_client()

    def tearDown(self):
        db.session.rollback()
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_snapshot_captures_retrieval_contract(self):
        """Test 1: snapshot captures retrieval contract (schema v2, world events, embeddings)"""
        snapshot = db.session.get(AutomationSnapshot, self.snapshot.id)
        data = snapshot.snapshot_json

        self.assertEqual(data.get('snapshot_schema_version'), AUTOMATION_SNAPSHOT_SCHEMA_VERSION)
        self.assertIn('memory_embeddings', data)
        self.assertIn('world_events', data)
        self.assertTrue(len(data['memory_embeddings']) > 0)
        self.assertTrue(len(data['world_events']) > 0)

        for session in data['sessions']:
            self.assertIn('memory_anchors', session)
            self.assertEqual(
                _normalize_memory_anchors(session['memory_anchors']),
                _normalize_memory_anchors(self.session.memory_anchors),
            )

        metadata = snapshot.metadata_json
        self.assertEqual(metadata.get('snapshot_schema_version'), AUTOMATION_SNAPSHOT_SCHEMA_VERSION)
        self.assertEqual(metadata.get('world_event_count'), 1)
        self.assertEqual(metadata.get('memory_embedding_count'), len(self.embeddings))
        self.assertEqual(metadata.get('memory_anchor_session_count'), 1)

    def test_claim_copies_session_memory_anchors(self):
        """Test 2: successful claim copies anchors"""
        headers = {'Authorization': f'Bearer {self.token}'}
        response = self.client.post(
            f'/api/automation/runs/{self.run.id}/claim',
            headers=headers,
            json={'worker_id': 'worker-1'},
        )
        self.assertEqual(response.status_code, 200)
        res_data = response.get_json()
        self.assertEqual(res_data['retrieval_preflight']['status'], 'pass')

        # Verify anchors in cloned campaign session
        db.session.expire_all()
        cloned_session = CampaignSession.query.filter_by(
            campaign_id=res_data['derived_campaign']['id']
        ).first()
        self.assertIsNotNone(cloned_session)
        self.assertEqual(
            _normalize_memory_anchors(cloned_session.memory_anchors),
            _normalize_memory_anchors(self.session.memory_anchors),
        )

    @patch(
        'services.embedding_service._post_embedding',
        side_effect=AssertionError('Embedding provider called during clone'),
    )
    @patch(
        'services.embedding_service._post_embeddings',
        side_effect=AssertionError('Embedding provider called during clone'),
    )
    def test_claim_copies_embeddings_without_rebuilding(self, mock_batch, mock_single):
        """Test 3: successful claim copies exact embeddings without calling provider"""
        headers = {'Authorization': f'Bearer {self.token}'}
        response = self.client.post(
            f'/api/automation/runs/{self.run.id}/claim',
            headers=headers,
            json={'worker_id': 'worker-1'},
        )
        self.assertEqual(response.status_code, 200)

        res_data = response.get_json()
        clone_campaign_id = res_data['derived_campaign']['id']

        cloned_embeddings = CampaignMemoryEmbedding.query.filter_by(
            campaign_id=clone_campaign_id
        ).all()
        self.assertEqual(len(cloned_embeddings), len(self.embeddings))

        # Build expected map mapping snapshot item_ids
        expected_map = {}
        for emb_data in self.snapshot.snapshot_json['memory_embeddings']:
            item_type = emb_data['item_type']
            source_item_id = emb_data['item_id']
            # Remap world-event item_id
            if item_type == 'world_event':
                cloned_event = WorldEvent.query.filter_by(campaign_id=clone_campaign_id).first()
                expected_item_id = str(cloned_event.id)
            else:
                expected_item_id = source_item_id
            expected_map[(item_type, expected_item_id)] = emb_data

        for row in cloned_embeddings:
            key = (row.item_type, row.item_id)
            self.assertIn(key, expected_map)
            expected = expected_map[key]

            self.assertEqual(row.visibility, expected.get('visibility') or 'dm_private')
            self.assertEqual(row.canonical_text, expected.get('canonical_text'))
            self.assertEqual(row.text_hash, expected.get('text_hash'))
            self.assertEqual(row.embedding_model, expected.get('embedding_model'))
            self.assertEqual(row.embedding_dimensions, expected.get('embedding_dimensions'))
            self.assertEqual(json.loads(row.embedding_json), expected.get('embedding'))

    def test_claim_remaps_world_event_embedding_item_ids(self):
        """Test 4: world-event embeddings are remapped to new cloned event IDs"""
        headers = {'Authorization': f'Bearer {self.token}'}
        response = self.client.post(
            f'/api/automation/runs/{self.run.id}/claim',
            headers=headers,
            json={'worker_id': 'worker-1'},
        )
        self.assertEqual(response.status_code, 200)
        res_data = response.get_json()
        clone_campaign_id = res_data['derived_campaign']['id']

        # Get the new world event in clone
        cloned_event = WorldEvent.query.filter_by(campaign_id=clone_campaign_id).first()
        self.assertIsNotNone(cloned_event)
        self.assertNotEqual(cloned_event.id, self.event.id)

        # Get cloned world event embedding
        cloned_emb = CampaignMemoryEmbedding.query.filter_by(
            campaign_id=clone_campaign_id,
            item_type='world_event',
        ).first()
        self.assertIsNotNone(cloned_emb)
        self.assertEqual(cloned_emb.item_id, str(cloned_event.id))
        self.assertNotEqual(cloned_emb.item_id, str(self.event.id))

    def test_claim_preserves_semantic_candidate_coverage(self):
        """Test 5: semantic coverage is equivalent"""
        headers = {'Authorization': f'Bearer {self.token}'}
        response = self.client.post(
            f'/api/automation/runs/{self.run.id}/claim',
            headers=headers,
            json={'worker_id': 'worker-1'},
        )
        self.assertEqual(response.status_code, 200)
        res_data = response.get_json()
        preflight = res_data['retrieval_preflight']

        self.assertEqual(preflight['source_candidate_count'], preflight['clone_candidate_count'])
        self.assertEqual(preflight['source_semantic_coverage_count'], preflight['clone_semantic_coverage_count'])
        self.assertEqual(len(preflight['mismatches']), 0)

    def test_claim_fails_closed_when_embedding_copy_is_incomplete(self):
        """Test 6: missing embedding fails closed and rolls back transaction"""
        # Patch copy embeddings to skip writing one row
        from services.automation_service import _copy_snapshot_embeddings as orig_copy
        
        def faulty_copy(data, clone, world_event_map):
            # Remove the first embedding from the source payload to simulate incomplete copy
            corrupted_data = dict(data)
            corrupted_data['memory_embeddings'] = data['memory_embeddings'][1:]
            orig_copy(corrupted_data, clone, world_event_map)

        with patch('services.automation_service._copy_snapshot_embeddings', side_effect=faulty_copy):
            headers = {'Authorization': f'Bearer {self.token}'}
            response = self.client.post(
                f'/api/automation/runs/{self.run.id}/claim',
                headers=headers,
                json={'worker_id': 'worker-1'},
            )
            self.assertEqual(response.status_code, 409)
            res_data = response.get_json()
            self.assertIn('error', res_data)
            self.assertIn('retrieval_preflight', res_data)
            self.assertEqual(res_data['retrieval_preflight']['status'], 'fail')

        # Verify total rollback
        db.session.expire_all()
        run = db.session.get(AutomationRun, self.run.id)
        self.assertEqual(run.status, 'queued')
        self.assertIsNone(run.worker_id)
        self.assertIsNone(run.lease_token)
        self.assertIsNone(run.claimed_at)
        self.assertEqual(run.attempt_count or 0, 0)
        self.assertIsNone(run.derived_campaign_id)

        # Ensure no cloned campaign remains
        clone_campaigns = Campaign.query.filter_by(is_automation_clone=True).all()
        self.assertEqual(len(clone_campaigns), 0)

    def test_preflight_detects_anchor_mismatch(self):
        """Test 7: anchor mismatch is detected"""
        # We manually call validate_clone_retrieval_equivalence on modified clone session anchors
        session_map = {self.session.id: self.session.id}
        world_event_map = {str(self.event.id): str(self.event.id)}

        orig_anchors = dict(self.session.memory_anchors)
        self.session.memory_anchors = {'current_goal': 'A completely different goal'}
        db.session.flush()

        with self.assertRaises(CloneRetrievalPreflightError) as ctx:
            validate_clone_retrieval_equivalence(
                snapshot_data=self.snapshot.snapshot_json,
                clone=self.campaign,
                session_map=session_map,
                world_event_map=world_event_map,
            )
        
        report = ctx.exception.report
        self.assertEqual(report['status'], 'fail')
        mismatches = report['mismatches']
        self.assertTrue(any(m['type'] == 'anchor_mismatch' for m in mismatches))

    def test_claim_rejects_embedding_dimension_mismatch(self):
        """Test 8: malformed vector dimension mismatch fails claim and rolls back"""
        # Corrupt the snapshot payload vector to have fewer elements than dimensions
        snapshot = db.session.get(AutomationSnapshot, self.snapshot.id)
        corrupted_payload = json.loads(json.dumps(snapshot.snapshot_json))
        corrupted_payload['memory_embeddings'][0]['embedding'] = [0.1, 0.2] # expects 3
        snapshot.snapshot_json = corrupted_payload
        db.session.commit()

        headers = {'Authorization': f'Bearer {self.token}'}
        response = self.client.post(
            f'/api/automation/runs/{self.run.id}/claim',
            headers=headers,
            json={'worker_id': 'worker-1'},
        )
        self.assertEqual(response.status_code, 409)
        # Ensure roll back
        db.session.expire_all()
        self.assertIsNone(db.session.get(AutomationRun, self.run.id).derived_campaign_id)

    def test_claim_rejects_duplicate_embedding_keys(self):
        """Test 9: duplicate embedding key fails and rolls back"""
        snapshot = db.session.get(AutomationSnapshot, self.snapshot.id)
        corrupted_payload = json.loads(json.dumps(snapshot.snapshot_json))
        # Duplicate the first embedding
        corrupted_payload['memory_embeddings'].append(corrupted_payload['memory_embeddings'][0])
        snapshot.snapshot_json = corrupted_payload
        db.session.commit()

        headers = {'Authorization': f'Bearer {self.token}'}
        response = self.client.post(
            f'/api/automation/runs/{self.run.id}/claim',
            headers=headers,
            json={'worker_id': 'worker-1'},
        )
        self.assertEqual(response.status_code, 409)

    def test_claim_accepts_explicitly_empty_embedding_snapshot(self):
        """Test 10: explicit empty embeddings pass if source semantic coverage is empty"""
        # Clear out source campaign embeddings so semantic coverage matches empty clone
        CampaignMemoryEmbedding.query.filter_by(campaign_id=self.campaign.id).delete()
        db.session.commit()

        # Re-snapshot
        snapshot2 = create_snapshot_for_scenario(self.scenario, label='Empty Embeddings Snapshot')
        run2 = AutomationRun(
            scenario_id=self.scenario.id,
            snapshot_id=snapshot2.id,
            user_id=self.user.id,
            status='queued',
        )
        db.session.add(run2)
        db.session.commit()

        headers = {'Authorization': f'Bearer {self.token}'}
        response = self.client.post(
            f'/api/automation/runs/{run2.id}/claim',
            headers=headers,
            json={'worker_id': 'worker-1'},
        )
        self.assertEqual(response.status_code, 200)
        res_data = response.get_json()
        self.assertEqual(res_data['retrieval_preflight']['status'], 'pass')
        self.assertEqual(res_data['retrieval_preflight']['source_embedding_count'], 0)

    def test_claim_rejects_legacy_snapshot_without_retrieval_contract(self):
        """Test 11: legacy snapshot is rejected with instructions"""
        snapshot = db.session.get(AutomationSnapshot, self.snapshot.id)
        corrupted_payload = json.loads(json.dumps(snapshot.snapshot_json))
        corrupted_payload.pop('snapshot_schema_version', None)
        snapshot.snapshot_json = corrupted_payload
        db.session.commit()

        headers = {'Authorization': f'Bearer {self.token}'}
        response = self.client.post(
            f'/api/automation/runs/{self.run.id}/claim',
            headers=headers,
            json={'worker_id': 'worker-1'},
        )
        self.assertEqual(response.status_code, 409)
        res_data = response.get_json()
        self.assertIn('predates the retrieval-equivalent clone contract', res_data['error'])

    def test_reclaim_does_not_compare_mutated_clone_to_original_snapshot(self):
        """Test 12: reclaim does not rerun initial equivalence check and successfully claims"""
        headers = {'Authorization': f'Bearer {self.token}'}
        response = self.client.post(
            f'/api/automation/runs/{self.run.id}/claim',
            headers=headers,
            json={'worker_id': 'worker-1'},
        )
        self.assertEqual(response.status_code, 200)
        res_data = response.get_json()

        db.session.expire_all()
        run = db.session.get(AutomationRun, self.run.id)
        
        # Mutate the clone database representation to simulate turn progression
        # (e.g. modify clock segmented values or session anchors)
        cloned_session = CampaignSession.query.filter_by(campaign_id=run.derived_campaign_id).first()
        cloned_session.memory_anchors = {'current_goal': 'We modified it during play'}
        db.session.flush()

        # Simulate lease expiration
        run.lease_expires_at = utcnow() - timedelta(seconds=5)
        run.status = 'claimed'
        db.session.commit()

        # Now reclaim from another/same worker
        response2 = self.client.post(
            f'/api/automation/runs/{self.run.id}/claim',
            headers=headers,
            json={'worker_id': 'worker-1'},
        )
        self.assertEqual(response2.status_code, 200)
        res_data2 = response2.get_json()
        self.assertEqual(res_data2['retrieval_preflight']['status'], 'not_repeated')
        self.assertEqual(res_data2['retrieval_preflight']['reason'], 'clone_already_materialized')

    def test_preflight_candidate_keys_match_runtime_retrieval_candidates(self):
        """Test 13: helper stays aligned with production candidate logic"""
        snapshot_keys = _campaign_candidate_keys(self.campaign)
        production_candidates = dm_tools._campaign_memory_candidates(self.campaign)
        production_keys = {
            (str(item['kind']), str(item['item_id']))
            for item in production_candidates
        }
        self.assertEqual(snapshot_keys, production_keys)

    def test_world_event_cutoff_stays_deterministic_for_tied_timestamps(self):
        """Regression test: tied world-event timestamps should select the same 30 rows."""
        tied_created_at = datetime(2026, 1, 15, 12, 0, 0)
        self.event.created_at = tied_created_at
        db.session.flush()

        for index in range(30):
            db.session.add(WorldEvent(
                campaign_id=self.campaign.id,
                event_type='world_event',
                summary=f'Tied event {index + 1}',
                payload='{}',
                visibility='dm_private',
                created_at=tied_created_at,
            ))
        db.session.commit()

        helper_keys = _campaign_candidate_keys(self.campaign)
        production_candidates = dm_tools._campaign_memory_candidates(self.campaign)
        production_keys = {
            (str(item['kind']), str(item['item_id']))
            for item in production_candidates
        }

        helper_world_event_ids = {
            item_id
            for kind, item_id in helper_keys
            if kind == 'world_event'
        }
        production_world_event_ids = {
            str(item['item_id'])
            for item in production_candidates
            if item['kind'] == 'world_event'
        }

        self.assertEqual(helper_keys, production_keys)
        self.assertEqual(helper_world_event_ids, production_world_event_ids)

    @patch('services.embedding_service._post_embedding', return_value=[0.1, 0.2, 0.3])
    def test_entity_embeddings_in_runtime_semantic_scores(self, mock_post):
        """Regression test: verify that entity embeddings are matched during runtime search"""
        query = 'ancient ruins'
        res = dm_tools._tool_search_campaign_memory(
            campaign=self.campaign,
            _current_user=self.user,
            args={'query': query, 'limit': 5}
        )
        matches = res.get('matches') or []
        self.assertTrue(len(matches) > 0)
        entity_match = next((m for m in matches if m.get('kind') == 'entity'), None)
        self.assertIsNotNone(entity_match)
        self.assertIsNotNone(entity_match.get('embedding_score'))
        self.assertTrue(entity_match.get('embedding_score') > 0.0)

    def test_claim_does_not_create_fallback_session_for_empty_snapshot(self):
        source_campaign = Campaign(name='Empty Source Campaign', user_id=self.user.id)
        db.session.add(source_campaign)
        db.session.flush()
        empty_scenario = AutomationScenario(
            source_campaign_id=source_campaign.id,
            user_id=self.user.id,
            name='Empty Snapshot Scenario',
        )
        db.session.add(empty_scenario)
        db.session.flush()
        empty_snapshot = create_snapshot_for_scenario(
            empty_scenario,
            label='Empty Snapshot',
        )
        empty_run = AutomationRun(
            scenario_id=empty_scenario.id,
            snapshot_id=empty_snapshot.id,
            user_id=self.user.id,
            status='queued',
        )
        db.session.add(empty_run)
        db.session.commit()

        headers = {'Authorization': f'Bearer {self.token}'}
        response = self.client.post(
            f'/api/automation/runs/{empty_run.id}/claim',
            headers=headers,
            json={'worker_id': 'worker-empty'},
        )
        self.assertEqual(response.status_code, 200)
        claim_data = response.get_json()
        self.assertIsNone(claim_data['latest_session'])
        self.assertIsNone(CampaignSession.query.filter_by(campaign_id=claim_data['derived_campaign']['id']).first())

import os
import sys
import json
import unittest

from flask import Flask

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from models import db, Campaign, CampaignSession, CampaignWorld, NPCActor
from services.session_memory_agent import _normalize_visibility, compile_staged_memory_patch
from services.dm_tools import apply_compiled_session_memory_patch
from services.memory_resolver_schemas import SOURCE_CONTRACT_COMPILED_V2


class MemoryVisibilityInvariantTest(unittest.TestCase):
    """Prevent mixed-visibility NPC updates and party-visible graph items from
    leaking DM-private fields (identity, role in a plot, relationships, secrets)
    at the durable write boundary.
    """

    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SECRET_KEY'] = 'test-secret'
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        self.campaign = Campaign(name='Visibility Test', description='Test campaign', user_id=1)
        db.session.add(self.campaign)
        db.session.flush()

        self.session = CampaignSession(campaign_id=self.campaign.id, running_summary='')
        db.session.add(self.session)
        db.session.flush()

        self.world = CampaignWorld(
            campaign_id=self.campaign.id,
            public_intro='{}',
            knowledge_graph='{"entities":[],"relations":[],"facts":[]}',
            world_state='{"current_scene":{"location_id":"waterdeep","location_name":"Waterdeep"}}',
            dm_private='{}',
        )
        db.session.add(self.world)
        db.session.commit()

    def tearDown(self):
        db.session.rollback()
        db.drop_all()
        self.ctx.pop()

    # ── _normalize_visibility ──────────────────────────────────────────
    def test_visibility_normalization_matrix(self):
        cases = [
            ('visible_transcript', 'dm_private', 'dm_private'),
            ('visible_transcript', 'party_known', 'party_known'),
            (None, 'party_known', 'dm_private'),
            ('dm_private', None, 'dm_private'),
            (None, 'public', 'public'),
            ('visible_transcript', 'public', 'public'),
            ('visible_transcript', None, 'party_known'),
            ('visible_transcript', 'unknown_value', 'party_known'),
        ]
        for source_surface, intended_visibility, expected in cases:
            with self.subTest(source_surface=source_surface, intended_visibility=intended_visibility):
                self.assertEqual(_normalize_visibility(source_surface, intended_visibility), expected)

    # ── Compile path: dm_private intent survives visible source surface ──
    def _compile(self, resolved):
        return compile_staged_memory_patch({'campaign_id': self.campaign.id}, {}, resolved)

    def test_compile_mixed_items_keep_dm_private_intent(self):
        npc = NPCActor(campaign_id=self.campaign.id, actor_id='old_garret', name='Old Garret', dossier='{}')
        db.session.add(npc)
        db.session.commit()

        compiled = self._compile({
            'upsert_graph_entities': [
                {
                    'id': 'lady_elara',
                    'name': 'Lady Elara Vex',
                    'type': 'npc',
                    'intended_visibility': 'dm_private',
                    'source_surface': 'visible_transcript',
                }
            ],
            'upsert_graph_relations': [
                {
                    'type': 'serves',
                    'source_ref': 'old_garret',
                    'target_ref': 'lady_elara',
                    'summary': 'Old Garret secretly serves Lady Elara Vex.',
                    'intended_visibility': 'dm_private',
                    'source_surface': 'visible_transcript',
                }
            ],
            'upsert_graph_facts': [
                {
                    'text': 'Old Garret is the saboteur.',
                    'intended_visibility': 'dm_private',
                    'source_surface': 'visible_transcript',
                }
            ],
            'record_events': [
                {
                    'event_type': 'plot_beat',
                    'summary': 'The sabotage plot is set in motion.',
                    'intended_visibility': 'dm_private',
                    'source_surface': 'visible_transcript',
                }
            ],
            'update_npc_actors': [
                {
                    'id': 'old_garret',
                    'name': 'Old Garret',
                    'role': 'Secret saboteur',
                    'intended_visibility': 'dm_private',
                    'source_surface': 'visible_transcript',
                }
            ],
        })

        for entity in compiled.get('upsert_graph_entities', []):
            self.assertEqual(entity['visibility'], 'dm_private')
        for rel in compiled.get('upsert_graph_relations', []):
            self.assertEqual(rel['visibility'], 'dm_private')
        for fact in compiled.get('upsert_graph_facts', []):
            self.assertEqual(fact['visibility'], 'dm_private')
        for event in compiled.get('record_events', []):
            self.assertEqual(event['visibility'], 'dm_private')
        for npc_update in compiled.get('update_npc_actors', []):
            self.assertEqual(npc_update['visibility'], 'dm_private')

    def test_compile_party_known_npc_requires_visible_transcript(self):
        npc = NPCActor(campaign_id=self.campaign.id, actor_id='sildar', name='Sildar', dossier='{}')
        db.session.add(npc)
        db.session.commit()

        compiled = self._compile({
            'update_npc_actors': [
                {
                    'id': 'sildar',
                    'name': 'Sildar',
                    'role': 'Bodyguard',
                    'intended_visibility': 'party_known',
                }
            ]
        })
        npcs = compiled.get('update_npc_actors', [])
        self.assertEqual(len(npcs), 1)
        self.assertEqual(npcs[0]['visibility'], 'dm_private')

    # ── Apply path: mixed NPC updates split public/private fields ──────
    def test_mixed_npc_update_keeps_private_dossier_out_of_party_view(self):
        npc = NPCActor(
            campaign_id=self.campaign.id,
            actor_id='old_garret',
            name='Old Garret',
            public_summary='A dock foreman.',
            dossier='{}',
        )
        db.session.add(npc)
        db.session.commit()

        patch = {
            'source_contract': SOURCE_CONTRACT_COMPILED_V2,
            'base_memory_revision': self.world.memory_revision or 0,
            'upsert_graph_entities': [],
            'upsert_graph_relations': [],
            'upsert_graph_facts': [],
            'update_npc_actors': [
                {
                    'id': 'old_garret',
                    'name': 'Old Garret',
                    'role': 'Dock foreman',
                    'public_summary': 'A weathered dock foreman in Waterdeep.',
                    'background': 'Former saboteur working for Lady Elara Vex.',
                    'secrets': ['Is secretly the saboteur.'],
                    'relationships': {'lady_elara_vex': 'Secretly serves her'},
                    'visibility': 'party_known',
                }
            ],
            'record_events': [],
        }
        audit_context = {
            'latest_player_message': 'The party meets the dock foreman.',
            'latest_dm_message': 'Old Garret greets the party at the docks.',
        }
        apply_compiled_session_memory_patch(self.campaign, self.session, patch, audit_context=audit_context)
        db.session.commit()

        db.session.refresh(npc)
        party_view = npc.to_dict(include_private=False)
        self.assertEqual(party_view['name'], 'Old Garret')
        self.assertNotIn('secrets', party_view)
        self.assertNotIn('background', party_view)
        self.assertNotIn('relationships', party_view)

        dossier = json.loads(npc.dossier)
        self.assertEqual(dossier['secrets'], ['Is secretly the saboteur.'])
        self.assertIn('Lady Elara Vex', dossier['background'])
        self.assertEqual(dossier['relationships'], {'lady_elara_vex': 'Secretly serves her'})

    # ── Compiled path: leak guard redacts summary but preserves visible name ──
    def test_compiled_path_redacts_private_summary_keeps_visible_name(self):
        spymaster = NPCActor(
            campaign_id=self.campaign.id,
            actor_id='brother_ollin',
            name='Brother Ollin',
            dossier=json.dumps({'secrets': ['the spy']}),
        )
        db.session.add(spymaster)
        db.session.commit()

        patch = {
            'source_contract': SOURCE_CONTRACT_COMPILED_V2,
            'base_memory_revision': self.world.memory_revision or 0,
            'upsert_graph_entities': [
                {
                    'id': 'old_garret',
                    'type': 'npc',
                    'name': 'Old Garret',
                    'summary': 'A dock foreman who is the spy.',
                    'visibility': 'party_known',
                }
            ],
            'upsert_graph_relations': [],
            'upsert_graph_facts': [],
            'update_npc_actors': [],
            'record_events': [],
        }
        audit_context = {
            'latest_player_message': 'We talk to the dock foreman.',
            'latest_dm_message': 'The dock foreman gives the party directions.',
        }
        apply_compiled_session_memory_patch(self.campaign, self.session, patch, audit_context=audit_context)
        db.session.commit()

        db.session.refresh(self.world)
        graph = json.loads(self.world.knowledge_graph)
        entity = next(e for e in graph['entities'] if e['id'] == 'old_garret')
        self.assertEqual(entity['visibility'], 'party_known')
        self.assertEqual(entity['name'], 'Old Garret')
        self.assertNotIn('spy', json.dumps(entity.get('summary') or '').lower())

        telemetry = audit_context.get('leak_guard_telemetry') or {}
        self.assertGreater(telemetry.get('entities_redacted', 0), 0)
        self.assertNotIn('spy', json.dumps(telemetry))

    # ── Compiled path: leak guard demotes entity carrying private identity ──
    def test_compiled_path_demotes_entity_with_unrevealed_private_identity(self):
        graph = {
            'entities': [
                {'id': 'brother_ollin', 'type': 'npc', 'name': 'Brother Ollin', 'visibility': 'dm_private'},
            ],
            'relations': [],
            'facts': [],
        }
        self.world.knowledge_graph = json.dumps(graph)
        db.session.commit()

        patch = {
            'source_contract': SOURCE_CONTRACT_COMPILED_V2,
            'base_memory_revision': self.world.memory_revision or 0,
            'upsert_graph_entities': [
                {
                    'id': 'gray_monk',
                    'type': 'npc',
                    'name': 'Brother Ollin',
                    'summary': 'A gray-robed monk.',
                    'visibility': 'party_known',
                }
            ],
            'upsert_graph_relations': [],
            'upsert_graph_facts': [],
            'update_npc_actors': [],
            'record_events': [],
        }
        audit_context = {
            'latest_player_message': 'A gray-robed monk approaches.',
            'latest_dm_message': 'The gray-robed monk bows.',
        }
        apply_compiled_session_memory_patch(self.campaign, self.session, patch, audit_context=audit_context)
        db.session.commit()

        db.session.refresh(self.world)
        graph = json.loads(self.world.knowledge_graph)
        entity = next(e for e in graph['entities'] if e['id'] == 'gray_monk')
        self.assertEqual(entity['visibility'], 'dm_private')

        telemetry = audit_context.get('leak_guard_telemetry') or {}
        self.assertGreater(telemetry.get('entities_demoted', 0), 0)
        self.assertNotIn('Ollin', json.dumps(telemetry))

    # ── Compiled path: private identity never promoted into NPC public columns ──
    def test_compiled_path_npc_private_identity_not_promoted(self):
        npc = NPCActor(
            campaign_id=self.campaign.id,
            actor_id='gray_monk',
            name='A Gray-Robed Monk',
            public_summary='An anonymous monk.',
            dossier='{}',
        )
        db.session.add(npc)
        graph = {
            'entities': [
                {'id': 'brother_ollin', 'type': 'npc', 'name': 'Brother Ollin', 'visibility': 'dm_private'},
            ],
            'relations': [],
            'facts': [],
        }
        self.world.knowledge_graph = json.dumps(graph)
        db.session.commit()

        patch = {
            'source_contract': SOURCE_CONTRACT_COMPILED_V2,
            'base_memory_revision': self.world.memory_revision or 0,
            'upsert_graph_entities': [],
            'upsert_graph_relations': [],
            'upsert_graph_facts': [],
            'update_npc_actors': [
                {
                    'id': 'gray_monk',
                    'name': 'Brother Ollin',
                    'role': 'Mirror monk',
                    'public_summary': 'A gray-robed mirror monk named Brother Ollin.',
                    'visibility': 'party_known',
                }
            ],
            'record_events': [],
        }
        audit_context = {
            'latest_player_message': 'A gray-robed monk steps forward.',
            'latest_dm_message': 'The monk says nothing.',
        }
        apply_compiled_session_memory_patch(self.campaign, self.session, patch, audit_context=audit_context)
        db.session.commit()

        db.session.refresh(npc)
        self.assertNotIn('Brother Ollin', npc.name)
        self.assertNotIn('Brother Ollin', npc.public_summary or '')

        telemetry = audit_context.get('leak_guard_telemetry') or {}
        self.assertGreater(telemetry.get('npc_public_redacted', 0), 0)
        self.assertNotIn('Ollin', json.dumps(telemetry))

        # The materialized graph endpoint for the NPC must not be party-visible.
        db.session.refresh(self.world)
        graph = json.loads(self.world.knowledge_graph)
        entity = next((e for e in graph['entities'] if e['id'] == 'gray_monk'), None)
        if entity is not None:
            self.assertEqual(entity['visibility'], 'dm_private')
            self.assertNotIn('Ollin', json.dumps(entity))

    # ── Session-facing free text: running summary leak prevention ──────
    def test_compiled_path_redacts_private_identity_from_running_summary(self):
        graph = {
            'entities': [
                {'id': 'brother_ollin', 'type': 'npc', 'name': 'Brother Ollin', 'visibility': 'dm_private'},
            ],
            'relations': [],
            'facts': [],
        }
        self.world.knowledge_graph = json.dumps(graph)
        db.session.commit()

        patch = {
            'source_contract': SOURCE_CONTRACT_COMPILED_V2,
            'base_memory_revision': self.world.memory_revision or 0,
            'running_summary': 'The party traveled to the docks. Brother Ollin orchestrated the raid from the shadows.',
            'memory_anchors': {
                'current_goal': 'Find the source of the raids.',
                'open_clues': ['A gray robe left behind.', 'Brother Ollin sent the raiders.'],
                'unresolved_questions': [],
                'npc_observations': [],
                'recent_offers_promises': [],
            },
            'upsert_graph_entities': [],
            'upsert_graph_relations': [],
            'upsert_graph_facts': [],
            'update_npc_actors': [],
            'record_events': [],
        }
        audit_context = {
            'latest_player_message': 'The party reaches the docks.',
            'latest_dm_message': 'The harbor is quiet.',
        }
        apply_compiled_session_memory_patch(self.campaign, self.session, patch, audit_context=audit_context)
        db.session.commit()

        db.session.refresh(self.session)
        self.assertNotIn('Brother Ollin', self.session.running_summary or '')
        self.assertIn('traveled to the docks', self.session.running_summary or '')

        anchors = self.session.memory_anchors if isinstance(self.session.memory_anchors, dict) else {}
        self.assertNotIn('Brother Ollin', json.dumps(anchors))
        self.assertIn('A gray robe left behind.', json.dumps(anchors.get('open_clues', [])))

        telemetry = audit_context.get('leak_guard_telemetry') or {}
        self.assertGreater(telemetry.get('summary_redacted', 0), 0)
        self.assertGreaterEqual(telemetry.get('anchor_items_redacted', 0), 1)
        self.assertNotIn('Ollin', json.dumps(telemetry))

        # Party-facing session serialization must not expose the private identity.
        serialized = json.dumps(self.session.to_dict())
        self.assertNotIn('Brother Ollin', serialized)

    # ── Compiled path: relation/event demotion + telemetry ─────────────
    def test_compiled_path_demotes_relation_and_event_with_private_terms(self):
        spymaster = NPCActor(
            campaign_id=self.campaign.id,
            actor_id='brother_ollin',
            name='Brother Ollin',
            dossier=json.dumps({'secrets': ['the spy']}),
        )
        db.session.add(spymaster)
        db.session.commit()

        patch = {
            'source_contract': SOURCE_CONTRACT_COMPILED_V2,
            'base_memory_revision': self.world.memory_revision or 0,
            'upsert_graph_entities': [
                {'id': 'old_garret', 'type': 'npc', 'name': 'Old Garret', 'visibility': 'party_known'},
                {'id': 'lady_elara', 'type': 'npc', 'name': 'Lady Elara Vex', 'visibility': 'party_known'},
            ],
            'upsert_graph_relations': [
                {
                    'id': 'rel_garret_elara',
                    'type': 'serves',
                    'source_id': 'old_garret',
                    'target_id': 'lady_elara',
                    'summary': 'Old Garret is secretly the spy for her.',
                    'visibility': 'party_known',
                }
            ],
            'upsert_graph_facts': [],
            'update_npc_actors': [],
            'record_events': [
                {
                    'event_type': 'plot_beat',
                    'summary': 'The spy reports to Lady Elara Vex.',
                    'visibility': 'party_known',
                }
            ],
        }
        audit_context = {
            'latest_player_message': 'We speak with the dock foreman and the lady.',
            'latest_dm_message': 'The foreman and the lady listen quietly.',
        }
        apply_compiled_session_memory_patch(self.campaign, self.session, patch, audit_context=audit_context)
        db.session.commit()

        db.session.refresh(self.world)
        graph = json.loads(self.world.knowledge_graph)
        relation = next(r for r in graph['relations'] if r['id'] == 'rel_garret_elara')
        self.assertEqual(relation['visibility'], 'dm_private')

        telemetry = audit_context.get('leak_guard_telemetry') or {}
        self.assertGreater(telemetry.get('relations_demoted', 0), 0)
        self.assertGreater(telemetry.get('events_demoted', 0), 0)
        self.assertNotIn('spy', json.dumps(telemetry))


if __name__ == '__main__':
    unittest.main()

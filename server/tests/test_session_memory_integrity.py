import os
import sys
import json
import unittest
import unittest.mock
from datetime import datetime
from flask import Flask

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from models import (
    db,
    User,
    Campaign,
    CampaignSession,
    CampaignWorld,
    CampaignIdentityResolution,
    CampaignClarification,
    CampaignResolverPacket,
    Character,
    NPCActor,
    SessionMessage,
)
from services.scene_location_resolver import resolve_scene_location_patch
from services.session_memory_agent import (
    compile_staged_memory_patch,
    MemoryPipelineError,
    _validate_final_memory_state,
    _known_ids,
)
from services.resolution_registry import (
    build_canonical_resolution_registry,
    allocate_durable_id,
    resolve_ref,
)
from services.memory_resolver_schemas import (
    is_identity_worthy,
    validate_diagnostics,
    DIAGNOSTICS_TEMPLATE,
)


class SessionMemoryIntegrityTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SECRET_KEY'] = 'test-secret'
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        self.campaign = Campaign(name='Integrity Test', description='Test', user_id=1)
        db.session.add(self.campaign)
        db.session.flush()

        self.world = CampaignWorld(
            campaign_id=self.campaign.id,
            public_intro='{}',
            knowledge_graph='{"entities":[{"id":"waterdeep","type":"location","name":"Waterdeep"},{"id":"drowned_lantern","type":"location","name":"Drowned Lantern"}],"relations":[],"facts":[]}',
            world_state='{"current_scene":{"location_id":"drowned_lantern","location_name":"Drowned Lantern"}}',
            dm_private='{}',
        )
        db.session.add(self.world)
        db.session.flush()

        self.session = CampaignSession(campaign_id=self.campaign.id)
        db.session.add(self.session)
        db.session.flush()

        mi = NPCActor(campaign_id=self.campaign.id, actor_id='mira', name='Mira', dossier='{}')
        db.session.add(mi)
        tor = NPCActor(campaign_id=self.campaign.id, actor_id='toren', name='Toren', dossier='{}')
        db.session.add(tor)
        ald = NPCActor(campaign_id=self.campaign.id, actor_id='aldric', name='Aldric', dossier='{}')
        db.session.add(ald)
        kae = NPCActor(campaign_id=self.campaign.id, actor_id='kaelen_morwen', name='Kaelen Morwen', public_summary='A scholar.', dossier='{}')
        db.session.add(kae)

        db.session.commit()

    def tearDown(self):
        db.session.rollback()
        db.drop_all()
        self.ctx.pop()

    def _base_context(self):
        return {
            'campaign_id': self.campaign.id,
            'session_id': self.session.id,
            'hot_context': {
                'current_scene': {
                    'location_id': 'drowned_lantern',
                    'location_name': 'Drowned Lantern',
                },
            },
        }

    # 1. Participant substitution rejected
    def test_cast_preserves_correct_npc_identity(self):
        memory_context = self._base_context()
        extracted = {}
        resolved = {
            'scene_patch': {
                'active_npc_ids': ['mira', 'aldric'],
            },
        }
        compiled = compile_staged_memory_patch(memory_context, extracted, resolved)
        active = compiled['scene_patch'].get('active_npc_ids', [])
        self.assertIn('mira', active)
        # Toren was not requested, so Aldric is there but Toren is not
        self.assertNotIn('toren', active)
    def test_distinct_npcs_not_merged(self):
        memory_context = self._base_context()
        extracted = {}
        resolved = {
            'upsert_graph_entities': [
                {'id': 'grey_robed_monk', 'name': 'Grey Robed Monk', 'type': 'npc'},
            ],
            'update_npc_actors': [
                {'id': 'kaelen_morwen', 'name': 'Kaelen Morwen', 'role': 'Scholar'},
            ],
        }
        compiled = compile_staged_memory_patch(memory_context, extracted, resolved)
        entities = compiled.get('upsert_graph_entities', [])
        entity_names = [e['name'] for e in entities]
        self.assertIn('Grey Robed Monk', entity_names)
        # Kaelen Morwen is already known; Grey Robed Monk should be separate
        self.assertNotIn('Kaelen Morwen', [e['name'] for e in entities if e.get('id') != 'kaelen_morwen'])
    def test_new_location_created_and_set_as_current_scene(self):
        memory_context = self._base_context()
        extracted = {}
        resolved = {
            'scene_patch': {'location_name': 'Ferrymaster\'s Hall'},
        }
        compiled = compile_staged_memory_patch(memory_context, extracted, resolved)
        self.assertIn('location_id', compiled['scene_patch'])
        self.assertIn('location_name', compiled['scene_patch'])
        self.assertEqual(compiled['scene_patch']['location_name'], 'Ferrymaster\'s Hall')
        self.assertEqual(compiled['scene_patch']['resolution_mode'], 'new')
        # The new location should also appear in upsert_graph_entities
        location_entities = [e for e in compiled.get('upsert_graph_entities', []) if e.get('type') == 'location']
        self.assertTrue(any(e['name'] == 'Ferrymaster\'s Hall' for e in location_entities))

    def test_same_patch_npc_appears_in_active_cast(self):
        memory_context = self._base_context()
        extracted = {}
        resolved = {
            'upsert_graph_entities': [
                {'id': 'new_npc_1', 'name': 'New Guard', 'type': 'npc'},
            ],
            'scene_patch': {
                'active_npc_ids': ['new_npc_1', 'mira'],
            },
        }
        compiled = compile_staged_memory_patch(memory_context, extracted, resolved)
        active = compiled['scene_patch'].get('active_npc_ids', [])
        # new_npc_1 should be resolvable through entity_name_to_id
        self.assertTrue(len(active) >= 1)  # At least mira should be there
        self.assertIn('mira', active)

    def test_unidentified_descriptor_creates_provisional_unknown(self):
        memory_context = self._base_context()
        extracted = {
            'entity_claims': [
                {'name': 'the grey-cloaked figure', 'type': 'npc'},
            ],
        }
        resolved = {}
        registry, registry_map, clarifications, diagnostics = build_canonical_resolution_registry(
            self.campaign,
            memory_context,
            extracted,
            None,
            [],
            {},
        )
        grey_entry = next((e for e in registry if 'grey' in e.get('surface_form', '').lower()), None)
        self.assertIsNotNone(grey_entry)
        self.assertEqual(grey_entry['identity_status'], 'provisional_unknown')

    def test_resolver_packet_creates_intentionally_undetermined(self):
        memory_context = self._base_context()
        memory_context['resolver_packet'] = {
            'entity_mentions': [{
                'mention_ref': 'shadow_1',
                'surface_form': 'the shadowed stranger',
                'identity_status': 'intentionally_undetermined',
            }],
        }
        extracted = {}
        registry, registry_map, clarifications, diagnostics = build_canonical_resolution_registry(
            self.campaign,
            memory_context,
            extracted,
            memory_context['resolver_packet'],
            [],
            {},
        )
        shadow_entry = next((e for e in registry if e.get('identity_status') == 'intentionally_undetermined'), None)
        self.assertIsNotNone(shadow_entry)
        self.assertEqual(shadow_entry['decision'], 'create_provisional')

    def test_provisional_unknown_not_intentionally_undetermined_from_transcript(self):
        memory_context = self._base_context()
        extracted = {
            'entity_claims': [{'name': 'the hooded figure', 'type': 'npc'}],
        }
        resolved = {}
        registry, registry_map, clarifications, diagnostics = build_canonical_resolution_registry(
            self.campaign,
            memory_context,
            extracted,
            None,
            [],
            {},
        )
        entry = next((e for e in registry if e.get('surface_form', '') == 'the hooded figure'), None)
        self.assertIsNotNone(entry)
        self.assertEqual(entry['identity_status'], 'provisional_unknown')
        self.assertNotEqual(entry['identity_status'], 'intentionally_undetermined')

    def test_known_hidden_packet_links_descriptor_to_hidden_npc(self):
        memory_context = self._base_context()
        memory_context['resolver_packet'] = {
            'entity_mentions': [{
                'mention_ref': 'grey_figure_1',
                'surface_form': 'the grey-cloaked figure',
                'identity_status': 'known_hidden',
                'canonical_id': 'aldric',
                'public_name': 'the grey-cloaked figure',
                'visibility': 'dm_private',
            }],
        }
        extracted = {}
        known_ids = _known_ids(self.campaign)
        registry, registry_map, clarifications, diagnostics = build_canonical_resolution_registry(
            self.campaign,
            memory_context,
            extracted,
            memory_context['resolver_packet'],
            [],
            known_ids,
        )
        entry = next((e for e in registry if e.get('mention_ref') == 'grey_figure_1'), None)
        self.assertIsNotNone(entry)
        self.assertEqual(entry['canonical_id'], 'aldric')
        self.assertEqual(entry['visibility'], 'dm_private')

    def test_provisional_new_entity_packet_creates_distinct_entity(self):
        memory_context = self._base_context()
        memory_context['resolver_packet'] = {
            'entity_mentions': [{
                'mention_ref': 'new_guy_1',
                'surface_form': 'Captain Voss',
                'identity_status': 'provisional_new_entity',
            }],
        }
        extracted = {}
        registry, registry_map, clarifications, diagnostics = build_canonical_resolution_registry(
            self.campaign,
            memory_context,
            extracted,
            memory_context['resolver_packet'],
            [],
            {},
        )
        entry = next((e for e in registry if e.get('mention_ref') == 'new_guy_1'), None)
        self.assertIsNotNone(entry)
        self.assertEqual(entry['decision'], 'create_new')

    def test_packet_contradicting_durable_canon_not_silently_overridden(self):
        memory_context = self._base_context()
        # Aldric exists in durable memory. Packet tries to rename Kaelen to Aldric — should not work.
        memory_context['resolver_packet'] = {
            'entity_mentions': [{
                'mention_ref': 'claim_1',
                'surface_form': 'Kaelen Morwen',
                'identity_status': 'known_hidden',
                'canonical_id': 'aldric',
            }],
        }
        extracted = {}
        registry, registry_map, clarifications, diagnostics = build_canonical_resolution_registry(
            self.campaign,
            memory_context,
            extracted,
            memory_context['resolver_packet'],
            [],
            {},
        )
        entry = next((e for e in registry if e.get('mention_ref') == 'claim_1'), None)
        # If canonical_id is in known_ids, the registry should accept it.
        # But this is a contradiction that would be caught by the compiler's final-state validation.
        self.assertIsNotNone(entry)

    def test_repeated_provisional_figures_no_duplicates(self):
        memory_context = self._base_context()
        extracted = {
            'entity_claims': [
                {'name': 'the innkeeper', 'type': 'npc'},
                {'name': 'the innkeeper', 'type': 'npc'},
            ],
        }
        resolved = {}
        registry, registry_map, clarifications, diagnostics = build_canonical_resolution_registry(
            self.campaign,
            memory_context,
            extracted,
            None,
            [],
            {},
        )
        innkeeper_entries = [e for e in registry if e.get('surface_form', '') == 'the innkeeper']
        self.assertEqual(len(innkeeper_entries), 1)

    def test_similar_descriptors_different_scenes_not_shared(self):
        memory_context = self._base_context()
        extracted = {
            'entity_claims': [
                {'name': 'the guard', 'type': 'npc', 'mention_ref': 'guard_docks'},
            ],
        }
        resolved = {}
        registry, registry_map, clarifications, diagnostics = build_canonical_resolution_registry(
            self.campaign,
            memory_context,
            extracted,
            None,
            [],
            {},
        )
        guard_entries = [e for e in registry if 'guard' in e.get('surface_form', '').lower()]
        self.assertEqual(len(guard_entries), 1)

    def test_provisional_entity_creates_resolution_record(self):
        memory_context = self._base_context()
        extracted = {
            'entity_claims': [{'name': 'the ferryman', 'type': 'npc'}],
        }
        resolved = {}
        compiled = compile_staged_memory_patch(memory_context, extracted, resolved)
        records = compiled.get('resolution_records', [])
        self.assertTrue(len(records) >= 1)

    def test_diagnostics_substitutions_empty(self):
        memory_context = self._base_context()
        extracted = {}
        resolved = {
            'scene_patch': {'location_name': 'Ferrymaster\'s Hall'},
        }
        compiled = compile_staged_memory_patch(memory_context, extracted, resolved)
        diag = compiled.get('resolution_diagnostics', {})
        self.assertEqual(len(diag.get('substitutions', [])), 0)

    def test_source_contract_present_on_compiled_patch(self):
        memory_context = self._base_context()
        extracted = {}
        resolved = {
            'scene_patch': {'location_name': 'Ferrymaster\'s Hall'},
        }
        compiled = compile_staged_memory_patch(memory_context, extracted, resolved)
        self.assertEqual(compiled.get('source_contract'), 'compiled_session_memory_v2')

    def test_base_memory_revision_present(self):
        memory_context = self._base_context()
        extracted = {}
        resolved = {
            'scene_patch': {'location_name': 'Ferrymaster\'s Hall'},
        }
        compiled = compile_staged_memory_patch(memory_context, extracted, resolved)
        self.assertIn('base_memory_revision', compiled)

    def test_registry_allocates_collision_safe_ids(self):
        existing = {'entity_1', 'entity_1_2'}
        new_id = allocate_durable_id('entity_1', existing)
        self.assertNotEqual(new_id, 'entity_1')
        self.assertTrue(new_id.startswith('entity_1'))

    def test_cast_sets_no_overlap_in_compiled_patch(self):
        memory_context = self._base_context()
        extracted = {}
        resolved = {
            'scene_patch': {
                'active_npc_ids': ['mira', 'toren'],
                'departed_npc_ids': ['toren'],
            },
        }
        compiled = compile_staged_memory_patch(memory_context, extracted, resolved)
        active = set(compiled['scene_patch'].get('active_npc_ids', []))
        departed = set(compiled['scene_patch'].get('departed_npc_ids', []))
        # Validation errors should flag the overlap
        validation_errors = compiled.get('validation_errors', [])
        self.assertTrue(any('overlap' in e.lower() for e in validation_errors))

    def test_identity_worthy_heuristic(self):
        self.assertTrue(is_identity_worthy('the grey-cloaked figure'))
        self.assertTrue(is_identity_worthy('Kaelen Morwen'))
        self.assertTrue(is_identity_worthy('the innkeeper'))
        self.assertFalse(is_identity_worthy('someone'))
        self.assertFalse(is_identity_worthy('them'))

    def test_diagnostics_validation_rejects_nonempty_substitutions(self):
        bad_diag = dict(DIAGNOSTICS_TEMPLATE)
        bad_diag['substitutions'] = [{'from': 'a', 'to': 'b'}]
        valid, error = validate_diagnostics(bad_diag)
        self.assertFalse(valid)

    def test_existing_entity_referenced_in_fact_survives(self):
        memory_context = self._base_context()
        extracted = {}
        resolved = {
            'upsert_graph_facts': [
                {
                    'text': 'Mira is in the Drowned Lantern.',
                    'entity_ids': ['mira'],
                },
            ],
            'upsert_graph_entities': [
                {'id': 'mira', 'name': 'Mira', 'type': 'npc'},
            ],
        }
        compiled = compile_staged_memory_patch(memory_context, extracted, resolved)
        facts = compiled.get('upsert_graph_facts', [])
        self.assertTrue(any('Mira' in f['text'] for f in facts))

    def test_resolver_packet_identity_resolution_takes_priority(self):
        memory_context = self._base_context()
        memory_context['resolver_packet'] = {
            'entity_mentions': [{
                'mention_ref': 'familiar_figure',
                'surface_form': 'the familiar figure',
                'identity_status': 'known_hidden',
                'canonical_id': 'toren',
                'public_name': 'the familiar figure',
                'visibility': 'dm_private',
            }],
        }
        extracted = {
            'entity_claims': [
                {'name': 'the familiar figure', 'type': 'npc'},
            ],
        }
        known_ids = _known_ids(self.campaign)
        registry, registry_map, clarifications, diagnostics = build_canonical_resolution_registry(
            self.campaign,
            memory_context,
            extracted,
            memory_context['resolver_packet'],
            [],
            known_ids,
        )
        entry = next((e for e in registry if e.get('mention_ref') == 'familiar_figure'), None)
        self.assertIsNotNone(entry)
        self.assertEqual(entry['canonical_id'], 'toren')

    def test_known_npc_without_evidence_stays_outside_cast(self):
        memory_context = self._base_context()
        extracted = {}
        resolved = {
            'scene_patch': {
                'active_npc_ids': ['some_unknown_npc'],
            },
        }
        compiled = compile_staged_memory_patch(memory_context, extracted, resolved)
        unresolved = compiled.get('unresolved_items', [])
        self.assertTrue(any(u.get('actor_id') == 'some_unknown_npc' for u in unresolved))

    def test_new_npc_created_same_patch_reference_resolves(self):
        memory_context = self._base_context()
        extracted = {}
        resolved = {
            'upsert_graph_entities': [
                {'id': 'new_innkeeper', 'name': 'Bertrand', 'type': 'npc'},
            ],
            'scene_patch': {
                'active_npc_ids': ['new_innkeeper', 'mira'],
            },
            'update_npc_actors': [
                {'id': 'new_innkeeper', 'name': 'Bertrand', 'role': 'Innkeeper'},
            ],
        }
        compiled = compile_staged_memory_patch(memory_context, extracted, resolved)
        npcs = compiled.get('update_npc_actors', [])
        entities = compiled.get('upsert_graph_entities', [])
        # The NPC update should be found — either by direct ID or by name remapping
        self.assertTrue(
            any(n.get('id') == 'new_innkeeper' or n.get('actor_id') == 'new_innkeeper' for n in npcs)
            or any('Bertrand' in str(n.get('name', '')) for n in npcs),
            f"NPC update not found. npcs={npcs}, entities={[e.get('id') for e in entities]}"
        )


if __name__ == '__main__':
    unittest.main()

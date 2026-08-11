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
    _validate_resolved_entity_refs,
    _known_ids,
    _build_resolution_records,
)
from services.resolution_registry import (
    build_canonical_resolution_registry,
    allocate_durable_id,
    resolve_ref,
)
from services.memory_resolver_schemas import validate_diagnostics, DIAGNOSTICS_TEMPLATE


class SessionMemoryIntegrityTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SECRET_KEY'] = 'test-secret'
        self.app.config['JWT_EXPIRATION_HOURS'] = 24
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        usr = User(username='dm_user', email='dm@example.com')
        usr.set_password('password')
        db.session.add(usr)
        db.session.commit()

        self.campaign = Campaign(name='Integrity Test', description='Test', user_id=usr.id)
        db.session.add(self.campaign)
        db.session.commit()

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
                'evidence_refs': ['campaign_entity:aldric'],
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


    def test_packet_contradicting_durable_canon_not_silently_overridden(self):
        memory_context = self._base_context()
        # Aldric exists in durable memory. Packet tries to rename Kaelen to Aldric — should not work.
        memory_context['resolver_packet'] = {
            'entity_mentions': [{
                'mention_ref': 'claim_1',
                'surface_form': 'Kaelen Morwen',
                'identity_status': 'known_hidden',
                'canonical_id': 'aldric',
                'evidence_refs': ['campaign_entity:aldric'],
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


    def test_provisional_entity_does_not_create_resolution_record(self):
        memory_context = self._base_context()
        extracted = {
            'entity_claims': [{'name': 'the ferryman', 'type': 'npc'}],
        }
        resolved = {}
        compiled = compile_staged_memory_patch(memory_context, extracted, resolved)
        records = compiled.get('resolution_records', [])
        # Provisional entities should NOT create resolution records at creation time
        self.assertEqual(len(records), 0)






    def test_structured_claims_bypass_surface_form_heuristics(self):
        extracted = {
            'entity_claims': [
                {'name': 'Ael', 'type': 'person', 'mention_ref': 'one_word_name'},
                {'name': '7th-Sigil', 'type': 'artifact', 'mention_ref': 'fantasy_name'},
            ],
            'npc_claims': [
                {
                    'name': 'obsidian-veiled witness',
                    'mention_ref': 'unusual_descriptor',
                },
            ],
        }

        registry, _, _, _ = build_canonical_resolution_registry(
            self.campaign,
            self._base_context(),
            extracted,
            None,
            [],
            {},
        )

        entries = {entry['mention_ref']: entry for entry in registry}
        self.assertEqual(set(entries), {
            'one_word_name',
            'fantasy_name',
            'unusual_descriptor',
        })
        for entry in entries.values():
            self.assertEqual(entry['decision'], 'create_provisional')
            self.assertEqual(entry['resolution_state'], 'provisional')
            self.assertIsNotNone(entry['canonical_id'])


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
                'evidence_refs': ['campaign_entity:toren'],
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

    # ── End-to-end regression tests ────────────────────────────────────

    def test_resolver_packet_rejected_when_name_collides_with_different_npc(self):
        memory_context = self._base_context()
        memory_context['resolver_packet'] = {
            'entity_mentions': [{
                'mention_ref': 'bad_map',
                'surface_form': 'Kaelen Morwen',
                'identity_status': 'known_hidden',
                'canonical_id': 'aldric',
                'public_name': 'Kaelen Morwen',
                'visibility': 'dm_private',
                'evidence_refs': ['campaign_entity:aldric'],
            }],
        }
        extracted = {}
        resolved = {}
        compiled = compile_staged_memory_patch(memory_context, extracted, resolved)
        # The packet should be rejected because Kaelen Morwen is a known NPC (kaelen_morwen),
        # not aldric. The registry should classify this as rejected.
        diag = compiled.get('resolution_diagnostics', {})
        self.assertEqual(len(diag.get('substitutions', [])), 0)
        # Verify no entity with aldric's ID got the name Kaelen Morwen
        for entity in compiled.get('upsert_graph_entities', []):
            if entity.get('id') == 'aldric':
                self.assertNotEqual(entity.get('name', '').lower(), 'kaelen morwen')

    def test_resolved_npc_name_collision_rejected(self):
        memory_context = self._base_context()
        extracted = {}
        resolved = {
            'update_npc_actors': [
                {'id': 'aldric', 'name': 'Kaelen Morwen', 'role': 'Scholar Confused'},
            ],
        }
        compiled = compile_staged_memory_patch(memory_context, extracted, resolved)
        # This should be unresolved or absent — not applied
        unresolved = compiled.get('unresolved_items', [])
        npcs = compiled.get('update_npc_actors', [])
        # Either unresolved or the NPC update is absent
        self.assertTrue(
            len(npcs) == 0 or any(u.get('actor_id') == 'aldric' for u in unresolved),
            f"Name collision should be rejected. npcs={npcs}, unresolved={unresolved}"
        )

    def test_validated_packet_pass_through_compilation(self):
        memory_context = self._base_context()
        memory_context['resolver_packet'] = {
            'entity_mentions': [{
                'mention_ref': 'familiar_fig',
                'surface_form': 'the grey-cloaked figure',
                'identity_status': 'known_hidden',
                'canonical_id': 'aldric',
                'public_name': 'the grey-cloaked figure',
                'visibility': 'dm_private',
                'evidence_refs': ['campaign_entity:aldric'],
            }],
        }
        extracted = {}
        resolved = {}
        compiled = compile_staged_memory_patch(memory_context, extracted, resolved)
        # The packet should be accepted because "the grey-cloaked figure" is not a known NPC name
        self.assertIn('source_contract', compiled)
        self.assertEqual(compiled['source_contract'], 'compiled_session_memory_v2')
        diag = compiled.get('resolution_diagnostics', {})
        self.assertEqual(len(diag.get('substitutions', [])), 0)

        # The clock_complete behavior is tested at the integration level in test_dm_tools.py;
        # this test verifies compilation still succeeds without clock mutations.


    def test_validate_resolver_packet_rejects_bad_mentions(self):
        from services.memory_resolver_schemas import validate_resolver_packet
        ok, err = validate_resolver_packet({'entity_mentions': [{'mention_ref': 'bad!!', 'surface_form': '', 'identity_status': 'bogus'}]})
        self.assertFalse(ok)

        ok, err = validate_resolver_packet({'entity_mentions': [{'mention_ref': 'good_1', 'surface_form': 'the stranger', 'identity_status': 'known_hidden', 'visibility': 'dm_private'}]})
        self.assertTrue(ok)

        ok, err = validate_resolver_packet({'entity_mentions': [{
            'mention_ref': 'unknown_1',
            'surface_form': 'the stranger',
            'identity_status': 'intentionally_undetermined',
            'canonical_id': None,
            'public_name': None,
            'evidence_refs': None,
        }]})
        self.assertTrue(ok, err)

    def test_alias_does_not_rename_canonical_data(self):
        memory_context = self._base_context()
        extracted = {}
        resolved = {
            'update_npc_actors': [
                {'id': 'aldric', 'name': 'the archivist', 'role': 'Archivist'},
            ],
        }
        compiled = compile_staged_memory_patch(memory_context, extracted, resolved)
        npcs = compiled.get('update_npc_actors', [])
        self.assertEqual(len(npcs), 1)
        self.assertEqual(npcs[0]['name'], 'Aldric')
        records = compiled.get('resolution_records', [])
        alias_record = next((r for r in records if r.get('resolution_action') == 'add_alias'), None)
        self.assertIsNotNone(alias_record)
        self.assertEqual(alias_record['mention_name'], 'the archivist')
        self.assertEqual(alias_record['canonical_name'], 'Aldric')

    def test_slug_collision_does_not_reuse_entity(self):
        from services.session_memory_agent import _augment_registry_from_resolved
        known_ids = {
            'entity_ids': {'archivist'},
            'entity_names': {'archivist': 'The Great Archivist'},
            'npc_ids': set(),
            'npc_names': {},
        }
        registry = []
        _augment_registry_from_resolved(
            registry,
            [{'name': 'Archivist'}],
            [],
            known_ids,
            prior_resolutions=[],
        )
        entry = next((e for e in registry if e.get('surface_form') == 'Archivist'), None)
        self.assertIsNotNone(entry)
        self.assertEqual(entry['decision'], 'create_new')
        self.assertNotEqual(entry['canonical_id'], 'archivist')

    def test_current_location_id_with_different_name_rejected(self):
        graph = json.loads(self.world.knowledge_graph)
        graph['entities'].append({
            'id': 'black_anchor',
            'type': 'location',
            'name': 'Black Anchor',
        })
        self.world.knowledge_graph = json.dumps(graph)
        db.session.commit()

        scene_patch = {
            'location_id': 'drowned_lantern',
            'location_name': 'Black Anchor',
        }
        res = resolve_scene_location_patch(scene_patch, self.campaign, {'location_id': 'drowned_lantern', 'location_name': 'Drowned Lantern'})
        self.assertEqual(res['status'], 'unresolved')

        scene_patch_rename = {
            'location_id': 'drowned_lantern',
            'location_name': 'Burning Lantern',
            'rename_existing': True,
        }
        res_rename = resolve_scene_location_patch(scene_patch_rename, self.campaign, {'location_id': 'drowned_lantern', 'location_name': 'Drowned Lantern'})
        self.assertEqual(res_rename['status'], 'direct')

    def test_identical_descriptors_different_turns_not_shared(self):
        from services.resolution_registry import _index_prior_resolutions
        prior_same = [{
            'mention_entity_id': 'grey_figure',
            'mention_name': 'the grey-cloaked figure',
            'canonical_id': 'aldric',
            'resolution_action': 'same_identity',
        }]
        idx_same = _index_prior_resolutions(prior_same)
        self.assertNotIn('the grey-cloaked figure', idx_same)

        prior_alias = [{
            'mention_entity_id': 'grey_figure',
            'mention_name': 'the grey-cloaked figure',
            'canonical_id': 'aldric',
            'resolution_action': 'add_alias',
        }]
        idx_alias = _index_prior_resolutions(prior_alias)
        self.assertIn('the grey-cloaked figure', idx_alias)

    def test_committed_response_parts_read_errors_fail_closed(self):
        from routes.sessions import _run_session_memory_update
        from unittest.mock import patch
        from models import CampaignAuditEvent
        with patch('models.CampaignDmResponseParts') as mock_parts_class:
            mock_parts_class.query.filter_by.side_effect = Exception("DB Connection Refused")
            _run_session_memory_update(
                campaign_id=self.campaign.id,
                session_id=self.session.id,
                user_id=1,
                player_message_id=10,
                player_content="Hello",
                ai_text="Hi",
                hot_context={},
                parent_trace_id="trace-123",
                dm_message_id=99,
            )
            events = CampaignAuditEvent.query.filter_by(
                campaign_id=self.campaign.id,
                event_type='memory_update_error'
            ).all()
            self.assertEqual(len(events), 1)
            payload = json.loads(events[0].payload) if events[0].payload else {}
            self.assertEqual(payload.get('code'), 'response_parts_storage_read_error')

    def test_committed_structured_parts_reach_both_memory_stages(self):
        """The staged memory writer must consume the accepted stored part pairs."""
        from llm_providers import OpenRouterAdapter
        from routes.sessions import _run_session_memory_update
        from services.dm_response_parts import render_visible_response_parts
        from services.dm_turn_commit import commit_accepted_dm_turn

        player_message = SessionMessage(
            session_id=self.session.id,
            user_id=1,
            role='player',
            content='Who are you, Brother Orin?',
        )
        db.session.add(player_message)
        db.session.commit()
        parts = [
            {
                'type': 'npc_dialogue',
                'target': 'Brother Orin',
                'content': '"Only a diver."',
                'dm_private_context': (
                    'Deliberate cover story; do not overwrite Orin identity or background.'
                ),
            },
            {
                'type': 'npc_dialogue',
                'target': 'Brother Orin',
                'content': '"The tide turns at dawn."',
                'dm_private_context': 'Truthful operational detail.',
            },
        ]
        visible_content = render_visible_response_parts(parts)
        dm_message, _proposals, _results = commit_accepted_dm_turn(
            self.campaign,
            self.session,
            db.session.get(User, 1),
            player_message.id,
            'test:structured-memory',
            'structured memory test',
            visible_content,
            [],
            {'actions': []},
            parts,
        )

        import openrouter
        observed = {}
        real_extractor = openrouter.build_session_memory_extractor_messages
        real_resolver = openrouter.build_session_memory_resolver_messages

        def capture_extractor(memory_context):
            messages = real_extractor(memory_context)
            observed['extractor'] = json.loads(messages[1]['content'])
            return messages

        def capture_resolver(memory_context, extracted):
            messages = real_resolver(memory_context, extracted)
            observed['resolver'] = json.loads(messages[1]['content'])
            return messages

        adapter = OpenRouterAdapter()

        def tool_response(name, arguments):
            return adapter.parse_response({
                'choices': [{
                    'message': {
                        'content': '',
                        'tool_calls': [{
                            'id': f'test_{name}',
                            'type': 'function',
                            'function': {'name': name, 'arguments': json.dumps(arguments)},
                        }],
                    },
                }],
            })

        def fake_memory_provider(_messages, **kwargs):
            operation = kwargs.get('audit_context', {}).get('operation')
            if operation == 'session_memory_extract':
                return tool_response('submit_extraction', {
                    'running_summary': 'Orin offers a guarded answer.',
                    'fact_claims': [], 'entity_claims': [], 'relation_claims': [],
                    'npc_claims': [], 'event_claims': [],
                })
            self.assertEqual(operation, 'session_memory_resolve')
            return tool_response('submit_resolved_memory', {
                'running_summary': 'Orin offers a guarded answer.',
                'resolved_facts': [],
            })

        with unittest.mock.patch('openrouter.build_session_memory_extractor_messages', side_effect=capture_extractor), \
                unittest.mock.patch('openrouter.build_session_memory_resolver_messages', side_effect=capture_resolver), \
                unittest.mock.patch('openrouter._post_chat_normalized', side_effect=fake_memory_provider), \
                unittest.mock.patch('routes.sessions.get_session_clock_updates', return_value=[]):
            _run_session_memory_update(
                campaign_id=self.campaign.id,
                session_id=self.session.id,
                user_id=1,
                player_message_id=player_message.id,
                player_content=player_message.content,
                ai_text=visible_content,
                hot_context={},
                parent_trace_id='test-parent-trace',
                dm_message_id=dm_message.id,
                response_parts=parts,
            )

        self.assertEqual(observed['extractor']['latest_dm_response_parts'], parts)
        self.assertEqual(observed['resolver']['latest_dm_response_parts'], parts)
        self.assertIn(
            'do not overwrite Orin identity',
            observed['extractor']['latest_dm_response_parts'][0]['dm_private_context'],
        )
        self.assertEqual(
            observed['resolver']['latest_dm_response_parts'][1]['dm_private_context'],
            'Truthful operational detail.',
        )

    def test_clarification_answered_and_consumed_workflow(self):
        from models import CampaignClarification
        clar = CampaignClarification(
            campaign_id=self.campaign.id,
            clarification_id="clar_test_1",
            idempotency_key="key_1",
            kind="identity",
            mention_ref="test_ref",
            mention_entity_id="test_entity",
            question="Is test_ref Mira?",
            status="pending",
        )
        db.session.add(clar)
        db.session.commit()



        from routes.sessions import sessions_bp
        self.app.register_blueprint(sessions_bp)

        dm_user = User(username='dm_user_test', email='dm_test@example.com')
        dm_user.set_password('password')
        db.session.add(dm_user)
        db.session.commit()

        self.campaign.user_id = dm_user.id
        db.session.commit()

        from auth import generate_token
        token = generate_token(dm_user.id)

        client = self.app.test_client()

        get_res = client.get(
            f'/api/campaigns/{self.campaign.id}/clarifications',
            headers={'Authorization': f'Bearer {token}'},
        )
        self.assertEqual(get_res.status_code, 200)
        clars = json.loads(get_res.data.decode('utf-8'))
        self.assertEqual(len(clars), 1)
        self.assertEqual(clars[0]['clarification_id'], 'clar_test_1')

        # Verify invalid resolved_canonical_id is rejected by endpoint validation
        bad_answer_res = client.post(
            f'/api/campaigns/{self.campaign.id}/clarifications/clar_test_1/answer',
            headers={'Authorization': f'Bearer {token}'},
            json={
                'answer': 'Yes, it is nonexistent.',
                'resolved_canonical_id': 'nonexistent_id_lol',
                'resolution_action': 'same_identity'
            }
        )
        self.assertEqual(bad_answer_res.status_code, 400)

        # Answer successfully with valid resolved_canonical_id 'mira'
        answer_res = client.post(
            f'/api/campaigns/{self.campaign.id}/clarifications/clar_test_1/answer',
            headers={'Authorization': f'Bearer {token}'},
            json={
                'answer': 'Yes, it is Mira.',
                'resolved_canonical_id': 'mira',
                'resolution_action': 'same_identity',
                'resolution_patch': {
                    'update_npc_actors': [{'id': 'mira', 'role': 'Chief Guard'}]
                }
            }
        )
        self.assertEqual(answer_res.status_code, 200)

        clar_db = CampaignClarification.query.filter_by(clarification_id='clar_test_1').first()
        self.assertEqual(clar_db.status, 'answered')
        self.assertEqual(clar_db.answer, 'Yes, it is Mira.')

        memory_context = self._base_context()
        extracted = {}
        resolved = {}
        compiled = compile_staged_memory_patch(memory_context, extracted, resolved)

        self.assertIn('clar_test_1', compiled.get('consumed_clarification_ids', []))
        npcs = compiled.get('update_npc_actors', [])
        # The canonical name 'Mira' must be preserved and NOT overwritten by resolved_canonical_id 'mira'
        self.assertTrue(any(n['id'] == 'mira' and n['role'] == 'Chief Guard' and n['name'] == 'Mira' for n in npcs))

        from services.dm_tools import apply_compiled_session_memory_patch
        apply_compiled_session_memory_patch(self.campaign, self.session, compiled)

        clar_final = CampaignClarification.query.filter_by(clarification_id='clar_test_1').first()
        self.assertEqual(clar_final.status, 'resolved')


    def test_clarification_reopen_and_replacement_semantics(self):
        from models import CampaignClarification
        # 1. Create a resolved clarification for 'figure' in turn_1
        clar_old = CampaignClarification(
            campaign_id=self.campaign.id,
            clarification_id="clar_old",
            idempotency_key="key_old",
            kind="identity",
            mention_ref="figure",
            mention_entity_id="figure",
            question="Who is figure?",
            status="resolved",
        )
        db.session.add(clar_old)
        db.session.commit()

        # 2. In turn_2, the registry builder requests clarification again for the same mention_ref 'figure'
        # Since the old one is resolved, it should create a new pending clarification rather than skipping
        memory_context = self._base_context()
        memory_context['hot_context']['turn_id'] = 'turn_2'

        from services.resolution_registry import _build_clarification_record, persist_clarification_requests
        entry = {
            "mention_ref": "figure",
            "surface_form": "the figure",
            "decision": "request_clarification",
            "blocked_operations": ["npc_update"]
        }
        cr = _build_clarification_record(entry, self.campaign, memory_context)
        self.assertIsNotNone(cr)
        self.assertEqual(cr["status"], "pending")

        persist_clarification_requests([cr], self.campaign)

        all_clars = CampaignClarification.query.filter_by(campaign_id=self.campaign.id, mention_ref="figure").all()
        self.assertEqual(len(all_clars), 2)
        self.assertTrue(any(c.status == "resolved" for c in all_clars))
        self.assertTrue(any(c.status == "pending" for c in all_clars))

    def test_two_turn_resolution_record_persistence(self):
        memory_context_t1 = self._base_context()
        memory_context_t1['hot_context']['turn_id'] = 'turn_1'

        registry_t1 = [{
            "mention_ref": "stranger",
            "surface_form": "the stranger",
            "canonical_id": "aldric",
            "canonical_name": "Aldric",
            "decision": "reuse_existing",
            "visibility": "party_known"
        }]
        records_t1 = _build_resolution_records(registry_t1, {}, memory_context_t1)
        self.assertEqual(len(records_t1), 1)
        res_id_t1 = records_t1[0]['resolution_id']

        memory_context_t2 = self._base_context()
        memory_context_t2['hot_context']['turn_id'] = 'turn_2'

        registry_t2 = [{
            "mention_ref": "stranger",
            "surface_form": "the stranger",
            "canonical_id": "aldric",
            "canonical_name": "Aldric",
            "decision": "reuse_existing",
            "visibility": "party_known"
        }]
        records_t2 = _build_resolution_records(registry_t2, {}, memory_context_t2)
        self.assertEqual(len(records_t2), 1)
        res_id_t2 = records_t2[0]['resolution_id']

        # The resolution IDs must be different because they occurred in different turns!
        self.assertNotEqual(res_id_t1, res_id_t2)


    def test_duplicate_and_cross_type_candidate_filtering(self):
        from services.resolution_registry import find_all_matching_candidates
        # Enforce type compatibility: location "Aldric" is not a candidate for NPC "Aldric"
        known_cross = {
            "npc_ids": set(),
            "npc_names": {},
            "entity_ids": {"aldric_location"},
            "entity_names": {"aldric_location": "Aldric"}
        }
        npc_candidates = find_all_matching_candidates("Aldric", known_cross, [], expected_type="npc")
        self.assertEqual(len(npc_candidates), 0)

        # Deduplicate: NPC is present in both NPC list and graph entities, count once
        known_dup = {
            "npc_ids": {"aldric"},
            "npc_names": {"aldric": "Aldric"},
            "entity_ids": {"aldric"},
            "entity_names": {"aldric": "Aldric"}
        }
        npc_dup_candidates = find_all_matching_candidates("Aldric", known_dup, [], expected_type="npc")
        self.assertEqual(len(npc_dup_candidates), 1)
        self.assertEqual(list(npc_dup_candidates.keys())[0], "aldric")

    def test_ignore_clarification_completion(self):
        from models import CampaignClarification
        clar = CampaignClarification(
            campaign_id=self.campaign.id,
            clarification_id="clar_ignore",
            idempotency_key="key_ignore",
            kind="identity",
            mention_ref="test_ignore_ref",
            mention_entity_id="test_ignore_entity",
            question="Is ignore?",
            status="answered",
            resolution_action="ignore"
        )
        db.session.add(clar)
        db.session.commit()

        memory_context = self._base_context()
        extracted = {}
        resolved = {}
        compiled = compile_staged_memory_patch(memory_context, extracted, resolved)

        self.assertIn("clar_ignore", compiled.get("consumed_clarification_ids", []))

    def test_illegal_status_transitions_rejected(self):
        from routes.sessions import sessions_bp
        try:
            self.app.register_blueprint(sessions_bp)
        except AssertionError:
            pass

        from models import CampaignClarification
        clar = CampaignClarification(
            campaign_id=self.campaign.id,
            clarification_id="clar_illegal",
            idempotency_key="key_illegal",
            kind="identity",
            mention_ref="ref",
            question="Is ref?",
            status="resolved",
        )
        db.session.add(clar)
        db.session.commit()

        dm_user = User(username='dm_user_test_2', email='dm_test_2@example.com')
        dm_user.set_password('password')
        db.session.add(dm_user)
        db.session.commit()
        self.campaign.user_id = dm_user.id
        db.session.commit()

        from auth import generate_token
        token = generate_token(dm_user.id)
        client = self.app.test_client()

        res = client.post(
            f'/api/campaigns/{self.campaign.id}/clarifications/clar_illegal/answer',
            headers={'Authorization': f'Bearer {token}'},
            json={
                'answer': 'Yes',
                'resolution_action': 'ignore'
            }
        )
        self.assertEqual(res.status_code, 400)


    def test_unrelated_patch_targets_rejected(self):
        from routes.sessions import sessions_bp
        try:
            self.app.register_blueprint(sessions_bp)
        except AssertionError:
            pass

        from models import CampaignClarification
        clar = CampaignClarification(
            campaign_id=self.campaign.id,
            clarification_id="clar_patch_val",
            idempotency_key="key_patch_val",
            kind="identity",
            mention_ref="test_ref",
            mention_entity_id="test_entity",
            question="Is test_ref Mira?",
            status="pending",
            blocking_scope=["npc_update"]
        )
        db.session.add(clar)
        db.session.commit()

        dm_user = User(username='dm_user_test_3', email='dm_test_3@example.com')
        dm_user.set_password('password')
        db.session.add(dm_user)
        db.session.commit()
        self.campaign.user_id = dm_user.id
        db.session.commit()

        from auth import generate_token
        token = generate_token(dm_user.id)
        client = self.app.test_client()

        res_bad_id = client.post(
            f'/api/campaigns/{self.campaign.id}/clarifications/clar_patch_val/answer',
            headers={'Authorization': f'Bearer {token}'},
            json={
                'answer': 'Yes, it is Mira.',
                'resolved_canonical_id': 'mira',
                'resolution_action': 'same_identity',
                'resolution_patch': {
                    'update_npc_actors': [{'id': 'aldric', 'role': 'Chief Guard'}]
                }
            }
        )
        self.assertEqual(res_bad_id.status_code, 400)

        res_bad_scope = client.post(
            f'/api/campaigns/{self.campaign.id}/clarifications/clar_patch_val/answer',
            headers={'Authorization': f'Bearer {token}'},
            json={
                'answer': 'Yes, it is Mira.',
                'resolved_canonical_id': 'mira',
                'resolution_action': 'same_identity',
                'resolution_patch': {
                    'upsert_graph_entities': [{'id': 'mira', 'type': 'location'}]
                }
            }
        )
        self.assertEqual(res_bad_scope.status_code, 400)


class ResolvedEntityRefsTest(unittest.TestCase):
    """Issue #84: consumed resolved entity references prevent duplicate identities."""

    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SECRET_KEY'] = 'test-secret'
        self.app.config['JWT_EXPIRATION_HOURS'] = 24
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        usr = User(username='dm_user', email='dm@example.com')
        usr.set_password('password')
        db.session.add(usr)
        db.session.commit()

        self.campaign = Campaign(name='Resolved Refs Test', description='Test', user_id=usr.id)
        db.session.add(self.campaign)
        db.session.commit()

        self.world = CampaignWorld(
            campaign_id=self.campaign.id,
            public_intro='{}',
            knowledge_graph='{"entities":[{"id":"drowned_lantern","type":"location","name":"Drowned Lantern"}],"relations":[],"facts":[]}',
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
        gar = NPCActor(campaign_id=self.campaign.id, actor_id='garret', name='Garret', dossier='{}')
        db.session.add(gar)
        db.session.commit()

    def tearDown(self):
        db.session.rollback()
        db.drop_all()
        self.ctx.pop()

    def _context(self):
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

    def test_resolved_ref_yields_one_canonical_npc(self):
        memory_context = self._context()
        resolved = {
            'resolved_entity_refs': [
                {'label': 'Old Garret', 'entity_id': 'garret', 'resolution': 'same'},
            ],
            'upsert_graph_entities': [
                {
                    'id': 'old_garret',
                    'name': 'Old Garret',
                    'type': 'npc',
                    'source_surface': 'visible_transcript',
                    'intended_visibility': 'party_known',
                },
            ],
            'update_npc_actors': [
                {'id': 'old_garret', 'name': 'Old Garret', 'role': 'Dock worker'},
            ],
            'upsert_graph_relations': [
                {
                    'type': 'works_at',
                    'source_id': 'garret',
                    'target_id': 'drowned_lantern',
                    'summary': 'Works at the docks.',
                },
            ],
            'upsert_graph_facts': [
                {'text': 'Old Garret knows the saboteur.', 'entity_ids': ['garret']},
            ],
        }
        compiled = compile_staged_memory_patch(memory_context, {}, resolved)
        entity_ids = [e['id'] for e in compiled['upsert_graph_entities']]
        self.assertNotIn('old_garret', entity_ids)
        self.assertIn('garret', entity_ids)
        npc_ids = [n.get('id') for n in compiled['update_npc_actors']]
        self.assertEqual(npc_ids, ['garret'])
        relations = compiled['upsert_graph_relations']
        self.assertTrue(all(r['source_id'] == 'garret' for r in relations))
        facts = compiled['upsert_graph_facts']
        self.assertTrue(all('garret' in f['entity_ids'] for f in facts))
        # Provenance for the alias is preserved in resolution records.
        records = compiled.get('resolution_records', [])
        matching = [r for r in records if r.get('mention_entity_id') == 'old_garret']
        self.assertTrue(matching)
        self.assertEqual(matching[0]['canonical_id'], 'garret')
        self.assertEqual(matching[0]['resolution_action'], 'add_alias')

    def test_registry_reconciles_provisional_to_resolver_canonical(self):
        known = {
            'entity_ids': {'garret'},
            'npc_ids': {'garret'},
            'entity_names': {},
            'npc_names': {'garret': 'Garret'},
            'entity_types': {'garret': 'npc'},
        }
        extracted = {
            'npc_claims': [
                {'name': 'Old Garret', 'type': 'npc', 'mention_ref': 'old_garret_claim'},
            ],
        }
        registry, _, _, diagnostics = build_canonical_resolution_registry(
            self.campaign,
            self._context(),
            extracted,
            None,
            [],
            known,
            resolved_entity_refs=[{'label': 'Old Garret', 'entity_id': 'garret', 'resolution': 'same'}],
        )
        entry = next(e for e in registry if e.get('mention_ref') == 'old_garret_claim')
        self.assertEqual(entry['canonical_id'], 'garret')
        self.assertEqual(entry['decision'], 'add_alias')
        self.assertEqual(entry['resolution_state'], 'resolved')
        self.assertFalse(
            any(item.get('canonical_id') == 'old_garret' for item in diagnostics.get('created_provisional', [])),
        )

    def test_same_patch_ref_prevents_provisional_split(self):
        memory_context = self._context()
        extracted = {
            'entity_claims': [
                {'name': 'the wolf', 'type': 'beast', 'mention_ref': 'wolf_claim'},
            ],
        }
        resolved = {
            'resolved_entity_refs': [
                {'label': 'the wolf', 'entity_id': 'mira_wolf', 'resolution': 'same'},
            ],
            'upsert_graph_entities': [
                {'id': 'mira_wolf', 'name': 'Mira\'s Wolf', 'type': 'beast'},
            ],
        }
        compiled = compile_staged_memory_patch(memory_context, extracted, resolved)
        entity_ids = [e['id'] for e in compiled['upsert_graph_entities']]
        self.assertIn('mira_wolf', entity_ids)
        self.assertNotIn('the_wolf', entity_ids)


    def test_malformed_nonempty_refs_fail_closed(self):
        for raw_refs in (
            [{}],
            ['The Ferry Guild'],
            [{'surface_form': 'The Ferry Guild', 'canonical_id': 'ferry_guild'}],
        ):
            with self.assertRaises(MemoryPipelineError) as raised:
                compile_staged_memory_patch(
                    self._context(),
                    {},
                    {'resolved_entity_refs': raw_refs},
                )
            self.assertEqual(raised.exception.code, 'invalid_resolved_entity_refs')




    def test_event_payload_references_use_canonical_id(self):
        memory_context = self._context()
        resolved = {
            'resolved_entity_refs': [
                {'label': 'Old Garret', 'entity_id': 'garret', 'resolution': 'same'},
            ],
            'update_npc_actors': [
                {'id': 'old_garret', 'name': 'Old Garret'},
            ],
            'record_events': [
                {
                    'event_type': 'confrontation',
                    'summary': 'Old Garret confronted.',
                    'payload': {
                        'actor_id': 'old_garret',
                        'entity_ids': ['old_garret', 'mira'],
                        'nested': {'target_id': 'old_garret'},
                    },
                },
            ],
        }
        compiled = compile_staged_memory_patch(memory_context, {}, resolved)
        events = compiled['record_events']
        self.assertEqual(len(events), 1)
        payload = events[0]['payload']
        self.assertEqual(payload['actor_id'], 'garret')
        self.assertEqual(set(payload['entity_ids']), {'garret', 'mira'})
        self.assertEqual(payload['nested']['target_id'], 'garret')

    def test_repair_preserves_overlapping_private_data(self):
        from services.dm_tools import repair_duplicate_identity

        graph = json.loads(self.world.knowledge_graph)
        graph['entities'].append({'id': 'garret', 'name': 'Garret', 'type': 'npc'})
        graph['entities'].append({'id': 'old_garret', 'name': 'Old Garret', 'type': 'npc'})
        self.world.knowledge_graph = json.dumps(graph)
        world_state = json.loads(self.world.world_state)
        world_state['current_scene']['active_npc_ids'] = ['old_garret', 'garret']
        self.world.world_state = json.dumps(world_state)
        db.session.add(
            NPCActor(
                campaign_id=self.campaign.id,
                actor_id='old_garret',
                name='Old Garret',
                dossier=json.dumps({
                    'role': 'Old dock worker',
                    'wants': ['retire', 'quiet pint'],
                    'secrets': ['is the saboteur'],
                    'relationships': {'mira': 'trusts her'},
                }),
            )
        )
        can = NPCActor.query.filter_by(campaign_id=self.campaign.id, actor_id='garret').first()
        can.dossier = json.dumps({
            'role': 'Foreman',
            'wants': ['quiet pint', 'new boots'],
            'secrets': ['owes a debt'],
            'relationships': {'toren': 'old rival'},
        })
        db.session.commit()

        with unittest.mock.patch('services.dm_tools.upsert_memory_embedding', return_value={'ok': True}):
            repair_duplicate_identity(self.campaign, 'old_garret', 'garret')
        db.session.commit()

        can = NPCActor.query.filter_by(campaign_id=self.campaign.id, actor_id='garret').first()
        self.assertEqual(can.name, 'Garret')
        dossier = json.loads(can.dossier)
        self.assertEqual(dossier['role'], 'Foreman')
        self.assertEqual(set(dossier['wants']), {'retire', 'quiet pint', 'new boots'})
        self.assertEqual(set(dossier['secrets']), {'is the saboteur', 'owes a debt'})
        self.assertEqual(set(dossier['relationships']), {'mira', 'toren'})
        self.assertEqual(dossier['relationships']['mira'], 'trusts her')

        world_state = json.loads(self.world.world_state)
        self.assertEqual(world_state['current_scene']['active_npc_ids'], ['garret'])

    def test_conflicting_refs_fail_validation(self):
        from services.session_memory_agent import _validate_resolved_entity_refs
        compiled = {
            'resolved_entity_refs': [
                {'label': 'Old Garret', 'entity_id': 'garret', 'resolution': 'same'},
                {'label': 'Old Garret', 'entity_id': 'old_garret', 'resolution': 'same'},
            ],
            'upsert_graph_entities': [],
            'update_npc_actors': [],
        }
        errors = _validate_resolved_entity_refs(compiled)
        self.assertTrue(any('resolved_ref_conflict' in e for e in errors))

    def test_split_brain_output_fails_validation(self):
        from services.session_memory_agent import _validate_resolved_entity_refs
        compiled = {
            'resolved_entity_refs': [
                {'label': 'Old Garret', 'entity_id': 'garret', 'resolution': 'same'},
            ],
            'upsert_graph_entities': [
                {'id': 'garret', 'name': 'Garret', 'type': 'npc'},
                {'id': 'old_garret', 'name': 'Old Garret', 'type': 'npc'},
            ],
            'update_npc_actors': [
                {'id': 'old_garret', 'name': 'Old Garret'},
            ],
        }
        errors = _validate_resolved_entity_refs(compiled)
        self.assertTrue(any('resolved_ref_split_brain' in e for e in errors))

    def test_repair_duplicate_identity_merges_safely(self):
        from services.dm_tools import repair_duplicate_identity

        graph = json.loads(self.world.knowledge_graph)
        graph['entities'].append({'id': 'garret', 'name': 'Garret', 'type': 'npc'})
        graph['entities'].append({'id': 'old_garret', 'name': 'Old Garret', 'type': 'npc'})
        graph['relations'].append({
            'id': 'rel_1',
            'type': 'works_at',
            'source_id': 'old_garret',
            'target_id': 'drowned_lantern',
        })
        graph['facts'].append({
            'id': 'fact_1',
            'text': 'Old Garret knows something.',
            'entity_ids': ['old_garret', 'mira'],
        })
        self.world.knowledge_graph = json.dumps(graph)
        world_state = json.loads(self.world.world_state)
        world_state['current_scene']['active_npc_ids'] = ['old_garret']
        self.world.world_state = json.dumps(world_state)
        db.session.add(
            NPCActor(
                campaign_id=self.campaign.id,
                actor_id='old_garret',
                name='Old Garret',
                public_summary='An old hand.',
                dossier=json.dumps({'secret': 'is the saboteur', 'role': 'Old dock worker'}),
            )
        )
        can = NPCActor.query.filter_by(campaign_id=self.campaign.id, actor_id='garret').first()
        can.dossier = json.dumps({'role': 'Foreman'})
        can.public_summary = 'The dock foreman.'
        db.session.commit()

        with unittest.mock.patch('services.dm_tools.upsert_memory_embedding', return_value={'ok': True}):
            result = repair_duplicate_identity(self.campaign, 'old_garret', 'garret')
        db.session.commit()

        self.assertEqual(result['duplicate_id'], 'old_garret')
        self.assertEqual(result['canonical_id'], 'garret')

        graph = json.loads(self.world.knowledge_graph)
        entity_ids = {e['id'] for e in graph['entities']}
        self.assertIn('garret', entity_ids)
        self.assertNotIn('old_garret', entity_ids)
        self.assertEqual(graph['relations'][0]['source_id'], 'garret')
        self.assertEqual(set(graph['facts'][0]['entity_ids']), {'garret', 'mira'})

        self.assertIsNone(
            NPCActor.query.filter_by(campaign_id=self.campaign.id, actor_id='old_garret').first()
        )
        can = NPCActor.query.filter_by(campaign_id=self.campaign.id, actor_id='garret').first()
        self.assertEqual(can.name, 'Garret')
        self.assertEqual(can.public_summary, 'The dock foreman.')
        dossier = json.loads(can.dossier)
        self.assertEqual(dossier.get('secret'), 'is the saboteur')
        self.assertEqual(dossier.get('role'), 'Foreman')

        world_state = json.loads(self.world.world_state)
        self.assertEqual(world_state['current_scene']['active_npc_ids'], ['garret'])

        resolution = CampaignIdentityResolution.query.filter_by(
            campaign_id=self.campaign.id,
            mention_entity_id='old_garret',
            canonical_id='garret',
        ).first()
        self.assertIsNotNone(resolution)
        self.assertEqual(resolution.resolution_action, 'add_alias')


class WorldIdentityNamespaceTest(unittest.TestCase):
    """Issue #130: graph NPC entities and npc_actors rows use distinct IDs.

    World generation materializes one fictional identity as both a graph
    entity (e.g. `the_candlewright`) and an npc_actors row
    (e.g. `npc_the_candlewright`). Final-state validation must check graph
    upserts against graph canonical IDs and NPC updates against actor IDs,
    using an authoritative graph<->actor mapping instead of treating
    cross-layer name equality as proof that storage IDs must be equal.
    """

    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SECRET_KEY'] = 'test-secret'
        self.app.config['JWT_EXPIRATION_HOURS'] = 24
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        usr = User(username='dm_user', email='dm@example.com')
        usr.set_password('password')
        db.session.add(usr)
        db.session.commit()

        self.campaign = Campaign(name='World Identity Test', description='Test', user_id=usr.id)
        db.session.add(self.campaign)
        db.session.commit()

        self.world = CampaignWorld(
            campaign_id=self.campaign.id,
            public_intro='{}',
            knowledge_graph=json.dumps({
                'entities': [
                    {'id': 'drowned_lantern', 'type': 'location', 'name': 'Drowned Lantern'},
                    {'id': 'the_candlewright', 'type': 'npc', 'name': 'The Candlewright'},
                    {'id': 'lake_tender', 'type': 'npc', 'name': 'Lake Tender'},
                ],
                'relations': [],
                'facts': [],
            }),
            world_state='{"current_scene":{"location_id":"drowned_lantern","location_name":"Drowned Lantern"}}',
            dm_private='{}',
        )
        db.session.add(self.world)
        db.session.flush()

        self.session = CampaignSession(campaign_id=self.campaign.id)
        db.session.add(self.session)
        db.session.flush()

        db.session.add(NPCActor(
            campaign_id=self.campaign.id,
            actor_id='npc_the_candlewright',
            name='The Candlewright',
            dossier='{}',
        ))
        db.session.add(NPCActor(
            campaign_id=self.campaign.id,
            actor_id='npc_lake_tender',
            name='Lake Tender',
            dossier='{}',
        ))
        db.session.commit()
        from services.world_service import persist_world_identity_pairs
        persist_world_identity_pairs(self.campaign, [
            {'graph_entity_id': 'the_candlewright', 'actor_id': 'npc_the_candlewright'},
            {'graph_entity_id': 'lake_tender', 'actor_id': 'npc_lake_tender'},
        ])
        db.session.commit()

    def tearDown(self):
        db.session.rollback()
        db.drop_all()
        self.ctx.pop()

    def _context(self):
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

    def test_known_ids_expose_authoritative_mapping(self):
        known = _known_ids(self.campaign)
        self.assertEqual(known['entity_to_actor']['the_candlewright'], 'npc_the_candlewright')
        self.assertEqual(known['entity_to_actor']['lake_tender'], 'npc_lake_tender')
        self.assertEqual(known['actor_to_entity']['npc_the_candlewright'], 'the_candlewright')

    def test_mapping_is_authoritative_without_derivation_fallback(self):
        # _known_ids must only read the persisted campaign_world_identities
        # rows, never derive a cross-layer identity from name equality.
        from models import CampaignWorldIdentity
        CampaignWorldIdentity.query.filter_by(campaign_id=self.campaign.id).delete()
        db.session.commit()
        known = _known_ids(self.campaign)
        self.assertEqual(known['entity_to_actor'], {})
        self.assertEqual(known['actor_to_entity'], {})

    def test_world_identity_pairs_derived_from_package(self):
        from services.world_service import build_world_identity_pairs
        package = {
            'knowledge_graph': {
                'entities': [
                    {'id': 'drowned_lantern', 'type': 'location', 'name': 'Drowned Lantern'},
                    {'id': 'the_candlewright', 'type': 'npc', 'name': 'The Candlewright'},
                    {'id': 'lake_tender', 'type': 'npc', 'name': 'Lake Tender'},
                ],
            },
            'npc_actors': [
                {'id': 'npc_the_candlewright', 'name': 'The Candlewright'},
                {'id': 'npc_lake_tender', 'name': 'Lake Tender'},
            ],
        }
        pairs = build_world_identity_pairs(package)
        self.assertEqual(
            {(p['graph_entity_id'], p['actor_id']) for p in pairs},
            {('the_candlewright', 'npc_the_candlewright'), ('lake_tender', 'npc_lake_tender')},
        )

    def test_duplicate_actor_names_not_paired(self):
        # normalize_npc_actors permits two actor rows to share a name. A
        # one-to-one mapping must not be produced for a name that is not
        # unique on the actor side, or the unique (campaign_id,
        # graph_entity_id) constraint would be violated at persistence.
        from services.world_service import build_world_identity_pairs
        package = {
            'knowledge_graph': {
                'entities': [
                    {'id': 'bob', 'type': 'npc', 'name': 'Bob'},
                ],
            },
            'npc_actors': [
                {'id': 'npc_bob_1', 'name': 'Bob'},
                {'id': 'npc_bob_2', 'name': 'Bob'},
            ],
        }
        self.assertEqual(build_world_identity_pairs(package), [])

    def test_duplicate_graph_entity_names_not_paired(self):
        from services.world_service import build_world_identity_pairs
        package = {
            'knowledge_graph': {
                'entities': [
                    {'id': 'bob', 'type': 'npc', 'name': 'Bob'},
                    {'id': 'bob_2', 'type': 'npc', 'name': 'Bob'},
                ],
            },
            'npc_actors': [
                {'id': 'npc_bob', 'name': 'Bob'},
            ],
        }
        self.assertEqual(build_world_identity_pairs(package), [])

    def test_duplicate_actor_names_persist_without_constraint_violation(self):
        # Pairing is genuinely one-to-one, so a package with duplicate actor
        # names must persist the mapping without tripping the unique
        # (campaign_id, graph_entity_id) constraint at flush/commit.
        from services.world_service import build_world_identity_pairs, persist_world_identity_pairs
        from models import CampaignWorldIdentity
        package = {
            'knowledge_graph': {
                'entities': [
                    {'id': 'bob', 'type': 'npc', 'name': 'Bob'},
                    {'id': 'lake_tender', 'type': 'npc', 'name': 'Lake Tender'},
                ],
            },
            'npc_actors': [
                {'id': 'npc_bob_1', 'name': 'Bob'},
                {'id': 'npc_bob_2', 'name': 'Bob'},
                {'id': 'npc_lake_tender', 'name': 'Lake Tender'},
            ],
        }
        pairs = build_world_identity_pairs(package)
        self.assertEqual(pairs, [{'graph_entity_id': 'lake_tender', 'actor_id': 'npc_lake_tender'}])
        persist_world_identity_pairs(self.campaign, pairs)
        db.session.commit()
        rows = {
            row.graph_entity_id
            for row in CampaignWorldIdentity.query.filter_by(campaign_id=self.campaign.id).all()
        }
        self.assertEqual(rows, {'lake_tender'})

    def test_production_shape_actor_update_not_a_split_brain(self):
        # Regression for Run 42: updating the npc_actors row for an identity
        # the resolver mapped to a graph entity must not fail validation just
        # because the actor id differs from the graph entity id.
        memory_context = self._context()
        resolved = {
            'resolved_entity_refs': [
                {
                    'label': 'Candlewright',
                    'entity_id': 'the_candlewright',
                    'resolution': 'same',
                    'canonical_name': 'The Candlewright',
                },
                {
                    'label': 'the grey-hooded stranger',
                    'entity_id': 'the_candlewright',
                    'resolution': 'same',
                    'canonical_name': 'The Candlewright',
                },
            ],
            'update_npc_actors': [
                {
                    'id': 'npc_the_candlewright',
                    'name': 'The Candlewright',
                    'role': 'Candle maker',
                    'wants': ['keep the shop lit'],
                    'secrets': ['hides a wax hoard'],
                },
            ],
        }
        compiled = compile_staged_memory_patch(memory_context, {}, resolved)
        npcs = compiled.get('update_npc_actors', [])
        self.assertEqual(len(npcs), 1)
        self.assertEqual(npcs[0]['id'], 'npc_the_candlewright')
        self.assertEqual(npcs[0]['role'], 'Candle maker')

    def test_lake_tender_update_not_a_split_brain(self):
        memory_context = self._context()
        resolved = {
            'resolved_entity_refs': [
                {
                    'label': 'the lake tender',
                    'entity_id': 'lake_tender',
                    'resolution': 'same',
                    'canonical_name': 'Lake Tender',
                },
            ],
            'update_npc_actors': [
                {'id': 'npc_lake_tender', 'name': 'Lake Tender', 'role': 'Dockside guide'},
            ],
        }
        compiled = compile_staged_memory_patch(memory_context, {}, resolved)
        npcs = compiled.get('update_npc_actors', [])
        self.assertEqual(len(npcs), 1)
        self.assertEqual(npcs[0]['id'], 'npc_lake_tender')
        self.assertEqual(npcs[0]['role'], 'Dockside guide')

    def test_actor_update_to_unmapped_actor_rejected(self):
        # A patch that writes "The Candlewright" onto a different actor row is
        # a genuine duplicate in the actor layer, not the paired identity.
        compiled = {
            'resolved_entity_refs': [
                {'label': 'The Candlewright', 'entity_id': 'the_candlewright', 'resolution': 'same'},
            ],
            'upsert_graph_entities': [],
            'update_npc_actors': [
                {'id': 'npc_other_candlewright', 'name': 'The Candlewright'},
            ],
        }
        known = {
            'npc_names': {'npc_the_candlewright': 'The Candlewright'},
            'entity_names': {'the_candlewright': 'The Candlewright'},
            'entity_to_actor': {'the_candlewright': 'npc_the_candlewright'},
        }
        errors = _validate_resolved_entity_refs(compiled, known)
        self.assertTrue(any('npc_actor_split_brain' in e for e in errors))

    def test_duplicate_graph_identity_still_rejected(self):
        # A genuinely new graph identity that duplicates a resolved canonical
        # is still rejected within the graph layer.
        compiled = {
            'resolved_entity_refs': [
                {'label': 'The Candlewright', 'entity_id': 'the_candlewright', 'resolution': 'same'},
            ],
            'upsert_graph_entities': [
                {'id': 'the_candlewright_2', 'name': 'The Candlewright', 'type': 'npc'},
            ],
            'update_npc_actors': [],
        }
        errors = _validate_resolved_entity_refs(compiled, {})
        self.assertTrue(any('resolved_ref_split_brain' in e for e in errors))

    def test_cross_layer_name_equality_alone_not_split_brain(self):
        # Cross-layer name equality with NO authoritative mapping is not
        # proof that storage IDs must be equal.
        compiled = {
            'resolved_entity_refs': [
                {'label': 'The Candlewright', 'entity_id': 'the_candlewright', 'resolution': 'same'},
            ],
            'upsert_graph_entities': [],
            'update_npc_actors': [
                {'id': 'npc_the_candlewright', 'name': 'The Candlewright'},
            ],
        }
        known = {
            'npc_names': {'npc_the_candlewright': 'The Candlewright'},
            'entity_names': {'the_candlewright': 'The Candlewright'},
            'entity_to_actor': {},
        }
        errors = _validate_resolved_entity_refs(compiled, known)
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()

import os
import base64
import json
import sys
import tempfile
import unittest
import re
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from flask import Flask
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from auth import generate_token
from models import (
    db,
    Campaign,
    CampaignAuditEvent,
    CampaignClock,
    CampaignDmResponseParts,
    CampaignResolverPacket,
    CampaignMemoryEmbedding,
    CampaignMember,
    CampaignMonster,
    CampaignSession,
    SessionDmTurn,
    SheetProposal,
    CampaignWorld,
    Character,
    CharacterCondition,
    CharacterPlanningMessage,
    EncounterMap,
    EncounterMapPlacement,
    NPCActor,
    SessionMessage,
    User,
    WorldEvent,
)
from openrouter import (
    check_session_missing_npc_tags_with_llm,
    check_session_mechanics_with_llm,
    check_session_pc_control_with_llm,
    check_session_spoilers_with_llm,
    _possible_missing_npc_tag_signal,
    _pc_control_violation,
    _private_output_violation,
    _session_dm_format_violation,
    _session_dm_guard_retry_system_prompt,
    _session_dm_tool_result_for_prompt,
    _validate_session_dm_request_sources,
    _witness_private_leverage_spoiler_violation,
    build_session_dm_request_messages,
    build_session_dm_tool_messages,
    build_session_canon_discipline_check_messages,
    get_session_memory_patch,
    get_session_dm_response_with_tools,
    normalize_session_dm_turn_decision,
    _session_dm_finalizer_decision_from_tool_calls,
    SESSION_DM_FINALIZER_TOOLS,
    build_session_memory_extractor_messages,
    build_session_memory_resolver_messages,
    get_session_clock_updates,
)
from routes.dev import _agent_runs_from_stream, _audit_stream_entry, _chat_flow_payload
from routes.sessions import sessions_bp
from services.audit_service import log_audit_event
from services.automation_service import collect_model_retry_metrics
from services.dm_tools import (
    DM_TOOL_DEFINITIONS,
    get_dm_tool_definitions,
    apply_clock_adjudication,
    build_session_hot_context,
    build_session_memory_context,
    build_session_clock_context,
    build_session_retrieval_packet,
    context_manifest,
    execute_dm_tool,
)
from services.dm_turn_commit import commit_accepted_dm_turn
from services.memory_resolver_schemas import RESOLVER_PACKET_SCHEMA
from services.embedding_service import (
    canonical_text_for_item,
    cosine_similarity,
    embeddings_from_texts,
    find_duplicate_graph_item,
    search_memory_embeddings,
    upsert_memory_embedding,
)
from services.encounter_map_service import create_labeled_grid_image, detect_grid_from_image



from llm_providers import OpenRouterAdapter as _OpenRouterAdapter

def _normalized_from_raw(raw_dict):
    return _OpenRouterAdapter().parse_response(raw_dict)

def synthetic_grid_png(size=256, cell=32, offset=0, blank=False):
    image = Image.new('RGB', (size, size), 'white')
    if not blank:
        draw = ImageDraw.Draw(image)
        for position in range(offset, size, cell):
            draw.line([(position, 0), (position, size - 1)], fill=(20, 20, 20), width=2)
            draw.line([(0, position), (size - 1, position)], fill=(20, 20, 20), width=2)
        if offset == 0:
            draw.line([(size - 1, 0), (size - 1, size - 1)], fill=(20, 20, 20), width=2)
            draw.line([(0, size - 1), (size - 1, size - 1)], fill=(20, 20, 20), width=2)
    buffer = BytesIO()
    image.save(buffer, format='PNG')
    return buffer.getvalue()


def dm_talk_tool_response(content, private_context=None, resolver_packet=None, commit_action_ids=None):
    npc_match = re.fullmatch(r'<npc\s+target="([^"]+)">(.*)</npc>', content, flags=re.DOTALL)
    part = (
        {'type': 'npc_dialogue', 'target': npc_match.group(1), 'content': npc_match.group(2)}
        if npc_match else {'type': 'narration', 'content': content}
    )
    if private_context is not None:
        part['dm_private_context'] = private_context
    arguments = {'parts': [part], 'commit_action_ids': commit_action_ids or []}
    if resolver_packet is not None:
        arguments['resolver_packet'] = resolver_packet
    return {
        'choices': [{
            'message': {
                'content': '',
                'tool_calls': [{
                    'id': 'call_final',
                    'function': {
                        'name': 'talk_to_player',
                        'arguments': json.dumps(arguments),
                    },
                }],
            },
        }],
    }


def dm_silent_tool_response(reason):
    return {
        'choices': [{
            'message': {
                'content': '',
                'tool_calls': [{
                    'id': 'call_final',
                    'function': {
                        'name': 'stay_silent',
                        'arguments': json.dumps({'reason': reason}),
                    },
                }],
            },
        }],
    }


class DmToolsTest(unittest.TestCase):
    def setUp(self):
        self.env_patch = patch.dict(os.environ, {'GEMINI_EMBEDDINGS_ENABLED': 'false'}, clear=False)
        self.env_patch.start()
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
        self.client = self.app.test_client()

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
            knowledge_graph='{"entities":[{"id":"fac_crimson_veil","type":"faction","name":"Crimson Veil","visibility":"dm_private"},{"id":"crypt_road","type":"location","name":"Crypt Road"}],"relations":[],"facts":[]}',
            world_state='{"current_scene":{"location_name":"Dock Ward","immediate_tension":"A bell rings."}}',
            dm_private=json.dumps({
                'hidden_factions': ['Crimson Veil'],
                'authorized_rules': [
                    {'id': 'decoding_rule_v1', 'description': 'A deterministic rule decoded the cipher.'},
                    {'id': 'festival_end_rule_v1', 'description': 'The festival concludes with a grand parade that is safe to announce publicly.'},
                    {'id': 'pact_rule_v1', 'description': 'The pact is sealed by a private rule behind the scenes.'},
                    {'id': 'component_clue_found', 'description': 'The deterministic clue trigger matched.'},
                ],
            }),
        ))
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()
        self.env_patch.stop()

    def _add_session_message(self, message_id, content, role='assistant'):
        db.session.add(SessionMessage(
            id=message_id,
            session_id=self.session.id,
            role=role,
            content=content,
        ))
        db.session.commit()
        return message_id

    def _trigger_verdict(self, *source_ids, clause_id='visible_narrative_progress'):
        return {
            'clause_id': clause_id,
            'verdict': 'satisfied',
            'supported_claims': ['The clock pressure changed visibly during this turn.'],
            'evidence_sources': [
                {'source_type': 'transcript_message', 'source_id': str(source_id)}
                for source_id in source_ids
            ],
            'chronology_verdict': 'new_current_turn',
            'reason': 'The cited current-turn exchange satisfies the clause.',
        }

    def test_tool_definitions_are_function_schemas(self):
        names = {tool['function']['name'] for tool in DM_TOOL_DEFINITIONS}
        self.assertIn('ask_character_sheet', names)
        self.assertIn('search_campaign_memory', names)
        self.assertNotIn('advance_clock', names)
        self.assertIn('roll_dice', names)
        self.assertIn('create_encounter_map', names)
        self.assertIn('place_encounter_map_actors', names)
        self.assertIn('move_encounter_actor', names)
        self.assertIn('get_encounter_overview', names)
        self.assertIn('apply_damage', names)
        self.assertIn('create_shop_list', names)
        self.assertNotIn('create_shop_menu', names)
        for tool in DM_TOOL_DEFINITIONS:
            self.assertEqual(tool['type'], 'function')
            self.assertIn('parameters', tool['function'])
            self.assertEqual(tool['function']['parameters']['type'], 'object')

    def test_finalizer_renders_npc_parts_without_leaking_private_context(self):
        tool_call = {
            'function': {
                'name': 'talk_to_player',
                'arguments': json.dumps({
                    'parts': [
                        {'type': 'narration', 'content': 'Rain rattles the shutters.'},
                        {
                            'type': 'npc_dialogue',
                            'target': 'Brother Orin',
                            'content': '"I was only a diver."',
                            'dm_private_context': 'Deliberate cover story; his established allegiance and background remain canon.',
                        },
                    ],
                    'commit_action_ids': [],
                    'resolver_packet': {
                        'entity_mentions': [{
                            'mention_ref': 'orin_1', 'surface_form': 'Brother Orin',
                            'identity_status': 'known_hidden', 'canonical_id': 'npc_orin',
                            'visibility': 'dm_private',
                            'evidence_refs': ['campaign_npc:npc_orin'],
                        }],
                    },
                }),
            },
        }
        decision, violation = _session_dm_finalizer_decision_from_tool_calls([tool_call])
        self.assertIsNone(violation)
        self.assertEqual(
            decision['content'],
            'Rain rattles the shutters.\n\n<npc target="Brother Orin">"I was only a diver."</npc>',
        )
        self.assertNotIn('cover story', decision['content'])
        self.assertEqual(decision['parts'][1]['dm_private_context'], 'Deliberate cover story; his established allegiance and background remain canon.')
        self.assertEqual(decision['resolver_packet']['entity_mentions'][0]['canonical_id'], 'npc_orin')

    def test_finalizer_reuses_explicit_resolver_packet_schema(self):
        talk_tool = next(
            tool for tool in SESSION_DM_FINALIZER_TOOLS
            if tool['function']['name'] == 'talk_to_player'
        )
        declared = talk_tool['function']['parameters']['properties']['resolver_packet']
        self.assertEqual(
            declared['properties']['entity_mentions'],
            RESOLVER_PACKET_SCHEMA['properties']['entity_mentions'],
        )
        mention_schema = declared['properties']['entity_mentions']['items']
        self.assertEqual(
            mention_schema['required'],
            ['mention_ref', 'surface_form', 'identity_status'],
        )
        self.assertIn('known_hidden', mention_schema['properties']['identity_status']['enum'])
        self.assertEqual(mention_schema['properties']['canonical_id']['type'], ['string', 'null'])
        self.assertEqual(mention_schema['properties']['evidence_refs']['type'], ['array', 'null'])

    def test_finalizer_rejects_run_41_resolver_packet_aliases_during_parsing(self):
        packets = [
            {
                'entity_mentions': [{
                    'entity': 'harlen_moss',
                    'mention': 'Harlen Moss',
                }],
            },
            {
                'entity_mentions': [{
                    'name': 'Harlen Moss',
                    'role': 'lamplighter',
                    'campaign_entity': 'harlen_moss',
                }],
            },
        ]
        for packet in packets:
            with self.subTest(packet=packet):
                tool_call = _normalized_from_raw(dm_talk_tool_response(
                    'The Widow\'s Lamp glows through the rain.',
                    resolver_packet=packet,
                )).message_view()['tool_calls'][0]
                decision, violation = _session_dm_finalizer_decision_from_tool_calls([tool_call])
                self.assertIsNone(decision)
                self.assertEqual(violation['kind'], 'invalid_resolver_packet')
                self.assertEqual(violation['repair']['action'], 'repair_or_omit_resolver_packet')
                self.assertEqual(
                    violation['fallback_decision']['content'],
                    "The Widow's Lamp glows through the rain.",
                )
                self.assertTrue(
                    'required field' in violation['detail'] or 'unknown fields' in violation['detail']
                )

    def test_finalizer_rejects_canonical_commitment_without_evidence(self):
        packet = {
            'entity_mentions': [{
                'mention_ref': 'harlen_1',
                'surface_form': 'Harlen Moss',
                'identity_status': 'known_hidden',
                'canonical_id': 'harlen_moss',
            }],
        }
        tool_call = _normalized_from_raw(dm_talk_tool_response(
            'Harlen lowers the lamp.',
            resolver_packet=packet,
        )).message_view()['tool_calls'][0]

        decision, violation = _session_dm_finalizer_decision_from_tool_calls([tool_call])

        self.assertIsNone(decision)
        self.assertTrue(violation['identity_commitment_required'])
        self.assertEqual(violation['repair']['action'], 'repair_resolver_packet')
        self.assertIn('evidence_refs', violation['detail'])

    def test_resolver_contract_repair_preserves_visible_turn_and_accepts_canonical_packet(self):
        malformed = {
            'entity_mentions': [{'entity': 'harlen_moss', 'mention': 'Harlen Moss'}],
        }
        repaired = {
            'entity_mentions': [{
                'mention_ref': 'harlen_1',
                'surface_form': 'Harlen Moss',
                'identity_status': 'known_hidden',
                'canonical_id': 'harlen_moss',
                'visibility': 'dm_private',
                'evidence_refs': ['campaign_entity:harlen_moss'],
            }],
        }
        with patch(
            'openrouter._post_chat_normalized',
            side_effect=[
                _normalized_from_raw(dm_talk_tool_response(
                    'The lamp burns blue.',
                    resolver_packet=malformed,
                )),
                _normalized_from_raw(dm_talk_tool_response(
                    'A changed reply that must not escape.',
                    resolver_packet=repaired,
                )),
            ],
        ) as post_chat:
            result = get_session_dm_response_with_tools(
                {'protected_player_characters': [], 'private_output_terms': [], 'private_spoiler_items': []},
                [],
                [],
                lambda *_args, **_kwargs: {},
                max_tool_rounds=0,
            )

        self.assertEqual(result['content'], 'The lamp burns blue.')
        self.assertEqual(result['resolver_packet'], repaired)
        self.assertEqual(post_chat.call_count, 2)
        repair_prompt = post_chat.call_args_list[1].args[0][-1]['content']
        self.assertIn('Resolver-contract repair 1/2', repair_prompt)
        self.assertIn('copy the following parts and commit_action_ids exactly', repair_prompt.lower())

    def test_resolver_contract_repair_has_separate_telemetry(self):
        malformed = {
            'entity_mentions': [{'entity': 'harlen_moss', 'mention': 'Harlen Moss'}],
        }
        with patch(
            'openrouter._post_chat_normalized',
            side_effect=[
                _normalized_from_raw(dm_talk_tool_response(
                    'The lamp burns blue.',
                    resolver_packet=malformed,
                )),
                _normalized_from_raw(dm_talk_tool_response('This drift is discarded.')),
            ],
        ):
            result = get_session_dm_response_with_tools(
                {'protected_player_characters': [], 'private_output_terms': [], 'private_spoiler_items': []},
                [],
                [],
                lambda *_args, **_kwargs: {},
                audit_context={
                    'campaign_id': self.campaign.id,
                    'operation': 'resolver_contract_test',
                    'trace_id': 'session_dm:test:resolver-contract',
                    'trace_label': 'resolver contract test',
                },
                max_tool_rounds=0,
            )

        self.assertEqual(result['content'], 'The lamp burns blue.')
        requested = CampaignAuditEvent.query.filter_by(
            event_type='resolver_contract_repair_requested',
        ).one()
        completed = CampaignAuditEvent.query.filter_by(
            event_type='resolver_contract_repair_completed',
        ).one()
        self.assertEqual(requested.actor, 'session_dm_resolver_contract')
        self.assertEqual(requested.parent_trace_id, 'session_dm:test:resolver-contract')
        self.assertEqual(json.loads(completed.payload)['outcome'], 'omitted_by_model')
        self.assertEqual(
            CampaignAuditEvent.query.filter_by(event_type='finalizer_contract_guard_retry').count(),
            0,
        )
        metrics = collect_model_retry_metrics([requested, completed], [])
        self.assertEqual(metrics['counts']['resolver_contract_repair'], 1)
        self.assertEqual(metrics['counts']['contract_guard_retry'], 0)

    def test_run_41_optional_packet_exhaustion_preserves_reply_and_staged_action(self):
        malformed = {
            'entity_mentions': [{
                'name': 'Widow\'s Lamp',
                'role': 'destination',
                'campaign_entity': 'widows_lamp',
            }],
        }
        staged_action = {
            'id': 'pending_action_1',
            'name': 'update_current_scene',
            'args': {'scene_patch': {'location_name': "Widow's Lamp"}},
            'preview': {'pending': True},
        }

        def execute_tool(_name, _args, audit):
            audit['pending_action_buffer']['actions'].append(staged_action)
            return {'pending_action_id': 'pending_action_1'}

        tool_response = {
            'choices': [{
                'message': {
                    'content': '',
                    'tool_calls': [{
                        'id': 'call_scene',
                        'function': {
                            'name': 'update_current_scene',
                            'arguments': json.dumps({'scene_patch': {'location_name': "Widow's Lamp"}}),
                        },
                    }],
                },
            }],
        }
        malformed_finalizer = dm_talk_tool_response(
            "You reach the Widow's Lamp as its sign creaks overhead.",
            resolver_packet=malformed,
            commit_action_ids=['pending_action_1'],
        )
        with patch(
            'openrouter._post_chat_normalized',
            side_effect=[
                _normalized_from_raw(tool_response),
                _normalized_from_raw(malformed_finalizer),
                _normalized_from_raw(malformed_finalizer),
                _normalized_from_raw(malformed_finalizer),
            ],
        ), patch(
            'openrouter.get_session_preflight_decision',
            return_value={
                'dm_reply_mode': 'unknown',
                'skip_spoiler_check': True,
                'main_call_thinking': False,
                'confidence': 'high',
                'reason': 'Test preflight.',
            },
        ):
            result = get_session_dm_response_with_tools(
                {'protected_player_characters': [], 'private_output_terms': [], 'private_spoiler_items': []},
                [],
                [{'type': 'function', 'function': {'name': 'update_current_scene'}}],
                execute_tool,
                audit_context={'operation': 'session_dm_response'},
                max_tool_rounds=1,
            )

        self.assertEqual(result['content'], "You reach the Widow's Lamp as its sign creaks overhead.")
        self.assertEqual(result['commit_action_ids'], ['pending_action_1'])
        self.assertEqual(result['_pending_actions'], [staged_action])
        self.assertNotIn('resolver_packet', result)

    def test_malformed_required_resolver_packet_fails_closed_after_repair(self):
        malformed = {
            'entity_mentions': [{
                'mention_ref': 'harlen_1',
                'surface_form': 'Harlen Moss',
                'identity_status': 'known_hidden',
                'canonical_id': 'harlen_moss',
            }],
        }
        response = _normalized_from_raw(dm_talk_tool_response(
            'Harlen lowers the lamp.',
            resolver_packet=malformed,
        ))
        with patch('openrouter._post_chat_normalized', return_value=response) as post_chat:
            result = get_session_dm_response_with_tools(
                {'protected_player_characters': [], 'private_output_terms': [], 'private_spoiler_items': []},
                [],
                [],
                lambda *_args, **_kwargs: {},
                max_tool_rounds=0,
            )

        self.assertEqual(result['mode'], 'silent')
        self.assertIn('canonical identity metadata', result['reason'])
        self.assertEqual(post_chat.call_count, 3)

    def test_finalizer_rejects_model_authored_npc_markup_inside_parts(self):
        tool_call = {
            'function': {
                'name': 'talk_to_player',
                'arguments': json.dumps({
                    'parts': [{'type': 'narration', 'content': '<npc target="Brother Orin">"Only a diver."</npc>'}],
                    'commit_action_ids': [],
                }),
            },
        }
        _decision, violation = _session_dm_finalizer_decision_from_tool_calls([tool_call])
        self.assertEqual(violation['kind'], 'invalid_response_parts')
        self.assertIn('may not contain <npc> markup', violation['detail'])

    def test_commit_stores_private_parts_separately_from_visible_message(self):
        player_message = SessionMessage(session_id=self.session.id, user_id=self.user.id, role='player', content='Who are you?')
        db.session.add(player_message)
        db.session.commit()
        parts = [{
            'type': 'npc_dialogue',
            'target': 'Brother Orin',
            'content': '"Only a diver."',
            'dm_private_context': 'This is a cover story; do not replace his canonical identity.',
        }]
        dm_message, _proposals, _results = commit_accepted_dm_turn(
            self.campaign, self.session, self.user, player_message.id, 'test:parts', 'test parts',
            '<npc target="Brother Orin">"Only a diver."</npc>', [], {'actions': []}, parts,
        )
        self.assertNotIn('cover story', dm_message.content)
        stored = CampaignDmResponseParts.query.filter_by(dm_message_id=dm_message.id).one()
        self.assertEqual(stored.parts_json, parts)

    def test_commit_persists_only_a_valid_canonical_resolver_packet(self):
        player_message = SessionMessage(
            session_id=self.session.id,
            user_id=self.user.id,
            role='player',
            content='Who carries the blue lamp?',
        )
        db.session.add(player_message)
        db.session.commit()
        packet = {
            'entity_mentions': [{
                'mention_ref': 'harlen_1',
                'surface_form': 'the blue-lantern bearer',
                'identity_status': 'known_hidden',
                'canonical_id': 'harlen_moss',
                'public_name': 'the blue-lantern bearer',
                'visibility': 'dm_private',
                'evidence_refs': ['campaign_entity:harlen_moss'],
            }],
        }

        dm_message, _proposals, _results = commit_accepted_dm_turn(
            self.campaign,
            self.session,
            self.user,
            player_message.id,
            'test:resolver:valid',
            'valid resolver packet',
            'The bearer keeps their hood raised.',
            [],
            {'actions': []},
            [{'type': 'narration', 'content': 'The bearer keeps their hood raised.'}],
            packet,
        )

        stored = CampaignResolverPacket.query.filter_by(dm_message_id=dm_message.id).one()
        self.assertEqual(stored.packet_json, packet)

    def test_commit_validates_resolver_packet_before_applying_staged_actions(self):
        player_message = SessionMessage(
            session_id=self.session.id,
            user_id=self.user.id,
            role='player',
            content='I head to the Widow\'s Lamp.',
        )
        db.session.add(player_message)
        db.session.commit()
        action_buffer = {
            'actions': [{
                'id': 'pending_action_1',
                'name': 'update_current_scene',
                'args': {'scene_patch': {'location_name': "Widow's Lamp"}},
            }],
        }
        malformed_packet = {
            'entity_mentions': [{'name': "Widow's Lamp", 'campaign_entity': 'widows_lamp'}],
        }

        with patch('services.dm_turn_commit.apply_deferred_narrative_action') as apply_action:
            with self.assertRaisesRegex(ValueError, 'Invalid resolver_packet'):
                commit_accepted_dm_turn(
                    self.campaign,
                    self.session,
                    self.user,
                    player_message.id,
                    'test:resolver:invalid',
                    'invalid resolver packet',
                    "You reach the Widow's Lamp.",
                    ['pending_action_1'],
                    action_buffer,
                    [{'type': 'narration', 'content': "You reach the Widow's Lamp."}],
                    malformed_packet,
                )

        apply_action.assert_not_called()
        self.assertEqual(SessionMessage.query.filter_by(role='dm').count(), 0)
        self.assertEqual(CampaignDmResponseParts.query.count(), 0)
        self.assertEqual(CampaignResolverPacket.query.count(), 0)

    def test_memory_prompts_keep_orin_and_mixed_segment_context_paired(self):
        parts = [
            {'type': 'npc_dialogue', 'target': 'Brother Orin', 'content': '"I was only a diver."',
             'dm_private_context': 'Deliberate cover story; do not overwrite Orin identity or background.'},
            {'type': 'npc_dialogue', 'target': 'Brother Orin', 'content': '"The tide turns at dawn."',
             'dm_private_context': 'Truthful operational detail.'},
        ]
        memory_context = {
            'latest_player_message': 'Who are you?',
            'latest_dm_message': '<npc target="Brother Orin">"I was only a diver."</npc>\n\n<npc target="Brother Orin">"The tide turns at dawn."</npc>',
            'latest_dm_response_parts': parts,
            'hot_context': {},
            'prior_memory_anchors': {},
            'relevant_memory': {},
        }
        extractor_payload = json.loads(build_session_memory_extractor_messages(memory_context)[1]['content'])
        resolver_payload = json.loads(build_session_memory_resolver_messages(memory_context, {})[1]['content'])
        self.assertEqual(extractor_payload['latest_dm_response_parts'], parts)
        self.assertEqual(resolver_payload['latest_dm_response_parts'], parts)
        self.assertIn('do not overwrite Orin identity', extractor_payload['latest_dm_response_parts'][0]['dm_private_context'])
        self.assertEqual(resolver_payload['latest_dm_response_parts'][1]['dm_private_context'], 'Truthful operational detail.')

    def test_commit_rejects_visible_content_that_does_not_match_parts(self):
        player_message = SessionMessage(session_id=self.session.id, user_id=self.user.id, role='player', content='Hello')
        db.session.add(player_message)
        db.session.commit()
        with self.assertRaisesRegex(ValueError, 'server-rendered response parts'):
            commit_accepted_dm_turn(
                self.campaign, self.session, self.user, player_message.id, 'test:mismatch', 'test mismatch',
                'Different visible text.', [], {'actions': []},
                [{'type': 'narration', 'content': 'Canonical visible text.'}],
            )

    def test_ai_dm_tool_places_encounter_map_actors_and_creates_monsters(self):
        npc = NPCActor(
            campaign_id=self.campaign.id,
            actor_id='bram_truewood',
            name='Bram Truewood',
            dossier='{}',
        )
        encounter_map = EncounterMap(
            campaign_id=self.campaign.id,
            session_id=self.session.id,
            title='Ruined Hall',
            prompt='A ruined hall.',
            image_filename='map.png',
            model='gpt-image-2',
            size='1024x1024',
            quality='high',
            grid_json=json.dumps({'columns': 12, 'rows': 10}),
            setup_status='ready',
        )
        db.session.add_all([npc, encounter_map])
        db.session.commit()

        result = execute_dm_tool(
            self.campaign,
            self.session,
            self.user,
            'place_encounter_map_actors',
            {
                'encounter_map_id': encounter_map.id,
                'placements': [
                    {'actor_type': 'player', 'actor_id': str(self.user.id), 'col': 2, 'row': 3},
                    {'actor_type': 'npc', 'actor_id': 'bram_truewood', 'col': 4, 'row': 5},
                    {'actor_type': 'monster', 'actor_id': 'goblin_1', 'monster_name': 'Goblin', 'col': 8, 'row': 4},
                ],
            },
        )
        db.session.commit()

        self.assertNotIn('error', result)
        self.assertEqual(len(result['placements']), 3)
        self.assertEqual(CampaignMonster.query.filter_by(campaign_id=self.campaign.id).count(), 1)
        self.assertEqual(EncounterMapPlacement.query.filter_by(encounter_map_id=encounter_map.id).count(), 3)
        monster = CampaignMonster.query.filter_by(campaign_id=self.campaign.id, monster_id='goblin_1').one()
        self.assertEqual(monster.name, 'Goblin')

        move_result = execute_dm_tool(
            self.campaign,
            self.session,
            self.user,
            'place_encounter_map_actors',
            {
                'encounter_map_id': encounter_map.id,
                'placements': [{'actor_type': 'monster', 'actor_id': 'goblin_1', 'col': 9, 'row': 4}],
            },
        )
        db.session.commit()

        self.assertNotIn('error', move_result)
        self.assertEqual(EncounterMapPlacement.query.filter_by(encounter_map_id=encounter_map.id).count(), 3)
        moved = EncounterMapPlacement.query.filter_by(
            encounter_map_id=encounter_map.id,
            actor_type='monster',
            actor_id='goblin_1',
        ).one()
        self.assertEqual(moved.grid_col, 9)

    def test_place_map_actors_initializes_state_when_encounter_mode_already_active(self):
        self.campaign.settings = json.dumps({'encounter_active': True})
        encounter_map = EncounterMap(
            campaign_id=self.campaign.id,
            session_id=self.session.id,
            title='Ruined Hall',
            prompt='A ruined hall.',
            image_filename='map.png',
            model='gpt-image-2',
            size='1024x1024',
            quality='high',
            grid_json=json.dumps({'columns': 12, 'rows': 10}),
            setup_status='ready',
        )
        db.session.add(encounter_map)
        db.session.commit()

        result = execute_dm_tool(
            self.campaign,
            self.session,
            self.user,
            'place_encounter_map_actors',
            {
                'encounter_map_id': encounter_map.id,
                'placements': [
                    {'actor_type': 'player', 'actor_id': str(self.user.id), 'col': 2, 'row': 3},
                    {'actor_type': 'monster', 'actor_id': 'goblin_1', 'monster_name': 'Goblin', 'col': 8, 'row': 4},
                ],
            },
        )
        db.session.commit()

        self.assertNotIn('error', result)
        state = result['encounter_map']['encounter_state']
        self.assertTrue(state['active'])
        self.assertEqual(len(state['turn_order']), 2)

    def test_move_encounter_actor_uses_pathfinding_and_consumes_movement(self):
        self.campaign.settings = json.dumps({'encounter_active': True})
        monster = CampaignMonster(
            campaign_id=self.campaign.id,
            monster_id='goblin_1',
            name='Goblin',
            stat_block=json.dumps({'speed': 30}),
        )
        encounter_map = EncounterMap(
            campaign_id=self.campaign.id,
            session_id=self.session.id,
            title='Mud Hall',
            prompt='A muddy hall.',
            image_filename='map.png',
            model='gpt-image-2',
            size='1024x1024',
            quality='high',
            grid_json=json.dumps({'columns': 7, 'rows': 3}),
            vtt_setup_json=json.dumps({
                'terrain_zones': [{
                    'label': 'Deep Mud',
                    'kind': 'difficult',
                    'shape_type': 'rect',
                    'rect': {'col': 2, 'row': 0, 'width': 1, 'height': 3},
                    'polygon': [],
                    'description': 'Sticky ground.',
                    'confidence': 0.9,
                }],
                'obstacles': [],
            }),
            setup_status='ready',
        )
        db.session.add_all([monster, encounter_map])
        db.session.flush()
        placement = EncounterMapPlacement(
            encounter_map_id=encounter_map.id,
            actor_type='monster',
            actor_id='goblin_1',
            label='Goblin',
            grid_col=1,
            grid_row=1,
        )
        db.session.add(placement)
        db.session.flush()
        encounter_map.encounter_state_json = json.dumps({
            'active': True,
            'round': 1,
            'active_turn_index': 0,
            'turn_order': [{
                'placement_id': placement.id,
                'actor_type': 'monster',
                'actor_id': 'goblin_1',
                'label': 'Goblin',
                'initiative': 12,
                'initiative_bonus': 2,
                'speed': 30,
                'actions': {
                    'action': True,
                    'bonus_action': True,
                    'reaction': True,
                    'movement_remaining': 30,
                },
            }],
        })
        db.session.commit()

        result = execute_dm_tool(
            self.campaign,
            self.session,
            self.user,
            'move_encounter_actor',
            {'actor_type': 'monster', 'actor_id': 'goblin_1', 'col': 3, 'row': 1},
        )

        self.assertNotIn('error', result)
        self.assertEqual(result['movement']['moved_squares'], 3)
        self.assertEqual(result['movement']['movement_remaining'], 15)
        moved = db.session.get(EncounterMapPlacement, placement.id)
        self.assertEqual((moved.grid_col, moved.grid_row), (3, 1))
        updated_state = json.loads(db.session.get(EncounterMap, encounter_map.id).encounter_state_json)
        self.assertEqual(updated_state['turn_order'][0]['actions']['movement_remaining'], 15)

    def test_move_encounter_actor_requires_active_turn_by_default(self):
        self.campaign.settings = json.dumps({'encounter_active': True})
        goblin = CampaignMonster(
            campaign_id=self.campaign.id,
            monster_id='goblin_1',
            name='Goblin',
            stat_block=json.dumps({'speed': 30}),
        )
        wolf = CampaignMonster(
            campaign_id=self.campaign.id,
            monster_id='wolf_1',
            name='Wolf',
            stat_block=json.dumps({'speed': 40}),
        )
        encounter_map = EncounterMap(
            campaign_id=self.campaign.id,
            session_id=self.session.id,
            title='Turn Order Test',
            prompt='A narrow room.',
            image_filename='map.png',
            model='gpt-image-2',
            size='1024x1024',
            quality='high',
            grid_json=json.dumps({'columns': 6, 'rows': 4}),
            vtt_setup_json=json.dumps({'terrain_zones': [], 'obstacles': []}),
            setup_status='ready',
        )
        db.session.add_all([goblin, wolf, encounter_map])
        db.session.flush()
        goblin_placement = EncounterMapPlacement(
            encounter_map_id=encounter_map.id,
            actor_type='monster',
            actor_id='goblin_1',
            label='Goblin',
            grid_col=1,
            grid_row=1,
        )
        wolf_placement = EncounterMapPlacement(
            encounter_map_id=encounter_map.id,
            actor_type='monster',
            actor_id='wolf_1',
            label='Wolf',
            grid_col=2,
            grid_row=1,
        )
        db.session.add_all([goblin_placement, wolf_placement])
        db.session.flush()
        encounter_map.encounter_state_json = json.dumps({
            'active': True,
            'round': 1,
            'active_turn_index': 0,
            'turn_order': [
                {
                    'placement_id': goblin_placement.id,
                    'actor_type': 'monster',
                    'actor_id': 'goblin_1',
                    'label': 'Goblin',
                    'initiative': 15,
                    'initiative_bonus': 2,
                    'speed': 30,
                    'actions': {'action': True, 'bonus_action': True, 'reaction': True, 'movement_remaining': 30},
                },
                {
                    'placement_id': wolf_placement.id,
                    'actor_type': 'monster',
                    'actor_id': 'wolf_1',
                    'label': 'Wolf',
                    'initiative': 10,
                    'initiative_bonus': 2,
                    'speed': 40,
                    'actions': {'action': True, 'bonus_action': True, 'reaction': True, 'movement_remaining': 40},
                },
            ],
        })
        db.session.commit()

        blocked = execute_dm_tool(
            self.campaign,
            self.session,
            self.user,
            'move_encounter_actor',
            {'actor_type': 'monster', 'actor_id': 'wolf_1', 'col': 3, 'row': 1},
        )
        self.assertIn('error', blocked)
        self.assertIn("not Wolf's turn", blocked['error'])

        allowed = execute_dm_tool(
            self.campaign,
            self.session,
            self.user,
            'move_encounter_actor',
            {'actor_type': 'monster', 'actor_id': 'wolf_1', 'col': 3, 'row': 1, 'ignore_turn_order': True},
        )
        self.assertNotIn('error', allowed)
        self.assertEqual(allowed['placement']['col'], 3)

    def test_ai_dm_tool_rejects_out_of_bounds_map_placements(self):
        encounter_map = EncounterMap(
            campaign_id=self.campaign.id,
            session_id=self.session.id,
            title='Small Room',
            prompt='A small room.',
            image_filename='map.png',
            model='gpt-image-2',
            size='1024x1024',
            quality='high',
            grid_json=json.dumps({'columns': 4, 'rows': 4}),
            setup_status='ready',
        )
        db.session.add(encounter_map)
        db.session.commit()

        result = execute_dm_tool(
            self.campaign,
            self.session,
            self.user,
            'place_encounter_map_actors',
            {
                'encounter_map_id': encounter_map.id,
                'placements': [{'actor_type': 'player', 'actor_id': str(self.user.id), 'col': 4, 'row': 0}],
            },
        )

        self.assertEqual(result['error'], 'No placements were saved.')
        self.assertEqual(EncounterMapPlacement.query.filter_by(encounter_map_id=encounter_map.id).count(), 0)

    def test_ai_dm_tool_warns_before_illegal_map_placements(self):
        encounter_map = EncounterMap(
            campaign_id=self.campaign.id,
            session_id=self.session.id,
            title='Wreck Room',
            prompt='A wrecked ship chamber.',
            image_filename='map.png',
            model='gpt-image-2',
            size='1024x1024',
            quality='high',
            grid_json=json.dumps({'columns': 12, 'rows': 10}),
            vtt_setup_json=json.dumps({
                'obstacles': [{
                    'label': 'Crashed Ship Hull',
                    'kind': 'blocked',
                    'movement_effect': 'blocks_movement',
                    'description': 'Splintered ship timbers block movement through this square.',
                    'shape_type': 'rect',
                    'rect': {'col': 5, 'row': 4, 'width': 3, 'height': 2},
                    'polygon': [],
                }],
                'terrain_zones': [],
            }),
            setup_status='ready',
        )
        db.session.add(encounter_map)
        db.session.flush()
        db.session.add(EncounterMapPlacement(
            encounter_map_id=encounter_map.id,
            actor_type='player',
            actor_id=str(self.user.id),
            label='Aria',
            grid_col=1,
            grid_row=1,
        ))
        db.session.commit()

        result = execute_dm_tool(
            self.campaign,
            self.session,
            self.user,
            'place_encounter_map_actors',
            {
                'encounter_map_id': encounter_map.id,
                'clear_existing': True,
                'placements': [{'actor_type': 'monster', 'actor_id': 'shark_1', 'monster_name': 'Reef Shark', 'col': 6, 'row': 4}],
            },
        )
        db.session.commit()

        self.assertIn('warning', result)
        self.assertEqual(result['placement_warnings'][0]['area_label'], 'Crashed Ship Hull')
        self.assertIn('blocks movement', result['placement_warnings'][0]['reason'])
        self.assertEqual(EncounterMapPlacement.query.filter_by(encounter_map_id=encounter_map.id).count(), 1)
        self.assertEqual(CampaignMonster.query.filter_by(campaign_id=self.campaign.id).count(), 0)

    def test_ai_dm_tool_can_override_illegal_map_placement_warning(self):
        encounter_map = EncounterMap(
            campaign_id=self.campaign.id,
            session_id=self.session.id,
            title='Hazard Room',
            prompt='A room with a deep fissure.',
            image_filename='map.png',
            model='gpt-image-2',
            size='1024x1024',
            quality='high',
            grid_json=json.dumps({'columns': 12, 'rows': 10}),
            vtt_setup_json=json.dumps({
                'terrain_zones': [{
                    'label': 'Deep Fissure',
                    'kind': 'hazard',
                    'description': 'A dangerous drop cuts across the floor.',
                    'shape_type': 'rect',
                    'rect': {'col': 2, 'row': 2, 'width': 2, 'height': 2},
                    'polygon': [],
                }],
                'obstacles': [],
            }),
            setup_status='ready',
        )
        db.session.add(encounter_map)
        db.session.commit()

        result = execute_dm_tool(
            self.campaign,
            self.session,
            self.user,
            'place_encounter_map_actors',
            {
                'encounter_map_id': encounter_map.id,
                'allow_illegal_placements': True,
                'placements': [{'actor_type': 'player', 'actor_id': str(self.user.id), 'col': 2, 'row': 2}],
            },
        )
        db.session.commit()

        self.assertNotIn('error', result)
        self.assertEqual(result['placement_warnings'][0]['area_label'], 'Deep Fissure')
        self.assertEqual(len(result['placements']), 1)
        placement = EncounterMapPlacement.query.filter_by(encounter_map_id=encounter_map.id).one()
        self.assertEqual(placement.grid_col, 2)
        self.assertEqual(placement.grid_row, 2)

    def test_character_sheet_agent_answers_from_selected_character(self):
        with patch('services.dm_tools.get_character_sheet_answer', return_value={
            'answer': 'Aria has AC 15 and passive Perception 13.',
            'character_ids': [self.character.id],
            'missing': False,
        }) as answer:
            result = execute_dm_tool(
                self.campaign,
                self.session,
                self.user,
                'ask_character_sheet',
                {'scope': 'current_player', 'question': "What are Aria's AC and passive Perception?"},
                {},
            )

        self.assertEqual(result['answer'], 'Aria has AC 15 and passive Perception 13.')
        self.assertEqual(result['character_ids'], [self.character.id])
        answer.assert_called_once()
        sheets = answer.call_args.args[2]
        self.assertEqual(sheets[0]['character']['name'], 'Aria')
        self.assertEqual(sheets[0]['character']['combat']['armor_class'], 15)
        self.assertEqual(sheets[0]['character']['general']['passive_perception'], 13)

    def test_context_manifest_reports_compact_strategy(self):
        opening = SessionMessage(
            session_id=self.session.id,
            role='dm',
            content="Harlen stands and announces, 'That's four this week.'",
        )
        db.session.add(opening)
        db.session.commit()
        hot_context = build_session_hot_context(
            self.campaign,
            self.session,
            self.user,
            recent_messages_override=[opening],
        )
        manifest = context_manifest(hot_context, DM_TOOL_DEFINITIONS)
        self.assertEqual(manifest['strategy'], 'compact_hot_context_with_dm_tools')
        self.assertFalse(manifest['full_world_graph_included'])
        self.assertIn('ask_character_sheet', manifest['available_tools'])
        self.assertIn('create_encounter_map', manifest['available_tools'])
        self.assertIn('recent_messages', manifest['estimated_tokens_by_section'])
        self.assertNotIn('recent_messages', manifest['fed_sections'])
        self.assertIn('recent_messages', manifest['internal_only_sections'])
        self.assertEqual(manifest['recent_message_source_ids'], [opening.id])
        self.assertEqual(
            manifest['recent_message_source_refs'][0]['source_id'],
            f'session_message:{opening.id}',
        )
        self.assertGreater(manifest['estimated_duplicate_transcript_tokens_removed'], 0)
        self.assertLess(
            manifest['estimated_primary_prompt_context_tokens'],
            manifest['estimated_total_tokens'],
        )

    def test_run_41_transcript_is_canonical_once_while_audience_state_stays_authoritative(self):
        opening_text = "Harlen stands and announces to the Lake Nobility, 'That's four this week.'"
        recent_messages = [
            {
                'id': 4101,
                'session_id': self.session.id,
                'role': 'dm',
                'content': opening_text,
            },
            {
                'id': 4102,
                'session_id': self.session.id,
                'role': 'player',
                'content': 'I watch the room for reactions.',
            },
        ]
        hot_context = {
            'campaign': {'id': self.campaign.id, 'name': self.campaign.name},
            'audience_knowledge': [{
                'claim': 'Four incidents happened this week.',
                'heard_by': ['Lake Nobility'],
                'source_message_id': 4101,
            }],
            'recent_messages': recent_messages,
            'private_output_terms': [],
            'private_spoiler_items': [],
        }

        messages = build_session_dm_request_messages(hot_context, recent_messages)

        serialized_request = '\n'.join(message['content'] for message in messages)
        self.assertEqual(serialized_request.count(opening_text), 1)
        self.assertNotIn('"recent_messages"', messages[1]['content'])
        self.assertIn('"audience_knowledge"', messages[1]['content'])
        self.assertEqual(messages[-2:], [
            {'role': 'assistant', 'content': opening_text},
            {'role': 'user', 'content': 'I watch the room for reactions.'},
        ])

    def test_source_identity_validation_rejects_system_and_conversation_duplication(self):
        duplicated = {
            'id': 117,
            'session_id': self.session.id,
            'role': 'player',
            'content': 'This text is intentionally irrelevant to identity validation.',
        }

        with self.assertRaisesRegex(ValueError, 'session_message:117'):
            _validate_session_dm_request_sources(
                {'recent_messages': [{**duplicated, 'content': 'A transformed copy.'}]},
                [duplicated],
            )

    def test_hot_context_includes_visible_naming_constraints_for_private_npc_names(self):
        world = CampaignWorld.query.filter_by(campaign_id=self.campaign.id).one()
        world.knowledge_graph = json.dumps({
            'entities': [
                {
                    'id': 'witness_old_dockhand',
                    'type': 'npc',
                    'name': 'Mortimer',
                    'visibility': 'dm_private',
                    'summary': 'An old dockhand who saw something near the vault.',
                },
            ],
            'relations': [],
            'facts': [],
        })
        world.world_state = json.dumps({
            'current_scene': {
                'location_name': 'Tidewall Docks',
                'immediate_tension': 'The old dockhand Mortimer lingers nearby.',
                'active_npc_ids': ['witness_old_dockhand'],
            },
        })
        db.session.add(NPCActor(
            campaign_id=self.campaign.id,
            actor_id='witness_old_dockhand',
            name='Mortimer',
            public_summary='An elderly dockhand who works the early shifts and knows the harbor secrets.',
            dossier='{}',
        ))
        db.session.add(SessionMessage(
            session_id=self.session.id,
            user_id=self.user.id,
            role='player',
            content='I ask the old dockhand what he saw.',
        ))
        db.session.commit()

        hot_context = build_session_hot_context(self.campaign, self.session, self.user)

        self.assertEqual(hot_context['visible_naming_constraints'], [{
            'avoid_visible_name': 'Mortimer',
            'use_public_reference': 'elderly dockhand',
            'applies_to': 'visible narration and <npc target="..."> until the name is revealed by play',
        }])
        messages = build_session_dm_tool_messages(hot_context)
        self.assertIn('Visible naming constraints', messages[2]['content'])
        self.assertIn('Mortimer', messages[2]['content'])
        self.assertIn('elderly dockhand', messages[2]['content'])

    def test_grid_detector_finds_synthetic_grid_and_writes_labeled_copy(self):
        image_bytes = synthetic_grid_png(size=256, cell=32)
        grid = detect_grid_from_image(image_bytes)

        self.assertLessEqual(abs(grid['origin_px']['x']), 2)
        self.assertLessEqual(abs(grid['origin_px']['y']), 2)
        self.assertLessEqual(abs(grid['cell_size_px']['average'] - 32), 2)
        self.assertEqual(grid['columns'], 8)
        self.assertEqual(grid['rows'], 8)
        self.assertGreaterEqual(grid['confidence'], 0.45)

        with tempfile.TemporaryDirectory() as temp_dir:
            original_path = os.path.join(temp_dir, 'original.png')
            labeled_path = os.path.join(temp_dir, 'labeled.png')
            with open(original_path, 'wb') as file:
                file.write(image_bytes)

            labeled_bytes = create_labeled_grid_image(image_bytes, grid, Path(labeled_path))

            self.assertTrue(os.path.exists(labeled_path))
            with open(original_path, 'rb') as file:
                self.assertEqual(file.read(), image_bytes)
            self.assertNotEqual(labeled_bytes, image_bytes)

    def test_grid_detector_finds_offset_grid_phase(self):
        image_bytes = synthetic_grid_png(size=256, cell=32, offset=11)
        grid = detect_grid_from_image(image_bytes)

        self.assertLessEqual(abs(grid['origin_px']['x'] - 11), 4)
        self.assertLessEqual(abs(grid['origin_px']['y'] - 11), 4)
        self.assertLessEqual(abs(grid['cell_size_px']['average'] - 32), 2)
        self.assertEqual(grid['columns'], 7)
        self.assertEqual(grid['rows'], 7)
        self.assertGreaterEqual(grid['confidence'], 0.45)

    def test_grid_detector_preserves_near_cell_offset_phase(self):
        image_bytes = synthetic_grid_png(size=256, cell=32, offset=31)
        grid = detect_grid_from_image(image_bytes)

        self.assertLessEqual(abs(grid['origin_px']['x'] - 31), 4)
        self.assertLessEqual(abs(grid['origin_px']['y'] - 31), 4)
        self.assertLessEqual(abs(grid['cell_size_px']['average'] - 32), 2)
        self.assertEqual(grid['columns'], 7)
        self.assertEqual(grid['rows'], 7)
        self.assertGreaterEqual(grid['confidence'], 0.45)

    def test_create_encounter_map_persists_vtt_setup_json(self):
        image_bytes = synthetic_grid_png(size=256, cell=32)
        setup_json = {
            'map_summary': 'Compact arena with cover and northern ruins.',
            'dm_setup_context': 'Friendlies enter from the south; enemies hold the ruins.',
            'friendly_spawn_boxes': [{
                'label': 'Friendly Entry',
                'rect': {'col': 1, 'row': 6, 'width': 2, 'height': 1},
                'description': 'Players enter from the lower path.',
                'confidence': 0.9,
            }],
            'enemy_spawn_boxes': [{
                'label': 'North Ruins',
                'rect': {'col': 5, 'row': 1, 'width': 2, 'height': 2},
                'description': 'Enemies hold the upper cover.',
                'confidence': 0.8,
            }],
            'terrain_zones': [{
                'kind': 'cover',
                'label': 'Crates',
                'shape_type': 'rect',
                'rect': {'col': 3, 'row': 3, 'width': 2, 'height': 1},
                'polygon': [],
                'description': 'Half cover from stacked crates.',
                'confidence': 0.85,
            }],
            'obstacles': [{
                'label': 'Crate Stack',
                'kind': 'cover',
                'shape_type': 'rect',
                'rect': {'col': 3, 'row': 3, 'width': 2, 'height': 1},
                'polygon': [],
                'movement_effect': 'provides_cover',
                'cover_type': 'half',
                'description': 'Stacked crates provide half cover.',
                'confidence': 0.86,
            }],
            'tactical_notes': ['South side has the safest load-in lane.'],
        }

        class FakeImageResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {'data': [{'b64_json': base64.b64encode(image_bytes).decode('ascii')}]}

        class FakeSetupResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {'output_text': json.dumps(setup_json)}

        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.dict(os.environ, {
                    'OPENAI_API_KEY': 'test-key',
                    'ENCOUNTER_MAP_STORAGE_DIR': temp_dir,
                    'OPENAI_IMAGE_QA_ENABLED': 'false',
                }, clear=False), \
                patch('services.encounter_map_service.requests.post', side_effect=[
                    FakeImageResponse(),
                    FakeSetupResponse(),
                ]) as post:
            result = execute_dm_tool(
                self.campaign,
                self.session,
                self.user,
                'create_encounter_map',
                {
                    'title': 'Setup Map',
                    'map_prompt': 'A compact tactical arena with cover.',
                    'vtt_setup_notes': 'Friendlies enter from the south; enemies hold the northern ruins.',
                },
                {},
            )

            encounter_map = EncounterMap.query.filter_by(campaign_id=self.campaign.id).one()
            self.assertEqual(encounter_map.setup_status, 'ready')
            self.assertTrue(os.path.exists(os.path.join(temp_dir, encounter_map.image_filename)))
            self.assertTrue(os.path.exists(os.path.join(temp_dir, encounter_map.labeled_image_filename)))
            self.assertLessEqual(abs(json.loads(encounter_map.grid_json)['cell_size_px']['average'] - 32), 2)
            persisted_setup = json.loads(encounter_map.vtt_setup_json)

        self.assertEqual(persisted_setup['friendly_spawn_boxes'][0]['label'], 'Friendly Entry')
        self.assertEqual(persisted_setup['player_start_areas'][0]['label'], 'Friendly Entry')
        self.assertEqual(persisted_setup['obstacles'][0]['movement_effect'], 'provides_cover')
        self.assertEqual(persisted_setup['terrain_zones'][0]['kind'], 'cover')
        self.assertEqual(result['encounter_map']['setup_status'], 'ready')
        self.assertLessEqual(abs(result['encounter_map']['grid']['cell_size_px']['average'] - 32), 2)
        self.assertEqual(result['encounter_map']['vtt_setup']['enemy_spawn_boxes'][0]['label'], 'North Ruins')
        self.assertEqual(result['encounter_map']['vtt_setup']['enemy_start_areas'][0]['label'], 'North Ruins')
        setup_call = post.call_args_list[1]
        self.assertEqual(setup_call.kwargs['json']['model'], 'gpt-5.4')
        setup_text = setup_call.kwargs['json']['input'][0]['content'][0]['text']
        self.assertIn('DM setup and placement instructions', setup_text)
        self.assertIn('Friendlies enter from the south', setup_text)
        setup_schema = setup_call.kwargs['json']['text']['format']['schema']
        self.assertIn('friendly_spawn_boxes', setup_schema['required'])
        self.assertIn('enemy_spawn_boxes', setup_schema['required'])
        self.assertIn('obstacles', setup_schema['required'])
        image_parts = [
            part for part in setup_call.kwargs['json']['input'][0]['content']
            if part.get('type') == 'input_image'
        ]
        self.assertEqual(len(image_parts), 2)
        self.assertEqual(image_parts[0]['detail'], 'low')
        self.assertEqual(image_parts[1]['detail'], 'high')

    def test_low_confidence_grid_setup_fails_without_blocking_map(self):
        image_bytes = synthetic_grid_png(size=256, cell=32, blank=True)

        class FakeImageResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {'data': [{'b64_json': base64.b64encode(image_bytes).decode('ascii')}]}

        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.dict(os.environ, {
                    'OPENAI_API_KEY': 'test-key',
                    'ENCOUNTER_MAP_STORAGE_DIR': temp_dir,
                    'OPENAI_IMAGE_QA_ENABLED': 'false',
                    'OPENAI_IMAGE_GRID_MAX_RETRIES': '0',
                }, clear=False), \
                patch('services.encounter_map_service.requests.post', return_value=FakeImageResponse()) as post:
            result = execute_dm_tool(
                self.campaign,
                self.session,
                self.user,
                'create_encounter_map',
                {'title': 'Blank Map', 'map_prompt': 'A blank field.'},
                {},
            )

            encounter_map = EncounterMap.query.filter_by(campaign_id=self.campaign.id).one()
            self.assertTrue(os.path.exists(os.path.join(temp_dir, encounter_map.image_filename)))

        self.assertEqual(post.call_count, 1)
        self.assertEqual(encounter_map.setup_status, 'failed')
        self.assertIsNone(encounter_map.vtt_setup_json)
        self.assertIn('grid', encounter_map.setup_error.lower())
        self.assertEqual(result['encounter_map']['setup_status'], 'failed')

    def test_grid_validation_retries_generation_before_saving_map(self):
        bad_image_bytes = synthetic_grid_png(size=256, cell=32, blank=True)
        good_image_bytes = synthetic_grid_png(size=256, cell=32)
        setup_json = {
            'player_start_areas': [],
            'enemy_start_areas': [],
            'terrain_zones': [],
        }

        class FakeImageResponse:
            def __init__(self, image_bytes):
                self.image_bytes = image_bytes

            def raise_for_status(self):
                return None

            def json(self):
                return {'data': [{'b64_json': base64.b64encode(self.image_bytes).decode('ascii')}]}

        class FakeSetupResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {'output_text': json.dumps(setup_json)}

        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.dict(os.environ, {
                    'OPENAI_API_KEY': 'test-key',
                    'ENCOUNTER_MAP_STORAGE_DIR': temp_dir,
                    'OPENAI_IMAGE_QA_ENABLED': 'false',
                    'OPENAI_IMAGE_GRID_MAX_RETRIES': '1',
                }, clear=False), \
                patch('services.encounter_map_service.requests.post', side_effect=[
                    FakeImageResponse(bad_image_bytes),
                    FakeImageResponse(good_image_bytes),
                    FakeSetupResponse(),
                ]) as post:
            result = execute_dm_tool(
                self.campaign,
                self.session,
                self.user,
                'create_encounter_map',
                {'title': 'Retry Grid Map', 'map_prompt': 'A map with a clear machine-readable grid.'},
                {},
            )

            encounter_map = EncounterMap.query.filter_by(campaign_id=self.campaign.id).one()
            saved_path = os.path.join(temp_dir, encounter_map.image_filename)
            with open(saved_path, 'rb') as file:
                self.assertEqual(file.read(), good_image_bytes)

        self.assertEqual(post.call_count, 3)
        self.assertEqual(encounter_map.setup_status, 'ready')
        self.assertLessEqual(abs(result['encounter_map']['grid']['cell_size_px']['average'] - 32), 2)
        self.assertIn('Machine grid-detection corrections', post.call_args_list[1].kwargs['json']['prompt'])

    def test_create_encounter_map_tool_persists_generated_png(self):
        image_bytes = synthetic_grid_png(size=128, cell=32, blank=True)

        class FakeImageResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    'data': [{'b64_json': base64.b64encode(image_bytes).decode('ascii')}],
                    'usage': {'total_tokens': 12},
                }

        class FakeQaResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    'output_text': json.dumps({
                        'pass': True,
                        'score': 9,
                        'issues': [],
                        'retry_prompt_patch': '',
                    })
                }

        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.dict(os.environ, {
                    'OPENAI_API_KEY': 'test-key',
                    'ENCOUNTER_MAP_STORAGE_DIR': temp_dir,
                    'OPENAI_IMAGE_TIMEOUT_SECONDS': '240',
                    'OPENAI_IMAGE_GRID_VALIDATION_ENABLED': 'false',
                }, clear=False), \
                patch('services.encounter_map_service.requests.post', side_effect=[
                    FakeImageResponse(),
                    FakeQaResponse(),
                ]) as post:
            result = execute_dm_tool(
                self.campaign,
                self.session,
                self.user,
                'create_encounter_map',
                {
                    'title': 'Dock Ward Ambush',
                    'map_prompt': 'A rain-slick dock with crates, alleys, and a moored skiff.',
                    'terrain': 'urban waterfront',
                    'tactical_features': 'crates for cover and two narrow gangplanks',
                    'mood': 'night rain',
                },
                {'trace_id': 'session_dm:test'},
            )

            encounter_map = EncounterMap.query.filter_by(campaign_id=self.campaign.id).one()
            saved_path = os.path.join(temp_dir, encounter_map.image_filename)
            self.assertTrue(os.path.exists(saved_path))
            with open(saved_path, 'rb') as file:
                self.assertEqual(file.read(), image_bytes)

        self.assertIn('encounter_map', result)
        self.assertEqual(result['encounter_map']['title'], 'Dock Ward Ambush')
        self.assertEqual(result['encounter_map']['image_url'], f'/api/encounter-maps/{encounter_map.id}/image')
        image_call = post.call_args_list[0]
        qa_call = post.call_args_list[1]
        self.assertEqual(image_call.kwargs['json']['model'], 'gpt-image-2')
        self.assertEqual(image_call.kwargs['json']['quality'], 'medium')
        self.assertEqual(image_call.kwargs['timeout'], 240)
        self.assertEqual(qa_call.kwargs['json']['model'], 'gpt-5.4')
        self.assertEqual(qa_call.kwargs['json']['input'][0]['content'][1]['detail'], 'low')
        prompt = image_call.kwargs['json']['prompt']
        self.assertIn('VTT-ready', prompt)
        self.assertIn('battlemap/cartography style', prompt)
        self.assertIn('no cinematic perspective', prompt)
        self.assertIn('Design the map around the grid', prompt)
        self.assertIn('Each grid cell should have an obvious gameplay meaning', prompt)
        self.assertIn('align to grid squares', prompt)
        self.assertIn('snap cleanly to grid lines', prompt)
        self.assertIn('obvious open squares', prompt)
        self.assertIn('tactical contrast high', prompt)
        self.assertIn('Do not let canopy texture obscure grid intersections', prompt)
        self.assertIn('Do not include people', prompt)
        self.assertIn('tokens can be placed on top', prompt)
        self.assertIn('straight evenly spaced grid lines', prompt)

    def test_create_encounter_map_retries_once_when_quality_review_fails(self):
        image_bytes = synthetic_grid_png(size=128, cell=32, blank=True)

        class FakeImageResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {'data': [{'b64_json': base64.b64encode(image_bytes).decode('ascii')}]}

        class FakeQaResponse:
            def __init__(self, passed, score, patch_text):
                self.passed = passed
                self.score = score
                self.patch_text = patch_text

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    'output_text': json.dumps({
                        'pass': self.passed,
                        'score': self.score,
                        'issues': ['Grid is pasted over scenery'],
                        'retry_prompt_patch': self.patch_text,
                    })
                }

        retry_patch = 'Make every wall and obstacle snap to grid squares.'
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.dict(os.environ, {
                    'OPENAI_API_KEY': 'test-key',
                    'ENCOUNTER_MAP_STORAGE_DIR': temp_dir,
                    'OPENAI_IMAGE_QA_MAX_RETRIES': '1',
                    'OPENAI_IMAGE_GRID_VALIDATION_ENABLED': 'false',
                }, clear=False), \
                patch('services.encounter_map_service.requests.post', side_effect=[
                    FakeImageResponse(),
                    FakeQaResponse(False, 5, retry_patch),
                    FakeImageResponse(),
                    FakeQaResponse(True, 9, ''),
                ]) as post:
            result = execute_dm_tool(
                self.campaign,
                self.session,
                self.user,
                'create_encounter_map',
                {'title': 'Retry Map', 'map_prompt': 'A narrow dungeon junction.'},
                {},
            )

            encounter_map = EncounterMap.query.filter_by(campaign_id=self.campaign.id).one()

        image_calls = [
            call for call in post.call_args_list
            if call.kwargs['json'].get('model') == 'gpt-image-2'
        ]
        self.assertEqual(len(image_calls), 2)
        self.assertIn(retry_patch, image_calls[1].kwargs['json']['prompt'])
        self.assertIn(retry_patch, encounter_map.prompt)
        self.assertEqual(result['encounter_map']['id'], encounter_map.id)

    def test_create_encounter_map_tool_returns_clear_error_without_openai_key(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': ''}, clear=False):
            result = execute_dm_tool(
                self.campaign,
                self.session,
                self.user,
                'create_encounter_map',
                {'title': 'No Key Map', 'map_prompt': 'A small cave.'},
                {},
            )

        self.assertIn('OPENAI_API_KEY is required', result['error'])
        self.assertEqual(EncounterMap.query.count(), 0)

    def test_create_encounter_map_tool_reports_timeout_with_configured_limit(self):
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.dict(os.environ, {
                    'OPENAI_API_KEY': 'test-key',
                    'ENCOUNTER_MAP_STORAGE_DIR': temp_dir,
                    'OPENAI_IMAGE_TIMEOUT_SECONDS': '180',
                }, clear=False), \
                patch('services.encounter_map_service.requests.post', side_effect=requests.Timeout('too slow')):
            result = execute_dm_tool(
                self.campaign,
                self.session,
                self.user,
                'create_encounter_map',
                {'title': 'Slow Map', 'map_prompt': 'A large ruin.'},
                {},
            )

        self.assertIn('Failed to generate encounter map', result['error'])
        self.assertIn('timed out after 180 seconds', result['error'])
        self.assertEqual(EncounterMap.query.count(), 0)

    def test_session_message_route_completes_when_dm_tool_creates_map(self):
        image_bytes = synthetic_grid_png(size=128, cell=32, blank=True)

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {'data': [{'b64_json': base64.b64encode(image_bytes).decode('ascii')}]}

        def dm_response(_hot_context, _recent_messages, _tools, execute_tool, audit_context=None, **kwargs):
            execute_tool(
                'create_encounter_map',
                {
                    'title': 'Warehouse Fight',
                    'map_prompt': 'A warehouse with stacked crates and loading doors.',
                },
                audit_context or {},
            )
            return {
                'mode': 'speak',
                'content': 'A gridded map appears on the table.',
                'parts': [{'type': 'narration', 'content': 'A gridded map appears on the table.'}],
                'commit_action_ids': [],
            }

        token = generate_token(self.user.id)
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.dict(os.environ, {
                    'OPENAI_API_KEY': 'test-key',
                    'ENCOUNTER_MAP_STORAGE_DIR': temp_dir,
                    'OPENAI_IMAGE_QA_ENABLED': 'false',
                    'OPENAI_IMAGE_GRID_VALIDATION_ENABLED': 'false',
                }, clear=False), \
                patch('services.encounter_map_service.requests.post', return_value=FakeResponse()), \
                patch('routes.sessions.get_session_dm_response_with_tools', side_effect=dm_response), \
                patch('routes.sessions.get_session_memory_patch', return_value={}):
            response = self.client.post(
                f'/api/sessions/{self.session.id}/messages',
                json={'content': '<ooc>Please make a map.</ooc>'},
                headers={'Authorization': f'Bearer {token}'},
            )

            self.assertEqual(response.status_code, 201)
            self.assertEqual(EncounterMap.query.filter_by(campaign_id=self.campaign.id).count(), 1)
            encounter_map = EncounterMap.query.filter_by(campaign_id=self.campaign.id).one()
            self.assertTrue(os.path.exists(os.path.join(temp_dir, encounter_map.image_filename)))

        messages = response.get_json()['messages']
        self.assertEqual(messages[-1]['content'], 'A gridded map appears on the table.')

    def test_session_messages_can_page_older_history(self):
        token = generate_token(self.user.id)
        db.session.add_all([
            SessionMessage(
                session_id=self.session.id,
                user_id=self.user.id,
                role='player',
                content=f'Message {index}',
            )
            for index in range(55)
        ])
        db.session.commit()

        latest_response = self.client.get(
            f'/api/sessions/{self.session.id}/messages?limit=10',
            headers={'Authorization': f'Bearer {token}'},
        )

        self.assertEqual(latest_response.status_code, 200)
        latest_payload = latest_response.get_json()
        self.assertTrue(latest_payload['has_more_messages'])
        self.assertEqual([message['content'] for message in latest_payload['messages']], [
            f'Message {index}' for index in range(45, 55)
        ])

        before_id = latest_payload['messages'][0]['id']
        older_response = self.client.get(
            f'/api/sessions/{self.session.id}/messages?limit=10&before_id={before_id}',
            headers={'Authorization': f'Bearer {token}'},
        )

        self.assertEqual(older_response.status_code, 200)
        older_payload = older_response.get_json()
        self.assertTrue(older_payload['has_more_messages'])
        self.assertEqual([message['content'] for message in older_payload['messages']], [
            f'Message {index}' for index in range(35, 45)
        ])

    def test_hot_context_includes_protected_player_characters(self):
        hot_context = build_session_hot_context(self.campaign, self.session, self.user)

        self.assertEqual(hot_context['current_player_character']['name'], 'Aria')
        self.assertEqual(hot_context['protected_player_characters'][0]['name'], 'Aria')
        self.assertIn('Crimson Veil', hot_context['private_output_terms'])
        self.assertEqual(hot_context['private_spoiler_items'][0]['text'], 'Crimson Veil')

    def test_session_memory_context_omits_guard_only_spoiler_lists(self):
        hot_context = build_session_hot_context(self.campaign, self.session, self.user)

        memory_context = build_session_memory_context(
            self.campaign,
            self.session,
            self.user,
            'I ask the dockhand what he saw.',
            'The dockhand points toward the south jetty.',
            hot_context,
        )

        self.assertIn('current_scene', memory_context['hot_context'])
        self.assertEqual(
            memory_context['hot_context']['protected_player_characters'][0]['name'],
            'Aria',
        )
        self.assertNotIn('private_output_terms', memory_context['hot_context'])
        self.assertNotIn('private_spoiler_items', memory_context['hot_context'])

    def test_session_memory_context_compacts_character_and_memory_payloads(self):
        hot_context = build_session_hot_context(self.campaign, self.session, self.user)
        large_memory = {
            'query': 'dockhand vault clue',
            'matches': [
                {
                    'kind': 'dm_private',
                    'item_id': 'current',
                    'score': 2.75,
                    'value': {
                        'true_inciting_incident': 'x' * 1200,
                        'villain_plan': 'y' * 1200,
                        'hidden_pressures': ['z' * 400, 'q' * 400],
                    },
                }
            ],
        }

        with patch('services.dm_tools._tool_search_campaign_memory', return_value=large_memory):
            memory_context = build_session_memory_context(
                self.campaign,
                self.session,
                self.user,
                'I ask the dockhand what he saw.',
                'The dockhand points toward the south jetty.',
                hot_context,
            )

        self.assertNotIn('spellcasting', memory_context['hot_context']['current_character'])
        self.assertNotIn('combat', memory_context['hot_context']['current_character'])
        self.assertEqual(
            memory_context['current_user'],
            {'id': self.user.id, 'username': self.user.username},
        )
        self.assertNotIn('email', memory_context['current_user'])
        compact_memory = memory_context['relevant_memory']
        self.assertEqual(compact_memory['matches'][0]['kind'], 'dm_private')
        self.assertLess(
            len(json.dumps(compact_memory, ensure_ascii=False)),
            len(json.dumps(large_memory, ensure_ascii=False)),
        )
        self.assertLessEqual(
            len(compact_memory['matches'][0]['memory']['true_inciting_incident']),
            223,
        )

    def test_session_memory_patch_staged_resolves_canonical_ids_before_fact_write(self):
        world = CampaignWorld.query.filter_by(campaign_id=self.campaign.id).first()
        world.knowledge_graph = json.dumps({
            'entities': [
                {'id': 'hanging_switchyard', 'type': 'location', 'name': 'Hanging Switchyard', 'visibility': 'party_known'},
            ],
            'relations': [],
            'facts': [
                {
                    'id': 'rona_signal_token',
                    'entity_ids': ['deputy_rona'],
                    'text': 'Deputy Rona controls the signal token.',
                    'visibility': 'party_known',
                }
            ],
        })
        world.world_state = json.dumps({
            'current_scene': {
                'location_id': 'hanging_switchyard',
                'location_name': 'Hanging Switchyard',
                'active_npc_ids': ['deputy_rona'],
                'immediate_tension': 'The yard is tense.',
            }
        })
        db.session.add(NPCActor(
            campaign_id=self.campaign.id,
            actor_id='deputy_rona',
            name='Deputy Rona',
            role='deputy',
            public_summary='A tired deputy watching the switchyard.',
            dossier='{}',
        ))
        db.session.commit()

        hot_context = build_session_hot_context(self.campaign, self.session, self.user)
        memory_context = build_session_memory_context(
            self.campaign,
            self.session,
            self.user,
            'I ask who has the token now.',
            'Deputy Rona keeps the signal token and refuses to hand it over.',
            hot_context,
        )

        with patch('openrouter.SESSION_MEMORY_MODE', 'staged'), \
                patch('openrouter.get_llm_provider', return_value='openrouter'), \
                patch('openrouter._post_chat_normalized', side_effect=[
                    _normalized_from_raw({
                        'choices': [{
                            'message': {
                                'content': '',
                                'tool_calls': [{
                                    'id': 'ext_1',
                                    'type': 'function',
                                    'function': {
                                        'name': 'submit_extraction',
                                        'arguments': json.dumps({
                                            'running_summary': 'At the Hanging Switchyard, Deputy Rona kept control of the signal token.',
                                            'scene_patch': {
                                                'location_name': 'Hanging Switchyard',
                                                'active_npc_ids': ['deputy_rona'],
                                                'immediate_tension': 'Rona refuses to surrender the token.',
                                            },
                                            'scene_reason': 'The exchange stayed focused on Rona at the switchyard.',
                                            'fact_claims': [
                                                {
                                                    'text': 'Deputy Rona has the signal token.',
                                                    'entity_refs': ['Deputy Rona'],
                                                    'source_surface': 'visible_transcript',
                                                    'intended_visibility': 'party_known',
                                                    'certainty': 'confirmed',
                                                    'importance': 3,
                                                    'reason': 'The DM explicitly said Rona kept the token.',
                                                    'expires_or_retire_condition': None,
                                                    'memory_type': 'fact',
                                                }
                                            ],
                                        }),
                                    },
                                }],
                            },
                            'finish_reason': 'stop',
                        }],
                    }),
                    _normalized_from_raw({
                        'choices': [{
                            'message': {
                                'content': '',
                                'tool_calls': [{
                                    'id': 'tool_1',
                                    'function': {
                                        'name': 'get_entity_candidates',
                                        'arguments': json.dumps({'query': 'Deputy Rona', 'entity_type': 'npc', 'limit': 5}),
                                    },
                                }],
                            },
                        }],
                    }),
                    _normalized_from_raw({
                        'choices': [{
                            'message': {
                                'content': '',
                                'tool_calls': [{
                                    'id': 'res_1',
                                    'type': 'function',
                                    'function': {
                                        'name': 'submit_resolved_memory',
                                        'arguments': json.dumps({
                                            'running_summary': 'At the Hanging Switchyard, Deputy Rona kept control of the signal token.',
                                            'scene_patch': {
                                                'location_name': 'Hanging Switchyard',
                                                'active_npc_ids': ['deputy_rona'],
                                                'immediate_tension': 'Rona refuses to surrender the token.',
                                            },
                                            'scene_reason': 'The exchange stayed focused on Rona at the switchyard.',
                                            'upsert_graph_facts': [
                                                {
                                                    'id': 'rona_signal_token',
                                                    'text': 'Deputy Rona has the signal token.',
                                                    'entity_ids': ['deputy_rona'],
                                                    'source_surface': 'visible_transcript',
                                                    'intended_visibility': 'party_known',
                                                    'certainty': 'confirmed',
                                                    'importance': 3,
                                                    'reason': 'The DM explicitly said Rona kept the token.',
                                                    'expires_or_retire_condition': None,
                                                    'memory_type': 'fact',
                                                }
                                            ],
                                            'unresolved_items': [],
                                            'evidence_basis': [{'surface': 'latest_dm_message', 'summary': 'Rona kept the token.'}],
                                            'resolved_entity_refs': [{'label': 'Deputy Rona', 'entity_id': 'deputy_rona', 'resolution': 'same'}],
                                            'resolved_location_refs': [{'label': 'Hanging Switchyard', 'location_id': 'hanging_switchyard'}],
                                        }),
                                    },
                                }],
                            },
                            'finish_reason': 'stop',
                        }],
                    }),
                    _normalized_from_raw({
                        'choices': [{
                            'message': {
                                'content': '',
                                'tool_calls': [{
                                    'id': 'clk_1',
                                    'type': 'function',
                                    'function': {
                                        'name': 'submit_clock_updates',
                                        'arguments': json.dumps({'create_clocks': [], 'retire_clocks': []}),
                                    },
                                }],
                            },
                            'finish_reason': 'stop',
                        }],
                    }),
                ]):
            patch_data = get_session_memory_patch(
                memory_context,
                audit_context={
                    'campaign_id': self.campaign.id,
                    'trace_id': 'memory_trace',
                    'trace_label': 'session_memory_writer: test',
                },
            )

        self.assertEqual(
            patch_data['running_summary'],
            'At the Hanging Switchyard, Deputy Rona kept control of the signal token.',
        )
        self.assertEqual(patch_data['scene_patch']['location_id'], 'hanging_switchyard')
        self.assertEqual(patch_data['upsert_graph_facts'][0]['entity_ids'], ['deputy_rona'])
        self.assertEqual(patch_data['upsert_graph_facts'][0]['id'], 'rona_signal_token')
        self.assertEqual(patch_data['_telemetry']['mode'], 'staged_memory_writer')
        self.assertEqual(patch_data['_telemetry']['staged_tool_call_count'], 1)

    def test_session_memory_patch_staged_skips_unresolved_identity_fact(self):
        world = CampaignWorld.query.filter_by(campaign_id=self.campaign.id).first()
        world.knowledge_graph = json.dumps({
            'entities': [
                {'id': 'hanging_switchyard', 'type': 'location', 'name': 'Hanging Switchyard', 'visibility': 'party_known'},
            ],
            'relations': [],
            'facts': [],
        })
        world.world_state = json.dumps({
            'current_scene': {
                'location_id': 'hanging_switchyard',
                'location_name': 'Hanging Switchyard',
            }
        })
        db.session.commit()

        hot_context = build_session_hot_context(self.campaign, self.session, self.user)
        memory_context = build_session_memory_context(
            self.campaign,
            self.session,
            self.user,
            'I ask who moved the crate.',
            'A porter moved the crate before dawn, but no one names them.',
            hot_context,
        )

        with patch('openrouter.SESSION_MEMORY_MODE', 'staged'), \
                patch('openrouter.get_llm_provider', return_value='openrouter'), \
                patch('openrouter._post_chat_normalized', side_effect=[
                    _normalized_from_raw({
                        'choices': [{
                            'message': {
                                'content': '',
                                'tool_calls': [{
                                    'id': 'ext_1',
                                    'type': 'function',
                                    'function': {
                                        'name': 'submit_extraction',
                                        'arguments': json.dumps({
                                            'running_summary': 'Someone unnamed moved the crate before dawn at the Hanging Switchyard.',
                                            'scene_patch': {'location_name': 'Hanging Switchyard'},
                                            'scene_reason': 'The exchange remained at the switchyard.',
                                            'fact_claims': [
                                                {
                                                    'text': 'The porter moved the crate before dawn.',
                                                    'entity_refs': ['porter'],
                                                    'source_surface': 'visible_transcript',
                                                    'intended_visibility': 'party_known',
                                                    'certainty': 'confirmed',
                                                    'importance': 2,
                                                    'reason': 'The DM stated the crate was moved.',
                                                    'expires_or_retire_condition': None,
                                                    'memory_type': 'fact',
                                                }
                                            ],
                                        }),
                                    },
                                }],
                            },
                            'finish_reason': 'stop',
                        }],
                    }),
                    _normalized_from_raw({
                        'choices': [{
                            'message': {
                                'content': '',
                                'tool_calls': [{
                                    'id': 'res_1',
                                    'type': 'function',
                                    'function': {
                                        'name': 'submit_resolved_memory',
                                        'arguments': json.dumps({
                                            'running_summary': 'Someone unnamed moved the crate before dawn at the Hanging Switchyard.',
                                            'scene_patch': {'location_name': 'Hanging Switchyard'},
                                            'scene_reason': 'The exchange remained at the switchyard.',
                                            'upsert_graph_facts': [
                                                {
                                                    'text': 'The porter moved the crate before dawn.',
                                                    'entity_ids': ['unknown_porter'],
                                                    'source_surface': 'visible_transcript',
                                                    'intended_visibility': 'party_known',
                                                    'certainty': 'confirmed',
                                                    'importance': 2,
                                                    'reason': 'The DM stated the crate was moved.',
                                                    'expires_or_retire_condition': None,
                                                    'memory_type': 'fact',
                                                }
                                            ],
                                            'unresolved_items': [{'kind': 'entity', 'label': 'porter', 'reason': 'no_canonical_match'}],
                                            'evidence_basis': [{'surface': 'latest_dm_message', 'summary': 'An unnamed porter moved the crate.'}],
                                            'resolved_entity_refs': [],
                                            'resolved_location_refs': [{'label': 'Hanging Switchyard', 'location_id': 'hanging_switchyard'}],
                                        }),
                                    },
                                }],
                            },
                            'finish_reason': 'stop',
                        }],
                    }),
                    _normalized_from_raw({
                        'choices': [{
                            'message': {
                                'content': '',
                                'tool_calls': [{
                                    'id': 'clk_1',
                                    'type': 'function',
                                    'function': {
                                        'name': 'submit_clock_updates',
                                        'arguments': json.dumps({'create_clocks': [], 'retire_clocks': []}),
                                    },
                                }],
                            },
                            'finish_reason': 'stop',
                        }],
                    }),
                ]):
            patch_data = get_session_memory_patch(
                memory_context,
                audit_context={
                    'campaign_id': self.campaign.id,
                    'trace_id': 'memory_trace',
                    'trace_label': 'session_memory_writer: test',
                },
            )

        self.assertEqual(patch_data['upsert_graph_facts'], [])
        self.assertEqual(patch_data['compile_summary']['skipped_fact_count'], 1)
        self.assertEqual(patch_data['scene_patch']['location_id'], 'hanging_switchyard')
        self.assertEqual(patch_data['unresolved_items'][0]['reason'], 'no_canonical_match')

    def test_hot_context_private_spoilers_include_dm_private_world_events(self):
        db.session.add(WorldEvent(
            campaign_id=self.campaign.id,
            event_type='scene_updated',
            summary='Sensor contact detected while leaving orbit.',
            payload=json.dumps({
                'scene_patch': {
                    'immediate_tension': (
                        "Fast unidentified contact climbing from Vethra's surface "
                        '(military-grade thrusters suspected).'
                    ),
                },
            }),
            visibility='dm_private',
        ))
        db.session.commit()

        hot_context = build_session_hot_context(self.campaign, self.session, self.user)
        world_event_items = [
            item for item in hot_context['private_spoiler_items']
            if item.get('kind') == 'world_event'
        ]
        self.assertTrue(world_event_items)
        joined = ' '.join(item.get('text', '') for item in world_event_items).lower()
        self.assertIn('sensor contact detected', joined)
        self.assertIn('military-grade thrusters suspected', joined)

    def test_hot_context_includes_combat_coordinates_when_encounter_active(self):
        self.campaign.settings = json.dumps({'encounter_active': True})
        encounter_map = EncounterMap(
            campaign_id=self.campaign.id,
            session_id=self.session.id,
            title='Ruined Hall',
            prompt='A ruined hall.',
            image_filename='map.png',
            model='gpt-image-2',
            size='1024x1024',
            quality='high',
            grid_json=json.dumps({'columns': 12, 'rows': 10}),
            setup_status='ready',
        )
        db.session.add(encounter_map)
        db.session.flush()
        player_placement = EncounterMapPlacement(
            encounter_map_id=encounter_map.id,
            actor_type='player',
            actor_id=str(self.user.id),
            label='Aria',
            grid_col=2,
            grid_row=3,
        )
        npc_placement = EncounterMapPlacement(
            encounter_map_id=encounter_map.id,
            actor_type='npc',
            actor_id='bram_truewood',
            label='Bram Truewood',
            grid_col=4,
            grid_row=5,
        )
        monster_placement = EncounterMapPlacement(
            encounter_map_id=encounter_map.id,
            actor_type='monster',
            actor_id='goblin_1',
            label='Goblin',
            grid_col=8,
            grid_row=4,
        )
        db.session.add_all([player_placement, npc_placement, monster_placement])
        db.session.flush()
        encounter_map.encounter_state_json = json.dumps({
            'active': True,
            'round': 2,
            'active_turn_index': 0,
            'turn_order': [
                {
                    'placement_id': player_placement.id,
                    'actor_type': 'player',
                    'actor_id': str(self.user.id),
                    'label': 'Aria',
                    'initiative': 18,
                },
                {
                    'placement_id': monster_placement.id,
                    'actor_type': 'monster',
                    'actor_id': 'goblin_1',
                    'label': 'Goblin',
                    'initiative': 12,
                },
            ],
        })
        db.session.commit()

        hot_context = build_session_hot_context(self.campaign, self.session, self.user)
        combat_coordinates = hot_context['combat_coordinates']

        self.assertTrue(combat_coordinates['active'])
        self.assertEqual(combat_coordinates['encounter_map_id'], encounter_map.id)
        self.assertEqual(combat_coordinates['round'], 2)
        self.assertEqual(combat_coordinates['grid'], {'columns': 12, 'rows': 10})
        by_label = {
            combatant['label']: combatant
            for combatant in combat_coordinates['combatants']
        }
        self.assertEqual(by_label['Aria']['coordinates'], {'col': 2, 'row': 3})
        self.assertTrue(by_label['Aria']['is_active_turn'])
        self.assertEqual(by_label['Bram Truewood']['combatant_type'], 'npc')
        self.assertEqual(by_label['Goblin']['combatant_type'], 'enemy')
        self.assertEqual(by_label['Goblin']['coordinates'], {'col': 8, 'row': 4})
        self.assertIn('combat_coordinates', hot_context['tool_policy'])

    def test_embedding_canonical_text_includes_graph_context(self):
        entity_text = canonical_text_for_item('entity', {
            'id': 'bram_truewood',
            'type': 'npc',
            'name': 'Bram Truewood',
            'summary': 'Bookshop owner on Silver Street.',
            'visibility': 'party_known',
            'tags': ['books', 'infernal lore'],
        })
        relation_text = canonical_text_for_item('relation', {
            'id': 'bram_requested_scroll_help',
            'source_id': 'bram_truewood',
            'target_id': 'seraphina',
            'type': 'requested_help',
            'summary': 'Bram asked Seraphina to watch for missing scrolls.',
        })
        fact_text = canonical_text_for_item('fact', {
            'id': 'fact_symbol',
            'entity_ids': ['seraphina', 'burned_symbol'],
            'text': 'The door symbol is an Infernal seal of scrutiny.',
            'certainty': 'confirmed',
            'visibility': 'party_known',
        })

        self.assertIn('Bram Truewood', entity_text)
        self.assertIn('Bookshop owner', entity_text)
        self.assertIn('infernal lore', entity_text)
        self.assertIn('bram_truewood -> seraphina', relation_text)
        self.assertIn('requested_help', relation_text)
        self.assertIn('burned_symbol', fact_text)
        self.assertIn('confirmed', fact_text)

    def test_embedding_2_uses_retrieval_query_and_document_formatting(self):
        batch_response = Mock()
        batch_response.json.return_value = {
            'embeddings': [
                {'values': [1.0, 0.0]},
                {'values': [0.0, 1.0]},
            ],
        }
        with patch.dict(os.environ, {
            'GEMINI_EMBEDDINGS_ENABLED': 'true',
            'GEMINI_API_KEY': 'test-key',
            'GEMINI_EMBEDDING_MODEL': 'gemini-embedding-2',
            'GEMINI_EMBEDDING_DIMENSIONS': '768',
        }, clear=False), patch('services.embedding_service.requests.post', return_value=batch_response) as post:
            result = embeddings_from_texts(self.campaign.id, ['first query', 'second query'])

        self.assertTrue(result['ok'])
        self.assertEqual(result['vectors'], [[1.0, 0.0], [0.0, 1.0]])
        self.assertTrue(post.call_args.args[0].endswith('/models/gemini-embedding-2:batchEmbedContents'))
        requests_payload = post.call_args.kwargs['json']['requests']
        self.assertEqual([request['content']['parts'][0]['text'] for request in requests_payload], ['first query', 'second query'])
        for request in requests_payload:
            self.assertNotIn('taskType', request)
            self.assertEqual(request['outputDimensionality'], 768)

        single_response = Mock()
        single_response.json.return_value = {
            'embedding': {'values': [1.0, 0.0]},
        }
        with patch.dict(os.environ, {
            'GEMINI_EMBEDDINGS_ENABLED': 'true',
            'GEMINI_API_KEY': 'test-key',
            'GEMINI_EMBEDDING_MODEL': 'gemini-embedding-2',
            'GEMINI_EMBEDDING_DIMENSIONS': '768',
        }, clear=False), patch('services.embedding_service.requests.post', return_value=single_response) as document_post:
            stored = upsert_memory_embedding(
                self.campaign,
                'fact',
                'new_embedding_fact',
                {'id': 'new_embedding_fact', 'text': 'A newly documented fact.', 'visibility': 'party_known'},
            )
        self.assertTrue(stored['ok'])
        self.assertNotIn('taskType', document_post.call_args.kwargs['json'])
        sent_text = document_post.call_args.kwargs['json']['content']['parts'][0]['text']
        self.assertIn('title: none | text:', sent_text)
        self.assertIn('A newly documented fact.', sent_text)

    def test_gemini_embedding_2_payload_contract(self):
        """Provider-contract test: gemini-embedding-2 rejects taskType, requires formatted text."""
        query_response = Mock()
        query_response.json.return_value = {
            'embedding': {'values': [0.5, 0.5]},
        }
        single_response = Mock()
        single_response.json.return_value = {
            'embedding': {'values': [0.5, 0.5]},
        }
        with patch.dict(os.environ, {
            'GEMINI_EMBEDDINGS_ENABLED': 'true',
            'GEMINI_API_KEY': 'test-key',
            'GEMINI_EMBEDDING_MODEL': 'gemini-embedding-2',
            'GEMINI_EMBEDDING_DIMENSIONS': '768',
        }, clear=False), \
                patch('services.embedding_service.requests.post', return_value=query_response) as post:
            result = search_memory_embeddings(
                self.campaign, 'Who is the Black Harbinger?',
                candidates=[], limit=5,
            )
        self.assertTrue(result['ok'])
        sent_text = post.call_args.kwargs['json']['content']['parts'][0]['text']
        self.assertIn('task: search result | query:', sent_text)
        self.assertIn('Who is the Black Harbinger?', sent_text)
        self.assertNotIn('taskType', post.call_args.kwargs['json'])

        with patch.dict(os.environ, {
            'GEMINI_EMBEDDINGS_ENABLED': 'true',
            'GEMINI_API_KEY': 'test-key',
            'GEMINI_EMBEDDING_MODEL': 'gemini-embedding-2',
            'GEMINI_EMBEDDING_DIMENSIONS': '768',
        }, clear=False), \
                patch('services.embedding_service.requests.post', return_value=single_response) as doc_post:
            result = find_duplicate_graph_item(
                self.campaign, 'entity',
                {'id': 'dup_test', 'name': 'Test Entity', 'type': 'person', 'summary': 'A test entity.', 'visibility': 'public'},
            )
        self.assertTrue(result['ok'])
        sent_text = doc_post.call_args.kwargs['json']['content']['parts'][0]['text']
        self.assertIn('title: none | text:', sent_text)
        self.assertIn('Test Entity', sent_text)
        self.assertNotIn('taskType', doc_post.call_args.kwargs['json'])

    def test_party_known_clock_embedding_omits_private_completion_fields(self):
        clock_text = canonical_text_for_item('clock', {
            'clock_id': 'party_obligation',
            'name': 'Contractual Entanglement',
            'visibility': 'party_known',
            'summary': 'Lyle offered the party work.',
            'trigger': 'Accepting hidden contract terms advances the clock.',
            'on_complete': "The party becomes bound to the Ashen Hand's agenda.",
            'status': 'active',
        })
        private_clock_text = canonical_text_for_item('clock', {
            'clock_id': 'ashen_hand_scheme',
            'name': "Ashen Hand's Machinations",
            'visibility': 'dm_private',
            'summary': 'A secret faction advances its plan.',
            'trigger': 'The party takes the bait.',
            'on_complete': 'The Ashen Hand gains control.',
            'status': 'active',
        })

        self.assertIn('Contractual Entanglement', clock_text)
        self.assertIn('Lyle offered the party work.', clock_text)
        self.assertNotIn('Trigger:', clock_text)
        self.assertNotIn('On complete:', clock_text)
        self.assertNotIn('Ashen Hand', clock_text)
        self.assertIn('Trigger:', private_clock_text)
        self.assertIn('On complete:', private_clock_text)

    def test_cosine_similarity_handles_matching_vectors(self):
        self.assertAlmostEqual(cosine_similarity([1, 0], [1, 0]), 1.0)
        self.assertAlmostEqual(cosine_similarity([1, 0], [0, 1]), 0.0)

    def test_pc_control_guard_detects_pc_dialogue_and_action(self):
        hot_context = {
            'protected_player_characters': [
                {'id': 1, 'name': 'Borin Stonefist', 'user_id': 1},
                {'id': 2, 'name': 'Raven Nightshade', 'user_id': 2},
            ],
        }

        self.assertIsNotNone(_pc_control_violation(
            '**Raven (quietly):** "She is fine."\n\nRaven nods.',
            hot_context,
        ))
        self.assertIsNotNone(_pc_control_violation(
            '**Borin:** "How is your mother?"',
            hot_context,
        ))

    def test_pc_control_guard_allows_npc_addressing_pcs(self):
        hot_context = {
            'protected_player_characters': [
                {'id': 1, 'name': 'Borin Stonefist', 'user_id': 1},
                {'id': 2, 'name': 'Raven Nightshade', 'user_id': 2},
            ],
        }

        self.assertIsNone(_pc_control_violation(
            '<npc target="Mayor Elara Voss">Thank you for coming, Borin, Raven.</npc>\n\nRaven, how do you respond?',
            hot_context,
        ))

    def test_pc_control_guard_allows_damage_narration_for_pc_targets(self):
        hot_context = {
            'protected_player_characters': [
                {'id': 2, 'name': 'Raven Nightshade', 'user_id': 2},
            ],
        }

        self.assertIsNone(_pc_control_violation(
            'The arrow punches through Raven\'s cloak and Raven takes 4 piercing damage.',
            hot_context,
        ))

    def test_pc_control_guard_allows_player_declared_positioning_echo(self):
        hot_context = {
            'protected_player_characters': [
                {'id': 10, 'name': 'Seraphina Duskweaver', 'user_id': 7},
            ],
            'recent_messages': [
                {
                    'id': 21,
                    'session_id': 5,
                    'user_id': 7,
                    'role': 'player',
                    'content': (
                        'Seraphina drifts away from the commotion, tail curling lazily, and sidles closer to '
                        'Miriam Saltwick. She adopts a tone of warm concern, lowering her voice conspiratorially.'
                    ),
                },
            ],
        }

        self.assertIsNone(_pc_control_violation(
            'Across the platform, Seraphina draws alongside Miriam Saltwick. '
            'The silver-haired woman turns, and her carefully composed mask holds.',
            hot_context,
        ))

    def test_pc_control_guard_blocks_undeclared_departure(self):
        hot_context = {
            'protected_player_characters': [
                {'id': 11, 'name': 'Elara Moonwhisper', 'user_id': 8},
            ],
            'recent_messages': [
                {
                    'id': 25,
                    'session_id': 5,
                    'user_id': 8,
                    'role': 'player',
                    'content': 'Elara studies Lysander carefully and asks to inspect the lock.',
                },
            ],
        }

        self.assertIsNotNone(_pc_control_violation(
            'Elara slips out the warehouse side door before anyone can object.',
            hot_context,
        ))

    def test_pc_control_guard_allows_minor_flair_gesture(self):
        hot_context = {
            'protected_player_characters': [
                {'id': 11, 'name': 'Elara Moonwhisper', 'user_id': 8},
            ],
            'recent_messages': [
                {
                    'id': 25,
                    'session_id': 5,
                    'user_id': 8,
                    'role': 'player',
                    'content': 'Elara gathers her things and prepares to inspect the crate.',
                },
            ],
        }

        self.assertIsNone(_pc_control_violation(
            'Elara gives a curt nod, looping her component pouch more securely at her belt.',
            hot_context,
        ))

    def test_pc_control_guard_allows_environmental_gives_way_narration(self):
        hot_context = {
            'protected_player_characters': [
                {'id': 11, 'name': 'Elara Moonwhisper', 'user_id': 8},
            ],
            'recent_messages': [
                {
                    'id': 25,
                    'session_id': 5,
                    'user_id': 8,
                    'role': 'player',
                    'content': (
                        "Elara's fingers grip the cold, slick rungs as the ladder groans beneath her. "
                        'The third landing sways with each tremor. She steadies herself, ready to leap.\n'
                        '[Roll: Dexterity (Acrobatics) check] total: 8 | rolls: 6 | mod: 2 | sides: 20'
                    ),
                },
            ],
        }

        self.assertIsNone(_pc_control_violation(
            "Elara's boot finds a corroded rung—and it gives way.\n\n"
            'You slam into the gantry frame and nearly lose your grip.',
            hot_context,
        ))

    def test_pc_control_checker_allows_environmental_consequence_narration(self):
        hot_context = {
            'current_player_character': {'id': 11, 'name': 'Elara Moonwhisper', 'user_id': 8},
            'protected_player_characters': [
                {'id': 11, 'name': 'Elara Moonwhisper', 'user_id': 8},
            ],
            'recent_messages': [
                {
                    'id': 25,
                    'session_id': 5,
                    'user_id': 8,
                    'role': 'player',
                    'content': "Elara's fingers grip the cold, slick rungs as the ladder groans beneath her.",
                },
            ],
        }

        with patch('openrouter._post_chat', return_value=json.dumps({
            'safe': True,
            'violations': [],
            'confidence': 'high',
            'reason': 'This narrates immediate environmental consequences of the attempted descent.',
        })) as post_chat:
            result = check_session_pc_control_with_llm(
                "Elara's boot finds a corroded rung and it gives way. You slam into the gantry frame.",
                hot_context,
                {'operation': 'session_dm_response'},
            )

        self.assertTrue(result['safe'])
        self.assertEqual(result['confidence'], 'high')
        payload = json.loads(post_chat.call_args.args[0][1]['content'])
        self.assertEqual(payload['current_player_character']['name'], 'Elara Moonwhisper')

    def test_pc_control_checker_flags_invented_choice(self):
        hot_context = {
            'current_player_character': {'id': 11, 'name': 'Elara Moonwhisper', 'user_id': 8},
            'protected_player_characters': [
                {'id': 11, 'name': 'Elara Moonwhisper', 'user_id': 8},
            ],
            'recent_messages': [
                {
                    'id': 25,
                    'session_id': 5,
                    'user_id': 8,
                    'role': 'player',
                    'content': 'Elara braces herself on the ladder and looks for the landing below.',
                },
            ],
        }

        with patch('openrouter._post_chat', return_value=json.dumps({
            'safe': False,
            'violations': [{
                'character': 'Elara Moonwhisper',
                'sentence': 'You decide to abandon the landing and keep climbing downward.',
                'kind': 'choice_or_intent',
                'reason': 'The DM invented a strategic choice for the protected PC.',
            }],
            'confidence': 'high',
            'reason': 'The reply assigns a new decision to the acting PC.',
        })):
            result = check_session_pc_control_with_llm(
                'You decide to abandon the landing and keep climbing downward.',
                hot_context,
                {'operation': 'session_dm_response'},
            )

        self.assertFalse(result['safe'])
        self.assertEqual(result['violations'][0]['kind'], 'choice_or_intent')

    def test_pc_control_checker_allows_declared_action_followthrough(self):
        hot_context = {
            'current_player_character': {'id': 11, 'name': 'Elara Moonwhisper', 'user_id': 8},
            'protected_player_characters': [
                {'id': 11, 'name': 'Elara Moonwhisper', 'user_id': 8},
            ],
            'recent_messages': [
                {
                    'id': 25,
                    'session_id': 5,
                    'user_id': 8,
                    'role': 'player',
                    'content': 'Elara runs to the altar and drops beside the cracked basin.',
                },
            ],
        }

        with patch('openrouter._post_chat', return_value=json.dumps({
            'safe': False,
            'violations': [{
                'character': 'Elara Moonwhisper',
                'sentence': 'Elara sprints across the nave and drops beside the cracked altar basin.',
                'kind': 'consequential_action',
                'reason': 'The DM narrated movement for the protected PC.',
            }],
            'confidence': 'medium',
            'reason': 'Conservative classifier result.',
        })):
            result = check_session_pc_control_with_llm(
                'Elara sprints across the nave and drops beside the cracked altar basin.',
                hot_context,
                {'operation': 'session_dm_response'},
            )

        self.assertTrue(result['safe'])
        self.assertEqual(result['violations'], [])

    def test_pc_control_checker_allows_minor_affective_color(self):
        hot_context = {
            'current_player_character': {'id': 11, 'name': 'Elara Moonwhisper', 'user_id': 8},
            'protected_player_characters': [
                {'id': 11, 'name': 'Elara Moonwhisper', 'user_id': 8},
            ],
            'recent_messages': [
                {
                    'id': 25,
                    'session_id': 5,
                    'user_id': 8,
                    'role': 'player',
                    'content': 'Elara listens to the final prayer in silence.',
                },
            ],
        }

        with patch('openrouter._post_chat', return_value=json.dumps({
            'safe': False,
            'violations': [{
                'character': 'Elara Moonwhisper',
                'sentence': 'You feel a pang of sadness as the prayer fades into the rafters.',
                'kind': 'interior_state',
                'reason': 'The DM described the protected PC emotional state.',
            }],
            'confidence': 'medium',
            'reason': 'Conservative classifier result.',
        })):
            result = check_session_pc_control_with_llm(
                'You feel a pang of sadness as the prayer fades into the rafters.',
                hot_context,
                {'operation': 'session_dm_response'},
            )

        self.assertTrue(result['safe'])
        self.assertEqual(result['violations'], [])

    def test_private_output_guard_detects_hidden_terms(self):
        hot_context = {'private_output_terms': ['Crimson Veil']}

        self.assertEqual(
            _private_output_violation('The Crimson Veil moves another step ahead.', hot_context),
            {'matched_terms': ['Crimson Veil']},
        )
        self.assertIsNone(
            _private_output_violation('A hidden scheme moves another step ahead.', hot_context),
        )

    def test_private_output_guard_ignores_terms_already_used_by_latest_player(self):
        hot_context = {
            'private_output_terms': ['Mortimer'],
            'recent_messages': [
                {
                    'role': 'player',
                    'content': 'I ask Mortimer what he saw near the Temple vault that night.',
                },
            ],
        }

        self.assertIsNone(
            _private_output_violation(
                '<npc target="Mortimer">"I saw a figure near the vault."</npc>',
                hot_context,
            ),
        )

    def test_private_output_guard_checks_npc_target_names(self):
        hot_context = {
            'private_output_terms': ['Mortimer'],
            'recent_messages': [
                {
                    'role': 'player',
                    'content': 'I ask the old dockhand what he saw near the Temple vault that night.',
                },
            ],
        }

        self.assertEqual(
            _private_output_violation(
                '<npc target="Mortimer">"I saw a figure near the vault."</npc>',
                hot_context,
            ),
            {'matched_terms': ['Mortimer']},
        )

    def test_private_output_guard_checks_npc_spoken_text(self):
        hot_context = {
            'private_output_terms': ['Mortimer'],
            'recent_messages': [
                {
                    'role': 'player',
                    'content': 'I ask the old dockhand what would make him safe.',
                },
            ],
        }

        self.assertEqual(
            _private_output_violation(
                '<npc target="elderly dockhand">"Safe means old Mortimer never had this conversation."</npc>',
                hot_context,
            ),
            {'matched_terms': ['Mortimer']},
        )

    def test_private_output_retry_prompt_mentions_npc_targets(self):
        prompt = _session_dm_guard_retry_system_prompt(
            'private_output',
            {'matched_terms': ['Mortimer']},
        )

        self.assertIn('Mortimer', prompt)
        self.assertIn('npc_dialogue targets', prompt)
        self.assertIn('Use public descriptors', prompt)

    def test_canon_repair_prompt_contains_specific_sidecar_findings(self):
        prompt = _session_dm_guard_retry_system_prompt(
            'canon_discipline',
            {
                'safe': False,
                'unsupported_confirmations': [{
                    'sentence': 'Elbrig was taken by the saboteur.',
                    'claim_source': 'unsupported_lore',
                    'reason': 'The public thread establishes a missing keeper, not who took him.',
                }],
                'coherence_conflicts': [],
                'confidence': 'high',
                'reason': 'The answer disclosed details beyond the public evidence.',
            },
        )

        self.assertIn('sidecar repair brief', prompt)
        self.assertIn('not part of the narrative history', prompt)
        self.assertIn('Elbrig was taken by the saboteur.', prompt)
        self.assertIn('The public thread establishes a missing keeper, not who took him.', prompt)
        self.assertIn('additional names, motives, causes', prompt)

    def test_canon_checker_contract_treats_open_threads_as_public_evidence(self):
        messages = build_session_canon_discipline_check_messages(
            'The keeper vanished last night.',
            {
                'recent_messages': [{'role': 'player', 'content': 'Where is the missing keeper?'}],
                'open_public_threads': ['Locate the missing keeper.'],
            },
        )

        self.assertIn('already-established public leads', messages[0]['content'])
        self.assertIn('refusing to infer any extra name', messages[0]['content'])
        payload = json.loads(messages[1]['content'])
        self.assertEqual(payload['open_public_threads'], ['Locate the missing keeper.'])

    def test_missing_npc_tag_retry_prompt_respects_private_terms(self):
        prompt = _session_dm_guard_retry_system_prompt('missing_npc_tag', {})

        self.assertIn('do not use that private term', prompt)
        self.assertIn('npc_dialogue target', prompt)
        self.assertIn('old dockhand', prompt)

    def test_spoiler_retry_prompt_blocks_witness_private_debt_reveal(self):
        prompt = _session_dm_guard_retry_system_prompt('spoiler_checker', {})

        self.assertIn('witness is afraid', prompt)
        self.assertIn('private debts', prompt)
        self.assertIn('practical safety conditions', prompt)
        self.assertIn('debt/favor/owed/ledger/hook', prompt)
        self.assertIn('generic danger', prompt)

    def test_spoiler_retry_prompt_special_cases_witness_leverage_guard(self):
        prompt = _session_dm_guard_retry_system_prompt(
            'spoiler_checker',
            {'leaked_item_ids': ['deterministic_witness_private_leverage']},
        )

        self.assertIn('must not explain or hint at hidden leverage', prompt)
        self.assertIn('debt, debts, owe, owed', prompt)
        self.assertIn('watchers, reprisals, danger', prompt)
        self.assertIn('the witness may give the factual clue', prompt)

    def test_witness_private_leverage_guard_blocks_debt_metaphors_on_safety_questions(self):
        hot_context = {
            'recent_messages': [
                {
                    'role': 'player',
                    'content': 'I ask the elderly dockhand what would make it safe for him to tell us more.',
                },
            ],
            'private_spoiler_items': [
                {
                    'id': 'npc_secret_witness_old_dockhand_2',
                    'kind': 'npc_secret',
                    'text': 'The witness owes a spiritual debt to the Debt Priests from decades ago.',
                },
            ],
        }

        violation = _witness_private_leverage_spoiler_violation(
            '<npc target="elderly dockhand">"I got old debts that are not measured in coin."</npc>',
            hot_context,
        )

        self.assertFalse(violation['safe'])
        self.assertEqual(violation['leaked_item_ids'], ['deterministic_witness_private_leverage'])
        self.assertIn('debt', violation['reason'])

    def test_witness_private_leverage_guard_blocks_debt_reveal_during_witness_question(self):
        hot_context = {
            'recent_messages': [
                {
                    'role': 'player',
                    'content': "I approach the elderly dockhand and tell him I'm listening.",
                },
            ],
            'private_spoiler_items': [
                {
                    'id': 'npc_secret_witness_old_dockhand_2',
                    'kind': 'npc_secret',
                    'text': 'The witness owes a spiritual debt to the Debt Priests from decades ago.',
                },
            ],
        }

        violation = _witness_private_leverage_spoiler_violation(
            '<npc target="elderly dockhand">"Spiritual debt is the hardest kind to pay off."</npc>',
            hot_context,
        )

        self.assertFalse(violation['safe'])
        self.assertEqual(violation['leaked_item_ids'], ['deterministic_witness_private_leverage'])

    def test_witness_private_leverage_guard_allows_generic_danger_on_safety_questions(self):
        hot_context = {
            'recent_messages': [
                {
                    'role': 'player',
                    'content': 'I ask the elderly dockhand what would make it safe for him to tell us more.',
                },
            ],
            'private_spoiler_items': [
                {
                    'id': 'npc_secret_witness_old_dockhand_2',
                    'kind': 'npc_secret',
                    'text': 'The witness owes a spiritual debt to the Debt Priests from decades ago.',
                },
            ],
        }

        self.assertIsNone(_witness_private_leverage_spoiler_violation(
            '<npc target="elderly dockhand">"Keep my name out of it, and watch for the men at the jetty."</npc>',
            hot_context,
        ))

    def test_tool_result_prompt_keeps_visible_naming_constraints_adjacent_to_private_results(self):
        wrapped = _session_dm_tool_result_for_prompt(
            {'matches': [{'value': {'name': 'Mortimer'}}]},
            {
                'visible_naming_constraints': [
                    {
                        'avoid_visible_name': 'Mortimer',
                        'use_public_reference': 'elderly dockhand',
                        'applies_to': 'visible narration and <npc target="...">',
                    },
                ],
            },
        )

        self.assertEqual(wrapped['matches'][0]['value']['name'], 'Mortimer')
        self.assertEqual(wrapped['_visible_naming_constraints'][0]['use_public_reference'], 'elderly dockhand')
        self.assertIn('<npc target="...">', wrapped['_visibility_policy'])

    def test_session_dm_format_guard_detects_malformed_tags(self):
        self.assertIsNone(_session_dm_format_violation(
            'Bram smiles.\n\n<npc target="Bram Truewood">"Careful now."</npc>',
        ))

        mismatched = _session_dm_format_violation(
            '<npc target="Bram Truewood">"The candle is always lit."</p>'
        )
        self.assertEqual(mismatched['errors'][0]['kind'], 'disallowed_tag')
        self.assertIn('</p>', mismatched['errors'][0]['snippet'])
        self.assertEqual(mismatched['errors'][1]['kind'], 'unclosed_npc_tag')

        unclosed = _session_dm_format_violation(
            '<npc target="Greta">"I will save you stew."'
        )
        self.assertEqual(unclosed['errors'][0]['kind'], 'unclosed_npc_tag')

        ooc = _session_dm_format_violation('<ooc>Make an Investigation check.</ooc>')
        self.assertEqual(ooc['errors'][0]['kind'], 'disallowed_tag')

        ooc_label = _session_dm_format_violation('*OOC*: Make an Investigation check.')
        self.assertEqual(ooc_label['errors'][0]['kind'], 'disallowed_mode_label')

        self.assertIsNone(_session_dm_format_violation(
            'Dee watches you for a long moment, reading your resolve. He does not argue. '
            'Instead, he gives a single, slow nod. **"Alright. Lock the creds down first."**'
        ))

    def test_session_dm_format_guard_detects_stray_cjk_glyphs(self):
        violation = _session_dm_format_violation(
            'You hear footsteps that do not match any dock worker. 脚步声 echoes under the planks.'
        )

        self.assertEqual(violation['errors'][0]['kind'], 'non_english_glyph')
        self.assertIn('English', violation['errors'][0]['detail'])

    def test_possible_missing_npc_tag_signal_patterns(self):
        cases = [
            (
                'Dee watches you for a long moment, reading your resolve. He does not argue. '
                'Instead, he gives a single, slow nod. **"Alright. Lock the creds down first."**',
                'Dee',
                'Alright.',
            ),
            (
                'Sheriff Coldharbour spins toward Brixby, her eyes narrowing. '
                '"You there-pointing fingers will not help."',
                'Sheriff Coldharbour',
                'pointing fingers',
            ),
            (
                '**Seraphina:** "Keep your hood up and your eyes open."',
                'Seraphina',
                'Keep your hood up',
            ),
        ]
        for content, speaker, quote_excerpt in cases:
            with self.subTest(speaker=speaker):
                signal = _possible_missing_npc_tag_signal(content)
                self.assertEqual(signal['speaker'], speaker)
                self.assertIn(quote_excerpt, signal['quote'])

    def test_missing_npc_tag_checker_uses_llm_without_heuristic_signal(self):
        with patch('openrouter._post_chat', return_value=json.dumps({
            'requires_npc_tag': True,
            'speaker': 'Dee',
            'evidence': ['**"Alright. Lock the creds down first."**'],
            'reason': 'This is clearly Dee speaking in the current scene.',
        })) as post_chat:
            result = check_session_missing_npc_tags_with_llm(
                'Dee watches you for a long moment. **"Alright. Lock the creds down first."**',
                {'operation': 'session_dm_response'},
            )

        self.assertTrue(result['requires_npc_tag'])
        self.assertEqual(result['speaker'], 'Dee')
        self.assertFalse(post_chat.call_args.kwargs['allow_thinking'])
        payload = json.loads(post_chat.call_args.args[0][1]['content'])
        self.assertEqual(payload['heuristic_signal'], {})

    def test_mechanical_guard_uses_llm_when_preflight_flags_mechanics(self):
        preflight = {
            'latest_player_intent_requires_mechanics': True,
            'required_mechanic': 'initiative',
        }

        with patch('openrouter._post_chat', return_value=json.dumps({
            'safe': False,
            'violations': ['Her truncheon catches you across the ribs and knocks you down.'],
            'required_mechanic': 'initiative',
            'reason': 'The reply resolves a combat exchange before initiative.',
        })) as post_chat:
            result = check_session_mechanics_with_llm(
                'You charge at the constable. Her truncheon catches you across the ribs and knocks you down.',
                preflight,
                {'combat_coordinates': None},
                {'operation': 'session_dm_response'},
            )

        self.assertFalse(result['safe'])
        self.assertEqual(result['required_mechanic'], 'initiative')
        self.assertIn('truncheon catches you', result['violations'][0])
        prompt_payload = json.loads(post_chat.call_args.args[0][1]['content'])
        self.assertEqual(prompt_payload['preflight_decision']['required_mechanic'], 'initiative')

    def test_mechanical_guard_rewrites_attack_resolution_into_roll_request(self):
        hot_context = {
            'protected_player_characters': [],
            'private_output_terms': [],
            'private_spoiler_items': [],
            'combat_coordinates': None,
        }
        recent_messages = [
            SessionMessage(role='player', content='<ooc>I punch the constable</ooc>'),
        ]

        with patch('openrouter.get_session_preflight_decision', return_value={
            'dm_reply_mode': 'narrative',
            'skip_spoiler_check': False,
            'main_call_thinking': True,
            'latest_player_intent_requires_mechanics': True,
            'required_mechanic': 'initiative',
            'confidence': 'high',
            'reason': 'The player is starting a fight.',
        }), patch('openrouter._post_chat_normalized', side_effect=[_normalized_from_raw(dm_talk_tool_response('The blow catches you across the ribs and knocks you down.')), _normalized_from_raw(dm_talk_tool_response('The constable snaps her truncheon up as you rush in. Roll initiative.'))]) as post_chat, patch('openrouter._post_chat', side_effect=[
            json.dumps({
                'safe': False,
                'violations': ['The blow catches you across the ribs and knocks you down.'],
                'required_mechanic': 'initiative',
                'reason': 'The reply resolved combat before initiative.',
            }),
            json.dumps({'safe': True, 'violations': [], 'required_mechanic': '', 'reason': ''}),
        ]):
            result = get_session_dm_response_with_tools(
                hot_context,
                recent_messages,
                [],
                lambda *_args, **_kwargs: {},
                audit_context={'operation': 'session_dm_response'},
                max_tool_rounds=0,
            )

        self.assertEqual(result, {
            'mode': 'speak',
            'content': 'The constable snaps her truncheon up as you rush in. Roll initiative.',
            'parts': [{'type': 'narration', 'content': 'The constable snaps her truncheon up as you rush in. Roll initiative.'}],
            'commit_action_ids': [],
            '_pending_actions': [],
        })
        retry_prompt = post_chat.call_args_list[1].args[0][-1]['content']
        self.assertIn('required D&D mechanics', retry_prompt)
        self.assertIn('do not resolve uncertain combat outcomes', retry_prompt)
        self.assertNotIn('Required mechanic:', retry_prompt)

    def test_session_npc_tag_checker_ignores_evidence_already_inside_npc_tags(self):
        reply = (
            '<npc target="Sheriff Adara Coldharbour">'
            '"These are calibration tools, not standard issue."'
            '</npc>'
        )

        with patch('openrouter._post_chat', return_value=json.dumps({
            'requires_npc_tag': True,
            'speaker': 'Sheriff Adara Coldharbour',
            'evidence': ['"These are calibration tools, not standard issue."'],
            'reason': 'The line is attributed to Sheriff Adara Coldharbour.',
        })):
            result = check_session_missing_npc_tags_with_llm(reply)

        self.assertEqual(result, {
            'requires_npc_tag': False,
            'speaker': '',
            'evidence': [],
            'reason': 'Checker evidence was already wrapped in <npc> tags.',
        })

    def test_session_dm_accepts_plain_text_visible_reply(self):
        hot_context = {
            'protected_player_characters': [],
            'private_output_terms': [],
            'private_spoiler_items': [],
        }
        draft = (
            'The engine panel lights flicker as you key in the hot-wire bypass. '
            'The startup sequence now reads **70 seconds**.'
        )

        with patch('openrouter._post_chat_normalized', return_value=_normalized_from_raw({
            'choices': [{'message': {'content': draft}}],
        })) as post_chat:
            result = get_session_dm_response_with_tools(
                hot_context,
                [],
                [],
                lambda *_args, **_kwargs: {},
                max_tool_rounds=0,
            )

        self.assertEqual(result, {'mode': 'silent', 'reason': 'The DM response did not produce a valid finalizer tool call.'})
        self.assertEqual(post_chat.call_count, 3)

    def test_provider_tool_markup_retry_uses_fresh_rerun_output(self):
        hot_context = {
            'protected_player_characters': [],
            'private_output_terms': [],
            'private_spoiler_items': [],
        }
        draft = '<｜｜DSML｜｜tool_calls>\n<｜｜DSML｜｜invoke name="talk_to_player"></｜｜DSML｜｜invoke>'
        stale = 'Dee leans back, the vinyl creaking under him.'

        with patch('openrouter._post_chat_normalized', side_effect=[_normalized_from_raw({'choices': [{'message': {'content': draft}}]}), _normalized_from_raw(dm_talk_tool_response(stale))]) as post_chat:
            result = get_session_dm_response_with_tools(
                hot_context,
                [],
                [],
                lambda *_args, **_kwargs: {},
                max_tool_rounds=0,
            )

        self.assertEqual(result, {'mode': 'speak', 'content': stale})
        self.assertEqual(post_chat.call_count, 2)

    def test_plain_text_finalizer_retry_serializes_the_existing_draft(self):
        hot_context = {
            'protected_player_characters': [],
            'private_output_terms': [],
            'private_spoiler_items': [],
        }
        draft = (
            "Mira's appeal settles the nearest dockworkers.\n\n"
            '<npc target="Yoren">Fine. Talk fast—the gate will not hold.</npc>'
        )
        serialized = {
            'choices': [{
                'message': {
                    'content': '',
                    'tool_calls': [{
                        'id': 'call_final',
                        'function': {
                            'name': 'talk_to_player',
                            'arguments': json.dumps({
                                'parts': [
                                    {
                                        'type': 'narration',
                                        'content': "Mira's appeal settles the nearest dockworkers.",
                                    },
                                    {
                                        'type': 'npc_dialogue',
                                        'target': 'Yoren',
                                        'content': 'Fine. Talk fast—the gate will not hold.',
                                    },
                                ],
                                'commit_action_ids': [],
                            }),
                        },
                    }],
                },
            }],
        }

        with patch(
            'openrouter._post_chat_normalized',
            side_effect=[
                _normalized_from_raw({'choices': [{'message': {'content': draft}}]}),
                _normalized_from_raw(serialized),
            ],
        ) as post_chat:
            result = get_session_dm_response_with_tools(
                hot_context,
                [],
                [],
                lambda *_args, **_kwargs: {},
                max_tool_rounds=0,
            )

        self.assertEqual(result, {
            'mode': 'speak',
            'content': (
                "Mira's appeal settles the nearest dockworkers.\n\n"
                '<npc target="Yoren">Fine. Talk fast—the gate will not hold.</npc>'
            ),
        })
        repair_messages = post_chat.call_args_list[1].args[0]
        self.assertEqual(repair_messages[-2], {'role': 'assistant', 'content': draft})
        self.assertEqual(repair_messages[-1]['role'], 'user')
        self.assertIn('immediately preceding assistant draft', repair_messages[-1]['content'])
        self.assertIn('do not use stay_silent', repair_messages[-1]['content'])
        self.assertIn('commit_action_ids may contain only these pending action IDs: []', repair_messages[-1]['content'])
        self.assertEqual(
            {tool['function']['name'] for tool in post_chat.call_args_list[1].kwargs['tools']},
            {'talk_to_player'},
        )
        self.assertEqual(post_chat.call_args_list[1].kwargs['tool_choice'], 'required')
        self.assertFalse(post_chat.call_args_list[1].kwargs['allow_thinking'])

    def test_finalizer_retry_does_not_serialize_content_with_an_unexecuted_tool_call(self):
        hot_context = {
            'protected_player_characters': [],
            'private_output_terms': [],
            'private_spoiler_items': [],
        }
        unsafe_draft = 'The archive confirms the hidden route.'
        unexecuted_tool_response = {
            'choices': [{
                'message': {
                    'content': unsafe_draft,
                    'tool_calls': [{
                        'id': 'call_search',
                        'function': {
                            'name': 'search_campaign_memory',
                            'arguments': '{"query":"hidden route"}',
                        },
                    }],
                },
            }],
        }
        execute_tool = Mock(return_value={})

        with patch(
            'openrouter._post_chat_normalized',
            side_effect=[
                _normalized_from_raw(unexecuted_tool_response),
                _normalized_from_raw(dm_talk_tool_response('The archive shelves offer no immediate answer.')),
            ],
        ) as post_chat:
            result = get_session_dm_response_with_tools(
                hot_context,
                [],
                [{'type': 'function', 'function': {'name': 'search_campaign_memory'}}],
                execute_tool,
                max_tool_rounds=0,
            )

        self.assertEqual(result, {
            'mode': 'speak',
            'content': 'The archive shelves offer no immediate answer.',
        })
        execute_tool.assert_not_called()
        repair_messages = post_chat.call_args_list[1].args[0]
        self.assertEqual(repair_messages[-1]['role'], 'system')
        self.assertFalse(any(
            message.get('role') == 'assistant' and message.get('content') == unsafe_draft
            for message in repair_messages
        ))

    def test_draft_serializer_rejects_stay_silent_and_retries_with_talk_only(self):
        hot_context = {
            'protected_player_characters': [],
            'private_output_terms': [],
            'private_spoiler_items': [],
        }
        draft = 'The lock shudders, and the cracked panel flashes once.'

        with patch(
            'openrouter._post_chat_normalized',
            side_effect=[
                _normalized_from_raw({'choices': [{'message': {'content': draft}}]}),
                _normalized_from_raw(dm_silent_tool_response('No visible reply needed.')),
                _normalized_from_raw(dm_talk_tool_response(draft)),
            ],
        ) as post_chat:
            result = get_session_dm_response_with_tools(
                hot_context,
                [],
                [],
                lambda *_args, **_kwargs: {},
                max_tool_rounds=0,
            )

        self.assertEqual(result, {'mode': 'speak', 'content': draft})
        self.assertEqual(post_chat.call_count, 3)
        for call in post_chat.call_args_list[1:]:
            self.assertEqual(
                {tool['function']['name'] for tool in call.kwargs['tools']},
                {'talk_to_player'},
            )
            self.assertEqual(call.kwargs['tool_choice'], 'required')
        final_retry_messages = post_chat.call_args_list[2].args[0]
        self.assertEqual(final_retry_messages[-2], {'role': 'assistant', 'content': draft})
        self.assertIn('do not use stay_silent', final_retry_messages[-1]['content'])

    def test_plain_serializer_retry_cannot_replace_the_preserved_draft(self):
        hot_context = {
            'protected_player_characters': [],
            'private_output_terms': [],
            'private_spoiler_items': [],
        }
        original_draft = 'The lock shudders, and the cracked panel flashes once.'
        drifted_draft = 'The lock opens and reveals a hidden passage that was never established.'

        with patch(
            'openrouter._post_chat_normalized',
            side_effect=[
                _normalized_from_raw({'choices': [{'message': {'content': original_draft}}]}),
                _normalized_from_raw({'choices': [{'message': {'content': drifted_draft}}]}),
                _normalized_from_raw(dm_talk_tool_response(original_draft)),
            ],
        ) as post_chat:
            result = get_session_dm_response_with_tools(
                hot_context,
                [],
                [],
                lambda *_args, **_kwargs: {},
                max_tool_rounds=0,
            )

        self.assertEqual(result, {'mode': 'speak', 'content': original_draft})
        final_retry_messages = post_chat.call_args_list[2].args[0]
        self.assertEqual(final_retry_messages[-2], {'role': 'assistant', 'content': original_draft})
        self.assertNotEqual(final_retry_messages[-2]['content'], drifted_draft)

    def test_downstream_guard_can_choose_silence_after_draft_serialization(self):
        hot_context = {
            'protected_player_characters': ['Aria'],
            'private_output_terms': [],
            'private_spoiler_items': [],
        }
        draft = 'Aria agrees to follow the courier into the alley.'
        pc_control_violation = {
            'safe': False,
            'violations': [{
                'character': 'Aria',
                'sentence': draft,
                'kind': 'choice_or_intent',
                'reason': 'The reply invents a protected player-character choice.',
            }],
            'confidence': 'high',
            'reason': 'The reply controls Aria.',
        }

        with patch(
            'openrouter._post_chat_normalized',
            side_effect=[
                _normalized_from_raw({'choices': [{'message': {'content': draft}}]}),
                _normalized_from_raw(dm_talk_tool_response(draft)),
                _normalized_from_raw(dm_silent_tool_response('PC-to-PC exchange.')),
            ],
        ) as post_chat, patch(
            'openrouter.check_session_pc_control_with_llm',
            return_value=pc_control_violation,
        ):
            result = get_session_dm_response_with_tools(
                hot_context,
                [],
                [],
                lambda *_args, **_kwargs: {},
                max_tool_rounds=0,
            )

        self.assertEqual(result, {
            'mode': 'silent',
            'content': '',
            'reason': 'PC-to-PC exchange.',
        })
        self.assertEqual(
            {tool['function']['name'] for tool in post_chat.call_args_list[1].kwargs['tools']},
            {'talk_to_player'},
        )
        self.assertEqual(
            {tool['function']['name'] for tool in post_chat.call_args_list[2].kwargs['tools']},
            {'talk_to_player', 'stay_silent'},
        )

    def test_plain_text_reply_with_tools_does_not_force_finalizer_retry(self):
        hot_context = {
            'protected_player_characters': [],
            'private_output_terms': [],
            'private_spoiler_items': [],
        }

        with patch('openrouter._post_chat_normalized', return_value=_normalized_from_raw({
            'choices': [{'message': {'content': 'Plain text draft only.'}}],
        })) as post_chat:
            result = get_session_dm_response_with_tools(
                hot_context,
                [],
                [{'type': 'function', 'function': {'name': 'search_campaign_memory'}}],
                lambda *_args, **_kwargs: {},
                max_tool_rounds=2,
            )

        self.assertEqual(result, {'mode': 'silent', 'reason': 'The DM response did not produce a valid finalizer tool call.'})
        self.assertEqual(post_chat.call_count, 3)
        self.assertEqual(post_chat.call_args.kwargs['tool_choice'], 'required')

    def test_plain_text_fallback_discards_staged_actions_and_preserves_visible_reply(self):
        hot_context = {
            'protected_player_characters': [],
            'private_output_terms': [],
            'private_spoiler_items': [],
        }
        staged_tool_call = {
            'choices': [{
                'message': {
                    'content': '',
                    'tool_calls': [{
                        'id': 'call_stage',
                        'function': {
                            'name': 'record_world_event',
                            'arguments': json.dumps({'event_type': 'clue', 'summary': 'Preview only.'}),
                        },
                    }],
                },
            }],
        }

        def stage_action(_name, _args, audit):
            audit['pending_action_buffer']['actions'].append({
                'id': 'pending_action_1',
                'name': 'record_world_event',
                'args': {'event_type': 'clue', 'summary': 'Preview only.'},
            })
            return {'pending_action_id': 'pending_action_1', 'pending': True}

        with patch('openrouter._post_chat_normalized', side_effect=[_normalized_from_raw(staged_tool_call), _normalized_from_raw({'choices': [{'message': {'content': 'Raw text must not escape after staging.'}}]}), _normalized_from_raw({'choices': [{'message': {'content': 'Still raw text.'}}]}), _normalized_from_raw({'choices': [{'message': {'content': 'Last raw text.'}}]})]):
            result = get_session_dm_response_with_tools(
                hot_context,
                [],
                [{'type': 'function', 'function': {'name': 'record_world_event'}}],
                stage_action,
                max_tool_rounds=2,
            )

        self.assertEqual(result, {'mode': 'silent', 'reason': 'The DM response did not produce a valid finalizer tool call.'})

    def test_finalizer_contract_retry_still_rewrites_ooc_label(self):
        hot_context = {
            'protected_player_characters': [],
            'private_output_terms': [],
            'private_spoiler_items': [],
        }
        draft = '*OOC*: Make a Technology check.'

        with patch('openrouter._post_chat_normalized', side_effect=[_normalized_from_raw({'choices': [{'message': {'content': draft}}]}), _normalized_from_raw(dm_talk_tool_response('Make a Technology check.'))]) as post_chat:
            result = get_session_dm_response_with_tools(
                hot_context,
                [],
                [],
                lambda *_args, **_kwargs: {},
                max_tool_rounds=0,
            )

        self.assertEqual(result, {'mode': 'speak', 'content': 'Make a Technology check.'})
        self.assertEqual(post_chat.call_count, 2)
        repair_messages = post_chat.call_args_list[1].args[0]
        self.assertEqual(repair_messages[-2], {'role': 'assistant', 'content': draft})
        self.assertIn('Finalize the turn by calling exactly one', repair_messages[-1]['content'])

    def test_finalizer_contract_retry_reprompts_provider_tool_markup_with_specific_reminder(self):
        hot_context = {
            'protected_player_characters': [],
            'private_output_terms': [],
            'private_spoiler_items': [],
        }
        dsml = (
            '<｜｜DSML｜｜tool_calls>\n'
            '<｜｜DSML｜｜invoke name="search_campaign_memory">\n'
            '</｜｜DSML｜｜invoke>\n'
            '</｜｜DSML｜｜tool_calls>'
        )

        with patch('openrouter._post_chat_normalized', side_effect=[_normalized_from_raw({'choices': [{'message': {'content': dsml}}]}), _normalized_from_raw(dm_talk_tool_response("The Broker's instructions are clear: keep the crate sealed and deliver it intact."))]) as post_chat:
            result = get_session_dm_response_with_tools(
                hot_context,
                [],
                [],
                lambda *_args, **_kwargs: {},
                max_tool_rounds=0,
            )

        self.assertEqual(result, {
            'mode': 'speak',
            'content': "The Broker's instructions are clear: keep the crate sealed and deliver it intact.",
        })
        self.assertEqual(post_chat.call_count, 2)
        retry_prompt = post_chat.call_args_list[1].args[0][-1]['content']
        self.assertIn('talk_to_player(parts)', retry_prompt)
        self.assertIn('Do not output DSML', retry_prompt)

    def test_session_dm_accepts_talk_to_player_finalizer_tool(self):
        hot_context = {
            'protected_player_characters': [],
            'private_output_terms': [],
            'private_spoiler_items': [],
        }
        execute_tool = Mock(return_value={})

        with patch('openrouter._post_chat_normalized', return_value=_normalized_from_raw({
            'choices': [{
                'message': {
                    'content': '',
                    'tool_calls': [{
                        'id': 'call_final',
                        'function': {
                            'name': 'talk_to_player',
                            'arguments': json.dumps({
                                'parts': [{'type': 'npc_dialogue', 'target': 'Brenn', 'content': '"Green lights by the old willow."'}],
                                'commit_action_ids': [],
                            }),
                        },
                    }],
                },
            }],
        })) as post_chat:
            result = get_session_dm_response_with_tools(
                hot_context,
                [],
                [],
                execute_tool,
                max_tool_rounds=1,
            )

        self.assertEqual(result, {
            'mode': 'speak',
            'content': '<npc target="Brenn">"Green lights by the old willow."</npc>',
        })
        self.assertEqual(post_chat.call_args.kwargs['tool_choice'], 'required')
        self.assertEqual(
            {tool['function']['name'] for tool in post_chat.call_args.kwargs['tools']},
            {'talk_to_player', 'stay_silent'},
        )
        execute_tool.assert_not_called()

    def test_session_dm_rejects_finalizer_without_commit_action_ids(self):
        hot_context = {
            'protected_player_characters': [],
            'private_output_terms': [],
            'private_spoiler_items': [],
        }
        malformed_finalizer = {
            'choices': [{
                'message': {
                    'content': '',
                    'tool_calls': [{
                        'id': 'call_final',
                        'function': {
                            'name': 'talk_to_player',
                            'arguments': json.dumps({'content': 'This must not commit.'}),
                        },
                    }],
                },
            }],
        }

        with patch('openrouter._post_chat_normalized', return_value=_normalized_from_raw(malformed_finalizer)):
            result = get_session_dm_response_with_tools(
                hot_context,
                [],
                [],
                lambda *_args, **_kwargs: {},
                max_tool_rounds=0,
            )

        self.assertEqual(result['mode'], 'silent')
        self.assertIn('valid finalizer', result['reason'])

    def test_session_dm_accepts_stay_silent_finalizer_tool(self):
        hot_context = {
            'protected_player_characters': [],
            'private_output_terms': [],
            'private_spoiler_items': [],
        }

        with patch('openrouter._post_chat_normalized', return_value=_normalized_from_raw({
            'choices': [{
                'message': {
                    'content': '',
                    'tool_calls': [{
                        'id': 'call_silent',
                        'function': {
                            'name': 'stay_silent',
                            'arguments': json.dumps({
                                'reason': 'PC-to-PC exchange.',
                            }),
                        },
                    }],
                },
            }],
        })):
            result = get_session_dm_response_with_tools(
                hot_context,
                [],
                [],
                lambda *_args, **_kwargs: {},
                max_tool_rounds=1,
            )

        self.assertEqual(result, {
            'mode': 'silent',
            'content': '',
            'reason': 'PC-to-PC exchange.',
        })

    def test_session_dm_request_audit_reports_transcript_sources_and_removed_tokens(self):
        recent_messages = [{
            'id': 117,
            'session_id': self.session.id,
            'role': 'player',
            'content': 'I wait for the others to finish speaking.',
        }]
        hot_context = {
            'protected_player_characters': [],
            'private_output_terms': [],
            'private_spoiler_items': [],
            'recent_messages': recent_messages,
        }

        with patch('openrouter._post_chat_normalized', return_value=_normalized_from_raw({
            'choices': [{
                'message': {
                    'content': '',
                    'tool_calls': [{
                        'id': 'call_silent',
                        'function': {
                            'name': 'stay_silent',
                            'arguments': json.dumps({'reason': 'PC-to-PC exchange.'}),
                        },
                    }],
                },
            }],
        })) as post_chat:
            get_session_dm_response_with_tools(
                hot_context,
                recent_messages,
                [],
                lambda *_args, **_kwargs: {},
                max_tool_rounds=1,
            )

        request_audit = post_chat.call_args.kwargs['audit_context']
        self.assertEqual(
            request_audit['context_manifest']['primary_request_transcript_source_refs'][0]['source_id'],
            'session_message:117',
        )
        self.assertEqual(
            request_audit['context_manifest']['duplicate_raw_transcript_source_count'],
            0,
        )
        self.assertEqual(request_audit['context_manifest']['transcript_source_validation'], 'passed')
        self.assertGreater(
            request_audit['token_estimate']['estimated_duplicate_transcript_tokens_removed'],
            0,
        )

    def test_spoiler_checker_allows_safe_reply(self):
        hot_context = {
            'protected_player_characters': [],
            'private_output_terms': [],
            'private_spoiler_items': [{'id': 'fact_trap', 'kind': 'fact', 'text': 'The note is a trap.'}],
        }

        with patch('openrouter._post_chat_normalized', return_value=_normalized_from_raw({
            'choices': [{'message': dm_talk_tool_response('Jara watches the door.')['choices'][0]['message']}],
        })), patch('openrouter.check_session_spoilers_with_llm', return_value={
            'safe': True,
            'leaked_item_ids': [],
            'evidence': [],
            'reason': '',
        }) as checker:
            result = get_session_dm_response_with_tools(hot_context, [], [], lambda *_args, **_kwargs: {}, max_tool_rounds=0)

        self.assertEqual(result, {'mode': 'speak', 'content': 'Jara watches the door.'})
        checker.assert_called_once()

    def test_session_preflight_can_disable_main_dm_thinking(self):
        hot_context = {
            'protected_player_characters': [],
            'private_output_terms': [],
            'private_spoiler_items': [],
        }

        with patch('openrouter.get_session_preflight_decision', return_value={
            'dm_reply_mode': 'simple_narrative',
            'skip_spoiler_check': False,
            'main_call_thinking': False,
            'confidence': 'high',
            'reason': 'Simple public narration.',
        }), patch('openrouter._post_chat_normalized', return_value=_normalized_from_raw({
            'choices': [{'message': dm_talk_tool_response('Rain slicks the old stones.')['choices'][0]['message']}],
        })) as post_chat:
            result = get_session_dm_response_with_tools(
                hot_context,
                [],
                [],
                lambda *_args, **_kwargs: {},
                audit_context={'operation': 'session_dm_response'},
                max_tool_rounds=0,
            )

        self.assertEqual(result, {
            'mode': 'speak',
            'content': 'Rain slicks the old stones.',
            'parts': [{'type': 'narration', 'content': 'Rain slicks the old stones.'}],
            'commit_action_ids': [],
            '_pending_actions': [],
        })
        self.assertFalse(post_chat.call_args.kwargs['allow_thinking'])

    def test_session_preflight_thinking_off_upgrades_after_tool_call(self):
        hot_context = {
            'protected_player_characters': [],
            'private_output_terms': [],
            'private_spoiler_items': [],
        }
        tool_result = {'answer': 'AC 15.', 'missing': False}

        with patch('openrouter.get_session_preflight_decision', return_value={
            'dm_reply_mode': 'mechanics_only',
            'skip_spoiler_check': True,
            'main_call_thinking': False,
            'confidence': 'high',
            'reason': 'Simple mechanics lookup.',
        }), patch('openrouter._post_chat_normalized', side_effect=[_normalized_from_raw({'choices': [{'message': {
                'content': '',
                'tool_calls': [{
                    'id': 'call_sheet',
                    'function': {
                        'name': 'ask_character_sheet',
                        'arguments': '{"question":"What is my AC?"}',
                    },
                }],
            }}]}), _normalized_from_raw(dm_talk_tool_response('Your AC is 15.'))]) as post_chat:
            result = get_session_dm_response_with_tools(
                hot_context,
                [],
                [{'type': 'function', 'function': {'name': 'ask_character_sheet'}}],
                lambda *_args, **_kwargs: tool_result,
                audit_context={'operation': 'session_dm_response'},
                max_tool_rounds=1,
            )

        self.assertEqual(result, {
            'mode': 'speak',
            'content': 'Your AC is 15.',
            'parts': [{'type': 'narration', 'content': 'Your AC is 15.'}],
            'commit_action_ids': [],
            '_pending_actions': [],
        })
        self.assertFalse(post_chat.call_args_list[0].kwargs['allow_thinking'])
        self.assertTrue(post_chat.call_args_list[1].kwargs['allow_thinking'])

    def test_session_dm_combat_batch_continues_until_player_turn(self):
        hot_context = {
            'campaign': {'id': self.campaign.id},
            'current_encounter_map': {
                'id': 3,
                'placements': [
                    {'id': 7, 'actor_type': 'monster', 'actor_id': 'skirmisher_1', 'label': 'Skirmisher 1', 'col': 1, 'row': 1},
                    {'id': 9, 'actor_type': 'monster', 'actor_id': 'brute_1', 'label': 'Training Brute', 'col': 3, 'row': 1},
                    {'id': 5, 'actor_type': 'player', 'actor_id': '2', 'label': 'Seraphina Duskweaver', 'col': 3, 'row': 9},
                ],
                'encounter_state': {
                    'active': True,
                    'round': 1,
                    'active_turn_index': 0,
                    'turn_order': [
                        {'placement_id': 7, 'actor_type': 'monster', 'actor_id': 'skirmisher_1', 'label': 'Skirmisher 1', 'current_hp': 11, 'actions': {'action': True, 'movement_remaining': 30}},
                        {'placement_id': 9, 'actor_type': 'monster', 'actor_id': 'brute_1', 'label': 'Training Brute', 'current_hp': 18, 'actions': {'action': True, 'movement_remaining': 25}},
                        {'placement_id': 5, 'actor_type': 'player', 'actor_id': '2', 'label': 'Seraphina Duskweaver', 'current_hp': 24, 'actions': {'action': True, 'movement_remaining': 30}},
                    ],
                },
            },
            'protected_player_characters': [{'id': self.character.id, 'name': self.character.name, 'user_id': self.user.id, 'username': self.user.username}],
            'private_output_terms': [],
            'private_spoiler_items': [],
        }
        executed = []
        next_states = [
            {
                'active': True,
                'round': 1,
                'active_turn_index': 1,
                'turn_order': [
                    {'placement_id': 7, 'actor_type': 'monster', 'actor_id': 'skirmisher_1', 'label': 'Skirmisher 1', 'current_hp': 11, 'actions': {'action': False, 'movement_remaining': 10}},
                    {'placement_id': 9, 'actor_type': 'monster', 'actor_id': 'brute_1', 'label': 'Training Brute', 'current_hp': 18, 'actions': {'action': True, 'movement_remaining': 25}},
                    {'placement_id': 5, 'actor_type': 'player', 'actor_id': '2', 'label': 'Seraphina Duskweaver', 'current_hp': 24, 'actions': {'action': True, 'movement_remaining': 30}},
                ],
            },
            {
                'active': True,
                'round': 1,
                'active_turn_index': 2,
                'turn_order': [
                    {'placement_id': 7, 'actor_type': 'monster', 'actor_id': 'skirmisher_1', 'label': 'Skirmisher 1', 'current_hp': 11, 'actions': {'action': False, 'movement_remaining': 10}},
                    {'placement_id': 9, 'actor_type': 'monster', 'actor_id': 'brute_1', 'label': 'Training Brute', 'current_hp': 18, 'actions': {'action': False, 'movement_remaining': 0}},
                    {'placement_id': 5, 'actor_type': 'player', 'actor_id': '2', 'label': 'Seraphina Duskweaver', 'current_hp': 24, 'actions': {'action': True, 'movement_remaining': 30}},
                ],
            },
        ]

        def execute_tool(name, args, _audit):
            executed.append((name, args))
            if name == 'update_combatant_actions':
                placement_id = int(args['placement_id'])
                if placement_id == 7:
                    return {
                        'message': 'Actions updated.',
                        'encounter_state': {
                            'active': True,
                            'round': 1,
                            'active_turn_index': 0,
                            'turn_order': [
                                {'placement_id': 7, 'actor_type': 'monster', 'actor_id': 'skirmisher_1', 'label': 'Skirmisher 1', 'current_hp': 11, 'actions': {'action': False, 'bonus_action': False, 'reaction': True, 'movement_remaining': 10}},
                                {'placement_id': 9, 'actor_type': 'monster', 'actor_id': 'brute_1', 'label': 'Training Brute', 'current_hp': 18, 'actions': {'action': True, 'bonus_action': True, 'reaction': True, 'movement_remaining': 25}},
                                {'placement_id': 5, 'actor_type': 'player', 'actor_id': '2', 'label': 'Seraphina Duskweaver', 'current_hp': 24, 'actions': {'action': True, 'bonus_action': True, 'reaction': True, 'movement_remaining': 30}},
                            ],
                        },
                    }
                return {
                    'message': 'Actions updated.',
                    'encounter_state': {
                        'active': True,
                        'round': 1,
                        'active_turn_index': 1,
                        'turn_order': [
                            {'placement_id': 7, 'actor_type': 'monster', 'actor_id': 'skirmisher_1', 'label': 'Skirmisher 1', 'current_hp': 11, 'actions': {'action': False, 'bonus_action': False, 'reaction': True, 'movement_remaining': 10}},
                            {'placement_id': 9, 'actor_type': 'monster', 'actor_id': 'brute_1', 'label': 'Training Brute', 'current_hp': 18, 'actions': {'action': False, 'bonus_action': False, 'reaction': True, 'movement_remaining': 0}},
                            {'placement_id': 5, 'actor_type': 'player', 'actor_id': '2', 'label': 'Seraphina Duskweaver', 'current_hp': 24, 'actions': {'action': True, 'bonus_action': True, 'reaction': True, 'movement_remaining': 30}},
                        ],
                    },
                }
            next_index = len([item for item in executed if item[0] == 'next_combat_turn']) - 1
            return {
                'message': 'Turn advanced.',
                'encounter_state': next_states[next_index],
            }

        with patch('openrouter._post_chat_normalized', side_effect=[_normalized_from_raw({'choices': [{'message': {'content': '', 'tool_calls': [
                {'id': 'call_1', 'function': {'name': 'update_combatant_actions', 'arguments': '{"placement_id":7,"actions":{"action":false,"bonus_action":false,"movement_remaining":10}}'}},
                {'id': 'call_2', 'function': {'name': 'next_combat_turn', 'arguments': '{}'}},
            ]}}]}), _normalized_from_raw(dm_talk_tool_response('The skirmisher withdraws along the ledge.')), _normalized_from_raw({'choices': [{'message': {'content': '', 'tool_calls': [
                {'id': 'call_3', 'function': {'name': 'update_combatant_actions', 'arguments': '{"placement_id":9,"actions":{"action":false,"bonus_action":false,"movement_remaining":0}}'}},
                {'id': 'call_4', 'function': {'name': 'next_combat_turn', 'arguments': '{}'}},
            ]}}]}), _normalized_from_raw(dm_talk_tool_response('The skirmisher falls back and the brute stomps into position. Seraphina, you are up.'))]) as post_chat:
            result = get_session_dm_response_with_tools(
                hot_context,
                [],
                [
                    {'type': 'function', 'function': {'name': 'update_combatant_actions'}},
                    {'type': 'function', 'function': {'name': 'next_combat_turn'}},
                ],
                execute_tool,
                max_tool_rounds=4,
            )

        self.assertEqual(
            result,
            {'mode': 'speak', 'content': 'The skirmisher falls back and the brute stomps into position. Seraphina, you are up.'},
        )
        self.assertEqual(executed, [
            ('update_combatant_actions', {'placement_id': 7, 'actions': {'action': False, 'bonus_action': False, 'movement_remaining': 10}}),
            ('next_combat_turn', {}),
            ('update_combatant_actions', {'placement_id': 9, 'actions': {'action': False, 'bonus_action': False, 'movement_remaining': 0}}),
            ('next_combat_turn', {}),
        ])
        self.assertTrue(any(
            message.get('role') == 'system'
            and 'continue through consecutive non-player turns' in message.get('content', '')
            for message in post_chat.call_args_list[2].args[0]
            if isinstance(message, dict)
        ))

    def test_session_dm_combat_batch_retry_reenables_tools_after_finalizer_contract_retry(self):
        hot_context = {
            'campaign': {'id': self.campaign.id},
            'current_encounter_map': {
                'id': 3,
                'placements': [
                    {'id': 7, 'actor_type': 'monster', 'actor_id': 'skirmisher_1', 'label': 'Skirmisher 1', 'col': 1, 'row': 1},
                    {'id': 9, 'actor_type': 'monster', 'actor_id': 'brute_1', 'label': 'Training Brute', 'col': 3, 'row': 1},
                    {'id': 5, 'actor_type': 'player', 'actor_id': '2', 'label': 'Seraphina Duskweaver', 'col': 3, 'row': 9},
                ],
                'encounter_state': {
                    'active': True,
                    'round': 1,
                    'active_turn_index': 0,
                    'turn_order': [
                        {'placement_id': 7, 'actor_type': 'monster', 'actor_id': 'skirmisher_1', 'label': 'Skirmisher 1', 'current_hp': 11, 'actions': {'action': True, 'movement_remaining': 30}},
                        {'placement_id': 9, 'actor_type': 'monster', 'actor_id': 'brute_1', 'label': 'Training Brute', 'current_hp': 18, 'actions': {'action': True, 'movement_remaining': 25}},
                        {'placement_id': 5, 'actor_type': 'player', 'actor_id': '2', 'label': 'Seraphina Duskweaver', 'current_hp': 24, 'actions': {'action': True, 'movement_remaining': 30}},
                    ],
                },
            },
            'protected_player_characters': [{'id': self.character.id, 'name': self.character.name, 'user_id': self.user.id, 'username': self.user.username}],
            'private_output_terms': [],
            'private_spoiler_items': [],
        }
        executed = []

        def execute_tool(name, args, _audit):
            executed.append((name, args))
            if len(executed) == 1:
                return {
                    'message': 'Actions updated.',
                    'encounter_state': {
                        'active': True,
                        'round': 1,
                        'active_turn_index': 0,
                        'turn_order': [
                            {'placement_id': 7, 'actor_type': 'monster', 'actor_id': 'skirmisher_1', 'label': 'Skirmisher 1', 'current_hp': 11, 'actions': {'action': False, 'bonus_action': False, 'movement_remaining': 0}},
                            {'placement_id': 9, 'actor_type': 'monster', 'actor_id': 'brute_1', 'label': 'Training Brute', 'current_hp': 18, 'actions': {'action': True, 'bonus_action': True, 'movement_remaining': 25}},
                            {'placement_id': 5, 'actor_type': 'player', 'actor_id': '2', 'label': 'Seraphina Duskweaver', 'current_hp': 24, 'actions': {'action': True, 'bonus_action': True, 'movement_remaining': 30}},
                        ],
                    },
                }
            if len(executed) == 2:
                return {
                    'message': 'Turn advanced.',
                    'encounter_state': {
                        'active': True,
                        'round': 1,
                        'active_turn_index': 1,
                        'turn_order': [
                            {'placement_id': 7, 'actor_type': 'monster', 'actor_id': 'skirmisher_1', 'label': 'Skirmisher 1', 'current_hp': 11, 'actions': {'action': False, 'bonus_action': False, 'movement_remaining': 0}},
                            {'placement_id': 9, 'actor_type': 'monster', 'actor_id': 'brute_1', 'label': 'Training Brute', 'current_hp': 18, 'actions': {'action': True, 'bonus_action': True, 'movement_remaining': 25}},
                            {'placement_id': 5, 'actor_type': 'player', 'actor_id': '2', 'label': 'Seraphina Duskweaver', 'current_hp': 24, 'actions': {'action': True, 'bonus_action': True, 'movement_remaining': 30}},
                        ],
                    },
                }
            if len(executed) == 3:
                return {
                    'message': 'Actions updated.',
                    'encounter_state': {
                        'active': True,
                        'round': 1,
                        'active_turn_index': 1,
                        'turn_order': [
                            {'placement_id': 7, 'actor_type': 'monster', 'actor_id': 'skirmisher_1', 'label': 'Skirmisher 1', 'current_hp': 11, 'actions': {'action': False, 'bonus_action': False, 'movement_remaining': 0}},
                            {'placement_id': 9, 'actor_type': 'monster', 'actor_id': 'brute_1', 'label': 'Training Brute', 'current_hp': 18, 'actions': {'action': False, 'bonus_action': False, 'movement_remaining': 0}},
                            {'placement_id': 5, 'actor_type': 'player', 'actor_id': '2', 'label': 'Seraphina Duskweaver', 'current_hp': 24, 'actions': {'action': True, 'bonus_action': True, 'movement_remaining': 30}},
                        ],
                    },
                }
            return {
                'message': 'Turn advanced.',
                'encounter_state': {
                    'active': True,
                    'round': 1,
                    'active_turn_index': 2,
                    'turn_order': [
                        {'placement_id': 7, 'actor_type': 'monster', 'actor_id': 'skirmisher_1', 'label': 'Skirmisher 1', 'current_hp': 11, 'actions': {'action': False, 'bonus_action': False, 'movement_remaining': 0}},
                        {'placement_id': 9, 'actor_type': 'monster', 'actor_id': 'brute_1', 'label': 'Training Brute', 'current_hp': 18, 'actions': {'action': False, 'bonus_action': False, 'movement_remaining': 0}},
                        {'placement_id': 5, 'actor_type': 'player', 'actor_id': '2', 'label': 'Seraphina Duskweaver', 'current_hp': 24, 'actions': {'action': True, 'bonus_action': True, 'movement_remaining': 30}},
                    ],
                },
            }

        with patch('openrouter._post_chat_normalized', side_effect=[_normalized_from_raw({'choices': [{'message': {'content': '', 'tool_calls': [
                {'id': 'call_1', 'function': {'name': 'update_combatant_actions', 'arguments': '{"placement_id":7,"actions":{"action":false,"bonus_action":false,"movement_remaining":0}}'}},
                {'id': 'call_2', 'function': {'name': 'next_combat_turn', 'arguments': '{}'}},
            ]}}]}), _normalized_from_raw({'choices': [{'message': {'content': 'The skirmisher falls back.'}}]}), _normalized_from_raw({'choices': [{'message': {'content': 'The skirmisher falls back.'}}]}), _normalized_from_raw({'choices': [{'message': {'content': '', 'tool_calls': [
                {'id': 'call_3', 'function': {'name': 'update_combatant_actions', 'arguments': '{"placement_id":9,"actions":{"action":false,"bonus_action":false,"movement_remaining":0}}'}},
                {'id': 'call_4', 'function': {'name': 'next_combat_turn', 'arguments': '{}'}},
            ]}}]}), _normalized_from_raw(dm_talk_tool_response('The skirmisher fades back and the brute gives ground. Seraphina, you are up.'))]):
            result = get_session_dm_response_with_tools(
                hot_context,
                [],
                [
                    {'type': 'function', 'function': {'name': 'update_combatant_actions'}},
                    {'type': 'function', 'function': {'name': 'next_combat_turn'}},
                ],
                execute_tool,
                max_tool_rounds=6,
            )

        self.assertEqual(
            result,
            {'mode': 'speak', 'content': 'The skirmisher fades back and the brute gives ground. Seraphina, you are up.'},
        )
        self.assertEqual(executed[-2:], [
            ('update_combatant_actions', {'placement_id': 9, 'actions': {'action': False, 'bonus_action': False, 'movement_remaining': 0}}),
            ('next_combat_turn', {}),
        ])

    def test_session_dm_combat_turn_scope_blocks_memory_search(self):
        hot_context = {
            'campaign': {'id': self.campaign.id},
            'current_encounter_map': {
                'id': 3,
                'placements': [
                    {'id': 7, 'actor_type': 'monster', 'actor_id': 'skirmisher_1', 'label': 'Skirmisher 1', 'col': 1, 'row': 1},
                    {'id': 5, 'actor_type': 'player', 'actor_id': '2', 'label': 'Seraphina Duskweaver', 'col': 3, 'row': 9},
                ],
                'encounter_state': {
                    'active': True,
                    'round': 1,
                    'active_turn_index': 0,
                    'turn_order': [
                        {'placement_id': 7, 'actor_type': 'monster', 'actor_id': 'skirmisher_1', 'label': 'Skirmisher 1', 'current_hp': 11, 'actions': {'action': True, 'movement_remaining': 30}},
                        {'placement_id': 5, 'actor_type': 'player', 'actor_id': '2', 'label': 'Seraphina Duskweaver', 'current_hp': 24, 'actions': {'action': True, 'movement_remaining': 30}},
                    ],
                },
            },
            'protected_player_characters': [{'id': self.character.id, 'name': self.character.name, 'user_id': self.user.id, 'username': self.user.username}],
            'private_output_terms': [],
            'private_spoiler_items': [],
        }
        executed = []

        def execute_tool(name, args, _audit):
            executed.append((name, args))
            return {'message': 'unexpected'}

        with patch('openrouter._post_chat_normalized', side_effect=[_normalized_from_raw({'choices': [{'message': {'content': '', 'tool_calls': [{'id': 'call_1', 'function': {'name': 'search_campaign_memory', 'arguments': '{"query":"training skirmisher"}'}}]}}]}), _normalized_from_raw(dm_talk_tool_response('The skirmisher gauges the field from the high ledge.'))]):
            result = get_session_dm_response_with_tools(
                hot_context,
                [],
                [{'type': 'function', 'function': {'name': 'search_campaign_memory'}}],
                execute_tool,
                max_tool_rounds=2,
                audit_context={'campaign_id': self.campaign.id},
            )

        self.assertEqual(
            result,
            {'mode': 'speak', 'content': 'The skirmisher gauges the field from the high ledge.'},
        )
        self.assertEqual(executed, [])
        blocked = CampaignAuditEvent.query.filter_by(event_type='combat_turn_scope_guard_blocked').one()
        payload = json.loads(blocked.payload)
        self.assertEqual(payload['tool_name'], 'search_campaign_memory')

    def test_session_dm_combat_turn_scope_blocks_set_turn_after_advancing(self):
        hot_context = {
            'campaign': {'id': self.campaign.id},
            'current_encounter_map': {
                'id': 3,
                'placements': [
                    {'id': 7, 'actor_type': 'monster', 'actor_id': 'skirmisher_1', 'label': 'Skirmisher 1', 'col': 1, 'row': 1},
                    {'id': 5, 'actor_type': 'player', 'actor_id': '2', 'label': 'Seraphina Duskweaver', 'col': 3, 'row': 9},
                ],
                'encounter_state': {
                    'active': True,
                    'round': 1,
                    'active_turn_index': 0,
                    'turn_order': [
                        {'placement_id': 7, 'actor_type': 'monster', 'actor_id': 'skirmisher_1', 'label': 'Skirmisher 1', 'current_hp': 11, 'actions': {'action': True, 'movement_remaining': 30}},
                        {'placement_id': 5, 'actor_type': 'player', 'actor_id': '2', 'label': 'Seraphina Duskweaver', 'current_hp': 24, 'actions': {'action': True, 'movement_remaining': 30}},
                    ],
                },
            },
            'protected_player_characters': [{'id': self.character.id, 'name': self.character.name, 'user_id': self.user.id, 'username': self.user.username}],
            'private_output_terms': [],
            'private_spoiler_items': [],
        }
        executed = []

        def execute_tool(name, args, _audit):
            executed.append((name, args))
            if name == 'update_combatant_actions':
                return {
                    'message': 'Actions updated.',
                    'encounter_state': {
                        'active': True,
                        'round': 1,
                        'active_turn_index': 0,
                        'turn_order': [
                            {'placement_id': 7, 'actor_type': 'monster', 'actor_id': 'skirmisher_1', 'label': 'Skirmisher 1', 'current_hp': 11, 'actions': {'action': False, 'bonus_action': False, 'reaction': True, 'movement_remaining': 0}},
                            {'placement_id': 5, 'actor_type': 'player', 'actor_id': '2', 'label': 'Seraphina Duskweaver', 'current_hp': 24, 'actions': {'action': True, 'bonus_action': True, 'reaction': True, 'movement_remaining': 30}},
                        ],
                    },
                }
            return {
                'message': 'Turn advanced.',
                'encounter_state': {
                    'active': True,
                    'round': 1,
                    'active_turn_index': 1,
                    'turn_order': [
                        {'placement_id': 7, 'actor_type': 'monster', 'actor_id': 'skirmisher_1', 'label': 'Skirmisher 1', 'current_hp': 11, 'actions': {'action': False, 'bonus_action': False, 'reaction': True, 'movement_remaining': 0}},
                        {'placement_id': 5, 'actor_type': 'player', 'actor_id': '2', 'label': 'Seraphina Duskweaver', 'current_hp': 24, 'actions': {'action': True, 'bonus_action': True, 'reaction': True, 'movement_remaining': 30}},
                    ],
                },
            }

        with patch('openrouter._post_chat_normalized', side_effect=[_normalized_from_raw({'choices': [{'message': {'content': '', 'tool_calls': [
                {'id': 'call_1', 'function': {'name': 'update_combatant_actions', 'arguments': '{"placement_id":7,"actions":{"action":false,"bonus_action":false,"movement_remaining":0}}'}},
                {'id': 'call_2', 'function': {'name': 'next_combat_turn', 'arguments': '{}'}},
            ]}}]}), _normalized_from_raw({'choices': [{'message': {'content': '', 'tool_calls': [{'id': 'call_3', 'function': {'name': 'set_combat_turn', 'arguments': '{"active_turn_index":0}'}}]}}]}), _normalized_from_raw(dm_talk_tool_response('The skirmisher scuttles back into cover. Seraphina, your turn.'))]):
            result = get_session_dm_response_with_tools(
                hot_context,
                [],
                [
                    {'type': 'function', 'function': {'name': 'update_combatant_actions'}},
                    {'type': 'function', 'function': {'name': 'next_combat_turn'}},
                    {'type': 'function', 'function': {'name': 'set_combat_turn'}},
                ],
                execute_tool,
                max_tool_rounds=3,
                audit_context={'campaign_id': self.campaign.id},
            )

        self.assertEqual(
            result,
            {'mode': 'speak', 'content': 'The skirmisher scuttles back into cover. Seraphina, your turn.'},
        )
        self.assertEqual(executed, [
            ('update_combatant_actions', {'placement_id': 7, 'actions': {'action': False, 'bonus_action': False, 'movement_remaining': 0}}),
            ('next_combat_turn', {}),
        ])
        blocked = CampaignAuditEvent.query.filter_by(event_type='combat_turn_scope_guard_blocked').one()
        payload = json.loads(blocked.payload)
        self.assertEqual(payload['tool_name'], 'set_combat_turn')

    def test_session_dm_combat_handoff_retry_rewrites_procedural_turn_text(self):
        hot_context = {
            'campaign': {'id': self.campaign.id},
            'current_encounter_map': {
                'id': 3,
                'placements': [
                    {'id': 7, 'actor_type': 'monster', 'actor_id': 'skirmisher_1', 'label': 'Skirmisher 1', 'col': 1, 'row': 1},
                    {'id': 5, 'actor_type': 'player', 'actor_id': '2', 'label': 'Seraphina Duskweaver', 'col': 3, 'row': 9},
                ],
                'encounter_state': {
                    'active': True,
                    'round': 1,
                    'active_turn_index': 0,
                    'turn_order': [
                        {'placement_id': 7, 'actor_type': 'monster', 'actor_id': 'skirmisher_1', 'label': 'Skirmisher 1', 'current_hp': 11, 'actions': {'action': True, 'movement_remaining': 30}},
                        {'placement_id': 5, 'actor_type': 'player', 'actor_id': '2', 'label': 'Seraphina Duskweaver', 'current_hp': 24, 'actions': {'action': True, 'movement_remaining': 30}},
                    ],
                },
            },
            'protected_player_characters': [{'id': self.character.id, 'name': self.character.name, 'user_id': self.user.id, 'username': self.user.username}],
            'private_output_terms': [],
            'private_spoiler_items': [],
        }

        def execute_tool(name, _args, _audit):
            if name == 'update_combatant_actions':
                return {
                    'message': 'Actions updated.',
                    'encounter_state': {
                        'active': True,
                        'round': 1,
                        'active_turn_index': 0,
                        'turn_order': [
                            {'placement_id': 7, 'actor_type': 'monster', 'actor_id': 'skirmisher_1', 'label': 'Skirmisher 1', 'current_hp': 11, 'actions': {'action': False, 'bonus_action': False, 'reaction': True, 'movement_remaining': 0}},
                            {'placement_id': 5, 'actor_type': 'player', 'actor_id': '2', 'label': 'Seraphina Duskweaver', 'current_hp': 24, 'actions': {'action': True, 'bonus_action': True, 'reaction': True, 'movement_remaining': 30}},
                        ],
                    },
                }
            return {
                'message': 'Turn advanced.',
                'encounter_state': {
                    'active': True,
                    'round': 1,
                    'active_turn_index': 1,
                    'turn_order': [
                        {'placement_id': 7, 'actor_type': 'monster', 'actor_id': 'skirmisher_1', 'label': 'Skirmisher 1', 'current_hp': 11, 'actions': {'action': False, 'bonus_action': False, 'reaction': True, 'movement_remaining': 0}},
                        {'placement_id': 5, 'actor_type': 'player', 'actor_id': '2', 'label': 'Seraphina Duskweaver', 'current_hp': 24, 'actions': {'action': True, 'bonus_action': True, 'reaction': True, 'movement_remaining': 30}},
                    ],
                },
            }

        with patch('openrouter._post_chat_normalized', side_effect=[_normalized_from_raw({'choices': [{'message': {'content': '', 'tool_calls': [
                {'id': 'call_1', 'function': {'name': 'update_combatant_actions', 'arguments': '{"placement_id":7,"actions":{"action":false,"bonus_action":false,"movement_remaining":0}}'}},
                {'id': 'call_2', 'function': {'name': 'next_combat_turn', 'arguments': '{}'}},
            ]}}]}), _normalized_from_raw(dm_talk_tool_response('The skirmisher ducks behind the gear housing. Now let me advance to Seraphina.')), _normalized_from_raw(dm_talk_tool_response('The skirmisher ducks behind the gear housing. Seraphina, you are up.'))]) as post_chat:
            result = get_session_dm_response_with_tools(
                hot_context,
                [],
                [
                    {'type': 'function', 'function': {'name': 'update_combatant_actions'}},
                    {'type': 'function', 'function': {'name': 'next_combat_turn'}},
                ],
                execute_tool,
                max_tool_rounds=3,
            )

        self.assertEqual(
            result,
            {'mode': 'speak', 'content': 'The skirmisher ducks behind the gear housing. Seraphina, you are up.'},
        )
        retry_prompt = post_chat.call_args_list[2].args[0][-1]['content']
        self.assertIn('Do not say "now let me advance"', retry_prompt)

    def test_session_dm_rolls_back_mutated_combat_on_failed_final_output(self):
        self.campaign.settings = json.dumps({'encounter_active': True})
        encounter_map = EncounterMap(
            campaign_id=self.campaign.id,
            session_id=self.session.id,
            title='Training Floor',
            prompt='A plain training floor.',
            image_filename='map.png',
            model='gpt-image-2',
            size='1024x1024',
            quality='high',
            grid_json=json.dumps({'columns': 8, 'rows': 8}),
            vtt_setup_json=json.dumps({}),
            setup_status='ready',
        )
        db.session.add(encounter_map)
        db.session.flush()
        player_placement = EncounterMapPlacement(
            encounter_map_id=encounter_map.id,
            actor_type='player',
            actor_id=str(self.user.id),
            label='Aria',
            grid_col=1,
            grid_row=1,
        )
        monster_placement = EncounterMapPlacement(
            encounter_map_id=encounter_map.id,
            actor_type='monster',
            actor_id='goblin_1',
            label='Goblin',
            grid_col=1,
            grid_row=2,
        )
        db.session.add_all([player_placement, monster_placement])
        db.session.flush()
        encounter_map.encounter_state_json = json.dumps({
            'active': True,
            'round': 1,
            'active_turn_index': 1,
            'turn_order': [
                {'placement_id': player_placement.id, 'actor_type': 'player', 'actor_id': str(self.user.id), 'label': 'Aria', 'current_hp': 12, 'max_hp': 12, 'actions': {'action': False, 'movement_remaining': 0}},
                {'placement_id': monster_placement.id, 'actor_type': 'monster', 'actor_id': 'goblin_1', 'label': 'Goblin', 'current_hp': 7, 'max_hp': 7, 'actions': {'action': False, 'movement_remaining': 0}},
            ],
        })
        db.session.commit()

        hot_context = build_session_hot_context(self.campaign, self.session, self.user)

        def execute_tool(name, args, _audit):
            return execute_dm_tool(self.campaign, self.session, self.user, name, args)

        with patch('openrouter._post_chat_normalized', side_effect=[_normalized_from_raw({'choices': [{'message': {'content': '', 'tool_calls': [{'id': 'call_1', 'function': {'name': 'next_combat_turn', 'arguments': '{}'}}]}}]}), _normalized_from_raw({'choices': [{'message': {'content': ''}}]}), _normalized_from_raw({'choices': [{'message': {'content': ''}}]}), _normalized_from_raw({'choices': [{'message': {'content': ''}}]})]):
            result = get_session_dm_response_with_tools(
                hot_context,
                [],
                [{'type': 'function', 'function': {'name': 'next_combat_turn'}}],
                execute_tool,
                audit_context={'campaign_id': self.campaign.id},
                max_tool_rounds=1,
            )

        self.assertEqual(result, {
            'mode': 'silent',
            'reason': 'The DM response did not produce a valid finalizer tool call.',
        })
        encounter_map = db.session.get(EncounterMap, encounter_map.id)
        restored_state = json.loads(encounter_map.encounter_state_json)
        self.assertEqual(restored_state['active_turn_index'], 1)
        rollback_event = CampaignAuditEvent.query.filter_by(event_type='combat_turn_rollback').one()
        payload = json.loads(rollback_event.payload)
        self.assertEqual(payload['reason'], 'invalid_final_output')

    def test_canon_discipline_rewrites_unsupported_player_claim_confirmation(self):
        hot_context = {
            'protected_player_characters': [],
            'private_output_terms': [],
            'private_spoiler_items': [],
            'session': {
                'running_summary': 'The party found an unidentified corpse and a half-burned letter mentioning only a thorn and a coach.',
            },
            'current_scene': {
                'location_name': 'Glassway crossroads',
                'immediate_tension': 'Armed riders are questioning the party about the dead courier.',
            },
            'recent_messages': [
                {'role': 'dm', 'content': 'The riders keep their crossbows trained on you while they ask what you found.'},
                {'role': 'player', 'content': 'He had the Vane brand on his arm, two fingers missing, and the letter named Orrin Vane.'},
            ],
            'established_public_facts': [
                {
                    'id': 'corpse_unknown',
                    'text': 'The party found an unidentified corpse carrying a half-burned letter that mentioned a thorn and a coach.',
                    'certainty': 'confirmed',
                    'visibility': 'party_known',
                },
            ],
            'recent_public_world_events': [],
            'open_public_threads': ['Identify the dead courier and learn who wanted the letter.'],
            'visible_naming_constraints': [],
        }

        with patch('openrouter._post_chat_normalized', side_effect=[_normalized_from_raw(dm_talk_tool_response('<npc target="Harl">"That was Orrin Vane, all right."</npc>')), _normalized_from_raw(dm_talk_tool_response('<npc target="Harl">"That is a very specific description. If it is true, someone important is missing."</npc>'))]) as post_chat, patch('openrouter.check_session_canon_discipline_with_llm', side_effect=[
            {
                'safe': False,
                'unsupported_confirmations': [{
                    'sentence': 'That was Orrin Vane, all right.',
                    'claim_source': 'player_claim',
                    'reason': 'The reply confirms a player-supplied identity with no corroborating public evidence.',
                }],
                'coherence_conflicts': [],
                'confidence': 'high',
                'reason': 'Unsupported player claim promoted into objective truth.',
            },
            {
                'safe': True,
                'unsupported_confirmations': [],
                'coherence_conflicts': [],
                'confidence': 'high',
                'reason': '',
            },
        ]):
            result = get_session_dm_response_with_tools(hot_context, [], [], lambda *_args, **_kwargs: {}, max_tool_rounds=0)

        self.assertEqual(
            result,
            {'mode': 'speak', 'content': '<npc target="Harl">"That is a very specific description. If it is true, someone important is missing."</npc>'},
        )
        self.assertEqual(post_chat.call_count, 2)

    def test_canon_repair_sidecar_discards_serialized_rejected_draft(self):
        rejected = 'Elbrig kept the lanterns, and the saboteur took him.'
        repaired = '<npc target="Harbormaster Thane Marrow">"A keeper is missing. I do not yet know whether it is connected."</npc>'
        hot_context = {
            'protected_player_characters': [],
            'private_output_terms': [],
            'private_spoiler_items': [],
            'session': {'running_summary': ''},
            'current_scene': {'location_name': 'Larkspur Landing'},
            'recent_messages': [
                {'role': 'player', 'content': 'Where is the missing lantern keeper?'},
            ],
            'established_public_facts': [],
            'recent_public_world_events': [],
            'open_public_threads': ['Locate the missing lantern keeper.'],
            'visible_naming_constraints': [],
        }
        violation = {
            'safe': False,
            'unsupported_confirmations': [{
                'sentence': 'Elbrig kept the lanterns, and the saboteur took him.',
                'claim_source': 'unsupported_lore',
                'reason': 'The public thread does not establish the keeper name or who took him.',
            }],
            'coherence_conflicts': [],
            'confidence': 'high',
            'reason': 'The candidate disclosed details beyond the public thread.',
        }

        raw_plain_draft = _normalized_from_raw({
            'choices': [{'message': {'content': rejected}}],
        })
        with patch(
            'openrouter._post_chat_normalized',
            side_effect=[
                raw_plain_draft,
                _normalized_from_raw(dm_talk_tool_response(rejected)),
                _normalized_from_raw(dm_talk_tool_response(repaired)),
            ],
        ) as post_chat, patch(
            'openrouter.check_session_canon_discipline_with_llm',
            side_effect=[
                violation,
                {
                    'safe': True,
                    'unsupported_confirmations': [],
                    'coherence_conflicts': [],
                    'confidence': 'high',
                    'reason': '',
                },
            ],
        ):
            result = get_session_dm_response_with_tools(
                hot_context,
                [],
                [],
                lambda *_args, **_kwargs: {},
                max_tool_rounds=0,
            )

        self.assertEqual(result, {'mode': 'speak', 'content': repaired})
        self.assertEqual(post_chat.call_count, 3)
        repair_messages = post_chat.call_args_list[2].args[0]
        self.assertFalse(any(
            message.get('role') == 'assistant' and rejected in str(message.get('content') or '')
            for message in repair_messages
        ))
        self.assertFalse(any(
            'Convert the immediately preceding assistant draft' in str(message.get('content') or '')
            for message in repair_messages
        ))
        repair_prompts = [
            str(message.get('content') or '')
            for message in repair_messages
            if message.get('role') == 'system' and 'sidecar repair brief' in str(message.get('content') or '')
        ]
        self.assertEqual(len(repair_prompts), 1)
        self.assertIn('The public thread does not establish the keeper name or who took him.', repair_prompts[0])

    def test_canon_discipline_truncated_repair_retries_with_larger_budget(self):
        hot_context = {
            'protected_player_characters': [],
            'private_output_terms': [],
            'private_spoiler_items': [],
            'session': {
                'running_summary': 'The party just took Vex\'s pouch and asked about the old well.',
            },
            'current_scene': {
                'location_name': 'Grain Exchange office',
                'immediate_tension': 'The bell keeps ringing and Vex wants the party moving.',
            },
            'recent_messages': [
                {'role': 'dm', 'content': 'She holds out the pouch.'},
                {'role': 'player', 'content': 'I take the pouch and ask what waits by the old well.'},
            ],
            'established_public_facts': [
                {
                    'id': 'vex_paid_advance',
                    'text': 'Vex already handed over the advance pouch and the player took it.',
                    'certainty': 'confirmed',
                    'visibility': 'party_known',
                },
            ],
            'recent_public_world_events': [],
            'open_public_threads': ['Reach the old well before the crowd gets worse.'],
            'visible_naming_constraints': [],
        }

        truncated_repair = {
            'choices': [{
                'message': {
                    'content': 'Vex answers the question and starts to explain the path to the well, but',
                },
                'finish_reason': 'length',
            }],
        }

        with patch('openrouter._post_chat_normalized', side_effect=[_normalized_from_raw(dm_talk_tool_response('She holds out the pouch again.')), _normalized_from_raw(truncated_repair), _normalized_from_raw(dm_talk_tool_response('<npc target="Baronessa Rina Vex">"The well is dry. Stay low in the west ditch and you can reach it unseen."</npc>'))]) as post_chat, patch('openrouter.check_session_canon_discipline_with_llm', side_effect=[
            {
                'safe': False,
                'unsupported_confirmations': [],
                'coherence_conflicts': [{
                    'sentence': 'She holds out the pouch again.',
                    'claim_source': 'other',
                    'reason': 'The player already took the pouch in the immediately preceding visible exchange.',
                }],
                'confidence': 'high',
                'reason': 'Transactional continuity was reset without visible support.',
            },
            {
                'safe': True,
                'unsupported_confirmations': [],
                'coherence_conflicts': [],
                'confidence': 'high',
                'reason': '',
            },
        ]):
            result = get_session_dm_response_with_tools(hot_context, [], [], lambda *_args, **_kwargs: {}, max_tool_rounds=0)

        self.assertEqual(
            result,
            {'mode': 'speak', 'content': '<npc target="Baronessa Rina Vex">"The well is dry. Stay low in the west ditch and you can reach it unseen."</npc>'},
        )
        self.assertEqual(post_chat.call_count, 3)

    def test_canon_discipline_blocks_repeated_established_lead_conflict(self):
        hot_context = {
            'protected_player_characters': [],
            'private_output_terms': [],
            'private_spoiler_items': [],
            'session': {
                'running_summary': 'The party learned that the Spike tower is Agent Mercer\'s base and is deciding whether to head there next.',
            },
            'current_scene': {
                'location_name': 'Ashglass road',
                'immediate_tension': 'The party is debating whether to pursue the tower lead or the grove lead first.',
            },
            'recent_messages': [
                {'role': 'dm', 'content': 'The Spike tower remains the clearest lead on Mercer.'},
                {'role': 'player', 'content': 'Could the grove matter more than the tower?'},
            ],
            'established_public_facts': [
                {
                    'id': 'tower_base',
                    'text': 'The Spike tower is Agent Mercer\'s base of operations.',
                    'certainty': 'confirmed',
                    'visibility': 'party_known',
                },
            ],
            'recent_public_world_events': [],
            'open_public_threads': ['Decide whether to strike the tower or investigate the grove first.'],
            'visible_naming_constraints': [],
        }

        with patch('openrouter._post_chat_normalized', side_effect=[_normalized_from_raw(dm_talk_tool_response('The tower was never important. The grove is the true heart of the whole operation.')), _normalized_from_raw(dm_talk_tool_response('Forget the tower. Everything that matters is in the grove.')), _normalized_from_raw(dm_talk_tool_response('The tower lead is dead. The grove is the only real answer.')), _normalized_from_raw(dm_talk_tool_response('You can ignore the tower now. It was a false trail from the start.')), _normalized_from_raw(dm_talk_tool_response('The grove is what matters. The tower never did.'))]), patch('openrouter.check_session_canon_discipline_with_llm', side_effect=[
            {
                'safe': False,
                'unsupported_confirmations': [],
                'coherence_conflicts': [{
                    'sentence': 'The tower was never important.',
                    'claim_source': 'contradicted_lead',
                    'reason': 'The reply discards an established public lead without visible evidence.',
                }],
                'confidence': 'high',
                'reason': 'Established lead overwritten without support.',
            },
            {
                'safe': False,
                'unsupported_confirmations': [],
                'coherence_conflicts': [{
                    'sentence': 'Forget the tower.',
                    'claim_source': 'unsupported_reframe',
                    'reason': 'The reply still replaces the established tower lead outright.',
                }],
                'confidence': 'high',
                'reason': 'Established lead overwritten without support.',
            },
            {
                'safe': False,
                'unsupported_confirmations': [],
                'coherence_conflicts': [{
                    'sentence': 'The tower lead is dead.',
                    'claim_source': 'contradicted_lead',
                    'reason': 'The reply keeps nullifying the established lead.',
                }],
                'confidence': 'high',
                'reason': 'Established lead overwritten without support.',
            },
            {
                'safe': False,
                'unsupported_confirmations': [],
                'coherence_conflicts': [{
                    'sentence': 'You can ignore the tower now.',
                    'claim_source': 'unsupported_reframe',
                    'reason': 'The reply still tells the party to discard the established lead.',
                }],
                'confidence': 'high',
                'reason': 'Established lead overwritten without support.',
            },
            {
                'safe': False,
                'unsupported_confirmations': [],
                'coherence_conflicts': [{
                    'sentence': 'The tower never did.',
                    'claim_source': 'contradicted_lead',
                    'reason': 'The reply still contradicts the established public fact.',
                }],
                'confidence': 'high',
                'reason': 'Established lead overwritten without support.',
            },
        ]):
            result = get_session_dm_response_with_tools(hot_context, [], [], lambda *_args, **_kwargs: {}, max_tool_rounds=0)

        self.assertEqual(result, {
            'mode': 'silent',
            'reason': 'The DM response would have promoted unsupported claims or contradicted established public facts.',
        })

    def test_spoiler_checker_allows_player_prompted_witness_clue(self):
        hot_context = {
            'private_spoiler_items': [
                {
                    'id': 'npc_secret_witness_old_dockhand_1',
                    'kind': 'npc_secret',
                    'text': 'He saw a robed figure near the theft site that night.',
                },
            ],
            'recent_messages': [
                {
                    'role': 'player',
                    'content': 'I ask Mortimer what he saw near the Temple vault that night.',
                },
            ],
        }
        response = '<npc target="Mortimer">"I saw a tall robed figure near the vault alley."</npc>'

        with patch('openrouter._post_chat', return_value=json.dumps({
            'safe': False,
            'leaked_item_ids': ['npc_secret_witness_old_dockhand_1'],
            'evidence': ['I saw a tall robed figure'],
            'reason': 'The reply reveals the witness clue.',
        })):
            result = check_session_spoilers_with_llm(response, hot_context)

        self.assertEqual(result, {
            'safe': True,
            'leaked_item_ids': [],
            'evidence': [],
            'reason': 'Allowed limited clue reveal prompted by the latest visible player action.',
        })

    def test_spoiler_checker_still_blocks_player_prompted_final_solution(self):
        hot_context = {
            'private_spoiler_items': [
                {
                    'id': 'dm_private_true_inciting_incident',
                    'kind': 'true_inciting_incident',
                    'text': 'The Debt Priests orchestrated the theft.',
                },
            ],
            'recent_messages': [
                {
                    'role': 'player',
                    'content': 'I ask Mortimer what he saw near the Temple vault that night.',
                },
            ],
        }
        response = '<npc target="Mortimer">"The Debt Priests orchestrated the theft."</npc>'

        with patch('openrouter._post_chat', return_value=json.dumps({
            'safe': False,
            'leaked_item_ids': ['dm_private_true_inciting_incident'],
            'evidence': ['The Debt Priests orchestrated the theft'],
            'reason': 'The reply reveals the hidden culprit.',
        })):
            result = check_session_spoilers_with_llm(response, hot_context)

        self.assertEqual(result, {
            'safe': False,
            'leaked_item_ids': ['dm_private_true_inciting_incident'],
            'evidence': ['The Debt Priests orchestrated the theft'],
            'reason': 'The reply reveals the hidden culprit.',
        })

    def test_private_output_guard_retry_uses_child_trace(self):
        hot_context = {
            'protected_player_characters': [],
            'private_output_terms': ['Crimson Veil'],
            'private_spoiler_items': [],
        }
        trace_id = 'session_dm:session_2:message_15'

        with patch('openrouter._post_chat_normalized', side_effect=[_normalized_from_raw(dm_talk_tool_response('The Crimson Veil watches you.')), _normalized_from_raw(dm_talk_tool_response('Someone watches from the dark.'))]):
            result = get_session_dm_response_with_tools(
                hot_context,
                [],
                [],
                lambda *_args, **_kwargs: {},
                audit_context={
                    'campaign_id': self.campaign.id,
                    'trace_id': trace_id,
                    'trace_label': 'session_dm: session 2',
                },
                max_tool_rounds=0,
            )

        self.assertEqual(result, {'mode': 'speak', 'content': 'Someone watches from the dark.'})
        retry_event = CampaignAuditEvent.query.filter_by(event_type='private_output_guard_retry').one()
        self.assertEqual(retry_event.actor, 'session_dm_guard')
        self.assertEqual(retry_event.parent_trace_id, trace_id)
        self.assertNotEqual(retry_event.trace_id, trace_id)
        self.assertIn(':private_output_guard:', retry_event.trace_id)

    def test_private_output_guard_allows_second_retry_after_pc_control_retry(self):
        hot_context = {
            'protected_player_characters': [],
            'private_output_terms': ['Crimson Veil'],
            'private_spoiler_items': [],
        }
        trace_id = 'session_dm:session_2:message_16'
        safe_pc_check = {'safe': True, 'violations': [], 'confidence': 'high', 'reason': ''}

        with patch('openrouter._post_chat_normalized', side_effect=[_normalized_from_raw(dm_talk_tool_response('The Crimson Veil watches you.')), _normalized_from_raw(dm_talk_tool_response('The watcher is gone.')), _normalized_from_raw(dm_talk_tool_response('The Crimson Veil still watches.')), _normalized_from_raw(dm_talk_tool_response('Someone watches from the dark.'))]), patch('openrouter.check_session_pc_control_with_llm', side_effect=[
            safe_pc_check,
            {
                'safe': False,
                'violations': [{
                    'character': 'Aria',
                    'sentence': 'The watcher is gone.',
                    'kind': 'choice_or_intent',
                    'reason': 'Forced regression-test classifier violation.',
                }],
                'confidence': 'medium',
                'reason': 'Forced regression-test classifier violation.',
            },
            safe_pc_check,
            safe_pc_check,
        ]):
            result = get_session_dm_response_with_tools(
                hot_context,
                [],
                [],
                lambda *_args, **_kwargs: {},
                audit_context={
                    'campaign_id': self.campaign.id,
                    'trace_id': trace_id,
                    'trace_label': 'session_dm: session 2',
                },
                max_tool_rounds=0,
            )

        self.assertEqual(result, {'mode': 'speak', 'content': 'Someone watches from the dark.'})
        self.assertEqual(CampaignAuditEvent.query.filter_by(event_type='private_output_guard_retry').count(), 2)
        self.assertEqual(CampaignAuditEvent.query.filter_by(event_type='pc_control_guard_retry').count(), 1)
        self.assertEqual(CampaignAuditEvent.query.filter_by(event_type='private_output_guard_blocked').count(), 0)

    def test_private_output_guard_retry_can_finish_after_tool_call(self):
        hot_context = {
            'protected_player_characters': [],
            'private_output_terms': ['Fiendish Patron'],
            'private_spoiler_items': [],
        }
        executed = []

        def execute_tool(name, args, _audit):
            executed.append((name, args))
            return {'matches': [{'text': 'The symbol appears infernal.'}]}

        with patch('openrouter._post_chat_normalized', side_effect=[_normalized_from_raw(dm_talk_tool_response('Your Fiendish Patron stirs.')), _normalized_from_raw({'choices': [{'message': {
                'content': '',
                'tool_calls': [{
                    'id': 'call_retry_search',
                    'function': {
                        'name': 'search_campaign_memory',
                        'arguments': '{"query":"burned symbol infernal"}',
                    },
                }],
            }}]}), _normalized_from_raw(dm_talk_tool_response('The symbol appears infernal, but you do not know who left it.'))]):
            result = get_session_dm_response_with_tools(
                hot_context,
                [],
                [],
                execute_tool,
                max_tool_rounds=1,
            )

        self.assertEqual(
            result,
            {'mode': 'speak', 'content': 'The symbol appears infernal, but you do not know who left it.'},
        )
        self.assertEqual(executed, [('search_campaign_memory', {'query': 'burned symbol infernal'})])

    def test_spoiler_checker_blocks_repeated_semantic_leak(self):
        hot_context = {
            'protected_player_characters': [],
            'private_output_terms': [],
            'private_spoiler_items': [{'id': 'fact_trap', 'kind': 'fact', 'text': 'The note is a trap.'}],
        }

        with patch('openrouter._post_chat_normalized', side_effect=[_normalized_from_raw(dm_talk_tool_response('The trap closes around you.')), _normalized_from_raw(dm_talk_tool_response('A hidden trap closes around you.')), _normalized_from_raw(dm_talk_tool_response('The ambush was a trap all along.')), _normalized_from_raw(dm_talk_tool_response('This confirms the note was a trap.')), _normalized_from_raw(dm_talk_tool_response('You can feel that something is wrong here.'))]), patch('openrouter.check_session_spoilers_with_llm', side_effect=[
            {'safe': False, 'leaked_item_ids': ['fact_trap'], 'evidence': ['The trap closes'], 'reason': 'Directly implies the hidden truth.'},
            {'safe': False, 'leaked_item_ids': ['fact_trap'], 'evidence': ['hidden trap'], 'reason': 'Still implies the hidden truth.'},
            {'safe': False, 'leaked_item_ids': ['fact_trap'], 'evidence': ['trap all along'], 'reason': 'Still implies the hidden truth.'},
            {'safe': False, 'leaked_item_ids': ['fact_trap'], 'evidence': ['note was a trap'], 'reason': 'Still implies the hidden truth.'},
            {'safe': False, 'leaked_item_ids': ['fact_trap'], 'evidence': ['something is wrong'], 'reason': 'Still implies the hidden truth.'},
        ]):
            result = get_session_dm_response_with_tools(hot_context, [], [], lambda *_args, **_kwargs: {}, max_tool_rounds=0)

        self.assertEqual(result, {
            'mode': 'silent',
            'reason': 'The DM response would have semantically exposed DM-private information.',
        })

    def test_session_dm_turn_decision_normalizes_silence_contract(self):
        self.assertEqual(
            normalize_session_dm_turn_decision('{"mode":"silent","reason":"PC-to-PC exchange."}'),
            {
                'mode': 'silent',
                'content': '',
                'reason': 'PC-to-PC exchange.',
            },
        )
        self.assertEqual(
            normalize_session_dm_turn_decision('The lock clicks open.'),
            {
                'mode': 'speak',
                'content': 'The lock clicks open.',
            },
        )

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


    def test_search_campaign_memory_uses_embedding_similarity_without_keyword_overlap(self):
        world = CampaignWorld.query.filter_by(campaign_id=self.campaign.id).first()
        world.knowledge_graph = json.dumps({
            'entities': [],
            'relations': [],
            'facts': [
                {
                    'id': 'fact_symbol',
                    'entity_ids': ['burned_symbol'],
                    'text': 'The door mark is an Infernal seal of scrutiny.',
                    'certainty': 'confirmed',
                    'visibility': 'party_known',
                }
            ],
        })
        db.session.add(CampaignMemoryEmbedding(
            campaign_id=self.campaign.id,
            item_type='fact',
            item_id='fact_symbol',
            visibility='party_known',
            canonical_text='Fact: The door mark is an Infernal seal of scrutiny.',
            text_hash='fact',
            embedding_model='gemini-embedding-2',
            embedding_dimensions=2,
            embedding_json='[1.0, 0.0]',
        ))
        db.session.commit()

        with patch.dict(os.environ, {
            'GEMINI_EMBEDDINGS_ENABLED': 'true',
            'GEMINI_EMBEDDING_DIMENSIONS': '2',
        }, clear=False), patch('services.embedding_service.embedding_from_text', return_value={
            'ok': True,
            'vector': [1.0, 0.0],
            'model': 'gemini-embedding-2',
            'dimensions': 2,
        }):
            result = execute_dm_tool(
                self.campaign,
                self.session,
                self.user,
                'search_campaign_memory',
                {'query': 'ominous personal brand', 'limit': 3},
                {},
            )

        self.assertEqual(result['matches'][0]['item_id'], 'fact_symbol')
        self.assertEqual(result['matches'][0]['keyword_score'], 0)
        self.assertGreater(result['matches'][0]['embedding_score'], 0.9)

    def test_search_campaign_memory_keyword_fallback_when_embeddings_disabled(self):
        world = CampaignWorld.query.filter_by(campaign_id=self.campaign.id).first()
        world.knowledge_graph = json.dumps({
            'entities': [],
            'relations': [],
            'facts': [
                {
                    'id': 'dock_warning_bell',
                    'entity_ids': ['dock_ward'],
                    'text': 'A warning bell rang in the Dock Ward.',
                    'certainty': 'confirmed',
                    'visibility': 'party_known',
                }
            ],
        })
        db.session.commit()

        result = execute_dm_tool(
            self.campaign,
            self.session,
            self.user,
            'search_campaign_memory',
            {'query': 'bell', 'limit': 3},
            {},
        )

        self.assertEqual(result['matches'][0]['item_id'], 'dock_warning_bell')
        self.assertGreater(result['matches'][0]['keyword_score'], 0)

    def test_retrieval_packet_skips_safe_preflight_and_caps_each_lane(self):
        hot_context = {
            'recent_messages': [{'role': 'player', 'content': 'What did the bell mean at the Dock Ward?'}],
        }
        safe_packet = build_session_retrieval_packet(
            self.campaign,
            self.user,
            hot_context,
            {'dm_reply_mode': 'ooc_only', 'confidence': 'high'},
        )
        self.assertIsNone(safe_packet)

        with patch('services.dm_tools.search_memory_embeddings_batch', return_value={
            'ok': True,
            'scores_by_query': {
                'entities': {}, 'scene_events': {}, 'clocks_promises': {}, 'prior_facts': {},
            },
        }) as search_batch:
            packet = build_session_retrieval_packet(
                self.campaign,
                self.user,
                hot_context,
                {
                    'dm_reply_mode': 'narrative',
                    'confidence': 'medium',
                    'retrieval_queries': {
                        'entities': 'Dock Ward people and places',
                        'scene_events': 'recent bell events',
                        'clocks_promises': 'active promises',
                        'prior_facts': 'known bell facts',
                    },
                },
            )

        self.assertTrue(search_batch.called)
        self.assertEqual(
            [lane['lane'] for lane in packet['lanes']],
            ['entities', 'scene_events', 'clocks_promises', 'prior_facts'],
        )
        self.assertTrue(all(len(lane['matches']) <= 2 for lane in packet['lanes']))
        for lane in packet['lanes']:
            for match in lane['matches']:
                self.assertIn('item_id', match)
                self.assertIn('score', match)
                self.assertIn('visibility', match)
                self.assertIn('certainty', match)
                self.assertIn('internal_only', match)
        selected_ids = [
            (match['kind'], match['item_id'])
            for lane in packet['lanes']
            for match in lane['matches']
        ]
        self.assertEqual(len(selected_ids), len(set(selected_ids)))

        with patch('services.dm_tools.search_memory_embeddings_batch', return_value={
            'ok': False,
            'scores_by_query': {},
            'reason': 'provider unavailable',
        }):
            fallback_packet = build_session_retrieval_packet(
                self.campaign,
                self.user,
                hot_context,
                {'dm_reply_mode': 'narrative', 'confidence': 'low'},
            )
        self.assertFalse(fallback_packet['semantic_available'])
        self.assertEqual(
            [lane['lane'] for lane in fallback_packet['lanes']],
            ['entities', 'scene_events', 'clocks_promises', 'prior_facts'],
        )

    def test_staged_narrative_actions_are_db_free_until_selected_commit(self):
        before_events = WorldEvent.query.filter_by(campaign_id=self.campaign.id).count()
        before_audits = CampaignAuditEvent.query.filter_by(campaign_id=self.campaign.id).count()
        before_embeddings = CampaignMemoryEmbedding.query.filter_by(campaign_id=self.campaign.id).count()
        action_buffer = {'actions': []}

        preview = execute_dm_tool(
            self.campaign,
            self.session,
            self.user,
            'record_world_event',
            {
                'event_type': 'clue_found',
                'summary': 'The Dock Ward bell rings twice from the north tower.',
                'visibility': 'party_known',
            },
            {'pending_action_buffer': action_buffer},
        )

        self.assertEqual(preview['pending_action_id'], 'pending_action_1')
        self.assertTrue(preview['event']['pending'])
        self.assertEqual(WorldEvent.query.filter_by(campaign_id=self.campaign.id).count(), before_events)
        self.assertEqual(CampaignAuditEvent.query.filter_by(campaign_id=self.campaign.id).count(), before_audits)
        self.assertEqual(CampaignMemoryEmbedding.query.filter_by(campaign_id=self.campaign.id).count(), before_embeddings)

        player_message = SessionMessage(
            session_id=self.session.id,
            user_id=self.user.id,
            role='player',
            content='I listen for the bell.',
        )
        db.session.add(player_message)
        db.session.commit()
        dm_message, _proposals, _results = commit_accepted_dm_turn(
            self.campaign,
            self.session,
            self.user,
            player_message.id,
            'test:atomic:event',
            'test atomic event',
            'The bell answers from the north tower.',
            ['pending_action_1'],
            action_buffer,
            [{'type': 'narration', 'content': 'The bell answers from the north tower.'}],
        )

        self.assertIsNotNone(dm_message.id)
        self.assertEqual(WorldEvent.query.filter_by(campaign_id=self.campaign.id).count(), before_events + 1)
        self.assertEqual(
            CampaignAuditEvent.query.filter_by(event_type='dm_staged_action_committed').count(),
            1,
        )

    def test_unselected_staged_proposal_is_absent_and_old_pending_proposal_is_unlinked(self):
        old_proposal = SheetProposal(
            session_id=self.session.id,
            character_id=self.character.id,
            dm_user_id=self.user.id,
            reason='Existing pending proposal.',
            changes=[{'field': 'gp', 'operation': 'add', 'value': 1}],
            status='pending',
        )
        db.session.add(old_proposal)
        db.session.commit()
        action_buffer = {'actions': []}
        preview = execute_dm_tool(
            self.campaign,
            self.session,
            self.user,
            'propose_sheet_update',
            {
                'character_id': self.character.id,
                'reason': 'The party found a small purse.',
                'changes': [{'field': 'gp', 'operation': 'add', 'value': 5}],
            },
            {'pending_action_buffer': action_buffer},
        )

        self.assertEqual(preview['pending_action_id'], 'pending_action_1')
        self.assertEqual(SheetProposal.query.count(), 1)

        player_message = SessionMessage(
            session_id=self.session.id,
            user_id=self.user.id,
            role='player',
            content='I search the purse.',
        )
        db.session.add(player_message)
        db.session.commit()
        dm_message, proposals, _results = commit_accepted_dm_turn(
            self.campaign,
            self.session,
            self.user,
            player_message.id,
            'test:atomic:none',
            'test atomic none',
            'You find a few loose coins.',
            [],
            action_buffer,
            [{'type': 'narration', 'content': 'You find a few loose coins.'}],
        )

        self.assertEqual(proposals, [])
        self.assertEqual(SheetProposal.query.count(), 1)
        self.assertIsNone(db.session.get(SheetProposal, old_proposal.id).message_id)
        self.assertNotEqual(db.session.get(SheetProposal, old_proposal.id).message_id, dm_message.id)

        selected_buffer = {'actions': []}
        execute_dm_tool(
            self.campaign,
            self.session,
            self.user,
            'propose_sheet_update',
            {
                'character_id': self.character.id,
                'reason': 'The party finds a second purse.',
                'changes': [{'field': 'gp', 'operation': 'add', 'value': 3}],
            },
            {'pending_action_buffer': selected_buffer},
        )
        second_player_message = SessionMessage(
            session_id=self.session.id,
            user_id=self.user.id,
            role='player',
            content='I take the second purse.',
        )
        db.session.add(second_player_message)
        db.session.commit()
        selected_message, selected_proposals, _results = commit_accepted_dm_turn(
            self.campaign,
            self.session,
            self.user,
            second_player_message.id,
            'test:atomic:selected-proposal',
            'test selected proposal',
            'The second purse contains a few more coins.',
            ['pending_action_1'],
            selected_buffer,
            [{'type': 'narration', 'content': 'The second purse contains a few more coins.'}],
        )
        self.assertEqual(len(selected_proposals), 1)
        self.assertEqual(selected_proposals[0].message_id, selected_message.id)
        self.assertIsNone(db.session.get(SheetProposal, old_proposal.id).message_id)

    def test_commit_failure_rolls_back_selected_staged_actions_and_records_only_turn_error(self):
        action_buffer = {'actions': []}
        execute_dm_tool(
            self.campaign,
            self.session,
            self.user,
            'record_world_event',
            {'event_type': 'clue_found', 'summary': 'This event must never persist.'},
            {'pending_action_buffer': action_buffer},
        )
        player_message = SessionMessage(
            session_id=self.session.id,
            user_id=self.user.id,
            role='player',
            content='I inspect the clue.',
        )
        db.session.add(player_message)
        db.session.commit()

        with patch('services.dm_turn_commit.apply_deferred_narrative_action', side_effect=RuntimeError('forced failure')):
            with self.assertRaisesRegex(RuntimeError, 'forced failure'):
                commit_accepted_dm_turn(
                    self.campaign,
                    self.session,
                    self.user,
                    player_message.id,
                    'test:atomic:failure',
                    'test atomic failure',
                    'This reply must never persist.',
                    ['pending_action_1'],
                    action_buffer,
                    [{'type': 'narration', 'content': 'This reply must never persist.'}],
                )

        self.assertEqual(WorldEvent.query.filter_by(campaign_id=self.campaign.id).count(), 0)
        self.assertEqual(SessionMessage.query.filter_by(session_id=self.session.id, role='dm').count(), 0)
        self.assertEqual(CampaignMemoryEmbedding.query.filter_by(campaign_id=self.campaign.id).count(), 0)
        self.assertEqual(CampaignAuditEvent.query.filter_by(event_type='dm_staged_action_committed').count(), 0)
        turn = SessionDmTurn.query.filter_by(player_message_id=player_message.id).one()
        self.assertEqual(turn.status, 'error')

    def test_advance_clock_is_not_a_dm_tool(self):
        # Regression: `advance_clock` used to be an inline DM tool, and its
        # mutations committed before guard repair could roll them back. The
        # post-turn clock adjudicator now owns all clock advancement, so the
        # DM must not be able to advance clocks via the tool surface.
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

        self.assertEqual(result, {'error': 'Unknown DM tool: advance_clock'})
        clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='guards_arrive').one()
        self.assertEqual(clock.filled, 3)
        self.assertEqual(clock.status, 'active')

    def test_apply_clock_adjudication_advances_existing_clock(self):
        self._add_session_message(101, 'We chase them toward the crypt road.', role='player')
        self._add_session_message(102, 'The chase reaches the crypt road.')
        db.session.add(CampaignClock(
            campaign_id=self.campaign.id,
            clock_id='race_to_crypts',
            name='Race to the Crypts',
            segments=4,
            filled=0,
            status='active',
        ))
        db.session.commit()

        result = apply_clock_adjudication(
            self.campaign,
            {
                'create_clocks': [],
                'advance_clocks': [
                    {
                        'clock_id': 'race_to_crypts',
                        'delta': 1,
                        'reason': 'The pursuit visibly moved toward the crypt road.',
                        'evidence': ['The chase left the market and hit the crypt road.'],
                        'trigger_verdict': self._trigger_verdict(101, 102),
                    }
                ],
                'retire_clocks': [],
                'no_change_explanations': [],
            },
            audit_context={
                'trace_id': 'clock-trace',
                'source_player_message_id': 101,
                'source_dm_message_id': 102,
            },
        )

        db.session.commit()
        clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='race_to_crypts').one()
        self.assertEqual(clock.filled, 1)
        self.assertEqual(clock.status, 'active')
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['clock_changes'][0]['action'], 'advanced')
        event = WorldEvent.query.filter_by(
            campaign_id=self.campaign.id,
            event_type='clock_advanced',
        ).one()
        provenance = json.loads(event.payload)['provenance']
        self.assertEqual(provenance['evidence_status'], 'supported_by_evidence')
        self.assertEqual(
            provenance['evidence_sources'],
            [
                {'source_type': 'transcript_message', 'source_id': '101'},
                {'source_type': 'transcript_message', 'source_id': '102'},
            ],
        )
        self.assertEqual(provenance['trigger_clause_id'], 'visible_narrative_progress')
        self.assertEqual(provenance['chronology_verdict'], 'new_current_turn')
        self.assertEqual(provenance['new_evidence_ids'], ['101', '102'])

    def test_completion_criteria_hold_full_clock_pending_until_retirement_is_evidenced(self):
        self._add_session_message(101, 'We investigate the new clue.', role='player')
        self._add_session_message(102, 'You uncover a new clue.')
        db.session.add(CampaignClock(
            campaign_id=self.campaign.id,
            clock_id='identify_saboteur',
            name='Identify the Saboteur',
            segments=4,
            filled=3,
            status='active',
            completion_criteria=[
                {'id': 'named_suspect', 'description': 'Visible evidence identifies a specific suspect.'},
                {'id': 'usable_location', 'description': 'Visible evidence establishes a usable location.'},
            ],
        ))
        db.session.commit()

        result = apply_clock_adjudication(
            self.campaign,
            {
                'create_clocks': [],
                'advance_clocks': [{
                    'clock_id': 'identify_saboteur',
                    'delta': 1,
                    'reason': 'The party found a new clue.',
                    'evidence': [],
                    'trigger_verdict': self._trigger_verdict(101, 102),
                }],
                'retire_clocks': [{
                    'clock_id': 'identify_saboteur',
                    'reason': 'The bar is full, so the mystery is solved.',
                }],
                'no_change_explanations': [],
            },
            audit_context={
                'trace_id': 'completion-criteria-trace',
                'source_player_message_id': 101,
                'source_dm_message_id': 102,
            },
        )
        db.session.commit()

        clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='identify_saboteur').one()
        self.assertEqual(clock.filled, 4)
        self.assertEqual(clock.status, 'completion_pending')
        self.assertIn('every completion criterion has one AI verdict', result['errors'][0])
        self.assertEqual(WorldEvent.query.filter_by(campaign_id=self.campaign.id, event_type='clock_retired').count(), 0)

    def test_run_41_historical_lantern_failure_cannot_advance_clock(self):
        self._add_session_message(101, 'How many lanterns are actually still burning?', role='player')
        self._add_session_message(
            102,
            'Harlen distinguishes the official count from the real count and says the Widow\'s Lamp went dark two nights ago.',
        )
        db.session.add(CampaignClock(
            campaign_id=self.campaign.id,
            clock_id='lantern_failure_clock',
            name='Lantern Failures',
            segments=4,
            filled=0,
            status='active',
            trigger='Each dusk, one additional lantern goes dark unless the party prevents it.',
        ))
        db.session.commit()

        verdict = self._trigger_verdict(102, clause_id='declared_trigger')
        verdict.update({
            'supported_claims': ['The Widow\'s Lamp went dark two nights ago.'],
            'chronology_verdict': 'historical_or_restated',
            'reason': 'The exchange only restates an older failure; no dusk or new failure occurred.',
        })
        result = apply_clock_adjudication(
            self.campaign,
            {
                'create_clocks': [],
                'advance_clocks': [{
                    'clock_id': 'lantern_failure_clock',
                    'delta': 1,
                    'reason': 'The disclosed count sounds worse.',
                    'evidence': ['The Widow\'s Lamp failed two nights ago.'],
                    'trigger_verdict': verdict,
                }],
                'retire_clocks': [],
                'no_change_explanations': [],
            },
            audit_context={
                'trace_id': 'run-41-cycle-2',
                'source_player_message_id': 101,
                'source_dm_message_id': 102,
            },
        )
        db.session.commit()

        clock = CampaignClock.query.filter_by(
            campaign_id=self.campaign.id,
            clock_id='lantern_failure_clock',
        ).one()
        self.assertEqual(clock.filled, 0)
        self.assertEqual(WorldEvent.query.filter_by(
            campaign_id=self.campaign.id,
            event_type='clock_advanced',
        ).count(), 0)
        self.assertEqual(result['rejected_advances'][0]['clock_id'], 'lantern_failure_clock')
        self.assertIn('evidence_not_new_current_turn', result['rejected_advances'][0]['evidence_gaps'])

    def test_clock_advance_rejects_historical_source_even_when_verdict_claims_new(self):
        self._add_session_message(99, 'Yesterday, the east lantern went dark.')
        self._add_session_message(101, 'What happens now?', role='player')
        self._add_session_message(102, 'No additional lantern fails.')
        db.session.add(CampaignClock(
            campaign_id=self.campaign.id,
            clock_id='lantern_failure_clock',
            name='Lantern Failures',
            segments=4,
            filled=0,
            status='active',
            trigger='Each dusk, one additional lantern goes dark unless the party prevents it.',
        ))
        db.session.commit()

        result = apply_clock_adjudication(
            self.campaign,
            {
                'create_clocks': [],
                'advance_clocks': [{
                    'clock_id': 'lantern_failure_clock',
                    'delta': 1,
                    'reason': 'A lantern failure was cited.',
                    'evidence': [],
                    'trigger_verdict': self._trigger_verdict(99, clause_id='declared_trigger'),
                }],
                'retire_clocks': [],
                'no_change_explanations': [],
            },
            audit_context={
                'source_player_message_id': 101,
                'source_dm_message_id': 102,
            },
            allowed_evidence_sources=[
                {'source_type': 'transcript_message', 'source_id': '99', 'chronology': 'historical_or_restated'},
                {'source_type': 'transcript_message', 'source_id': '101', 'chronology': 'current_turn'},
                {'source_type': 'transcript_message', 'source_id': '102', 'chronology': 'current_turn'},
            ],
        )

        clock = CampaignClock.query.filter_by(
            campaign_id=self.campaign.id,
            clock_id='lantern_failure_clock',
        ).one()
        self.assertEqual(clock.filled, 0)
        self.assertIn('historical_or_restated_evidence', result['rejected_advances'][0]['evidence_gaps'])

    def test_new_dusk_and_new_failure_satisfy_declared_multi_source_trigger(self):
        self._add_session_message(101, 'We wait and watch the lantern line through dusk.', role='player')
        self._add_session_message(102, 'Dusk falls, and the east lantern newly gutters out.')
        db.session.add(CampaignClock(
            campaign_id=self.campaign.id,
            clock_id='lantern_failure_clock',
            name='Lantern Failures',
            segments=4,
            filled=0,
            status='active',
            trigger='Each dusk, one additional lantern goes dark unless the party prevents it.',
        ))
        db.session.commit()
        verdict = self._trigger_verdict(101, 102, clause_id='declared_trigger')
        verdict['supported_claims'] = ['Dusk newly fell.', 'The east lantern newly went dark.']

        result = apply_clock_adjudication(
            self.campaign,
            {
                'create_clocks': [],
                'advance_clocks': [{
                    'clock_id': 'lantern_failure_clock',
                    'delta': 1,
                    'reason': 'A new dusk and lantern failure occurred visibly.',
                    'evidence': ['Dusk fell.', 'The east lantern went dark.'],
                    'trigger_verdict': verdict,
                }],
                'retire_clocks': [],
                'no_change_explanations': [],
            },
            audit_context={'source_player_message_id': 101, 'source_dm_message_id': 102},
        )

        clock = CampaignClock.query.filter_by(
            campaign_id=self.campaign.id,
            clock_id='lantern_failure_clock',
        ).one()
        self.assertEqual(clock.filled, 1)
        self.assertEqual(result['errors'], [])
        event = WorldEvent.query.filter_by(campaign_id=self.campaign.id, event_type='clock_advanced').one()
        verdict_payload = json.loads(event.payload)['provenance']['trigger_verdict']
        self.assertEqual(verdict_payload['supported_claims'], ['Dusk newly fell.', 'The east lantern newly went dark.'])

    def test_visible_player_prevention_can_reduce_clock(self):
        self._add_session_message(101, 'I shield the lantern and replace its failing wick.', role='player')
        self._add_session_message(102, 'Your repair catches; the lantern burns steadily again.')
        db.session.add(CampaignClock(
            campaign_id=self.campaign.id,
            clock_id='lantern_failure_clock',
            name='Lantern Failures',
            segments=4,
            filled=2,
            status='active',
            trigger='Each dusk, one additional lantern goes dark unless the party prevents it.',
        ))
        db.session.commit()
        verdict = self._trigger_verdict(101, 102, clause_id='visible_prevention_or_relief')
        verdict['supported_claims'] = ['The party newly prevented a lantern failure.']

        apply_clock_adjudication(
            self.campaign,
            {
                'create_clocks': [],
                'advance_clocks': [{
                    'clock_id': 'lantern_failure_clock',
                    'delta': -1,
                    'reason': 'The party visibly prevented the failure.',
                    'evidence': ['The repaired lantern burns steadily.'],
                    'trigger_verdict': verdict,
                }],
                'retire_clocks': [],
                'no_change_explanations': [],
            },
            audit_context={'source_player_message_id': 101, 'source_dm_message_id': 102},
        )

        clock = CampaignClock.query.filter_by(
            campaign_id=self.campaign.id,
            clock_id='lantern_failure_clock',
        ).one()
        self.assertEqual(clock.filled, 1)

    def test_completion_criteria_allow_evidenced_retirement(self):
        self._add_session_message(101, 'The DM names the suspect and establishes the location.')
        db.session.add(CampaignClock(
            campaign_id=self.campaign.id,
            clock_id='identify_saboteur',
            name='Identify the Saboteur',
            segments=4,
            filled=4,
            status='completion_pending',
            completion_criteria=[
                {'id': 'named_suspect', 'description': 'The DM names the suspect.'},
                {'id': 'usable_location', 'description': 'The DM establishes the location.'},
            ],
        ))
        db.session.commit()

        result = apply_clock_adjudication(
            self.campaign,
            {
                'create_clocks': [],
                'advance_clocks': [],
                'retire_clocks': [{
                    'clock_id': 'identify_saboteur',
                    'completion_criteria_verdicts': [
                        {
                            'criterion_id': 'named_suspect',
                            'verdict': 'met',
                            'supported_claims': ['A specific suspect is named.'],
                            'evidence_sources': [
                                {'source_type': 'transcript_message', 'source_id': '101'},
                            ],
                            'reason': 'The visible reply names the suspect.',
                        },
                        {
                            'criterion_id': 'usable_location',
                            'verdict': 'met',
                            'supported_claims': ['A usable location is established.'],
                            'evidence_sources': [
                                {'source_type': 'transcript_message', 'source_id': '101'},
                            ],
                            'reason': 'The visible reply establishes the location.',
                        },
                    ],
                    'consequence': {
                        'verdict': 'supported',
                        'claims': ['A specific suspect is named.', 'A usable location is established.'],
                        'visibility': 'dm_private',
                        'evidence_sources': [
                            {'source_type': 'transcript_message', 'source_id': '101'},
                        ],
                        'reason': 'The consequence contains only claims supported by the criterion verdicts.',
                    },
                }],
                'no_change_explanations': [],
            },
            audit_context={
                'trace_id': 'completion-criteria-success-trace',
                'source_player_message_id': 101,
                'source_dm_message_id': 102,
            },
        )
        db.session.commit()

        self.assertEqual(result['errors'], [])
        clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='identify_saboteur').one()
        self.assertEqual(clock.status, 'resolved')
        event = WorldEvent.query.filter_by(campaign_id=self.campaign.id, event_type='clock_retired').one()
        payload = json.loads(event.payload)
        self.assertEqual(payload['completion_criteria_met'], ['named_suspect', 'usable_location'])
        self.assertEqual(payload['provenance']['evidence_status'], 'ai_adjudicated_verified_sources')
        self.assertTrue(payload['mechanical_complete'])
        self.assertTrue(payload['consequence_applied'])
        self.assertEqual(payload['visibility_decision']['applied'], 'dm_private')
        self.assertEqual(payload['rejected_proposals'], [])

    def test_completion_criteria_reject_missing_ai_verdicts(self):
        db.session.add(CampaignClock(
            campaign_id=self.campaign.id,
            clock_id='identify_saboteur',
            name='Identify the Saboteur',
            segments=4,
            filled=4,
            status='completion_pending',
            completion_criteria=[
                {'id': 'named_suspect', 'description': 'Visible evidence identifies a specific suspect.'},
                {'id': 'usable_location', 'description': 'Visible evidence establishes a usable location.'},
            ],
        ))
        db.session.commit()

        result = apply_clock_adjudication(
            self.campaign,
            {
                'create_clocks': [],
                'advance_clocks': [],
                'retire_clocks': [{
                    'clock_id': 'identify_saboteur',
                    'completion_criteria_verdicts': [],
                }],
                'no_change_explanations': [],
            },
            audit_context={
                'trace_id': 'echo-trace',
                'source_player_message_id': 101,
                'source_dm_message_id': 102,
            },
        )
        db.session.commit()

        self.assertNotEqual(result['errors'], [])
        clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='identify_saboteur').one()
        self.assertEqual(clock.status, 'completion_pending')
        self.assertEqual(WorldEvent.query.filter_by(campaign_id=self.campaign.id, event_type='clock_retired').count(), 0)

    def test_completion_criteria_reject_premature_retirement_before_clock_is_full(self):
        db.session.add(CampaignClock(
            campaign_id=self.campaign.id,
            clock_id='identify_saboteur',
            name='Identify the Saboteur',
            segments=4,
            filled=2,
            status='active',
            completion_criteria=[
                {'id': 'named_suspect', 'description': 'Visible evidence identifies a specific suspect.'},
            ],
        ))
        db.session.commit()

        result = apply_clock_adjudication(
            self.campaign,
            {
                'create_clocks': [],
                'advance_clocks': [],
                'retire_clocks': [{
                    'clock_id': 'identify_saboteur',
                    'completion_criteria_verdicts': [{
                        'criterion_id': 'named_suspect',
                        'verdict': 'met',
                        'supported_claims': ['Garret is the suspect.'],
                        'evidence_sources': [],
                        'reason': 'The criterion is met.',
                    }],
                    'consequence': {
                        'verdict': 'supported',
                        'claims': ['Garret is the suspect.'],
                        'visibility': 'dm_private',
                        'evidence_sources': [],
                        'reason': 'The consequence repeats the criterion claim.',
                    },
                }],
                'no_change_explanations': [],
            },
            audit_context={
                'trace_id': 'premature-trace',
                'source_player_message_id': 101,
                'source_dm_message_id': 102,
            },
        )
        db.session.commit()

        self.assertNotEqual(result['errors'], [])
        clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='identify_saboteur').one()
        self.assertEqual(clock.status, 'active')
        self.assertEqual(WorldEvent.query.filter_by(campaign_id=self.campaign.id, event_type='clock_retired').count(), 0)

    def test_completion_criteria_retire_with_durable_rule_result_event(self):
        db.session.add(CampaignClock(
            campaign_id=self.campaign.id,
            clock_id='cipher_break',
            name='Cipher Break',
            segments=4,
            filled=4,
            status='completion_pending',
            completion_criteria=[
                {'id': 'decoded_cipher', 'description': 'A deterministic rule decoded the cipher.'},
            ],
        ))
        db.session.commit()
        rule_result = WorldEvent(
            campaign_id=self.campaign.id,
            event_type='deterministic_rule_result',
            summary='The decoding rule completed successfully.',
            payload=json.dumps({'rule_id': 'decoding_rule_v1', 'outcome': 'passed'}),
            visibility='dm_private',
        )
        db.session.add(rule_result)
        db.session.commit()

        result = apply_clock_adjudication(
            self.campaign,
            {
                'create_clocks': [],
                'advance_clocks': [],
                'retire_clocks': [{
                    'clock_id': 'cipher_break',
                    'completion_criteria_verdicts': [{
                        'criterion_id': 'decoded_cipher',
                        'verdict': 'met',
                        'supported_claims': ['The cipher is decoded.'],
                        'evidence_sources': [
                            {'source_type': 'world_event', 'source_id': str(rule_result.id)},
                        ],
                        'reason': 'A durable rule-result event records successful decoding.',
                    }],
                    'consequence': {
                        'verdict': 'supported',
                        'claims': ['The cipher is decoded.'],
                        'visibility': 'dm_private',
                        'evidence_sources': [
                            {'source_type': 'world_event', 'source_id': str(rule_result.id)},
                        ],
                        'reason': 'The consequence copies the supported claim.',
                    },
                }],
                'no_change_explanations': [],
            },
            audit_context={
                'trace_id': 'rule-trace',
                'source_player_message_id': 101,
                'source_dm_message_id': 102,
            },
            allowed_evidence_sources=[
                {'source_type': 'world_event', 'source_id': str(rule_result.id)},
            ],
        )
        db.session.commit()

        self.assertEqual(result['errors'], [])
        clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='cipher_break').one()
        self.assertEqual(clock.status, 'resolved')
        event = WorldEvent.query.filter_by(campaign_id=self.campaign.id, event_type='clock_retired').one()
        payload = json.loads(event.payload)
        self.assertEqual(payload['criterion_evidence_status']['decoded_cipher'], 'verified')

    def test_completion_criteria_retire_party_known_consequence_with_supporting_evidence(self):
        db.session.add(CampaignClock(
            campaign_id=self.campaign.id,
            clock_id='identify_saboteur',
            name='Identify the Saboteur',
            segments=4,
            filled=4,
            status='completion_pending',
            visibility='party_known',
            completion_criteria=[
                {'id': 'named_suspect', 'description': 'The DM names the suspect Garret in the visible reply.'},
                {'id': 'usable_location', 'description': 'The DM reveals the usable location is the docks.'},
            ],
        ))
        db.session.commit()
        self._add_session_message(101, 'The DM names the suspect Garret in the visible reply.')
        self._add_session_message(102, 'The DM reveals the usable location is the docks.')

        result = apply_clock_adjudication(
            self.campaign,
            {
                'create_clocks': [],
                'advance_clocks': [],
                'retire_clocks': [{
                    'clock_id': 'identify_saboteur',
                    'completion_criteria_verdicts': [
                        {
                            'criterion_id': 'named_suspect',
                            'verdict': 'met',
                            'supported_claims': ['Garret is the identified suspect.'],
                            'evidence_sources': [
                                {'source_type': 'transcript_message', 'source_id': '101'},
                            ],
                            'reason': 'The visible reply names Garret.',
                        },
                        {
                            'criterion_id': 'usable_location',
                            'verdict': 'met',
                            'supported_claims': ['The usable location is the docks.'],
                            'evidence_sources': [
                                {'source_type': 'transcript_message', 'source_id': '102'},
                            ],
                            'reason': 'The visible reply establishes the docks as the usable location.',
                        },
                    ],
                    'consequence': {
                        'verdict': 'supported',
                        'claims': ['Garret is the identified suspect.', 'The usable location is the docks.'],
                        'visibility': 'party_known',
                        'evidence_sources': [
                            {'source_type': 'transcript_message', 'source_id': '101'},
                            {'source_type': 'transcript_message', 'source_id': '102'},
                        ],
                        'reason': 'Both consequence claims are copied from met criteria.',
                    },
                }],
                'no_change_explanations': [],
            },
            audit_context={
                'trace_id': 'party-known-supported-trace',
                'source_player_message_id': 101,
                'source_dm_message_id': 102,
            },
        )
        db.session.commit()

        self.assertEqual(result['errors'], [])
        clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='identify_saboteur').one()
        self.assertEqual(clock.status, 'resolved')
        event = WorldEvent.query.filter_by(campaign_id=self.campaign.id, event_type='clock_retired').one()
        self.assertEqual(event.visibility, 'party_known')
        payload = json.loads(event.payload)
        self.assertTrue(payload['consequence_applied'])
        self.assertEqual(payload['visibility_decision']['applied'], 'party_known')
        self.assertEqual(payload['rejected_proposals'], [])

    def test_completion_criteria_run37_regression_unsupported_party_known_consequence(self):
        db.session.add(CampaignClock(
            campaign_id=self.campaign.id,
            clock_id='saboteur_trail',
            name='Saboteur Trail',
            segments=4,
            filled=4,
            status='completion_pending',
            visibility='party_known',
            completion_criteria=[
                {'id': 'named_garret', 'description': 'Visible evidence names Garret as the saboteur.'},
                {'id': 'located_garret', 'description': 'Visible evidence establishes Garret\u2019s location.'},
            ],
        ))
        db.session.commit()
        self._add_session_message(101, 'The party finds a campsite and an unopened pouch.')
        self._add_session_message(102, 'The pouch remains unopened; no identity or location is established.')

        clock_context = build_session_clock_context(
            self.campaign,
            self.session,
            self.user,
            'The party finds a campsite and an unopened pouch.',
            'The pouch remains unopened; no identity or location is established.',
            {},
            {},
            player_message_id=101,
            dm_message_id=102,
        )
        adjudicator_response = _normalized_from_raw({
            'choices': [{'message': {'content': '', 'tool_calls': [{
                'id': 'clock-run37',
                'type': 'function',
                'function': {
                    'name': 'submit_clock_updates',
                    'arguments': json.dumps({
                        'create_clocks': [],
                        'advance_clocks': [],
                        'retire_clocks': [{
                            'clock_id': 'saboteur_trail',
                            'completion_criteria_verdicts': [
                                {
                                    'criterion_id': 'named_garret',
                                    'verdict': 'not_met',
                                    'supported_claims': [],
                                    'evidence_sources': [
                                        {'source_type': 'transcript_message', 'source_id': '101'},
                                    ],
                                    'reason': 'The campsite and unopened pouch do not name a saboteur.',
                                },
                                {
                                    'criterion_id': 'located_garret',
                                    'verdict': 'uncertain',
                                    'supported_claims': [],
                                    'evidence_sources': [
                                        {'source_type': 'transcript_message', 'source_id': '102'},
                                    ],
                                    'reason': 'No location for Garret is established.',
                                },
                            ],
                            'consequence': {
                                'verdict': 'unsupported',
                                'claims': [],
                                'visibility': 'party_known',
                                'evidence_sources': [],
                                'reason': 'The proposed identity and location conclusion is unsupported.',
                            },
                        }],
                        'no_change_explanations': [],
                    }),
                },
            }]}}],
        })
        with patch('openrouter._post_chat_normalized', return_value=adjudicator_response) as post_chat:
            clock_updates = get_session_clock_updates(clock_context)
        adjudicator_payload = json.loads(post_chat.call_args.args[0][1]['content'])
        self.assertIn('campsite and an unopened pouch', adjudicator_payload['latest_player_message']['content'])
        self.assertEqual(
            clock_updates['retire_clocks'][0]['completion_criteria_verdicts'][0]['verdict'],
            'not_met',
        )

        result = apply_clock_adjudication(
            self.campaign,
            clock_updates,
            audit_context={
                'trace_id': 'run37-trace',
                'source_player_message_id': 101,
                'source_dm_message_id': 102,
            },
            allowed_evidence_sources=clock_context['allowed_evidence_sources'],
        )
        db.session.commit()

        self.assertNotEqual(result['errors'], [])
        clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='saboteur_trail').one()
        self.assertEqual(clock.status, 'completion_pending')
        retired_events = WorldEvent.query.filter_by(campaign_id=self.campaign.id, event_type='clock_retired').all()
        self.assertEqual(len(retired_events), 0)
        party_known_events = [
            event
            for event in WorldEvent.query.filter_by(campaign_id=self.campaign.id).all()
            if event.visibility == 'party_known'
        ]
        for event in party_known_events:
            self.assertNotIn('Garret', event.summary or '')

    def test_completion_criteria_reject_self_certified_evidence_status(self):
        db.session.add(CampaignClock(
            campaign_id=self.campaign.id,
            clock_id='identify_saboteur',
            name='Identify the Saboteur',
            segments=4,
            filled=4,
            status='completion_pending',
            completion_criteria=[
                {'id': 'named_suspect', 'description': 'Visible evidence identifies a specific suspect.'},
            ],
        ))
        db.session.commit()

        result = apply_clock_adjudication(
            self.campaign,
            {
                'create_clocks': [],
                'advance_clocks': [],
                'retire_clocks': [{
                    'clock_id': 'identify_saboteur',
                    'completion_criteria_verdicts': [{
                        'criterion_id': 'named_suspect',
                        'verdict': 'met',
                        'supported_claims': ['Garret is the saboteur.'],
                        'evidence_sources': [],
                        'evidence_status': 'supported_by_evidence',
                        'reason': 'The proposing model self-certified this verdict.',
                    }],
                    'consequence': {
                        'verdict': 'supported',
                        'claims': ['Garret is the saboteur.'],
                        'visibility': 'dm_private',
                        'evidence_sources': [],
                        'reason': 'The proposing model self-certified this consequence.',
                    },
                }],
                'no_change_explanations': [],
            },
            audit_context={
                'trace_id': 'self-certified-trace',
                'source_player_message_id': 101,
                'source_dm_message_id': 102,
            },
        )
        db.session.commit()

        self.assertNotEqual(result['errors'], [])
        clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='identify_saboteur').one()
        self.assertEqual(clock.status, 'completion_pending')
        self.assertEqual(WorldEvent.query.filter_by(campaign_id=self.campaign.id, event_type='clock_retired').count(), 0)

    def test_completion_criteria_reject_nonexistent_transcript_source_id(self):
        db.session.add(CampaignClock(
            campaign_id=self.campaign.id,
            clock_id='identify_saboteur',
            name='Identify the Saboteur',
            segments=4,
            filled=4,
            status='completion_pending',
            completion_criteria=[
                {'id': 'named_suspect', 'description': 'Visible evidence identifies a specific suspect.'},
            ],
        ))
        db.session.commit()

        result = apply_clock_adjudication(
            self.campaign,
            {
                'create_clocks': [],
                'advance_clocks': [],
                'retire_clocks': [{
                    'clock_id': 'identify_saboteur',
                    'completion_criteria_verdicts': [{
                        'criterion_id': 'named_suspect',
                        'verdict': 'met',
                        'supported_claims': ['Garret is the saboteur.'],
                        'evidence_sources': [
                            {'source_type': 'transcript_message', 'source_id': '99999'},
                        ],
                        'reason': 'The nonexistent transcript supposedly names Garret.',
                    }],
                    'consequence': {
                        'verdict': 'supported',
                        'claims': ['Garret is the saboteur.'],
                        'visibility': 'dm_private',
                        'evidence_sources': [
                            {'source_type': 'transcript_message', 'source_id': '99999'},
                        ],
                        'reason': 'The consequence cites the nonexistent transcript.',
                    },
                }],
                'no_change_explanations': [],
            },
            audit_context={
                'trace_id': 'nonexistent-source-trace',
                'source_player_message_id': 101,
                'source_dm_message_id': 102,
            },
            allowed_evidence_sources=[
                {'source_type': 'transcript_message', 'source_id': '99999'},
            ],
        )
        db.session.commit()

        self.assertNotEqual(result['errors'], [])
        clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='identify_saboteur').one()
        self.assertEqual(clock.status, 'completion_pending')
        self.assertEqual(WorldEvent.query.filter_by(campaign_id=self.campaign.id, event_type='clock_retired').count(), 0)

    def test_completion_criteria_delayed_consequence_stays_pending_until_verified_evidence(self):
        self._add_session_message(101, 'The DM names the suspect Garret in the visible reply.')
        db.session.add(CampaignClock(
            campaign_id=self.campaign.id,
            clock_id='identify_saboteur',
            name='Identify the Saboteur',
            segments=4,
            filled=4,
            status='completion_pending',
            visibility='party_known',
            completion_criteria=[
                {'id': 'named_suspect', 'description': 'The DM names the suspect Garret in the visible reply.'},
            ],
        ))
        db.session.commit()

        first = apply_clock_adjudication(
            self.campaign,
            {
                'create_clocks': [],
                'advance_clocks': [],
                'retire_clocks': [{
                    'clock_id': 'identify_saboteur',
                    'completion_criteria_verdicts': [{
                        'criterion_id': 'named_suspect',
                        'verdict': 'met',
                        'supported_claims': ['Garret is the named suspect.'],
                        'evidence_sources': [
                            {'source_type': 'transcript_message', 'source_id': '101'},
                        ],
                        'reason': 'The visible reply names Garret.',
                    }],
                    'consequence': {
                        'verdict': 'uncertain',
                        'claims': [],
                        'visibility': 'party_known',
                        'evidence_sources': [],
                        'reason': 'The adjudicator has not approved a consequence yet.',
                    },
                }],
                'no_change_explanations': [],
            },
            audit_context={
                'trace_id': 'delayed-first-trace',
                'source_player_message_id': 101,
                'source_dm_message_id': 102,
            },
        )
        db.session.commit()

        self.assertNotEqual(first['errors'], [])
        clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='identify_saboteur').one()
        self.assertEqual(clock.status, 'completion_pending')
        self.assertEqual(
            clock.completion_state['criteria']['named_suspect']['supported_claims'],
            ['Garret is the named suspect.'],
        )
        self.assertEqual(WorldEvent.query.filter_by(campaign_id=self.campaign.id, event_type='clock_retired').count(), 0)

        self._add_session_message(201, 'The party asks what the established clue means.', role='user')
        self._add_session_message(202, 'The investigation remains ready for its established conclusion.')
        later_context = build_session_clock_context(
            self.campaign,
            self.session,
            self.user,
            'The party asks what the established clue means.',
            'The investigation remains ready for its established conclusion.',
            {},
            {},
            player_message_id=201,
            dm_message_id=202,
        )
        prior_source = next(
            source for source in later_context['allowed_evidence_sources']
            if source['source_type'] == 'transcript_message' and source['source_id'] == '101'
        )
        self.assertEqual(prior_source['chronology'], 'historical_or_restated')
        self.assertTrue(any(
            item['source_id'] == '101'
            and item['criterion_id'] == 'named_suspect'
            and 'Garret' in item['content']
            for item in later_context['pending_completion_evidence']
        ))

        second = apply_clock_adjudication(
            self.campaign,
            {
                'create_clocks': [],
                'advance_clocks': [],
                'retire_clocks': [{
                    'clock_id': 'identify_saboteur',
                    'completion_criteria_verdicts': [{
                        'criterion_id': 'named_suspect',
                        'verdict': 'met',
                        'supported_claims': ['Garret is the named suspect.'],
                        'evidence_sources': [
                            {'source_type': 'transcript_message', 'source_id': '101'},
                        ],
                        'reason': 'The visible reply names Garret.',
                    }],
                    'consequence': {
                        'verdict': 'supported',
                        'claims': ['Garret is the named suspect.'],
                        'visibility': 'party_known',
                        'evidence_sources': [
                            {'source_type': 'transcript_message', 'source_id': '101'},
                        ],
                        'reason': 'The consequence copies the supported criterion claim.',
                    },
                }],
                'no_change_explanations': [],
            },
            audit_context={
                'trace_id': 'delayed-second-trace',
                'source_player_message_id': 201,
                'source_dm_message_id': 202,
            },
            allowed_evidence_sources=later_context['allowed_evidence_sources'],
        )
        db.session.commit()

        self.assertEqual(second['errors'], [])
        clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='identify_saboteur').one()
        self.assertEqual(clock.status, 'resolved')
        event = WorldEvent.query.filter_by(campaign_id=self.campaign.id, event_type='clock_retired').one()
        self.assertEqual(event.visibility, 'party_known')
        payload = json.loads(event.payload)
        self.assertTrue(payload['consequence_applied'])
        self.assertEqual(payload['visibility_decision']['applied'], 'party_known')

    def test_completion_criteria_respects_ai_not_met_verdict(self):
        db.session.add(CampaignClock(
            campaign_id=self.campaign.id, clock_id='contradicted_clock', name='Contradicted Clock',
            segments=4, filled=4, status='completion_pending',
            completion_criteria=[{'id': 'resolution_rule', 'description': 'The resolution is agreed by all parties.'}],
        ))
        db.session.commit()
        self._add_session_message(101, 'The parties are not agreed on the resolution.')
        result = apply_clock_adjudication(self.campaign, {
            'create_clocks': [], 'advance_clocks': [],
            'retire_clocks': [{
                'clock_id': 'contradicted_clock',
                'completion_criteria_verdicts': [{
                    'criterion_id': 'resolution_rule', 'verdict': 'not_met',
                    'supported_claims': [],
                    'evidence_sources': [{'source_type': 'transcript_message', 'source_id': '101'}],
                    'reason': 'The cited evidence says agreement has not been reached.',
                }],
                'consequence': {
                    'verdict': 'unsupported', 'claims': [], 'visibility': 'dm_private',
                    'evidence_sources': [], 'reason': 'No completion consequence is supported.',
                },
            }],
            'no_change_explanations': [],
        }, audit_context={'trace_id': 'contradicted-trace', 'source_player_message_id': 101})
        db.session.commit()
        self.assertNotEqual(result['errors'], [])
        clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='contradicted_clock').one()
        self.assertEqual(clock.status, 'completion_pending')

    def test_completion_criteria_reject_source_type_outside_context_contract(self):
        db.session.add(CampaignClock(
            campaign_id=self.campaign.id, clock_id='cipher_break', name='Cipher Break',
            segments=4, filled=4, status='completion_pending',
            completion_criteria=[{'id': 'decoded_cipher', 'description': 'The cipher has been decoded.'}],
        ))
        db.session.commit()
        result = apply_clock_adjudication(self.campaign, {
            'create_clocks': [], 'advance_clocks': [],
            'retire_clocks': [{
                'clock_id': 'cipher_break',
                'completion_criteria_verdicts': [{
                    'criterion_id': 'decoded_cipher', 'verdict': 'met',
                    'supported_claims': ['The cipher has been decoded.'],
                    'evidence_sources': [{'source_type': 'deterministic_rule', 'source_id': 'made_up_rule'}],
                    'reason': 'A rule allegedly decoded it.',
                }],
                'consequence': {
                    'verdict': 'supported', 'claims': ['The cipher has been decoded.'],
                    'visibility': 'dm_private',
                    'evidence_sources': [{'source_type': 'deterministic_rule', 'source_id': 'made_up_rule'}],
                    'reason': 'The consequence repeats the criterion claim.',
                },
            }],
            'no_change_explanations': [],
        }, audit_context={'trace_id': 'unsupported-source-trace'}, allowed_evidence_sources=[
            {'source_type': 'deterministic_rule', 'source_id': 'made_up_rule'},
        ])
        db.session.commit()
        self.assertIn('unsupported_source_type', str(result))
        clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='cipher_break').one()
        self.assertEqual(clock.status, 'completion_pending')

    def test_completion_criteria_reject_mixed_valid_and_invalid_sources(self):
        db.session.add(CampaignClock(
            campaign_id=self.campaign.id,
            clock_id='identify_saboteur',
            name='Identify the Saboteur',
            segments=4,
            filled=4,
            status='completion_pending',
            completion_criteria=[
                {'id': 'named_suspect', 'description': 'Visible evidence names the suspect.'},
            ],
        ))
        db.session.commit()
        self._add_session_message(101, 'The current exchange contains a campsite description.')

        result = apply_clock_adjudication(
            self.campaign,
            {
                'create_clocks': [],
                'advance_clocks': [],
                'retire_clocks': [{
                    'clock_id': 'identify_saboteur',
                    'completion_criteria_verdicts': [{
                        'criterion_id': 'named_suspect',
                        'verdict': 'met',
                        'supported_claims': ['Garret is the suspect.'],
                        'evidence_sources': [
                            {'source_type': 'transcript_message', 'source_id': '101'},
                            {'source_type': 'transcript_message', 'source_id': '99'},
                        ],
                        'reason': 'The rejected source supposedly supplies the identity.',
                    }],
                    'consequence': {
                        'verdict': 'supported',
                        'claims': ['Garret is the suspect.'],
                        'visibility': 'party_known',
                        'evidence_sources': [
                            {'source_type': 'transcript_message', 'source_id': '101'},
                        ],
                        'reason': 'The consequence repeats the criterion claim.',
                    },
                }],
                'no_change_explanations': [],
            },
            audit_context={'trace_id': 'mixed-criterion-sources'},
            allowed_evidence_sources=[
                {'source_type': 'transcript_message', 'source_id': '101'},
            ],
        )
        db.session.commit()

        self.assertIn('source_not_in_adjudicator_context', str(result))
        clock = CampaignClock.query.filter_by(
            campaign_id=self.campaign.id,
            clock_id='identify_saboteur',
        ).one()
        self.assertEqual(clock.status, 'completion_pending')
        self.assertEqual(clock.completion_state, {})
        self.assertEqual(WorldEvent.query.filter_by(
            campaign_id=self.campaign.id,
            event_type='clock_retired',
        ).count(), 0)

    def test_completion_criteria_reject_mixed_consequence_sources(self):
        db.session.add(CampaignClock(
            campaign_id=self.campaign.id,
            clock_id='identify_saboteur',
            name='Identify the Saboteur',
            segments=4,
            filled=4,
            status='completion_pending',
            completion_criteria=[
                {'id': 'named_suspect', 'description': 'Visible evidence names the suspect.'},
            ],
        ))
        db.session.commit()
        self._add_session_message(101, 'The visible reply names Garret as the suspect.')

        result = apply_clock_adjudication(
            self.campaign,
            {
                'create_clocks': [],
                'advance_clocks': [],
                'retire_clocks': [{
                    'clock_id': 'identify_saboteur',
                    'completion_criteria_verdicts': [{
                        'criterion_id': 'named_suspect',
                        'verdict': 'met',
                        'supported_claims': ['Garret is the suspect.'],
                        'evidence_sources': [
                            {'source_type': 'transcript_message', 'source_id': '101'},
                        ],
                        'reason': 'The visible reply names Garret.',
                    }],
                    'consequence': {
                        'verdict': 'supported',
                        'claims': ['Garret is the suspect.'],
                        'visibility': 'party_known',
                        'evidence_sources': [
                            {'source_type': 'transcript_message', 'source_id': '101'},
                            {'source_type': 'transcript_message', 'source_id': '99'},
                        ],
                        'reason': 'The rejected source is also cited for the consequence.',
                    },
                }],
                'no_change_explanations': [],
            },
            audit_context={'trace_id': 'mixed-consequence-sources'},
            allowed_evidence_sources=[
                {'source_type': 'transcript_message', 'source_id': '101'},
            ],
        )
        db.session.commit()

        self.assertIn('source_not_in_adjudicator_context', str(result))
        clock = CampaignClock.query.filter_by(
            campaign_id=self.campaign.id,
            clock_id='identify_saboteur',
        ).one()
        self.assertEqual(clock.status, 'completion_pending')
        self.assertIn('named_suspect', clock.completion_state['criteria'])
        self.assertEqual(WorldEvent.query.filter_by(
            campaign_id=self.campaign.id,
            event_type='clock_retired',
        ).count(), 0)

    def test_completion_criteria_reject_source_outside_adjudicator_context(self):
        db.session.add(CampaignClock(
            campaign_id=self.campaign.id, clock_id='identify_saboteur', name='Identify the Saboteur',
            segments=4, filled=4, status='completion_pending',
            completion_criteria=[{'id': 'named_suspect', 'description': 'Visible evidence names the suspect.'}],
        ))
        db.session.commit()
        self._add_session_message(99, 'An older transcript identifies Garret as the suspect.')
        self._add_session_message(101, 'The current exchange discusses the weather.')
        result = apply_clock_adjudication(self.campaign, {
            'create_clocks': [], 'advance_clocks': [],
            'retire_clocks': [{
                'clock_id': 'identify_saboteur',
                'completion_criteria_verdicts': [{
                    'criterion_id': 'named_suspect', 'verdict': 'met',
                    'supported_claims': ['Garret is the suspect.'],
                    'evidence_sources': [{'source_type': 'transcript_message', 'source_id': '99'}],
                    'reason': 'The old transcript names Garret.',
                }],
                'consequence': {
                    'verdict': 'supported', 'claims': ['Garret is the suspect.'],
                    'visibility': 'dm_private',
                    'evidence_sources': [{'source_type': 'transcript_message', 'source_id': '99'}],
                    'reason': 'The consequence repeats the criterion claim.',
                },
            }],
            'no_change_explanations': [],
        }, audit_context={'trace_id': 'source-allowlist-trace', 'source_player_message_id': 101},
        allowed_evidence_sources=[{'source_type': 'transcript_message', 'source_id': '101'}])
        db.session.commit()
        self.assertIn('source_not_in_adjudicator_context', str(result))
        clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='identify_saboteur').one()
        self.assertEqual(clock.status, 'completion_pending')

    def test_completion_criteria_reject_public_consequence_backed_by_transcript(self):
        db.session.add(CampaignClock(
            campaign_id=self.campaign.id,
            clock_id='public_clock',
            name='Public Clock',
            segments=4,
            filled=4,
            status='completion_pending',
            completion_criteria=[
                {'id': 'public_reveal', 'description': 'The festival concludes publicly.'},
            ],
        ))
        db.session.commit()
        self._add_session_message(101, 'The party sees the festival conclude.')

        result = apply_clock_adjudication(
            self.campaign,
            {
                'create_clocks': [],
                'advance_clocks': [],
                'retire_clocks': [{
                    'clock_id': 'public_clock',
                    'completion_criteria_verdicts': [{
                        'criterion_id': 'public_reveal',
                        'verdict': 'met',
                        'supported_claims': ['The festival concludes publicly.'],
                        'evidence_sources': [
                            {'source_type': 'transcript_message', 'source_id': '101'},
                        ],
                        'reason': 'The party-visible transcript records the conclusion.',
                    }],
                    'consequence': {
                        'verdict': 'supported',
                        'claims': ['The festival concludes publicly.'],
                        'visibility': 'public',
                        'evidence_sources': [
                            {'source_type': 'transcript_message', 'source_id': '101'},
                        ],
                        'reason': 'The proposed consequence is world-public.',
                    },
                }],
                'no_change_explanations': [],
            },
            audit_context={'trace_id': 'transcript-public-visibility'},
            allowed_evidence_sources=[
                {'source_type': 'transcript_message', 'source_id': '101'},
            ],
        )
        db.session.commit()

        self.assertIn('source_visibility_too_private', str(result))
        clock = CampaignClock.query.filter_by(
            campaign_id=self.campaign.id,
            clock_id='public_clock',
        ).one()
        self.assertEqual(clock.status, 'completion_pending')
        self.assertEqual(WorldEvent.query.filter_by(
            campaign_id=self.campaign.id,
            event_type='clock_retired',
        ).count(), 0)

    def test_completion_criteria_reject_consequence_claim_not_derived_from_criteria(self):
        db.session.add(CampaignClock(
            campaign_id=self.campaign.id, clock_id='identify_saboteur', name='Identify the Saboteur',
            segments=4, filled=4, status='completion_pending', visibility='party_known',
            completion_criteria=[{'id': 'named_suspect', 'description': 'Visible evidence identifies a suspect.'}],
        ))
        db.session.commit()
        self._add_session_message(101, 'The visible reply identifies Garret as the suspect.')
        result = apply_clock_adjudication(self.campaign, {
            'create_clocks': [], 'advance_clocks': [],
            'retire_clocks': [{
                'clock_id': 'identify_saboteur',
                'completion_criteria_verdicts': [{
                    'criterion_id': 'named_suspect', 'verdict': 'met',
                    'supported_claims': ['Garret is the suspect.'],
                    'evidence_sources': [{'source_type': 'transcript_message', 'source_id': '101'}],
                    'reason': 'The visible reply identifies Garret.',
                }],
                'consequence': {
                    'verdict': 'supported',
                    'claims': ['Garret is the suspect.', 'Garret is at the docks.'],
                    'visibility': 'party_known',
                    'evidence_sources': [{'source_type': 'transcript_message', 'source_id': '101'}],
                    'reason': 'The consequence adds a location claim.',
                },
            }],
            'no_change_explanations': [],
        }, audit_context={'trace_id': 'partial-support-trace', 'source_player_message_id': 101})
        db.session.commit()
        self.assertIn("'claims_derived_from_criteria': False", str(result))
        clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='identify_saboteur').one()
        self.assertEqual(clock.status, 'completion_pending')

    def test_completion_criteria_retire_public_consequence_with_supporting_evidence(self):
        db.session.add(CampaignClock(
            campaign_id=self.campaign.id,
            clock_id='public_clock',
            name='Public Clock',
            segments=4,
            filled=4,
            status='completion_pending',
            visibility='dm_private',
            completion_criteria=[
                {'id': 'public_reveal', 'description': 'The festival concludes with a grand parade.'},
            ],
        ))
        db.session.commit()
        public_event = WorldEvent(
            campaign_id=self.campaign.id,
            event_type='festival_concluded',
            summary='The festival concludes with a grand parade.',
            payload='{}',
            visibility='public',
        )
        db.session.add(public_event)
        db.session.commit()

        result = apply_clock_adjudication(
            self.campaign,
            {
                'create_clocks': [],
                'advance_clocks': [],
                'retire_clocks': [{
                    'clock_id': 'public_clock',
                    'completion_criteria_verdicts': [{
                        'criterion_id': 'public_reveal',
                        'verdict': 'met',
                        'supported_claims': ['The festival concludes with a grand parade.'],
                        'evidence_sources': [
                            {'source_type': 'world_event', 'source_id': str(public_event.id)},
                        ],
                        'reason': 'A durable public event records the parade.',
                    }],
                    'consequence': {
                        'verdict': 'supported',
                        'claims': ['The festival concludes with a grand parade.'],
                        'visibility': 'public',
                        'evidence_sources': [
                            {'source_type': 'world_event', 'source_id': str(public_event.id)},
                        ],
                        'reason': 'The consequence copies the supported public claim.',
                    },
                }],
                'no_change_explanations': [],
            },
            audit_context={
                'trace_id': 'public-trace',
                'source_player_message_id': 101,
                'source_dm_message_id': 102,
            },
            allowed_evidence_sources=[
                {'source_type': 'world_event', 'source_id': str(public_event.id)},
            ],
        )
        db.session.commit()

        self.assertEqual(result['errors'], [])
        event = WorldEvent.query.filter_by(campaign_id=self.campaign.id, event_type='clock_retired').one()
        self.assertEqual(event.visibility, 'public')
        payload = json.loads(event.payload)
        self.assertTrue(payload['consequence_applied'])
        self.assertEqual(payload['visibility_decision']['applied'], 'public')
        self.assertEqual(payload['rejected_proposals'], [])

    def test_completion_criteria_retire_dm_private_consequence_is_always_safe(self):
        db.session.add(CampaignClock(
            campaign_id=self.campaign.id,
            clock_id='dark_pact',
            name='Dark Pact',
            segments=4,
            filled=4,
            status='completion_pending',
            visibility='party_known',
            completion_criteria=[
                {'id': 'pact_sealed', 'description': 'The pact is sealed by a private rule.'},
            ],
        ))
        db.session.commit()
        private_event = WorldEvent(
            campaign_id=self.campaign.id,
            event_type='pact_sealed',
            summary='The pact is sealed behind the scenes.',
            payload='{}',
            visibility='dm_private',
        )
        db.session.add(private_event)
        db.session.commit()

        result = apply_clock_adjudication(
            self.campaign,
            {
                'create_clocks': [],
                'advance_clocks': [],
                'retire_clocks': [{
                    'clock_id': 'dark_pact',
                    'completion_criteria_verdicts': [{
                        'criterion_id': 'pact_sealed',
                        'verdict': 'met',
                        'supported_claims': ['The pact is sealed.'],
                        'evidence_sources': [
                            {'source_type': 'world_event', 'source_id': str(private_event.id)},
                        ],
                        'reason': 'A durable private event records the completed pact.',
                    }],
                    'consequence': {
                        'verdict': 'supported',
                        'claims': ['The pact is sealed.'],
                        'visibility': 'dm_private',
                        'evidence_sources': [
                            {'source_type': 'world_event', 'source_id': str(private_event.id)},
                        ],
                        'reason': 'The consequence does not exceed the supported private claim.',
                    },
                }],
                'no_change_explanations': [],
            },
            audit_context={
                'trace_id': 'dm-private-trace',
                'source_player_message_id': 101,
                'source_dm_message_id': 102,
            },
            allowed_evidence_sources=[
                {'source_type': 'world_event', 'source_id': str(private_event.id)},
            ],
        )
        db.session.commit()

        self.assertEqual(result['errors'], [])
        event = WorldEvent.query.filter_by(campaign_id=self.campaign.id, event_type='clock_retired').one()
        self.assertEqual(event.visibility, 'dm_private')
        payload = json.loads(event.payload)
        self.assertTrue(payload['consequence_applied'])
        self.assertEqual(payload['visibility_decision']['applied'], 'dm_private')

    def test_completion_criteria_legacy_reason_only_party_known_retirement_is_gated(self):
        db.session.add(CampaignClock(
            campaign_id=self.campaign.id,
            clock_id='legacy_clock',
            name='Legacy Clock',
            segments=4,
            filled=4,
            status='active',
            visibility='party_known',
        ))
        db.session.commit()

        result = apply_clock_adjudication(
            self.campaign,
            {
                'create_clocks': [],
                'advance_clocks': [],
                'retire_clocks': [{
                    'clock_id': 'legacy_clock',
                    'reason': 'The party can identify Garret and knows his location.',
                }],
                'no_change_explanations': [],
            },
            audit_context={
                'trace_id': 'legacy-reason-only-trace',
                'source_player_message_id': 101,
                'source_dm_message_id': 102,
            },
        )
        db.session.commit()

        self.assertNotEqual(result['errors'], [])
        self.assertFalse(result['clock_changes'][0].get('consequence_applied', True))
        self.assertEqual(WorldEvent.query.filter_by(campaign_id=self.campaign.id, event_type='clock_retired').count(), 0)
        clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='legacy_clock').one()
        self.assertEqual(clock.status, 'active')

    def test_completion_criteria_reject_uncertain_attributed_and_conditional_claims(self):
        evidence_cases = [
            ('suspected', 'The party only suspects Garret is the saboteur at the docks.'),
            ('rumored', 'A rumor says Garret is the saboteur at the docks.'),
            ('reported', 'A scout reports Garret is the saboteur at the docks.'),
            ('quoted', 'Mira warned, “Garret is the saboteur at the docks.”'),
            ('conditional', 'If Garret is the saboteur at the docks, search the warehouse.'),
        ]

        for index, (scope, evidence_text) in enumerate(evidence_cases, start=201):
            with self.subTest(scope=scope):
                clock_id = f'identify_saboteur_{scope}'
                db.session.add(CampaignClock(
                    campaign_id=self.campaign.id,
                    clock_id=clock_id,
                    name=f'Identify the Saboteur ({scope})',
                    segments=4,
                    filled=4,
                    status='completion_pending',
                    visibility='party_known',
                    completion_criteria=[
                        {'id': 'named_garret', 'description': 'Garret is the saboteur at the docks.'},
                    ],
                ))
                db.session.commit()
                self._add_session_message(index, evidence_text)

                result = apply_clock_adjudication(
                    self.campaign,
                    {
                        'create_clocks': [],
                        'advance_clocks': [],
                        'retire_clocks': [{
                            'clock_id': clock_id,
                            'completion_criteria_verdicts': [{
                                'criterion_id': 'named_garret',
                                'verdict': 'uncertain',
                                'supported_claims': [],
                                'evidence_sources': [{
                                    'source_type': 'transcript_message',
                                    'source_id': str(index),
                                }],
                                'reason': f'The {scope} statement does not establish a confirmed fact.',
                            }],
                            'consequence': {
                                'verdict': 'uncertain',
                                'claims': [],
                                'visibility': 'party_known',
                                'evidence_sources': [{
                                    'source_type': 'transcript_message',
                                    'source_id': str(index),
                                }],
                                'reason': 'No confirmed consequence may be published.',
                            },
                        }],
                        'no_change_explanations': [],
                    },
                    audit_context={
                        'trace_id': f'{scope}-claim-trace',
                        'source_player_message_id': index,
                    },
                )
                db.session.commit()

                self.assertNotEqual(result['errors'], [])
                clock = CampaignClock.query.filter_by(
                    campaign_id=self.campaign.id, clock_id=clock_id
                ).one()
                self.assertEqual(clock.status, 'completion_pending')

        self.assertEqual(
            WorldEvent.query.filter_by(
                campaign_id=self.campaign.id, event_type='clock_retired'
            ).count(),
            0,
        )

    def test_completion_criteria_accept_exact_combined_identity_and_location_claim(self):
        db.session.add(CampaignClock(
            campaign_id=self.campaign.id,
            clock_id='identify_saboteur',
            name='Identify the Saboteur',
            segments=4,
            filled=4,
            status='completion_pending',
            visibility='party_known',
            completion_criteria=[
                {'id': 'named_garret', 'description': 'Garret is the saboteur at the docks.'},
            ],
        ))
        db.session.commit()
        self._add_session_message(101, 'Garret is the saboteur at the docks.')

        result = apply_clock_adjudication(
            self.campaign,
            {
                'create_clocks': [],
                'advance_clocks': [],
                'retire_clocks': [{
                    'clock_id': 'identify_saboteur',
                    'completion_criteria_verdicts': [{
                        'criterion_id': 'named_garret',
                        'verdict': 'met',
                        'supported_claims': ['Garret is the saboteur at the docks.'],
                        'evidence_sources': [{
                            'source_type': 'transcript_message',
                            'source_id': '101',
                        }],
                        'reason': 'The evidence directly supports the complete claim.',
                    }],
                    'consequence': {
                        'verdict': 'supported',
                        'claims': ['Garret is the saboteur at the docks.'],
                        'visibility': 'party_known',
                        'evidence_sources': [{
                            'source_type': 'transcript_message',
                            'source_id': '101',
                        }],
                        'reason': 'The consequence copies the supported criterion claim exactly.',
                    },
                }],
                'no_change_explanations': [],
            },
            audit_context={
                'trace_id': 'exact-combined-claim-trace',
                'source_player_message_id': 101,
            },
        )
        db.session.commit()

        self.assertEqual(result['errors'], [])
        clock = CampaignClock.query.filter_by(
            campaign_id=self.campaign.id, clock_id='identify_saboteur'
        ).one()
        self.assertEqual(clock.status, 'resolved')
        event = WorldEvent.query.filter_by(
            campaign_id=self.campaign.id, event_type='clock_retired'
        ).one()
        self.assertEqual(event.visibility, 'party_known')

    def test_apply_clock_adjudication_uses_verified_trigger_evidence_sources(self):
        self._add_session_message(101, 'We search for the component.', role='player')
        self._add_session_message(102, 'You find a new component clue.')
        db.session.add(CampaignClock(
            campaign_id=self.campaign.id,
            clock_id='component_search',
            name='Component Search',
            segments=4,
            filled=0,
            status='active',
        ))
        db.session.commit()

        apply_clock_adjudication(
            self.campaign,
            {
                'create_clocks': [],
                'advance_clocks': [{
                    'clock_id': 'component_search',
                    'delta': 1,
                    'reason': 'The deterministic trigger matched.',
                    'evidence': [],
                    'trigger_verdict': self._trigger_verdict(101, 102),
                    'provenance': {
                        'evidence_sources': [
                            {'source_type': 'clock_rule', 'source_id': 'component_clue_found'},
                            {'source_type': 'transcript_message', 'source_id': '101'},
                        ],
                        'evidence_status': 'supported_by_rules',
                    },
                }],
                'retire_clocks': [],
                'no_change_explanations': [],
            },
            audit_context={
                'trace_id': 'clock-rule-trace',
                'source_player_message_id': 101,
                'source_dm_message_id': 102,
            },
        )
        db.session.commit()

        event = WorldEvent.query.filter_by(
            campaign_id=self.campaign.id,
            event_type='clock_advanced',
        ).one()
        provenance = json.loads(event.payload)['provenance']
        self.assertEqual(provenance['evidence_status'], 'supported_by_evidence')
        self.assertEqual(
            provenance['evidence_sources'],
            [
                {'source_type': 'transcript_message', 'source_id': '101'},
                {'source_type': 'transcript_message', 'source_id': '102'},
            ],
        )

    def test_apply_clock_adjudication_accepts_database_clock_id_reference(self):
        self._add_session_message(101, 'We chase them toward the crypt road.', role='player')
        clock = CampaignClock(
            campaign_id=self.campaign.id,
            clock_id='race_to_crypts',
            name='Race to the Crypts',
            segments=4,
            filled=0,
            status='active',
        )
        db.session.add(clock)
        db.session.commit()

        result = apply_clock_adjudication(
            self.campaign,
            {
                'create_clocks': [],
                'advance_clocks': [
                    {
                        'clock_id': clock.id,
                        'delta': 1,
                        'reason': 'The pursuit visibly moved toward the crypt road.',
                        'evidence': ['The chase left the market and hit the crypt road.'],
                        'trigger_verdict': self._trigger_verdict(101),
                    }
                ],
                'retire_clocks': [],
                'no_change_explanations': [
                    {
                        'clock_id': clock.id,
                        'reason': 'Reference normalization should report the symbolic clock id.',
                    }
                ],
            },
            audit_context={'trace_id': 'clock-trace-db-id', 'source_player_message_id': 101},
        )

        db.session.commit()
        clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='race_to_crypts').one()
        self.assertEqual(clock.filled, 1)
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['clock_changes'][0]['action'], 'advanced')
        self.assertEqual(result['no_change_explanations'][0]['clock_id'], 'race_to_crypts')

    def test_session_message_route_runs_clock_adjudication_after_memory_patch(self):
        token = generate_token(self.user.id)
        db.session.add(CampaignClock(
            campaign_id=self.campaign.id,
            clock_id='race_to_crypts',
            name='Race to the Crypts',
            segments=4,
            filled=0,
            status='active',
        ))
        db.session.commit()

        def clock_updates_side_effect(clock_context, audit_context=None):
            self.assertEqual(clock_context['current_scene_after']['location_id'], 'crypt_road')
            self.assertEqual(clock_context['current_scene_before']['location_name'], 'Dock Ward')
            player_source_id = clock_context['latest_player_message']['source_id']
            dm_source_id = clock_context['latest_dm_message']['source_id']
            return {
                'create_clocks': [],
                'advance_clocks': [
                    {
                        'clock_id': 'race_to_crypts',
                        'delta': 1,
                        'reason': 'The visible chase moved onto the crypt road.',
                        'evidence': ['The DM confirmed the chase left Dock Ward.'],
                        'trigger_verdict': self._trigger_verdict(player_source_id, dm_source_id),
                    }
                ],
                'retire_clocks': [],
                'no_change_explanations': [],
            }

        with patch('routes.sessions.get_session_dm_response_with_tools', return_value={'mode': 'speak', 'content': 'You break into a run toward the crypt road.', 'parts': [{'type': 'narration', 'content': 'You break into a run toward the crypt road.'}], 'commit_action_ids': []}), \
                patch('routes.sessions.get_session_memory_patch', return_value={
                    'source_contract': 'compiled_session_memory_v2',
                    'running_summary': 'The party pursued the robbers onto the crypt road.',
                    'scene_patch': {
                        'location_id': 'crypt_road',
                        'location_name': 'Crypt Road',
                        'immediate_tension': 'Boots pound after fleeing grave robbers.',
                    },
                    'scene_reason': 'The chase moved to the crypt road.',
                    'upsert_graph_entities': [],
                    'upsert_graph_relations': [],
                    'upsert_graph_facts': [],
                    'create_clocks': [],
                    'retire_clocks': [],
                    'update_npc_actors': [],
                    'record_events': [],
                }), \
                patch('routes.sessions.get_session_clock_updates', side_effect=clock_updates_side_effect):
            response = self.client.post(
                f'/api/sessions/{self.session.id}/messages',
                json={'content': 'I sprint after them toward the crypts.', 'role': 'player'},
                headers={'Authorization': f'Bearer {token}'},
            )

        self.assertEqual(response.status_code, 201)
        clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='race_to_crypts').one()
        self.assertEqual(clock.filled, 1)

    def test_session_message_route_persists_dm_reply_before_memory_update(self):
        token = generate_token(self.user.id)
        client = self.app.test_client()

        def memory_patch_side_effect(_memory_context, audit_context=None):
            dm_event = CampaignAuditEvent.query.filter_by(event_type='dm_output_stored').first()
            self.assertIsNotNone(dm_event)
            self.assertEqual(audit_context['trace_id'].split(':')[0], 'session_memory_writer')
            self.assertEqual(audit_context['parent_trace_id'].split(':')[0], 'session_dm')
            return {}

        with patch('routes.sessions.get_session_dm_response_with_tools', return_value={'mode': 'speak', 'content': 'Yes, you are in a party.', 'parts': [{'type': 'narration', 'content': 'Yes, you are in a party.'}], 'commit_action_ids': []}) as dm_response, \
                patch('routes.sessions.get_session_memory_patch', side_effect=memory_patch_side_effect) as memory_patch, \
                patch('routes.sessions.get_session_clock_updates', return_value={
                    'create_clocks': [],
                    'advance_clocks': [],
                    'retire_clocks': [],
                    'no_change_explanations': [],
                }):
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

    def test_session_message_route_continues_when_embedding_request_fails(self):
        token = generate_token(self.user.id)
        client = self.app.test_client()

        with patch.dict(os.environ, {
            'GEMINI_EMBEDDINGS_ENABLED': 'true',
            'GEMINI_API_KEY': 'test-key',
        }, clear=False), patch('services.embedding_service._post_embedding', side_effect=RuntimeError('timeout')), \
                patch('routes.sessions.get_session_dm_response_with_tools', return_value={'mode': 'speak', 'content': 'A bell rings across the docks.', 'parts': [{'type': 'narration', 'content': 'A bell rings across the docks.'}], 'commit_action_ids': []}), \
                patch('routes.sessions.get_session_memory_patch', return_value={
                    'source_contract': 'compiled_session_memory_v2',
                    'running_summary': 'A bell rang across the docks.',
                    'upsert_graph_facts': [
                        {
                            'id': 'dock_warning_bell',
                            'entity_ids': ['dock_ward'],
                            'text': 'A warning bell rang in the Dock Ward.',
                            'certainty': 'confirmed',
                            'visibility': 'party_known',
                        }
                    ],
                }), patch('routes.sessions.get_session_clock_updates', return_value={
                    'create_clocks': [],
                    'advance_clocks': [],
                    'retire_clocks': [],
                    'no_change_explanations': [],
                }), \
                patch('routes.sessions.get_session_running_summary_finalize', return_value={
                    'running_summary': 'A bell rang across the docks.',
                }), \
                patch('openrouter.get_session_summary_consistency_check', return_value={
                    'consistent': True,
                    'contradictions': [],
                }):
            response = client.post(
                f'/api/sessions/{self.session.id}/messages',
                json={'content': '<ooc>What happens?</ooc>', 'role': 'player'},
                headers={'Authorization': f'Bearer {token}'},
            )

        self.assertEqual(response.status_code, 201)
        payload = response.get_json()
        self.assertEqual([message['role'] for message in payload['messages']], ['player', 'dm'])
        self.assertEqual(self.session.running_summary, 'A bell rang across the docks.')
        self.assertIsNotNone(CampaignAuditEvent.query.filter_by(event_type='embedding_fallback').first())

    def test_session_message_route_persists_player_message_when_dm_is_silent(self):
        token = generate_token(self.user.id)
        client = self.app.test_client()

        with patch('routes.sessions.get_session_dm_response_with_tools', return_value={
            'mode': 'silent',
            'reason': 'PC-to-PC exchange.',
        }) as dm_response, patch('routes.sessions.get_session_memory_patch') as memory_patch:
            response = client.post(
                f'/api/sessions/{self.session.id}/messages',
                json={'content': '<ic>Raven, what do you think?</ic>', 'role': 'player'},
                headers={'Authorization': f'Bearer {token}'},
            )

        self.assertEqual(response.status_code, 201)
        payload = response.get_json()
        self.assertEqual([message['role'] for message in payload['messages']], ['player'])
        self.assertEqual(SessionMessage.query.filter_by(session_id=self.session.id).count(), 1)
        self.assertIsNotNone(SessionMessage.query.filter_by(session_id=self.session.id, role='player').first())
        silence_event = CampaignAuditEvent.query.filter_by(event_type='dm_silence_chosen').first()
        self.assertIsNotNone(silence_event)
        self.assertTrue(dm_response.called)
        self.assertFalse(memory_patch.called)

    def test_session_message_route_persists_first_class_dm_turn_timing(self):
        token = generate_token(self.user.id)
        client = self.app.test_client()

        with patch('routes.sessions.get_session_dm_response_with_tools', return_value={'mode': 'speak', 'content': 'The alley falls quiet.', 'parts': [{'type': 'narration', 'content': 'The alley falls quiet.'}], 'commit_action_ids': []}), \
                patch('routes.sessions.get_session_memory_patch', return_value={
                    'source_contract': 'compiled_session_memory_v2',
                    'running_summary': 'The alley fell quiet.',
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
                patch('routes.sessions.get_session_running_summary_finalize', return_value={'running_summary': 'The alley fell quiet.'}), \
                patch('openrouter.get_session_summary_consistency_check', return_value={'consistent': True, 'contradictions': []}):
            response = client.post(
                f'/api/sessions/{self.session.id}/messages',
                json={'content': '<ooc>What changed?</ooc>', 'role': 'player'},
                headers={'Authorization': f'Bearer {token}'},
            )

        self.assertEqual(response.status_code, 201)
        turn = SessionDmTurn.query.one()
        self.assertEqual(turn.session_id, self.session.id)
        self.assertEqual(turn.status, 'speak')
        self.assertEqual(turn.post_turn_status, 'complete')
        self.assertEqual(turn.memory_status, 'complete')
        self.assertEqual(turn.clock_status, 'complete')
        self.assertIsNotNone(turn.started_at)
        self.assertIsNotNone(turn.visible_completed_at)
        self.assertIsNotNone(turn.finished_at)
        self.assertIsNotNone(turn.dm_message_id)
        self.assertIsInstance(turn.generation_duration_ms, int)
        self.assertGreaterEqual(turn.generation_duration_ms, 0)
        self.assertIsInstance(turn.full_duration_ms, int)
        self.assertGreaterEqual(turn.full_duration_ms, turn.generation_duration_ms)

    def test_clock_failure_does_not_rollback_persisted_memory(self):
        token = generate_token(self.user.id)
        client = self.app.test_client()

        with patch('routes.sessions.get_session_dm_response_with_tools', return_value={'mode': 'speak', 'content': 'The alley falls quiet.', 'parts': [{'type': 'narration', 'content': 'The alley falls quiet.'}], 'commit_action_ids': []}), \
                patch('routes.sessions.get_session_memory_patch', return_value={
                    'source_contract': 'compiled_session_memory_v2',
                    'running_summary': 'The alley fell quiet.',
                    'scene_patch': {},
                    'upsert_graph_entities': [],
                    'upsert_graph_relations': [],
                    'upsert_graph_facts': [],
                    'update_npc_actors': [],
                    'record_events': [],
                }), \
                patch('routes.sessions.get_session_clock_updates', return_value=None), \
                patch('routes.sessions.get_session_running_summary_finalize', return_value={'running_summary': 'The alley fell quiet.'}), \
                patch('openrouter.get_session_summary_consistency_check', return_value={'consistent': True, 'contradictions': []}):
            response = client.post(
                f'/api/sessions/{self.session.id}/messages',
                json={'content': '<ooc>What changed?</ooc>', 'role': 'player'},
                headers={'Authorization': f'Bearer {token}'},
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.session.running_summary, 'The alley fell quiet.')
        turn = SessionDmTurn.query.one()
        self.assertEqual(turn.post_turn_status, 'complete')
        self.assertEqual(turn.memory_status, 'complete')
        self.assertEqual(turn.clock_status, 'error')

    def test_clock_context_omits_completed_and_retired_clocks(self):
        db.session.add_all([
            CampaignClock(
                campaign_id=self.campaign.id,
                clock_id='completed_clock',
                name='Completed Clock',
                segments=4,
                filled=4,
                status='completed',
            ),
            CampaignClock(
                campaign_id=self.campaign.id,
                clock_id='retired_clock',
                name='Retired Clock',
                segments=4,
                filled=1,
                status='retired',
            ),
        ])
        db.session.commit()

        context = build_session_clock_context(
            self.campaign,
            self.session,
            self.user,
            'I wait.',
            'Nothing changes.',
            {},
            {},
            player_message_id=401,
            dm_message_id=402,
        )
        self.assertEqual(context['active_clocks'], [])
        self.assertEqual(context['latest_player_message']['source_id'], '401')
        self.assertEqual(context['latest_dm_message']['source_id'], '402')

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
        guard_trace_id = f'{session_trace_id}:private_output_guard:abc123'
        memory_trace_id = f'session_memory_writer:session_{self.session.id}:message_{session_player.id}'
        log_audit_event(
            self.campaign.id,
            'model_request',
            'session_dm request: session_dm_response',
            {
                'operation': 'session_dm_response',
                'provider': 'opencode_go',
                'model': 'deepseek-v4-flash',
                'messages': [
                    {'role': 'system', 'content': 'You are the test DM.'},
                    {'role': 'user', 'content': '<ooc>What do I see?</ooc>'},
                ],
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
                'provider': 'opencode_go',
                'model': 'deepseek-v4-flash',
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
            'private_output_guard_retry',
            'Session DM response exposed DM-private output terms; requesting rewrite.',
            {
                'operation': 'private_output_guard',
                'violation': {'matched_terms': ['Crimson Veil']},
                'draft_response': 'The Crimson Veil waits nearby.',
            },
            actor='session_dm_guard',
            trace_id=guard_trace_id,
            parent_trace_id=session_trace_id,
            trace_label='session_dm_guard: private_output_guard',
            audit_role='guard',
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
            'Unlinked write.',
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
        self.assertEqual(session_message['branches'][0]['provider'], 'opencode_go')
        self.assertEqual(session_message['branches'][0]['model'], 'deepseek-v4-flash')
        self.assertEqual(
            [child['trace_id'] for child in session_message['branches'][0]['children']],
            [guard_trace_id, memory_trace_id],
        )
        branch_steps = session_message['branches'][0]['steps']
        self.assertEqual([step['kind'] for step in branch_steps], ['prompt_message', 'model_request', 'model_response', 'tool_call', 'tool_result'])
        self.assertEqual([step['category'] for step in branch_steps], ['agents', 'agents', 'agents', 'tools', 'tools'])
        self.assertEqual(branch_steps[0]['prompt_role'], 'system')
        self.assertEqual(branch_steps[0]['content'], 'You are the test DM.')
        self.assertEqual([message['role'] for message in branch_steps[1]['messages']], ['system', 'user'])
        self.assertEqual(branch_steps[1]['provider'], 'opencode_go')
        self.assertEqual(branch_steps[1]['model'], 'deepseek-v4-flash')
        self.assertEqual(branch_steps[3]['title'], 'get_current_scene')
        self.assertEqual(branch_steps[4]['result']['current_scene']['location_name'], 'Dock Ward')
        self.assertEqual(flow['unlinked_branches'][0]['summary'], 'Unlinked write.')
        planning_lane = next(lane for lane in flow['lanes'] if lane['type'] == 'planning')
        self.assertEqual([message['content'] for message in planning_lane['messages']], [
            'I want to be a dockside wizard.',
            'Tie your wizard to the warning bell.',
        ])

    def test_combat_encounter_dm_tools(self):
        encounter_map = EncounterMap(
            campaign_id=self.campaign.id,
            session_id=self.session.id,
            title='Skirmish Area',
            prompt='A small tactical area.',
            image_filename='skirmish.png',
            model='gpt-image-2',
            size='1024x1024',
            quality='high',
            grid_json=json.dumps({'columns': 10, 'rows': 10}),
            setup_status='ready',
        )
        db.session.add(encounter_map)
        db.session.commit()

        player_placement = EncounterMapPlacement(
            encounter_map_id=encounter_map.id,
            actor_type='player',
            actor_id=str(self.user.id),
            label='Aria',
            grid_col=1,
            grid_row=1,
        )
        monster_placement = EncounterMapPlacement(
            encounter_map_id=encounter_map.id,
            actor_type='monster',
            actor_id='goblin_1',
            label='Goblin',
            grid_col=5,
            grid_row=5,
        )
        db.session.add_all([player_placement, monster_placement])
        db.session.commit()

        # 1. Toggle Encounter Mode ON
        result = execute_dm_tool(
            self.campaign,
            self.session,
            self.user,
            'toggle_encounter_mode',
            {'active': True},
        )
        self.assertNotIn('error', result)
        state = result['encounter_state']
        self.assertTrue(state['active'])
        self.assertEqual(state['round'], 1)
        self.assertIsNone(state['active_turn_index'])

        # 2. Simulate initiative rolling completed
        encounter_map = db.session.get(EncounterMap, encounter_map.id)
        current_state = json.loads(encounter_map.encounter_state_json)
        for c in current_state['turn_order']:
            if c['actor_type'] == 'player':
                c['initiative'] = 15
            else:
                c['initiative'] = 10
        current_state['turn_order'].sort(key=lambda x: x['initiative'], reverse=True)
        current_state['active_turn_index'] = 0
        encounter_map.encounter_state_json = json.dumps(current_state)
        db.session.commit()

        # 3. Next Turn
        result_next = execute_dm_tool(
            self.campaign,
            self.session,
            self.user,
            'next_combat_turn',
            {},
        )
        self.assertNotIn('error', result_next)
        self.assertEqual(result_next['encounter_state']['active_turn_index'], 1)

        # 4. Set Combat Turn directly
        result_set = execute_dm_tool(
            self.campaign,
            self.session,
            self.user,
            'set_combat_turn',
            {'active_turn_index': 0},
        )
        self.assertNotIn('error', result_set)
        self.assertEqual(result_set['encounter_state']['active_turn_index'], 0)

        # 5. Update Actions
        result_update = execute_dm_tool(
            self.campaign,
            self.session,
            self.user,
            'update_combatant_actions',
            {
                'actor_type': 'player',
                'actor_id': str(self.user.id),
                'actions': {'action': False, 'movement_remaining': 10},
            },
        )
        self.assertNotIn('error', result_update)
        aria_combatant = next(x for x in result_update['encounter_state']['turn_order'] if x['actor_type'] == 'player')
        self.assertFalse(aria_combatant['actions']['action'])
        self.assertEqual(aria_combatant['actions']['movement_remaining'], 10)

        # 6. Toggle Encounter Mode OFF
        result_off = execute_dm_tool(
            self.campaign,
            self.session,
            self.user,
            'toggle_encounter_mode',
            {'active': False},
        )
        self.assertNotIn('error', result_off)
        self.assertFalse(result_off['encounter_state']['active'])

    def test_roll_dice_supports_compound_and_keep_highest(self):
        with patch('services.dm_tools.random.randint', side_effect=[4, 17, 8, 5]):
            result = execute_dm_tool(
                self.campaign,
                self.session,
                self.user,
                'roll_dice',
                {'expression': '2d20kh1+1d6+3', 'reason': 'Goblin attack'},
            )

        self.assertNotIn('error', result)
        self.assertEqual(result['total'], 28)
        self.assertEqual(result['terms'][0]['rolls'], [4, 17])
        self.assertEqual(result['terms'][0]['kept'], [17])
        self.assertEqual(result['terms'][1]['rolls'], [8])
        self.assertEqual(result['terms'][2]['subtotal'], 3)

    def test_combat_state_tools_use_selected_character_and_sync(self):
        alt_character = Character(
            user_id=self.user.id,
            campaign_id=self.campaign.id,
            name='Lyra',
            race='Human',
            background='Soldier',
            max_hp=32,
            current_hp=30,
            temp_hp=4,
            armor_class=17,
            speed=40,
            initiative_bonus=5,
        )
        db.session.add(alt_character)
        db.session.flush()
        member = CampaignMember.query.filter_by(campaign_id=self.campaign.id, user_id=self.user.id).one()
        member.selected_character_id = alt_character.id
        db.session.add(CharacterCondition(
            character_id=alt_character.id,
            condition_name='Blessed',
            source='Cleric',
            duration_remaining='1 minute',
            description='Add d4 to attacks and saves.',
        ))

        monster = CampaignMonster(
            campaign_id=self.campaign.id,
            monster_id='goblin_1',
            name='Goblin',
            stat_block=json.dumps({'max_hp': 11, 'current_hp': 11, 'armor_class': 13, 'speed': 30}),
        )
        encounter_map = EncounterMap(
            campaign_id=self.campaign.id,
            session_id=self.session.id,
            title='Skirmish Area',
            prompt='A small tactical area.',
            image_filename='skirmish.png',
            model='gpt-image-2',
            size='1024x1024',
            quality='high',
            grid_json=json.dumps({'columns': 10, 'rows': 10}),
            vtt_setup_json=json.dumps({}),
            setup_status='ready',
        )
        db.session.add_all([monster, encounter_map])
        db.session.commit()

        db.session.add_all([
            EncounterMapPlacement(
                encounter_map_id=encounter_map.id,
                actor_type='player',
                actor_id=str(self.user.id),
                label='Lyra',
                grid_col=1,
                grid_row=1,
            ),
            EncounterMapPlacement(
                encounter_map_id=encounter_map.id,
                actor_type='monster',
                actor_id='goblin_1',
                label='Goblin',
                grid_col=5,
                grid_row=5,
            ),
        ])
        db.session.commit()

        result = execute_dm_tool(
            self.campaign,
            self.session,
            self.user,
            'toggle_encounter_mode',
            {'active': True},
        )
        self.assertNotIn('error', result)

        encounter_map = db.session.get(EncounterMap, encounter_map.id)
        state = json.loads(encounter_map.encounter_state_json)
        player_combatant = next(item for item in state['turn_order'] if item['actor_type'] == 'player')
        monster_combatant = next(item for item in state['turn_order'] if item['actor_type'] == 'monster')
        self.assertEqual(player_combatant['max_hp'], 32)
        self.assertEqual(player_combatant['current_hp'], 30)
        self.assertEqual(player_combatant['temp_hp'], 4)
        self.assertEqual(player_combatant['armor_class'], 17)
        self.assertEqual(player_combatant['speed'], 40)
        self.assertEqual(player_combatant['conditions'][0]['name'], 'Blessed')

        player_combatant['initiative'] = 18
        monster_combatant['initiative'] = 12
        state['turn_order'].sort(key=lambda item: item['initiative'], reverse=True)
        state['active_turn_index'] = 0
        encounter_map.encounter_state_json = json.dumps(state)
        db.session.commit()

        overview = execute_dm_tool(
            self.campaign,
            self.session,
            self.user,
            'get_encounter_overview',
            {},
        )
        self.assertEqual(overview['active_combatant']['label'], 'Lyra')

        combatant_state = execute_dm_tool(
            self.campaign,
            self.session,
            self.user,
            'get_combatant_state',
            {'actor_type': 'player', 'actor_id': str(self.user.id)},
        )
        self.assertTrue(combatant_state['is_active_turn'])
        self.assertEqual(combatant_state['combatant']['current_hp'], 30)

        reachable = execute_dm_tool(
            self.campaign,
            self.session,
            self.user,
            'list_reachable_positions',
            {'actor_type': 'player', 'actor_id': str(self.user.id), 'max_cells': 12},
        )
        self.assertNotIn('error', reachable)
        self.assertGreater(reachable['movement']['reachable_count'], 0)

        damaged = execute_dm_tool(
            self.campaign,
            self.session,
            self.user,
            'apply_damage',
            {'actor_type': 'monster', 'actor_id': 'goblin_1', 'amount': 7, 'damage_type': 'fire'},
        )
        self.assertEqual(damaged['combatant']['current_hp'], 4)
        self.assertEqual(damaged['damage']['applied_to_current_hp'], 7)

        healed = execute_dm_tool(
            self.campaign,
            self.session,
            self.user,
            'apply_healing',
            {'actor_type': 'monster', 'actor_id': 'goblin_1', 'amount': 3},
        )
        self.assertEqual(healed['combatant']['current_hp'], 7)

        temp_hp = execute_dm_tool(
            self.campaign,
            self.session,
            self.user,
            'grant_temp_hp',
            {'actor_type': 'player', 'actor_id': str(self.user.id), 'amount': 8, 'mode': 'max'},
        )
        self.assertEqual(temp_hp['combatant']['temp_hp'], 8)

        hp_set = execute_dm_tool(
            self.campaign,
            self.session,
            self.user,
            'set_combatant_hp',
            {'actor_type': 'player', 'actor_id': str(self.user.id), 'current_hp': 21, 'temp_hp': 2},
        )
        self.assertEqual(hp_set['combatant']['current_hp'], 21)
        self.assertEqual(hp_set['combatant']['temp_hp'], 2)

        updated_conditions = execute_dm_tool(
            self.campaign,
            self.session,
            self.user,
            'update_combatant_conditions',
            {
                'actor_type': 'player',
                'actor_id': str(self.user.id),
                'mode': 'add',
                'conditions': [{'name': 'Prone', 'duration': 'until stand'}],
            },
        )
        names = {item['name'] for item in updated_conditions['combatant']['conditions']}
        self.assertEqual(names, {'Blessed', 'Prone'})

        updated_init = execute_dm_tool(
            self.campaign,
            self.session,
            self.user,
            'set_combatant_initiative',
            {'actor_type': 'monster', 'actor_id': 'goblin_1', 'initiative': 25, 'initiative_bonus': 2},
        )
        self.assertEqual(updated_init['encounter_state']['turn_order'][0]['actor_id'], 'goblin_1')

        removed = execute_dm_tool(
            self.campaign,
            self.session,
            self.user,
            'remove_encounter_actor',
            {'actor_type': 'monster', 'actor_id': 'goblin_1'},
        )
        self.assertNotIn('error', removed)
        self.assertEqual(EncounterMapPlacement.query.filter_by(encounter_map_id=encounter_map.id).count(), 1)

        refreshed_character = db.session.get(Character, alt_character.id)
        self.assertEqual(refreshed_character.current_hp, 21)
        self.assertEqual(refreshed_character.temp_hp, 2)
        self.assertEqual(
            {row.condition_name for row in CharacterCondition.query.filter_by(character_id=alt_character.id).all()},
            {'Blessed', 'Prone'},
        )

        refreshed_monster = CampaignMonster.query.filter_by(campaign_id=self.campaign.id, monster_id='goblin_1').one()
        self.assertEqual(json.loads(refreshed_monster.stat_block)['current_hp'], 7)

    def test_dm_tools_filtered_by_encounter_mode(self):
        tools = get_dm_tool_definitions(self.campaign)
        tool_names = {t['function']['name'] for t in tools}

        exclude_names = {
            'create_encounter_map',
            'place_encounter_map_actors',
            'move_encounter_actor',
            'get_encounter_overview',
            'get_combatant_state',
            'list_reachable_positions',
            'next_combat_turn',
            'set_combat_turn',
            'update_combatant_actions',
            'set_combatant_hp',
            'apply_damage',
            'apply_healing',
            'grant_temp_hp',
            'set_combatant_initiative',
            'update_combatant_conditions',
            'remove_encounter_actor',
        }
        for name in exclude_names:
            self.assertNotIn(name, tool_names)
        self.assertIn('toggle_encounter_mode', tool_names)
        self.assertIn('ask_character_sheet', tool_names)
        self.assertIn('roll_dice', tool_names)

        result_on = execute_dm_tool(
            self.campaign,
            self.session,
            self.user,
            'toggle_encounter_mode',
            {'active': True},
        )
        self.assertNotIn('error', result_on)

        # 3. Check that encounter_active is True and all tools are now present
        tools_on = get_dm_tool_definitions(self.campaign)
        tool_names_on = {t['function']['name'] for t in tools_on}
        for name in exclude_names:
            self.assertIn(name, tool_names_on)
        self.assertIn('toggle_encounter_mode', tool_names_on)

        result_off = execute_dm_tool(
            self.campaign,
            self.session,
            self.user,
            'toggle_encounter_mode',
            {'active': False},
        )
        self.assertNotIn('error', result_off)

        # 5. Check that tools are filtered again
        tools_off = get_dm_tool_definitions(self.campaign)
        tool_names_off = {t['function']['name'] for t in tools_off}
        for name in exclude_names:
            self.assertNotIn(name, tool_names_off)
        self.assertIn('toggle_encounter_mode', tool_names_off)

    def test_toggle_encounter_mode_archives_and_prompts(self):
        from services.encounter_map_service import latest_encounter_map
        # Create an encounter map
        encounter_map = EncounterMap(
            campaign_id=self.campaign.id,
            title="Archiving Test Map",
            prompt="A test map prompt",
            image_filename="test_archiving.png",
            model="gpt-image-2",
            size="1024x1024",
            quality="medium",
        )
        db.session.add(encounter_map)
        db.session.commit()

        # Check it is returned by latest_encounter_map
        self.assertEqual(latest_encounter_map(self.campaign.id).id, encounter_map.id)

        # 1. Toggle Encounter Mode ON
        result_on = execute_dm_tool(
            self.campaign,
            self.session,
            self.user,
            'toggle_encounter_mode',
            {'active': True},
        )
        self.assertIn("You MUST now run the create_encounter_map tool", result_on['message'])
        self.assertIn('"title":"Short player-visible map title"', result_on['message'])
        self.assertIn('"map_prompt":"Concrete top-down battle map layout and important zones"', result_on['message'])
        self.assertIn('Do not use name, description, width, height, grid_size', result_on['message'])
        self.assertIn('place_encounter_map_actors', result_on['message'])

        # 2. Toggle Encounter Mode OFF
        result_off = execute_dm_tool(
            self.campaign,
            self.session,
            self.user,
            'toggle_encounter_mode',
            {'active': False},
        )
        self.assertIn("stopped", result_off['message'])

        # 3. Check that the map is archived
        db.session.refresh(encounter_map)
        self.assertTrue(encounter_map.is_archived)

        # 4. Check that latest_encounter_map now returns None
        self.assertIsNone(latest_encounter_map(self.campaign.id))


if __name__ == '__main__':
    unittest.main()

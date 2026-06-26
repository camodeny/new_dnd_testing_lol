import os
import base64
import json
import sys
import tempfile
import unittest
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
    CampaignMemoryEmbedding,
    CampaignMember,
    CampaignMonster,
    CampaignSession,
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
    _possible_missing_npc_tag_signal,
    _pc_control_violation,
    _private_output_violation,
    _session_dm_format_violation,
    get_session_dm_response_with_tools,
    normalize_session_dm_turn_decision,
)
from routes.dev import _agent_runs_from_stream, _audit_stream_entry, _chat_flow_payload
from routes.sessions import sessions_bp
from services.audit_service import log_audit_event
from services.dm_tools import (
    DM_TOOL_DEFINITIONS,
    get_dm_tool_definitions,
    apply_memory_patch,
    build_session_hot_context,
    context_manifest,
    execute_dm_tool,
)
from services.embedding_service import canonical_text_for_item, cosine_similarity
from services.encounter_map_service import create_labeled_grid_image, detect_grid_from_image


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


def dm_talk_tool_response(content):
    return {
        'choices': [{
            'message': {
                'content': '',
                'tool_calls': [{
                    'id': 'call_final',
                    'function': {
                        'name': 'talk_to_player',
                        'arguments': json.dumps({'content': content}),
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
            knowledge_graph='{"entities":[{"id":"fac_crimson_veil","type":"faction","name":"Crimson Veil","visibility":"dm_private"}],"relations":[],"facts":[]}',
            world_state='{"current_scene":{"location_name":"Dock Ward","immediate_tension":"A bell rings."}}',
            dm_private='{"hidden_factions":["Crimson Veil"]}',
        ))
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()
        self.env_patch.stop()

    def test_tool_definitions_are_function_schemas(self):
        names = {tool['function']['name'] for tool in DM_TOOL_DEFINITIONS}
        self.assertIn('ask_character_sheet', names)
        self.assertIn('search_campaign_memory', names)
        self.assertIn('advance_clock', names)
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
        hot_context = build_session_hot_context(self.campaign, self.session, self.user)
        manifest = context_manifest(hot_context, DM_TOOL_DEFINITIONS)
        self.assertEqual(manifest['strategy'], 'compact_hot_context_with_dm_tools')
        self.assertFalse(manifest['full_world_graph_included'])
        self.assertIn('ask_character_sheet', manifest['available_tools'])
        self.assertIn('create_encounter_map', manifest['available_tools'])
        self.assertIn('recent_messages', manifest['estimated_tokens_by_section'])

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

        def dm_response(_hot_context, _recent_messages, _tools, execute_tool, audit_context=None):
            execute_tool(
                'create_encounter_map',
                {
                    'title': 'Warehouse Fight',
                    'map_prompt': 'A warehouse with stacked crates and loading doors.',
                },
                audit_context or {},
            )
            return {'mode': 'speak', 'content': 'A gridded map appears on the table.'}

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

    def test_session_dm_pc_control_classifier_rewrites_invented_choice(self):
        hot_context = {
            'current_player_character': {'id': 11, 'name': 'Elara Moonwhisper', 'user_id': 8},
            'protected_player_characters': [
                {'id': 11, 'name': 'Elara Moonwhisper', 'user_id': 8},
            ],
            'private_output_terms': [],
            'private_spoiler_items': [],
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

        with patch('openrouter.get_session_preflight_decision', return_value={
            'dm_reply_mode': 'narrative',
            'skip_spoiler_check': False,
            'main_call_thinking': True,
            'confidence': 'high',
            'reason': 'The player is in an immediate hazard.',
        }), patch('openrouter._post_chat_response', side_effect=[
            dm_talk_tool_response('You decide to retreat up the ladder and try a safer approach.'),
            dm_talk_tool_response('The rung shudders beneath you. The landing remains within reach, but the ladder is failing fast. What do you do?'),
        ]) as post_chat, patch('openrouter._post_chat', side_effect=[
            json.dumps({
                'safe': False,
                'violations': [{
                    'character': 'Elara Moonwhisper',
                    'sentence': 'You decide to retreat up the ladder and try a safer approach.',
                    'kind': 'choice_or_intent',
                    'reason': 'The DM invented a strategic choice for the protected PC.',
                }],
                'confidence': 'high',
                'reason': 'The reply assigns a new decision to the acting PC.',
            }),
            json.dumps({
                'safe': True,
                'violations': [],
                'confidence': 'high',
                'reason': 'The revised reply describes only the hazard and asks the player to choose.',
            }),
        ]):
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
            'content': 'The rung shudders beneath you. The landing remains within reach, but the ladder is failing fast. What do you do?',
        })
        retry_prompt = post_chat.call_args_list[1].args[0][-1]['content']
        self.assertIn('do not control a protected player character', retry_prompt)

    def test_private_output_guard_detects_hidden_terms(self):
        hot_context = {'private_output_terms': ['Crimson Veil']}

        self.assertEqual(
            _private_output_violation('The Crimson Veil moves another step ahead.', hot_context),
            {'matched_terms': ['Crimson Veil']},
        )
        self.assertIsNone(
            _private_output_violation('A hidden scheme moves another step ahead.', hot_context),
        )

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

    def test_possible_missing_npc_tag_signal_detects_attributed_quote(self):
        signal = _possible_missing_npc_tag_signal(
            'Dee watches you for a long moment, reading your resolve. He does not argue. '
            'Instead, he gives a single, slow nod. **"Alright. Lock the creds down first."**'
        )
        self.assertEqual(signal['speaker'], 'Dee')
        self.assertIn('Alright.', signal['quote'])

    def test_possible_missing_npc_tag_signal_detects_sentence_lead_speaker(self):
        signal = _possible_missing_npc_tag_signal(
            'Sheriff Coldharbour spins toward Brixby, her eyes narrowing. '
            '"You there-pointing fingers will not help."'
        )
        self.assertEqual(signal['speaker'], 'Sheriff Coldharbour')
        self.assertIn('pointing fingers', signal['quote'])

    def test_possible_missing_npc_tag_signal_detects_markdown_speaker_label(self):
        signal = _possible_missing_npc_tag_signal(
            '**Seraphina:** "Keep your hood up and your eyes open."'
        )
        self.assertEqual(signal['speaker'], 'Seraphina')
        self.assertIn('Keep your hood up', signal['quote'])

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
        }), patch('openrouter._post_chat_response', side_effect=[
            dm_talk_tool_response('The blow catches you across the ribs and knocks you down.'),
            dm_talk_tool_response('The constable snaps her truncheon up as you rush in. Roll initiative.'),
        ]) as post_chat, patch('openrouter._post_chat', side_effect=[
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
        })
        retry_prompt = post_chat.call_args_list[1].args[0][-1]['content']
        self.assertIn('required D&D mechanics', retry_prompt)
        self.assertIn('do not resolve uncertain combat outcomes', retry_prompt)
        self.assertNotIn('Required mechanic:', retry_prompt)

    def test_session_dm_format_guard_rewrites_malformed_reply(self):
        hot_context = {
            'protected_player_characters': [],
            'private_output_terms': [],
            'private_spoiler_items': [],
        }

        with patch('openrouter._post_chat_response', side_effect=[
            dm_talk_tool_response('<npc target="Bram Truewood">"The candle is always lit."</p>'),
            dm_talk_tool_response('<npc target="Bram Truewood">"The candle is always lit."</npc>'),
        ]) as post_chat:
            result = get_session_dm_response_with_tools(
                hot_context,
                [],
                [],
                lambda *_args, **_kwargs: {},
                max_tool_rounds=0,
            )

        self.assertEqual(result, {
            'mode': 'speak',
            'content': '<npc target="Bram Truewood">"The candle is always lit."</npc>',
        })
        retry_prompt = post_chat.call_args_list[1].args[0][-1]['content']
        self.assertIn('Guard reminder', retry_prompt)
        self.assertIn('only allowed angle-bracket tag', retry_prompt)
        self.assertNotIn('</p>', retry_prompt)
        self.assertFalse(post_chat.call_args_list[1].kwargs.get('json_mode'))
        self.assertIsNotNone(post_chat.call_args_list[1].kwargs.get('tools'))
        self.assertFalse(post_chat.call_args_list[1].kwargs.get('allow_thinking'))

    def test_session_dm_format_guard_rewrites_missing_npc_tag_reply_with_special_prompt(self):
        hot_context = {
            'protected_player_characters': [],
            'private_output_terms': [],
            'private_spoiler_items': [],
        }

        with patch('openrouter._post_chat_response', side_effect=[
            dm_talk_tool_response(
                'Dee watches you for a long moment, reading your resolve. He does not argue. '
                'Instead, he gives a single, slow nod. **"Alright. Lock the creds down first."**'
            ),
            dm_talk_tool_response(
                'Dee watches you for a long moment, reading your resolve. He does not argue. '
                'Instead, he gives a single, slow nod.\n\n'
                '<npc target="Dee">"Alright. Lock the creds down first."</npc>'
            ),
        ]) as post_chat, patch('openrouter._post_chat', side_effect=[
            json.dumps({
                'requires_npc_tag': True,
                'speaker': 'Dee',
                'evidence': ['**"Alright. Lock the creds down first."**'],
                'reason': 'This is clearly Dee speaking in the current scene.',
            }),
            json.dumps({
                'requires_npc_tag': False,
                'speaker': '',
                'evidence': [],
                'reason': 'The reply already uses the required NPC tag.',
            }),
        ]):
            result = get_session_dm_response_with_tools(
                hot_context,
                [],
                [],
                lambda *_args, **_kwargs: {},
                max_tool_rounds=0,
            )

        self.assertEqual(result, {
            'mode': 'speak',
            'content': 'Dee watches you for a long moment, reading your resolve. He does not argue. '
                       'Instead, he gives a single, slow nod.\n\n'
                       '<npc target="Dee">"Alright. Lock the creds down first."</npc>',
        })
        retry_prompt = post_chat.call_args_list[1].args[0][-1]['content']
        self.assertIn('clearly attributed NPC speech', retry_prompt)
        self.assertIn('use the required <npc> wrapper', retry_prompt)
        self.assertNotIn('Rewrite the same reply', retry_prompt)
        self.assertNotIn('Preserve the narration', retry_prompt)

    def test_session_dm_format_guard_runs_npc_checker_without_heuristic_signal(self):
        hot_context = {
            'protected_player_characters': [],
            'private_output_terms': [],
            'private_spoiler_items': [],
        }

        with patch('openrouter._post_chat_response', side_effect=[
            dm_talk_tool_response(
                'Sheriff Coldharbour spins toward Brixby, her eyes narrowing. '
                '"You there-pointing fingers will not help."'
            ),
            dm_talk_tool_response(
                'Sheriff Coldharbour spins toward Brixby, her eyes narrowing.\n\n'
                '<npc target="Sheriff Coldharbour">"You there-pointing fingers will not help."</npc>'
            ),
        ]) as post_chat_response, patch('openrouter._post_chat', side_effect=[
            json.dumps({
                'requires_npc_tag': True,
                'speaker': 'Sheriff Coldharbour',
                'evidence': ['"You there-pointing fingers will not help."'],
                'reason': 'This is clearly Sheriff Coldharbour speaking in the current scene.',
            }),
            json.dumps({
                'requires_npc_tag': False,
                'speaker': '',
                'evidence': [],
                'reason': 'The reply already uses the required NPC tag.',
            }),
        ]) as post_chat, patch('openrouter._possible_missing_npc_tag_signal', return_value=None):
            result = get_session_dm_response_with_tools(
                hot_context,
                [],
                [],
                lambda *_args, **_kwargs: {},
                max_tool_rounds=0,
            )

        self.assertEqual(result, {
            'mode': 'speak',
            'content': 'Sheriff Coldharbour spins toward Brixby, her eyes narrowing.\n\n'
                       '<npc target="Sheriff Coldharbour">"You there-pointing fingers will not help."</npc>',
        })
        self.assertEqual(post_chat.call_count, 1)
        self.assertEqual(post_chat_response.call_count, 2)

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

    def test_session_dm_format_retry_repairs_non_json_meta_response(self):
        hot_context = {
            'protected_player_characters': [],
            'private_output_terms': [],
            'private_spoiler_items': [],
        }

        with patch('openrouter._post_chat_response', side_effect=[
            dm_talk_tool_response('<ic>"Green lights."</ic>'),
            {'choices': [{'message': {'content': 'Understood. No `<ic>` tags.\n\n<npc target="Brenn">"Green lights by the old willow."</npc>'}}]},
            dm_talk_tool_response('<npc target="Brenn">"Green lights by the old willow."</npc>'),
        ]) as post_chat:
            result = get_session_dm_response_with_tools(
                hot_context,
                [],
                [],
                lambda *_args, **_kwargs: {},
                max_tool_rounds=0,
            )

        self.assertEqual(result, {
            'mode': 'speak',
            'content': '<npc target="Brenn">"Green lights by the old willow."</npc>',
        })
        self.assertEqual(post_chat.call_count, 3)
        contract_retry_prompt = post_chat.call_args_list[2].args[0][-1]['content']
        self.assertIn('calling exactly one of talk_to_player or stay_silent', contract_retry_prompt)
        self.assertIn('Do not send the final visible reply as plain assistant text', contract_retry_prompt)

    def test_finalizer_contract_retry_discards_candidate_and_reruns_with_system_reminder(self):
        hot_context = {
            'protected_player_characters': [],
            'private_output_terms': [],
            'private_spoiler_items': [],
        }
        draft = (
            'The engine panel lights flicker as you key in the hot-wire bypass. '
            'The startup sequence now reads **70 seconds**.'
        )

        with patch('openrouter._post_chat_response', side_effect=[
            {'choices': [{'message': {'content': draft}}]},
            dm_silent_tool_response('Awaiting player response.'),
        ]) as post_chat:
            result = get_session_dm_response_with_tools(
                hot_context,
                [],
                [],
                lambda *_args, **_kwargs: {},
                max_tool_rounds=0,
            )

        self.assertEqual(result, {'mode': 'silent', 'content': '', 'reason': 'Awaiting player response.'})
        contract_retry_prompt = post_chat.call_args_list[1].args[0][-1]['content']
        retry_kwargs = post_chat.call_args_list[1].kwargs
        self.assertIn('calling exactly one of talk_to_player or stay_silent', contract_retry_prompt)
        self.assertIn('Do not send the final visible reply as plain assistant text', contract_retry_prompt)
        self.assertEqual(retry_kwargs['tool_choice'], 'required')
        self.assertEqual(
            {tool['function']['name'] for tool in retry_kwargs['tools']},
            {'talk_to_player', 'stay_silent'},
        )

    def test_finalizer_contract_retry_uses_fresh_rerun_output(self):
        hot_context = {
            'protected_player_characters': [],
            'private_output_terms': [],
            'private_spoiler_items': [],
        }
        draft = 'You watch the neon rain streak down the window while Dee studies the shard in silence.'
        stale = 'Dee leans back, the vinyl creaking under him.'

        with patch('openrouter._post_chat_response', side_effect=[
            {'choices': [{'message': {'content': draft}}]},
            dm_talk_tool_response(stale),
        ]) as post_chat:
            result = get_session_dm_response_with_tools(
                hot_context,
                [],
                [],
                lambda *_args, **_kwargs: {},
                max_tool_rounds=0,
            )

        self.assertEqual(result, {'mode': 'speak', 'content': stale})
        self.assertEqual(post_chat.call_count, 2)

    def test_finalizer_contract_retry_switches_to_required_finalizer_only_tools(self):
        hot_context = {
            'protected_player_characters': [],
            'private_output_terms': [],
            'private_spoiler_items': [],
        }

        with patch('openrouter._post_chat_response', side_effect=[
            {'choices': [{'message': {'content': 'Plain text draft only.'}}]},
            dm_silent_tool_response('No visible reply needed.'),
        ]) as post_chat:
            result = get_session_dm_response_with_tools(
                hot_context,
                [],
                [{'type': 'function', 'function': {'name': 'search_campaign_memory'}}],
                lambda *_args, **_kwargs: {},
                max_tool_rounds=2,
            )

        self.assertEqual(result, {'mode': 'silent', 'content': '', 'reason': 'No visible reply needed.'})
        retry_kwargs = post_chat.call_args_list[1].kwargs
        self.assertEqual(retry_kwargs['tool_choice'], 'required')
        self.assertEqual(
            {tool['function']['name'] for tool in retry_kwargs['tools']},
            {'talk_to_player', 'stay_silent'},
        )

    def test_finalizer_contract_retry_still_rewrites_ooc_label(self):
        hot_context = {
            'protected_player_characters': [],
            'private_output_terms': [],
            'private_spoiler_items': [],
        }
        draft = '*OOC*: Make a Technology check.'

        with patch('openrouter._post_chat_response', side_effect=[
            {'choices': [{'message': {'content': draft}}]},
            dm_talk_tool_response('*OOC*: Make a Technology check.'),
            dm_talk_tool_response('Make a Technology check.'),
        ]) as post_chat:
            result = get_session_dm_response_with_tools(
                hot_context,
                [],
                [],
                lambda *_args, **_kwargs: {},
                max_tool_rounds=0,
            )

        self.assertEqual(result, {'mode': 'speak', 'content': 'Make a Technology check.'})
        self.assertEqual(post_chat.call_count, 3)
        format_retry_prompt = post_chat.call_args_list[2].args[0][-1]['content']
        self.assertIn('use valid visible-message syntax', format_retry_prompt)
        self.assertNotIn('OOC/IC mode labels', format_retry_prompt)

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

        with patch('openrouter._post_chat_response', side_effect=[
            {'choices': [{'message': {'content': dsml}}]},
            dm_talk_tool_response("The Broker's instructions are clear: keep the crate sealed and deliver it intact."),
        ]) as post_chat:
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
        self.assertIn('player-facing visible result inside talk_to_player(content)', retry_prompt)
        self.assertIn('Do not output DSML', retry_prompt)

    def test_session_dm_accepts_talk_to_player_finalizer_tool(self):
        hot_context = {
            'protected_player_characters': [],
            'private_output_terms': [],
            'private_spoiler_items': [],
        }
        execute_tool = Mock(return_value={})

        with patch('openrouter._post_chat_response', return_value={
            'choices': [{
                'message': {
                    'content': '',
                    'tool_calls': [{
                        'id': 'call_final',
                        'function': {
                            'name': 'talk_to_player',
                            'arguments': json.dumps({
                                'content': '<npc target="Brenn">"Green lights by the old willow."</npc>',
                            }),
                        },
                    }],
                },
            }],
        }) as post_chat:
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

    def test_session_dm_accepts_stay_silent_finalizer_tool(self):
        hot_context = {
            'protected_player_characters': [],
            'private_output_terms': [],
            'private_spoiler_items': [],
        }

        with patch('openrouter._post_chat_response', return_value={
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
        }):
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

    def test_spoiler_checker_allows_safe_reply(self):
        hot_context = {
            'protected_player_characters': [],
            'private_output_terms': [],
            'private_spoiler_items': [{'id': 'fact_trap', 'kind': 'fact', 'text': 'The note is a trap.'}],
        }

        with patch('openrouter._post_chat_response', return_value={
            'choices': [{'message': dm_talk_tool_response('Jara watches the door.')['choices'][0]['message']}],
        }), patch('openrouter.check_session_spoilers_with_llm', return_value={
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
        }), patch('openrouter._post_chat_response', return_value={
            'choices': [{'message': dm_talk_tool_response('Rain slicks the old stones.')['choices'][0]['message']}],
        }) as post_chat:
            result = get_session_dm_response_with_tools(
                hot_context,
                [],
                [],
                lambda *_args, **_kwargs: {},
                audit_context={'operation': 'session_dm_response'},
                max_tool_rounds=0,
            )

        self.assertEqual(result, {'mode': 'speak', 'content': 'Rain slicks the old stones.'})
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
        }), patch('openrouter._post_chat_response', side_effect=[
            {'choices': [{'message': {
                'content': '',
                'tool_calls': [{
                    'id': 'call_sheet',
                    'function': {
                        'name': 'ask_character_sheet',
                        'arguments': '{"question":"What is my AC?"}',
                    },
                }],
            }}]},
            dm_talk_tool_response('Your AC is 15.'),
        ]) as post_chat:
            result = get_session_dm_response_with_tools(
                hot_context,
                [],
                [{'type': 'function', 'function': {'name': 'ask_character_sheet'}}],
                lambda *_args, **_kwargs: tool_result,
                audit_context={'operation': 'session_dm_response'},
                max_tool_rounds=1,
            )

        self.assertEqual(result, {'mode': 'speak', 'content': 'Your AC is 15.'})
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

        with patch('openrouter._post_chat_response', side_effect=[
            {'choices': [{'message': {'content': '', 'tool_calls': [
                {'id': 'call_1', 'function': {'name': 'update_combatant_actions', 'arguments': '{"placement_id":7,"actions":{"action":false,"bonus_action":false,"movement_remaining":10}}'}},
                {'id': 'call_2', 'function': {'name': 'next_combat_turn', 'arguments': '{}'}},
            ]}}]},
            dm_talk_tool_response('The skirmisher withdraws along the ledge.'),
            {'choices': [{'message': {'content': '', 'tool_calls': [
                {'id': 'call_3', 'function': {'name': 'update_combatant_actions', 'arguments': '{"placement_id":9,"actions":{"action":false,"bonus_action":false,"movement_remaining":0}}'}},
                {'id': 'call_4', 'function': {'name': 'next_combat_turn', 'arguments': '{}'}},
            ]}}]},
            dm_talk_tool_response('The skirmisher falls back and the brute stomps into position. Seraphina, you are up.'),
        ]) as post_chat:
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

        with patch('openrouter._post_chat_response', side_effect=[
            {'choices': [{'message': {'content': '', 'tool_calls': [
                {'id': 'call_1', 'function': {'name': 'update_combatant_actions', 'arguments': '{"placement_id":7,"actions":{"action":false,"bonus_action":false,"movement_remaining":0}}'}},
                {'id': 'call_2', 'function': {'name': 'next_combat_turn', 'arguments': '{}'}},
            ]}}]},
            {'choices': [{'message': {'content': 'The skirmisher falls back.'}}]},
            {'choices': [{'message': {'content': 'The skirmisher falls back.'}}]},
            {'choices': [{'message': {'content': '', 'tool_calls': [
                {'id': 'call_3', 'function': {'name': 'update_combatant_actions', 'arguments': '{"placement_id":9,"actions":{"action":false,"bonus_action":false,"movement_remaining":0}}'}},
                {'id': 'call_4', 'function': {'name': 'next_combat_turn', 'arguments': '{}'}},
            ]}}]},
            dm_talk_tool_response('The skirmisher fades back and the brute gives ground. Seraphina, you are up.'),
        ]):
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

        with patch('openrouter._post_chat_response', side_effect=[
            {'choices': [{'message': {'content': '', 'tool_calls': [{'id': 'call_1', 'function': {'name': 'search_campaign_memory', 'arguments': '{"query":"training skirmisher"}'}}]}}]},
            dm_talk_tool_response('The skirmisher gauges the field from the high ledge.'),
        ]):
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

        with patch('openrouter._post_chat_response', side_effect=[
            {'choices': [{'message': {'content': '', 'tool_calls': [
                {'id': 'call_1', 'function': {'name': 'update_combatant_actions', 'arguments': '{"placement_id":7,"actions":{"action":false,"bonus_action":false,"movement_remaining":0}}'}},
                {'id': 'call_2', 'function': {'name': 'next_combat_turn', 'arguments': '{}'}},
            ]}}]},
            {'choices': [{'message': {'content': '', 'tool_calls': [{'id': 'call_3', 'function': {'name': 'set_combat_turn', 'arguments': '{"active_turn_index":0}'}}]}}]},
            dm_talk_tool_response('The skirmisher scuttles back into cover. Seraphina, your turn.'),
        ]):
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

        with patch('openrouter._post_chat_response', side_effect=[
            {'choices': [{'message': {'content': '', 'tool_calls': [
                {'id': 'call_1', 'function': {'name': 'update_combatant_actions', 'arguments': '{"placement_id":7,"actions":{"action":false,"bonus_action":false,"movement_remaining":0}}'}},
                {'id': 'call_2', 'function': {'name': 'next_combat_turn', 'arguments': '{}'}},
            ]}}]},
            dm_talk_tool_response('The skirmisher ducks behind the gear housing. Now let me advance to Seraphina.'),
            dm_talk_tool_response('The skirmisher ducks behind the gear housing. Seraphina, you are up.'),
        ]) as post_chat:
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

        with patch('openrouter._post_chat_response', side_effect=[
            {'choices': [{'message': {'content': '', 'tool_calls': [{'id': 'call_1', 'function': {'name': 'next_combat_turn', 'arguments': '{}'}}]}}]},
            {'choices': [{'message': {'content': ''}}]},
            {'choices': [{'message': {'content': ''}}]},
            {'choices': [{'message': {'content': ''}}]},
        ]):
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

    def test_spoiler_checker_rewrites_semantic_leak(self):
        hot_context = {
            'protected_player_characters': [],
            'private_output_terms': [],
            'private_spoiler_items': [{'id': 'fact_trap', 'kind': 'fact', 'text': 'The note is a trap.'}],
        }

        with patch('openrouter._post_chat_response', side_effect=[
            dm_talk_tool_response('The trap closes around you.'),
            dm_talk_tool_response('The air feels tense as you leave.'),
        ]) as post_chat, patch('openrouter.check_session_spoilers_with_llm', side_effect=[
            {'safe': False, 'leaked_item_ids': ['fact_trap'], 'evidence': ['The trap closes'], 'reason': 'Directly implies the hidden truth.'},
            {'safe': True, 'leaked_item_ids': [], 'evidence': [], 'reason': ''},
        ]):
            result = get_session_dm_response_with_tools(hot_context, [], [], lambda *_args, **_kwargs: {}, max_tool_rounds=0)

        self.assertEqual(result, {'mode': 'speak', 'content': 'The air feels tense as you leave.'})
        retry_prompt = post_chat.call_args_list[1].args[0][-1]['content']
        self.assertIn('keep the visible reply spoiler-safe', retry_prompt)
        self.assertIn('keep the visible reply spoiler-safe', retry_prompt)
        self.assertNotIn('The trap closes', retry_prompt)

    def test_private_output_guard_retry_uses_child_trace(self):
        hot_context = {
            'protected_player_characters': [],
            'private_output_terms': ['Crimson Veil'],
            'private_spoiler_items': [],
        }
        trace_id = 'session_dm:session_2:message_15'

        with patch('openrouter._post_chat_response', side_effect=[
            dm_talk_tool_response('The Crimson Veil watches you.'),
            dm_talk_tool_response('Someone watches from the dark.'),
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
        retry_event = CampaignAuditEvent.query.filter_by(event_type='private_output_guard_retry').one()
        self.assertEqual(retry_event.actor, 'session_dm_guard')
        self.assertEqual(retry_event.parent_trace_id, trace_id)
        self.assertNotEqual(retry_event.trace_id, trace_id)
        self.assertIn(':private_output_guard:', retry_event.trace_id)

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

        with patch('openrouter._post_chat_response', side_effect=[
            dm_talk_tool_response('Your Fiendish Patron stirs.'),
            {'choices': [{'message': {
                'content': '',
                'tool_calls': [{
                    'id': 'call_retry_search',
                    'function': {
                        'name': 'search_campaign_memory',
                        'arguments': '{"query":"burned symbol infernal"}',
                    },
                }],
            }}]},
            dm_talk_tool_response('The symbol appears infernal, but you do not know who left it.'),
        ]):
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

        with patch('openrouter._post_chat_response', side_effect=[
            dm_talk_tool_response('The trap closes around you.'),
            dm_talk_tool_response('A hidden trap closes around you.'),
        ]), patch('openrouter.check_session_spoilers_with_llm', side_effect=[
            {'safe': False, 'leaked_item_ids': ['fact_trap'], 'evidence': ['The trap closes'], 'reason': 'Directly implies the hidden truth.'},
            {'safe': False, 'leaked_item_ids': ['fact_trap'], 'evidence': ['hidden trap'], 'reason': 'Still implies the hidden truth.'},
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

    def test_memory_patch_creates_clock_and_graph_fact(self):
        result = apply_memory_patch(
            self.campaign,
            self.session,
            {
                'running_summary': 'The party heard a warning bell at the docks.',
                'upsert_graph_facts': [
                    {
                        'id': 'dock_warning_bell',
                        'entity_ids': ['dock_ward'],
                        'text': 'A warning bell rang in the Dock Ward.',
                        'certainty': 'confirmed',
                        'visibility': 'party_known',
                    }
                ],
                'create_clocks': [
                    {
                        'id': 'dock_alarm_spreads',
                        'name': 'Dock Alarm Spreads',
                        'segments': 4,
                        'filled': 1,
                        'summary': 'The alarm draws more attention.',
                        'trigger': 'The party delays or makes noise.',
                        'on_complete': 'Guards lock down the docks.',
                    }
                ],
            },
            {},
        )
        clock = CampaignClock.query.filter_by(campaign_id=self.campaign.id, clock_id='dock_alarm_spreads').first()
        self.assertIsNotNone(clock)
        self.assertEqual(clock.filled, 1)
        self.assertTrue(result['running_summary_updated'])
        self.assertEqual(self.session.running_summary, 'The party heard a warning bell at the docks.')

    def test_memory_patch_logs_runs_and_changes(self):
        from models import CampaignMemoryRun, CampaignMemoryLog
        CampaignMemoryRun.query.delete()
        CampaignMemoryLog.query.delete()
        db.session.commit()

        result = apply_memory_patch(
            self.campaign,
            self.session,
            {
                '_telemetry': {
                    'prompt_chars': 1000,
                    'prompt_tokens_estimate': 250,
                    'response_chars': 500,
                    'context_breakdown': {'entities': 400}
                },
                'running_summary': 'The party heard a warning bell at the docks.',
                'upsert_graph_facts': [
                    {
                        'id': 'dock_warning_bell',
                        'entity_ids': ['dock_ward'],
                        'text': 'A warning bell rang in the Dock Ward.',
                        'certainty': 'confirmed',
                        'visibility': 'party_known',
                        'importance': 4,
                        'reason': 'A bell rang to warn of guards.',
                        'memory_type': 'fact'
                    }
                ]
            },
            {
                'trace_id': 'test_trace_123',
                'player_message_id': 42,
                'dm_message_id': 43
            },
        )

        run = CampaignMemoryRun.query.one()
        self.assertEqual(run.campaign_id, self.campaign.id)
        self.assertEqual(run.session_id, self.session.id)
        self.assertEqual(run.source_player_message_id, 42)
        self.assertEqual(run.source_dm_message_id, 43)
        self.assertEqual(run.prompt_chars, 1000)
        self.assertEqual(run.prompt_tokens_estimate, 250)
        self.assertEqual(run.response_chars, 500)

        logs = CampaignMemoryLog.query.all()
        self.assertEqual(len(logs), 2)
        
        fact_log = next(l for l in logs if l.memory_id == 'dock_warning_bell')
        self.assertEqual(fact_log.operation, 'create')
        self.assertEqual(fact_log.memory_type, 'fact')
        self.assertEqual(fact_log.importance, 4)
        self.assertEqual(fact_log.certainty, 'confirmed')
        self.assertEqual(fact_log.reason, 'A bell rang to warn of guards.')
        self.assertEqual(fact_log.source_player_message_id, 42)
        self.assertEqual(fact_log.source_dm_message_id, 43)

    def test_memory_patch_no_op_logging(self):
        from models import CampaignMemoryRun, CampaignMemoryLog
        CampaignMemoryRun.query.delete()
        CampaignMemoryLog.query.delete()
        db.session.commit()

        result = apply_memory_patch(
            self.campaign,
            self.session,
            {},
            {
                'trace_id': 'test_trace_noop',
                'player_message_id': 50,
                'dm_message_id': 51
            },
        )

        logs = CampaignMemoryLog.query.all()
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].operation, 'no-op')
        self.assertEqual(logs[0].status, 'no_op')
        self.assertEqual(logs[0].source_player_message_id, 50)
        self.assertEqual(logs[0].source_dm_message_id, 51)

    def test_memory_patch_embedding_dedupe_updates_similar_entity(self):
        world = CampaignWorld.query.filter_by(campaign_id=self.campaign.id).first()
        world.knowledge_graph = json.dumps({
            'entities': [
                {
                    'id': 'silver_street_bookshop',
                    'type': 'location',
                    'name': "Bram Truewood's Bookshop",
                    'summary': 'A bookshop on Silver Street.',
                    'visibility': 'party_known',
                }
            ],
            'relations': [],
            'facts': [],
        })
        db.session.add(CampaignMemoryEmbedding(
            campaign_id=self.campaign.id,
            item_type='entity',
            item_id='silver_street_bookshop',
            visibility='party_known',
            canonical_text="Entity: Bram Truewood's Bookshop",
            text_hash='old',
            embedding_model='gemini-embedding-001',
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
            'model': 'gemini-embedding-001',
            'dimensions': 2,
        }):
            result = apply_memory_patch(
                self.campaign,
                self.session,
                {
                    'upsert_graph_entities': [
                        {
                            'id': 'bram_truewood_bookshop',
                            'type': 'location',
                            'name': "Bram Truewood's Bookshop",
                            'summary': 'A cluttered bookshop on Silver Street run by Bram.',
                            'visibility': 'party_known',
                        }
                    ],
                    'upsert_graph_facts': [
                        {
                            'id': 'fact_bookshop_visit',
                            'entity_ids': ['bram_truewood_bookshop'],
                            'text': 'Seraphina visited Bram Truewood\'s bookshop.',
                            'certainty': 'confirmed',
                            'visibility': 'party_known',
                        }
                    ],
                },
                {},
            )

        graph = json.loads(world.knowledge_graph)
        self.assertEqual(len(graph['entities']), 1)
        self.assertEqual(graph['entities'][0]['id'], 'silver_street_bookshop')
        self.assertIn('cluttered bookshop', graph['entities'][0]['summary'])
        self.assertEqual(graph['facts'][0]['entity_ids'], ['silver_street_bookshop'])
        self.assertEqual(result['graph_changes'][0]['action'], 'updated')
        self.assertEqual(
            result['graph_changes'][0]['embedding_dedupe']['dedupe_match_id'],
            'silver_street_bookshop',
        )

    def test_memory_patch_keeps_distinct_companion_relations(self):
        world = CampaignWorld.query.filter_by(campaign_id=self.campaign.id).first()
        world.knowledge_graph = json.dumps({
            'entities': [
                {'id': 'kai_swiftstrike', 'type': 'pc', 'name': 'Kai Swiftstrike'},
                {'id': 'acolyte_mariko', 'type': 'npc', 'name': 'Acolyte Mariko'},
                {'id': 'acolyte_tobin', 'type': 'npc', 'name': 'Acolyte Tobin'},
            ],
            'relations': [],
            'facts': [],
        })
        db.session.commit()

        with patch('services.dm_tools.find_duplicate_graph_item') as relation_dedupe:
            result = apply_memory_patch(
                self.campaign,
                self.session,
                {
                    'upsert_graph_relations': [
                        {
                            'id': 'kai_mariko_travel',
                            'source_id': 'kai_swiftstrike',
                            'target_id': 'acolyte_mariko',
                            'type': 'traveling_with',
                            'summary': 'Kai Swiftstrike is traveling with Acolyte Mariko.',
                            'visibility': 'public',
                        },
                        {
                            'id': 'kai_tobin_travel',
                            'source_id': 'kai_swiftstrike',
                            'target_id': 'acolyte_tobin',
                            'type': 'traveling_with',
                            'summary': 'Kai Swiftstrike is traveling with Acolyte Tobin.',
                            'visibility': 'public',
                        },
                    ],
                },
                {},
            )

        graph = json.loads(world.knowledge_graph)
        self.assertFalse(relation_dedupe.called)
        self.assertEqual(len(graph['relations']), 2)
        self.assertEqual(
            {
                (relation['type'], relation['source_id'], relation['target_id'])
                for relation in graph['relations']
            },
            {
                ('traveling_with', 'acolyte_mariko', 'kai_swiftstrike'),
                ('traveling_with', 'acolyte_tobin', 'kai_swiftstrike'),
            },
        )
        self.assertEqual([change['action'] for change in result['graph_changes']], ['created', 'created'])

    def test_memory_patch_dedupes_reverse_symmetric_relation(self):
        world = CampaignWorld.query.filter_by(campaign_id=self.campaign.id).first()
        world.knowledge_graph = json.dumps({
            'entities': [
                {'id': 'kai_swiftstrike', 'type': 'pc', 'name': 'Kai Swiftstrike'},
                {'id': 'acolyte_mariko', 'type': 'npc', 'name': 'Acolyte Mariko'},
            ],
            'relations': [
                {
                    'id': 'kai_mariko_travel',
                    'source_id': 'kai_swiftstrike',
                    'target_id': 'acolyte_mariko',
                    'type': 'traveling_with',
                    'summary': 'Kai Swiftstrike is traveling with Acolyte Mariko.',
                    'visibility': 'public',
                },
            ],
            'facts': [],
        })
        db.session.commit()

        result = apply_memory_patch(
            self.campaign,
            self.session,
            {
                'upsert_graph_relations': [
                    {
                        'id': 'mariko_kai_travel',
                        'source_id': 'acolyte_mariko',
                        'target_id': 'kai_swiftstrike',
                        'type': 'traveling_with',
                        'summary': 'Acolyte Mariko is traveling with Kai Swiftstrike.',
                        'visibility': 'public',
                    },
                ],
            },
            {},
        )

        graph = json.loads(world.knowledge_graph)
        self.assertEqual(len(graph['relations']), 1)
        relation = graph['relations'][0]
        self.assertEqual(relation['id'], 'kai_mariko_travel')
        self.assertEqual(relation['source_id'], 'acolyte_mariko')
        self.assertEqual(relation['target_id'], 'kai_swiftstrike')
        self.assertIn('Mariko is traveling with Kai', relation['summary'])
        self.assertEqual(result['graph_changes'][0]['action'], 'updated')
        self.assertEqual(
            result['graph_changes'][0]['embedding_dedupe']['dedupe_strategy'],
            'relation_identity',
        )

    def test_memory_patch_embedding_low_similarity_creates_entity(self):
        db.session.add(CampaignMemoryEmbedding(
            campaign_id=self.campaign.id,
            item_type='entity',
            item_id='fac_crimson_veil',
            visibility='dm_private',
            canonical_text='Entity: Crimson Veil',
            text_hash='old',
            embedding_model='gemini-embedding-001',
            embedding_dimensions=2,
            embedding_json='[1.0, 0.0]',
        ))
        db.session.commit()

        with patch.dict(os.environ, {
            'GEMINI_EMBEDDINGS_ENABLED': 'true',
            'GEMINI_EMBEDDING_DIMENSIONS': '2',
        }, clear=False), patch('services.embedding_service.embedding_from_text', return_value={
            'ok': True,
            'vector': [0.0, 1.0],
            'model': 'gemini-embedding-001',
            'dimensions': 2,
        }):
            result = apply_memory_patch(
                self.campaign,
                self.session,
                {
                    'upsert_graph_entities': [
                        {
                            'id': 'dock_ward',
                            'type': 'location',
                            'name': 'Dock Ward',
                            'summary': 'A busy waterfront district.',
                            'visibility': 'party_known',
                        }
                    ],
                },
                {},
            )

        world = CampaignWorld.query.filter_by(campaign_id=self.campaign.id).first()
        graph = json.loads(world.knowledge_graph)
        self.assertTrue(any(entity['id'] == 'dock_ward' for entity in graph['entities']))
        self.assertEqual(result['graph_changes'][0]['action'], 'created')
        self.assertIsNone(result['graph_changes'][0]['embedding_dedupe']['dedupe_match_id'])

    def test_memory_patch_embedding_failure_falls_back_and_audits(self):
        with patch.dict(os.environ, {
            'GEMINI_EMBEDDINGS_ENABLED': 'true',
            'GEMINI_API_KEY': '',
        }, clear=False):
            result = apply_memory_patch(
                self.campaign,
                self.session,
                {
                    'upsert_graph_entities': [
                        {
                            'id': 'dock_ward',
                            'type': 'location',
                            'name': 'Dock Ward',
                            'summary': 'A busy waterfront district.',
                            'visibility': 'party_known',
                        }
                    ],
                },
                {},
            )

        self.assertEqual(result['graph_changes'][0]['id'], 'dock_ward')
        self.assertIsNotNone(CampaignAuditEvent.query.filter_by(event_type='embedding_fallback').first())

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
            embedding_model='gemini-embedding-001',
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
            'model': 'gemini-embedding-001',
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

    def test_advance_clock_mutates_existing_clock(self):
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
        self.assertEqual(result['clock']['filled'], 4)
        self.assertEqual(result['clock']['status'], 'completed')

    def test_session_message_route_persists_dm_reply_before_memory_update(self):
        token = generate_token(self.user.id)
        client = self.app.test_client()

        def memory_patch_side_effect(_memory_context, audit_context=None):
            dm_event = CampaignAuditEvent.query.filter_by(event_type='dm_output_stored').first()
            self.assertIsNotNone(dm_event)
            self.assertEqual(audit_context['trace_id'].split(':')[0], 'session_memory_writer')
            self.assertEqual(audit_context['parent_trace_id'].split(':')[0], 'session_dm')
            return {}

        with patch('routes.sessions.get_session_dm_response_with_tools', return_value='Yes, you are in a party.') as dm_response, \
                patch('routes.sessions.get_session_memory_patch', side_effect=memory_patch_side_effect) as memory_patch:
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
                patch('routes.sessions.get_session_dm_response_with_tools', return_value='A bell rings across the docks.'), \
                patch('routes.sessions.get_session_memory_patch', return_value={
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

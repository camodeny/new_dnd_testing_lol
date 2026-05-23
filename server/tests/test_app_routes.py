import os
import sys
import tempfile
import unittest
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
os.environ['OPENROUTER_RUNTIME_MODEL_FILE'] = os.path.join(
    tempfile.gettempdir(),
    f'new_dnd_testing_lol_openrouter_model_test_{os.getpid()}',
)

from app import app
from auth import generate_token
from models import (
    db,
    Campaign,
    CampaignInvite,
    CampaignMember,
    Character,
    EncounterMap,
    EncounterMapPlacement,
    PlanningBondProposal,
    User,
)
from openrouter import get_openrouter_model, reset_openrouter_model
from services.planning_service import apply_bond_suggestions, planning_context


class AppRouteTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_root_path = app.root_path
        app.root_path = self.temp_dir.name

        with app.app_context():
            db.drop_all()
            db.create_all()

        static_dir = Path(self.temp_dir.name) / 'static'
        (static_dir / 'assets').mkdir(parents=True)
        (static_dir / 'index.html').write_text('<!doctype html><div id="root"></div>', encoding='utf-8')
        (static_dir / 'assets' / 'app.js').write_text('console.log("ok")', encoding='utf-8')

        self.client = app.test_client()

    def tearDown(self):
        reset_openrouter_model()
        app.root_path = self.old_root_path
        with app.app_context():
            db.session.remove()
        self.temp_dir.cleanup()

    def create_user_token(self):
        with app.app_context():
            user = User(username='dev', email='dev@example.com')
            user.set_password('password')
            db.session.add(user)
            db.session.commit()
            return generate_token(user.id)

    def create_campaign_with_invite(self, code, expires_at=None):
        with app.app_context():
            owner = User(username='owner', email='owner@example.com')
            owner.set_password('password')
            player = User(username='player', email='player@example.com')
            player.set_password('password')
            db.session.add_all([owner, player])
            db.session.commit()

            campaign = Campaign(
                name='Ashes Under Alderfen',
                user_id=owner.id,
                invite_code=code,
            )
            db.session.add(campaign)
            db.session.commit()

            db.session.add(CampaignMember(campaign_id=campaign.id, user_id=owner.id, role='player'))
            db.session.add(CampaignInvite(
                campaign_id=campaign.id,
                code=code,
                created_by=owner.id,
                expires_at=expires_at,
                is_used=False,
            ))
            db.session.commit()

            token = generate_token(player.id)
            return campaign.id, token

    def test_spa_routes_fall_back_to_index(self):
        response = self.client.get('/join/1?code=COWJVBID')

        self.assertEqual(response.status_code, 200)
        self.assertIn('text/html', response.content_type)
        self.assertIn(b'id="root"', response.data)

    def test_static_assets_are_served(self):
        response = self.client.get('/assets/app.js')

        self.assertEqual(response.status_code, 200)
        self.assertIn('text/javascript', response.content_type)
        self.assertEqual(response.data, b'console.log("ok")')

    def test_encounter_map_routes_require_campaign_membership(self):
        maps_dir = Path(self.temp_dir.name) / 'maps'
        maps_dir.mkdir()
        (maps_dir / 'map.png').write_bytes(b'\x89PNG\r\n\x1a\nfake')
        (maps_dir / 'map_labeled.png').write_bytes(b'\x89PNG\r\n\x1a\nlabeled')

        with app.app_context():
            owner = User(username='owner2', email='owner2@example.com')
            owner.set_password('password')
            member = User(username='member2', email='member2@example.com')
            member.set_password('password')
            outsider = User(username='outsider', email='outsider@example.com')
            outsider.set_password('password')
            db.session.add_all([owner, member, outsider])
            db.session.commit()

            campaign = Campaign(name='Map Campaign', user_id=owner.id)
            db.session.add(campaign)
            db.session.commit()
            db.session.add(CampaignMember(campaign_id=campaign.id, user_id=owner.id, role='player'))
            db.session.add(CampaignMember(campaign_id=campaign.id, user_id=member.id, role='player'))
            encounter_map = EncounterMap(
                campaign_id=campaign.id,
                title='Bridge Fight',
                prompt='A bridge over a chasm.',
                image_filename='map.png',
                labeled_image_filename='map_labeled.png',
                model='gpt-image-2',
                size='1024x1024',
                quality='high',
                grid_json=json.dumps({
                    'origin_px': {'x': 0, 'y': 0},
                    'cell_size_px': {'x': 64.0, 'y': 64.0, 'average': 64.0},
                    'columns': 16,
                    'rows': 16,
                    'rotation_degrees': 0,
                    'confidence': 0.9,
                    'warnings': [],
                }),
                vtt_setup_json=json.dumps({
                    'map_summary': 'Bridge over a chasm.',
                    'dm_setup_context': 'Enemies start hidden on the far ridge.',
                    'friendly_spawn_boxes': [],
                    'player_start_areas': [],
                    'enemy_spawn_boxes': [{
                        'label': 'Hidden Ridge',
                        'rect': {'col': 12, 'row': 2, 'width': 2, 'height': 2},
                        'description': 'Enemy start.',
                        'confidence': 0.8,
                    }],
                    'enemy_start_areas': [],
                    'terrain_zones': [],
                    'obstacles': [],
                    'tactical_notes': ['Do not reveal enemy start until initiative.'],
                }),
                setup_status='ready',
            )
            db.session.add(encounter_map)
            db.session.commit()
            campaign_id = campaign.id
            map_id = encounter_map.id
            owner_token = generate_token(owner.id)
            member_token = generate_token(member.id)
            outsider_token = generate_token(outsider.id)

        with patch.dict(os.environ, {'ENCOUNTER_MAP_STORAGE_DIR': str(maps_dir)}, clear=False):
            owner_response = self.client.get(
                f'/api/campaigns/{campaign_id}/encounter-maps/current',
                headers={'Authorization': f'Bearer {owner_token}'},
            )
            member_response = self.client.get(
                f'/api/campaigns/{campaign_id}/encounter-maps/current',
                headers={'Authorization': f'Bearer {member_token}'},
            )
            outsider_response = self.client.get(
                f'/api/campaigns/{campaign_id}/encounter-maps/current',
                headers={'Authorization': f'Bearer {outsider_token}'},
            )
            outsider_image = self.client.get(
                f'/api/encounter-maps/{map_id}/image',
                headers={'Authorization': f'Bearer {outsider_token}'},
            )
            owner_labeled_image = self.client.get(
                f'/api/encounter-maps/{map_id}/labeled-image',
                headers={'Authorization': f'Bearer {owner_token}'},
            )
            outsider_labeled_image = self.client.get(
                f'/api/encounter-maps/{map_id}/labeled-image',
                headers={'Authorization': f'Bearer {outsider_token}'},
            )

        self.assertEqual(owner_response.status_code, 200)
        owner_map = owner_response.get_json()['encounter_map']
        self.assertEqual(owner_map['title'], 'Bridge Fight')
        self.assertEqual(owner_map['setup_status'], 'ready')
        self.assertEqual(owner_map['grid']['cell_size_px']['average'], 64.0)
        self.assertEqual(owner_map['labeled_image_url'], f'/api/encounter-maps/{map_id}/labeled-image')
        self.assertIn('enemy_spawn_boxes', owner_map['vtt_setup'])
        self.assertIn('dm_setup_context', owner_map['vtt_setup'])
        self.assertEqual(member_response.status_code, 200)
        member_map = member_response.get_json()['encounter_map']
        self.assertIn('friendly_spawn_boxes', member_map['vtt_setup'])
        self.assertIn('terrain_zones', member_map['vtt_setup'])
        self.assertNotIn('enemy_spawn_boxes', member_map['vtt_setup'])
        self.assertNotIn('dm_setup_context', member_map['vtt_setup'])
        self.assertEqual(outsider_response.status_code, 403)
        self.assertEqual(outsider_image.status_code, 403)
        self.assertEqual(owner_labeled_image.status_code, 200)
        self.assertEqual(outsider_labeled_image.status_code, 403)

    def test_human_users_do_not_have_map_placement_routes(self):
        with app.app_context():
            owner = User(username='mapowner', email='mapowner@example.com')
            owner.set_password('password')
            db.session.add(owner)
            db.session.commit()

            campaign = Campaign(name='Placement Routes Campaign', user_id=owner.id)
            db.session.add(campaign)
            db.session.commit()
            db.session.add(CampaignMember(campaign_id=campaign.id, user_id=owner.id, role='player'))
            encounter_map = EncounterMap(
                campaign_id=campaign.id,
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
            campaign_id = campaign.id
            map_id = encounter_map.id
            owner_token = generate_token(owner.id)

        create_monster = self.client.post(
            f'/api/campaigns/{campaign_id}/monsters',
            json={'monster_id': 'goblin_1', 'name': 'Goblin'},
            headers={'Authorization': f'Bearer {owner_token}'},
        )
        place_actor = self.client.post(
            f'/api/encounter-maps/{map_id}/placements',
            json={'actor_type': 'monster', 'actor_id': 'goblin_1', 'col': 1, 'row': 1},
            headers={'Authorization': f'Bearer {owner_token}'},
        )
        current_map = self.client.get(
            f'/api/campaigns/{campaign_id}/encounter-maps/current',
            headers={'Authorization': f'Bearer {owner_token}'},
        )

        self.assertIn(create_monster.status_code, {404, 405})
        self.assertIn(place_actor.status_code, {404, 405})
        self.assertEqual(current_map.status_code, 200)
        self.assertEqual(current_map.get_json()['encounter_map']['placements'], [])

    def test_player_can_move_own_map_token_within_character_speed(self):
        with app.app_context():
            player = User(username='mover', email='mover@example.com')
            player.set_password('password')
            db.session.add(player)
            db.session.flush()
            campaign = Campaign(name='Movement Campaign', user_id=player.id)
            db.session.add(campaign)
            db.session.flush()
            character = Character(
                user_id=player.id,
                campaign_id=campaign.id,
                name='Fast Boots',
                race='Human',
                speed=30,
            )
            db.session.add(character)
            db.session.flush()
            db.session.add(CampaignMember(
                campaign_id=campaign.id,
                user_id=player.id,
                role='player',
                selected_character_id=character.id,
            ))
            encounter_map = EncounterMap(
                campaign_id=campaign.id,
                title='Training Grid',
                prompt='A training grid.',
                image_filename='map.png',
                model='gpt-image-2',
                size='1024x1024',
                quality='high',
                grid_json=json.dumps({'columns': 12, 'rows': 10}),
                setup_status='ready',
            )
            db.session.add(encounter_map)
            db.session.flush()
            db.session.add(EncounterMapPlacement(
                encounter_map_id=encounter_map.id,
                actor_type='player',
                actor_id=str(player.id),
                label=character.name,
                grid_col=1,
                grid_row=1,
            ))
            db.session.commit()
            map_id = encounter_map.id
            token = generate_token(player.id)

        response = self.client.patch(
            f'/api/encounter-maps/{map_id}/placements/me',
            json={'col': 7, 'row': 1},
            headers={'Authorization': f'Bearer {token}'},
        )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data['movement']['max_squares'], 6)
        self.assertEqual(data['placement']['col'], 7)
        self.assertEqual(data['placement']['row'], 1)
        self.assertEqual(data['encounter_map']['placements'][0]['col'], 7)

    def test_player_cannot_move_map_token_beyond_character_speed(self):
        with app.app_context():
            player = User(username='slowmover', email='slowmover@example.com')
            player.set_password('password')
            db.session.add(player)
            db.session.flush()
            campaign = Campaign(name='Limited Movement Campaign', user_id=player.id)
            db.session.add(campaign)
            db.session.flush()
            character = Character(
                user_id=player.id,
                campaign_id=campaign.id,
                name='Measured Step',
                race='Dwarf',
                speed=25,
            )
            db.session.add(character)
            db.session.flush()
            db.session.add(CampaignMember(
                campaign_id=campaign.id,
                user_id=player.id,
                role='player',
                selected_character_id=character.id,
            ))
            encounter_map = EncounterMap(
                campaign_id=campaign.id,
                title='Long Hall',
                prompt='A long hall.',
                image_filename='map.png',
                model='gpt-image-2',
                size='1024x1024',
                quality='high',
                grid_json=json.dumps({'columns': 12, 'rows': 10}),
                setup_status='ready',
            )
            db.session.add(encounter_map)
            db.session.flush()
            placement = EncounterMapPlacement(
                encounter_map_id=encounter_map.id,
                actor_type='player',
                actor_id=str(player.id),
                label=character.name,
                grid_col=1,
                grid_row=1,
            )
            db.session.add(placement)
            db.session.commit()
            map_id = encounter_map.id
            placement_id = placement.id
            token = generate_token(player.id)

        response = self.client.patch(
            f'/api/encounter-maps/{map_id}/placements/me',
            json={'col': 7, 'row': 1},
            headers={'Authorization': f'Bearer {token}'},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['movement']['max_squares'], 5)
        with app.app_context():
            placement = db.session.get(EncounterMapPlacement, placement_id)
            self.assertEqual(placement.grid_col, 1)
            self.assertEqual(placement.grid_row, 1)

    def test_player_cannot_move_map_token_through_blocking_terrain(self):
        with app.app_context():
            player = User(username='wallblocked', email='wallblocked@example.com')
            player.set_password('password')
            db.session.add(player)
            db.session.flush()
            campaign = Campaign(name='Blocked Movement Campaign', user_id=player.id)
            db.session.add(campaign)
            db.session.flush()
            character = Character(
                user_id=player.id,
                campaign_id=campaign.id,
                name='Wall Tester',
                race='Human',
                speed=30,
            )
            db.session.add(character)
            db.session.flush()
            db.session.add(CampaignMember(
                campaign_id=campaign.id,
                user_id=player.id,
                role='player',
                selected_character_id=character.id,
            ))
            encounter_map = EncounterMap(
                campaign_id=campaign.id,
                title='Wall Hall',
                prompt='A hall split by a wall.',
                image_filename='map.png',
                model='gpt-image-2',
                size='1024x1024',
                quality='high',
                grid_json=json.dumps({'columns': 5, 'rows': 3}),
                vtt_setup_json=json.dumps({
                    'terrain_zones': [],
                    'obstacles': [{
                        'label': 'Stone Wall',
                        'kind': 'wall',
                        'movement_effect': 'blocks_movement',
                        'shape_type': 'rect',
                        'rect': {'col': 2, 'row': 0, 'width': 1, 'height': 3},
                        'polygon': [],
                    }],
                }),
                setup_status='ready',
            )
            db.session.add(encounter_map)
            db.session.flush()
            placement = EncounterMapPlacement(
                encounter_map_id=encounter_map.id,
                actor_type='player',
                actor_id=str(player.id),
                label=character.name,
                grid_col=1,
                grid_row=1,
            )
            db.session.add(placement)
            db.session.commit()
            map_id = encounter_map.id
            placement_id = placement.id
            token = generate_token(player.id)

        response = self.client.patch(
            f'/api/encounter-maps/{map_id}/placements/me',
            json={'col': 3, 'row': 1},
            headers={'Authorization': f'Bearer {token}'},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('not reachable', response.get_json()['error'])
        with app.app_context():
            placement = db.session.get(EncounterMapPlacement, placement_id)
            self.assertEqual(placement.grid_col, 1)
            self.assertEqual(placement.grid_row, 1)

    def test_player_move_counts_difficult_terrain_extra_cost(self):
        with app.app_context():
            player = User(username='mudrunner', email='mudrunner@example.com')
            player.set_password('password')
            db.session.add(player)
            db.session.flush()
            campaign = Campaign(name='Difficult Movement Campaign', user_id=player.id)
            db.session.add(campaign)
            db.session.flush()
            character = Character(
                user_id=player.id,
                campaign_id=campaign.id,
                name='Mud Runner',
                race='Elf',
                speed=30,
            )
            db.session.add(character)
            db.session.flush()
            db.session.add(CampaignMember(
                campaign_id=campaign.id,
                user_id=player.id,
                role='player',
                selected_character_id=character.id,
            ))
            encounter_map = EncounterMap(
                campaign_id=campaign.id,
                title='Mud Hall',
                prompt='A hall covered in mud.',
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
                        'rect': {'col': 2, 'row': 0, 'width': 3, 'height': 3},
                        'polygon': [],
                    }],
                    'obstacles': [],
                }),
                setup_status='ready',
            )
            db.session.add(encounter_map)
            db.session.flush()
            placement = EncounterMapPlacement(
                encounter_map_id=encounter_map.id,
                actor_type='player',
                actor_id=str(player.id),
                label=character.name,
                grid_col=1,
                grid_row=1,
            )
            db.session.add(placement)
            db.session.commit()
            map_id = encounter_map.id
            placement_id = placement.id
            token = generate_token(player.id)

        response = self.client.patch(
            f'/api/encounter-maps/{map_id}/placements/me',
            json={'col': 5, 'row': 1},
            headers={'Authorization': f'Bearer {token}'},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['movement']['max_squares'], 6)
        with app.app_context():
            placement = db.session.get(EncounterMapPlacement, placement_id)
            self.assertEqual(placement.grid_col, 1)
            self.assertEqual(placement.grid_row, 1)

    def test_missing_api_routes_stay_json_404s(self):
        response = self.client.get('/api/not-real')

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json(), {'error': 'Not found'})

    def test_campaign_preview_accepts_current_invite_code_even_when_invite_row_expired(self):
        campaign_id, token = self.create_campaign_with_invite(
            'COWJVBID',
            expires_at=(datetime.now(UTC) - timedelta(days=1)).replace(tzinfo=None),
        )

        response = self.client.get(
            f'/api/campaigns/{campaign_id}?code=cowjvbid',
            headers={'Authorization': f'Bearer {token}'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['campaign']['id'], campaign_id)

    def test_join_accepts_current_invite_code_case_insensitively(self):
        campaign_id, token = self.create_campaign_with_invite('COWJVBID')

        response = self.client.post(
            f'/api/campaigns/{campaign_id}/join',
            json={'code': 'cowjvbid'},
            headers={'Authorization': f'Bearer {token}'},
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()['member']['campaign_id'], campaign_id)

    def test_campaign_preview_accepts_stored_invite_code_after_current_code_changes(self):
        campaign_id, token = self.create_campaign_with_invite('COWJVBID')
        with app.app_context():
            campaign = db.session.get(Campaign, campaign_id)
            campaign.invite_code = 'KI9FIV1K'
            db.session.add(CampaignInvite(
                campaign_id=campaign_id,
                code='KI9FIV1K',
                created_by=campaign.user_id,
                is_used=False,
            ))
            db.session.commit()

        response = self.client.get(
            f'/api/campaigns/{campaign_id}?code=COWJVBID',
            headers={'Authorization': f'Bearer {token}'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['campaign']['id'], campaign_id)

    def test_get_invite_returns_current_invite_without_creating_a_new_one(self):
        campaign_id, token = self.create_campaign_with_invite('COWJVBID')

        response = self.client.get(
            f'/api/campaigns/{campaign_id}/invites',
            headers={'Authorization': f'Bearer {token}'},
        )

        self.assertEqual(response.status_code, 403)

        with app.app_context():
            owner_id = db.session.get(Campaign, campaign_id).user_id
            owner_token = generate_token(owner_id)

        response = self.client.get(
            f'/api/campaigns/{campaign_id}/invites',
            headers={'Authorization': f'Bearer {owner_token}'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['invite']['code'], 'COWJVBID')
        with app.app_context():
            self.assertEqual(CampaignInvite.query.filter_by(campaign_id=campaign_id).count(), 1)

    def test_dev_model_settings_can_override_openrouter_model(self):
        token = self.create_user_token()

        response = self.client.put(
            '/api/dev/model',
            json={'model': 'anthropic/claude-sonnet-4.5'},
            headers={'Authorization': f'Bearer {token}'},
        )

        self.assertEqual(response.status_code, 200)
        settings = response.get_json()['settings']
        self.assertEqual(settings['model'], 'anthropic/claude-sonnet-4.5')
        self.assertEqual(settings['source'], 'runtime')
        self.assertTrue(settings['is_overridden'])
        self.assertEqual(get_openrouter_model(), 'anthropic/claude-sonnet-4.5')

        refreshed_response = self.client.get(
            '/api/dev/model',
            headers={'Authorization': f'Bearer {token}'},
        )

        self.assertEqual(refreshed_response.status_code, 200)
        refreshed_settings = refreshed_response.get_json()['settings']
        self.assertEqual(refreshed_settings['model'], 'anthropic/claude-sonnet-4.5')
        self.assertEqual(refreshed_settings['source'], 'runtime')

    def test_dev_model_settings_reject_blank_model(self):
        token = self.create_user_token()

        response = self.client.put(
            '/api/dev/model',
            json={'model': '   '},
            headers={'Authorization': f'Bearer {token}'},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json(), {'error': 'Model is required'})

    def test_create_character_normalizes_nested_model_output(self):
        token = self.create_user_token()

        response = self.client.post(
            '/api/characters',
            json={
                'name': 'Raven Nightshade',
                'player_name': 'dev',
                'race': 'Human Variant',
                'background': 'Charlatan',
                'classes': [{'class_name': 'Rogue', 'level': 1}],
                'skills': ['Deception', 'Stealth'],
                'saving_throws': ['Dexterity'],
                'proficiencies': ['Thieves tools'],
                'weapons': [{'name': 'Rapier', 'damage': '1d8 piercing', 'properties': ['finesse']}],
                'equipment': [{'name': 'Studded Leather', 'type': 'armor', 'properties': ['light']}],
            },
            headers={'Authorization': f'Bearer {token}'},
        )

        self.assertEqual(response.status_code, 201)
        character = response.get_json()['character']
        self.assertEqual(character['skills'][0]['skill_name'], 'Deception')
        self.assertEqual(character['saving_throws'][0]['ability'], 'Dexterity')
        self.assertEqual(character['proficiencies'][0]['name'], 'Thieves tools')
        self.assertEqual(character['weapons'][0]['properties'], 'finesse')
        self.assertEqual(character['equipment'][0]['equipment_type'], 'armor')
        self.assertEqual(character['equipment'][0]['properties'], 'light')

        with app.app_context():
            self.assertEqual(Character.query.filter_by(name='Raven Nightshade').count(), 1)

    def test_bond_suggestions_are_deduplicated(self):
        with app.app_context():
            owner = User(username='owner', email='owner@example.com')
            owner.set_password('password')
            player = User(username='player', email='player@example.com')
            player.set_password('password')
            db.session.add_all([owner, player])
            db.session.commit()

            campaign = Campaign(name='Lost Stones', user_id=owner.id)
            db.session.add(campaign)
            db.session.commit()

            suggestion = {
                'title': 'Inverse Former Lover Bond',
                'description': "Former lover of Phazedrl's mother.",
                'involved_user_ids': [owner.id, player.id],
            }

            self.assertEqual(len(apply_bond_suggestions(campaign.id, [suggestion])), 1)
            self.assertEqual(len(apply_bond_suggestions(campaign.id, [suggestion])), 0)
            db.session.commit()

            self.assertEqual(PlanningBondProposal.query.filter_by(campaign_id=campaign.id).count(), 1)

    def test_planning_context_read_only_mode_sanitizes_invalid_ready_state_without_dirtying_session(self):
        with app.app_context():
            owner = User(username='owner', email='owner@example.com')
            owner.set_password('password')
            player = User(username='player', email='player@example.com')
            player.set_password('password')
            db.session.add_all([owner, player])
            db.session.commit()

            campaign = Campaign(name='Lost Stones', user_id=owner.id)
            db.session.add(campaign)
            db.session.flush()
            member = CampaignMember(
                campaign_id=campaign.id,
                user_id=player.id,
                role='player',
                selected_character_id=9999,
                character_ready_at=datetime(2026, 1, 1, 12, 0, 0),
            )
            db.session.add_all([
                CampaignMember(campaign_id=campaign.id, user_id=owner.id, role='player'),
                member,
            ])
            db.session.commit()

            context = planning_context(campaign, player, clean_ready_states=False)
            player_member = next(item for item in context['members'] if item['user_id'] == player.id)

            self.assertIsNone(player_member['selected_character_id'])
            self.assertIsNone(player_member['character_ready_at'])
            self.assertFalse(player_member['is_character_ready'])
            self.assertFalse(db.session.dirty)
            persisted_member = db.session.get(CampaignMember, member.id)
            self.assertEqual(persisted_member.selected_character_id, 9999)
            self.assertIsNotNone(persisted_member.character_ready_at)

    def test_dev_audit_does_not_flush_invalid_ready_state_cleanup(self):
        with app.app_context():
            owner = User(username='owner', email='owner@example.com')
            owner.set_password('password')
            player = User(username='player', email='player@example.com')
            player.set_password('password')
            db.session.add_all([owner, player])
            db.session.commit()

            campaign = Campaign(name='Lost Stones', user_id=owner.id)
            db.session.add(campaign)
            db.session.flush()
            member = CampaignMember(
                campaign_id=campaign.id,
                user_id=player.id,
                role='player',
                selected_character_id=9999,
                character_ready_at=datetime(2026, 1, 1, 12, 0, 0),
            )
            db.session.add_all([
                CampaignMember(campaign_id=campaign.id, user_id=owner.id, role='player'),
                member,
            ])
            db.session.commit()
            campaign_id = campaign.id
            member_id = member.id
            player_id = player.id
            token = generate_token(player.id)

        response = self.client.get(
            f'/api/campaigns/{campaign_id}/dev',
            headers={'Authorization': f'Bearer {token}'},
        )

        self.assertEqual(response.status_code, 200)
        planning_member = next(
            item for item in response.get_json()['planning']['context']['members']
            if item['user_id'] == player_id
        )
        self.assertIsNone(planning_member['selected_character_id'])
        with app.app_context():
            persisted_member = db.session.get(CampaignMember, member_id)
            self.assertEqual(persisted_member.selected_character_id, 9999)
            self.assertIsNotNone(persisted_member.character_ready_at)

    def test_pending_bond_blocks_character_ready(self):
        with app.app_context():
            owner = User(username='owner', email='owner@example.com')
            owner.set_password('password')
            player = User(username='player', email='player@example.com')
            player.set_password('password')
            db.session.add_all([owner, player])
            db.session.commit()

            campaign = Campaign(name='Lost Stones', user_id=owner.id)
            db.session.add(campaign)
            db.session.flush()
            character = Character(
                user_id=player.id,
                campaign_id=campaign.id,
                name='Raven Nightshade',
                race='Human',
            )
            db.session.add(character)
            db.session.flush()
            db.session.add(CampaignMember(campaign_id=campaign.id, user_id=owner.id, role='player'))
            db.session.add(CampaignMember(
                campaign_id=campaign.id,
                user_id=player.id,
                role='player',
                selected_character_id=character.id,
            ))
            db.session.add(PlanningBondProposal(
                campaign_id=campaign.id,
                title='Former Lover Bond',
                description='A proposed connection that needs approval.',
                involved_user_ids=f'[{owner.id}, {player.id}]',
                approval_states=f'{{"{owner.id}": "accepted", "{player.id}": "pending"}}',
                status='pending',
            ))
            db.session.commit()
            token = generate_token(player.id)
            campaign_id = campaign.id

        response = self.client.put(
            f'/api/campaigns/{campaign_id}/planning/ready',
            json={'ready': True},
            headers={'Authorization': f'Bearer {token}'},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json(), {'error': 'Resolve pending bond proposals before marking ready'})

    def test_encounter_combat_flow_and_movement_restrictions(self):
        from routes.encounter_maps import build_initial_encounter_state, check_and_start_turns
        with app.app_context():
            owner = User(username='dm_user', email='dm@example.com')
            owner.set_password('password')
            player = User(username='player_user', email='player@example.com')
            player.set_password('password')
            db.session.add_all([owner, player])
            db.session.commit()

            campaign = Campaign(name='Combat Campaign', user_id=owner.id)
            db.session.add(campaign)
            db.session.commit()

            character = Character(
                user_id=player.id,
                campaign_id=campaign.id,
                name='Fighter Hero',
                race='Dwarf',
                speed=30,
            )
            db.session.add(character)
            db.session.commit()

            db.session.add(CampaignMember(
                campaign_id=campaign.id,
                user_id=owner.id,
                role='player',
            ))
            db.session.add(CampaignMember(
                campaign_id=campaign.id,
                user_id=player.id,
                role='player',
                selected_character_id=character.id,
            ))
            
            encounter_map = EncounterMap(
                campaign_id=campaign.id,
                title='Arena',
                prompt='A flat sandy arena.',
                image_filename='arena.png',
                model='gpt-image-2',
                size='1024x1024',
                quality='high',
                grid_json=json.dumps({'columns': 10, 'rows': 10}),
                vtt_setup_json=json.dumps({
                    'terrain_zones': [],
                    'obstacles': [],
                }),
                setup_status='ready',
            )
            db.session.add(encounter_map)
            db.session.commit()

            player_placement = EncounterMapPlacement(
                encounter_map_id=encounter_map.id,
                actor_type='player',
                actor_id=str(player.id),
                label=character.name,
                grid_col=0,
                grid_row=0,
            )
            monster_placement = EncounterMapPlacement(
                encounter_map_id=encounter_map.id,
                actor_type='monster',
                actor_id='goblin-1',
                label='Goblin',
                grid_col=5,
                grid_row=5,
            )
            db.session.add_all([player_placement, monster_placement])
            db.session.commit()

            campaign_id = campaign.id
            map_id = encounter_map.id
            player_placement_id = player_placement.id
            dm_token = generate_token(owner.id)
            player_token = generate_token(player.id)

        # 1. Assert that deleted endpoints return 404
        for endpoint in ('toggle', 'prev-turn', 'set-turn', 'update-actions'):
            response = self.client.post(
                f'/api/encounter-maps/{map_id}/encounter/{endpoint}',
                json={},
                headers={'Authorization': f'Bearer {dm_token}'},
            )
            self.assertEqual(response.status_code, 404)

        # 2. Set up initial combat state directly in database
        with app.app_context():
            encounter_map = db.session.get(EncounterMap, map_id)
            campaign = db.session.get(Campaign, encounter_map.campaign_id)
            encounter_state = build_initial_encounter_state(encounter_map, campaign)
            # Give the monster a low initiative (e.g. 5) so player init 10 will go first
            monster_combatant = next(x for x in encounter_state['turn_order'] if x['actor_type'] == 'monster')
            monster_combatant['initiative'] = 5
            encounter_map.encounter_state_json = json.dumps(encounter_state)
            db.session.commit()

        # 3. Roll initiative for player (fails for others)
        response = self.client.post(
            f'/api/encounter-maps/{map_id}/encounter/roll-initiative',
            json={'actor_type': 'monster', 'actor_id': 'goblin-1', 'initiative': 15},
            headers={'Authorization': f'Bearer {player_token}'},
        )
        self.assertEqual(response.status_code, 403)

        # Player rolls initiative for themselves
        response = self.client.post(
            f'/api/encounter-maps/{map_id}/encounter/roll-initiative',
            json={'actor_type': 'player', 'actor_id': str(player.id), 'initiative': 10},
            headers={'Authorization': f'Bearer {player_token}'},
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()['encounter_map']
        state = data['encounter_state']
        # Active turn should be player (index 0) because player initiative (10) > monster initiative (5)
        self.assertEqual(state['active_turn_index'], 0)
        active_combatant = state['turn_order'][0]
        self.assertEqual(active_combatant['actor_type'], 'player')

        # 4. End Turn restriction: DM cannot end player's turn via next-turn API
        response = self.client.post(
            f'/api/encounter-maps/{map_id}/encounter/next-turn',
            headers={'Authorization': f'Bearer {dm_token}'},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn('Only the active player', response.get_json()['error'])

        # Player can end their own turn
        response = self.client.post(
            f'/api/encounter-maps/{map_id}/encounter/next-turn',
            headers={'Authorization': f'Bearer {player_token}'},
        )
        self.assertEqual(response.status_code, 200)
        state = response.get_json()['encounter_map']['encounter_state']
        self.assertEqual(state['active_turn_index'], 1)
        active_combatant = state['turn_order'][1]
        self.assertEqual(active_combatant['actor_type'], 'monster')

        # Since it is now the monster's turn, player cannot move
        response = self.client.patch(
            f'/api/encounter-maps/{map_id}/placements/me',
            json={'col': 1, 'row': 1},
            headers={'Authorization': f'Bearer {player_token}'},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('not your turn', response.get_json()['error'])

        # Player cannot call next-turn when it is not their turn
        response = self.client.post(
            f'/api/encounter-maps/{map_id}/encounter/next-turn',
            headers={'Authorization': f'Bearer {player_token}'},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn('Only the active player', response.get_json()['error'])

        # Directly set turn back to player (index 0) in DB to test player movement
        with app.app_context():
            encounter_map = db.session.get(EncounterMap, map_id)
            state = json.loads(encounter_map.encounter_state_json)
            state['active_turn_index'] = 0
            encounter_map.encounter_state_json = json.dumps(state)
            db.session.commit()

        # Now it is player's turn, move the player token by 2 squares (col: 0->2, row: 0->0)
        response = self.client.patch(
            f'/api/encounter-maps/{map_id}/placements/me',
            json={'col': 2, 'row': 0},
            headers={'Authorization': f'Bearer {player_token}'},
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()['encounter_map']
        state = data['encounter_state']
        player_combatant = next(x for x in state['turn_order'] if x['actor_type'] == 'player')
        self.assertEqual(player_combatant['actions']['movement_remaining'], 20)

        # Mutate action budget directly in DB (e.g. update actions for next movement test)
        with app.app_context():
            encounter_map = db.session.get(EncounterMap, map_id)
            state = json.loads(encounter_map.encounter_state_json)
            player_combatant = next(x for x in state['turn_order'] if x['actor_type'] == 'player')
            player_combatant['actions']['action'] = False
            player_combatant['actions']['movement_remaining'] = 15
            encounter_map.encounter_state_json = json.dumps(state)
            db.session.commit()

        # Verify mutated values are returned properly
        response = self.client.get(
            f'/api/campaigns/{campaign_id}/encounter-maps/current',
            headers={'Authorization': f'Bearer {player_token}'},
        )
        self.assertEqual(response.status_code, 200)
        state = response.get_json()['encounter_map']['encounter_state']
        player_combatant = next(x for x in state['turn_order'] if x['actor_type'] == 'player')
        self.assertFalse(player_combatant['actions']['action'])
        self.assertEqual(player_combatant['actions']['movement_remaining'], 15)

    def test_lookup_invite_code_success(self):
        campaign_id, token = self.create_campaign_with_invite('MYCODE12')
        response = self.client.get(
            '/api/invites/lookup?code=mycode12',
            headers={'Authorization': f'Bearer {token}'},
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data['campaign_id'], campaign_id)
        self.assertEqual(data['campaign_name'], 'Ashes Under Alderfen')

    def test_lookup_invite_code_missing(self):
        token = self.create_user_token()
        response = self.client.get(
            '/api/invites/lookup',
            headers={'Authorization': f'Bearer {token}'},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error'], 'Missing invite code')

    def test_lookup_invite_code_not_found(self):
        token = self.create_user_token()
        response = self.client.get(
            '/api/invites/lookup?code=NONEXISTENT',
            headers={'Authorization': f'Bearer {token}'},
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()['error'], 'Invalid invite code')


if __name__ == '__main__':
    unittest.main()

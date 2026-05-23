import os
import json
import sys
import unittest
from datetime import datetime
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from flask import Flask
from auth import generate_token
from models import (
    db,
    Campaign,
    CampaignMember,
    CampaignSession,
    CampaignShop,
    CampaignWorld,
    Character,
    CharacterEquipment,
    SessionMessage,
    User,
)
from routes.shops import shops_bp
from services.dm_tools import execute_dm_tool


class ShopsTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        self.app.config['SECRET_KEY'] = 'test-secret'
        self.app.config['JWT_EXPIRATION_HOURS'] = 1
        self.app.register_blueprint(shops_bp)
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

        # Seed test user
        self.user = User(username='player', email='player@example.com')
        self.user.set_password('password')
        db.session.add(self.user)
        db.session.flush()

        # Seed test campaign
        self.campaign = Campaign(name='Shop Test Campaign', description='Testing shops', user_id=self.user.id)
        db.session.add(self.campaign)
        db.session.flush()

        # Seed test character
        self.character = Character(
            user_id=self.user.id,
            campaign_id=self.campaign.id,
            name='Garrick',
            race='Human',
            gp=100
        )
        db.session.add(self.character)
        db.session.flush()

        # Seed member
        db.session.add(CampaignMember(
            campaign_id=self.campaign.id,
            user_id=self.user.id,
            selected_character_id=self.character.id
        ))

        # Seed active session
        self.session = CampaignSession(campaign_id=self.campaign.id, is_active=True)
        db.session.add(self.session)
        self.world = CampaignWorld(
            campaign_id=self.campaign.id,
            public_intro='{}',
            knowledge_graph='{}',
            world_state=json.dumps({
                'current_scene': {
                    'location_id': 'market_square',
                    'location_name': 'Market Square',
                },
            }),
            dm_private='{}',
        )
        db.session.add(self.world)
        db.session.commit()

        # Generate auth token
        self.token = generate_token(self.user.id)
        self.headers = {'Authorization': f'Bearer {self.token}'}

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_create_shop_list_tool(self):
        # Call the scene-level tool to create multiple shop menus
        args = {
            'scene_note': 'A busy market square with permanent stalls.',
            'shops': [
                {
                    'name': "Alaric's Alchemical Curiosities",
                    'description': 'A wizard shop with glowing bottles.',
                    'specialties': ['potions', 'scrolls'],
                    'item_count': 2,
                },
                {
                    'name': 'Gromm Ironhand',
                    'description': 'A soot-streaked smith selling practical arms.',
                    'specialties': ['weapons', 'armor'],
                    'item_count': 2,
                },
            ]
        }

        with patch('openrouter._post_chat', side_effect=[
            json.dumps({'items': [
                {'name': 'Potion of Healing', 'description': 'Restores 2d4+2 HP.', 'cost_gp': 50, 'quantity': 5},
                {'name': 'Scroll of Fireball', 'description': 'Deals 8d6 fire damage.', 'cost_gp': 150, 'quantity': 1},
            ]}),
            json.dumps({'items': [
                {'name': 'Dagger', 'description': 'Simple weapon', 'cost_gp': 2, 'quantity': None},
                {'name': 'Shield', 'description': 'Plain iron-banded shield.', 'cost_gp': 10, 'quantity': 3},
            ]}),
        ]) as post_chat:
            result = execute_dm_tool(
                self.campaign,
                self.session,
                self.user,
                'create_shop_list',
                args
            )

        db.session.commit()

        self.assertNotIn('error', result)
        self.assertEqual(len(result['shops']), 2)
        self.assertEqual(post_chat.call_count, 2)

        # Check DB directly
        shop = CampaignShop.query.filter_by(campaign_id=self.campaign.id, name="Alaric's Alchemical Curiosities").first()
        self.assertIsNotNone(shop)
        self.assertEqual(shop.name, "Alaric's Alchemical Curiosities")
        self.assertEqual(shop.location_id, 'market_square')
        self.assertEqual(shop.location_name, 'Market Square')
        self.assertEqual(shop.to_dict()['items'][0]['name'], 'Potion of Healing')
        self.assertIsNotNone(CampaignShop.query.filter_by(campaign_id=self.campaign.id, name='Gromm Ironhand').first())

        # Verify chat announcement was created
        announcements = SessionMessage.query.filter_by(session_id=self.session.id, role='dm').all()
        self.assertEqual(len(announcements), 1)
        self.assertIn("Alaric's Alchemical Curiosities", announcements[0].content)
        self.assertIn('Gromm Ironhand', announcements[0].content)
        self.assertIn('Market Square', announcements[0].content)

    def test_list_shops_route(self):
        # Insert a shop manually
        items = [{'name': 'Dagger', 'description': 'Simple weapon', 'cost_gp': 2, 'quantity': None}]
        shop = CampaignShop(
            campaign_id=self.campaign.id,
            location_id='market_square',
            location_name='Market Square',
            name='Blacksmith',
            description='Metalwork shop',
            items_json=json.dumps(items)
        )
        remote_shop = CampaignShop(
            campaign_id=self.campaign.id,
            location_id='dock_ward',
            location_name='Dock Ward',
            name='Dock Chandler',
            description='Ship supplies',
            items_json=json.dumps(items)
        )
        db.session.add(shop)
        db.session.add(remote_shop)
        db.session.commit()

        # GET campaign shops
        res = self.client.get(f'/api/campaigns/{self.campaign.id}/shops', headers=self.headers)
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data['current_scene']['location_id'], 'market_square')
        self.assertEqual(len(data['shops']), 1)
        self.assertEqual(data['shops'][0]['name'], 'Blacksmith')
        self.assertEqual(data['shops'][0]['location_name'], 'Market Square')
        self.assertEqual(data['shops'][0]['items'][0]['name'], 'Dagger')

    def test_buy_item_success(self):
        # Insert shop
        items = [
            {'name': 'Potion of Healing', 'description': 'Heal', 'cost_gp': 50, 'quantity': 2},
            {'name': 'Longsword', 'description': 'Slash', 'cost_gp': 15, 'quantity': None}
        ]
        shop = CampaignShop(
            campaign_id=self.campaign.id,
            location_id='market_square',
            location_name='Market Square',
            name='General Store',
            description='All goods',
            items_json=json.dumps(items)
        )
        db.session.add(shop)
        db.session.commit()

        # Buy 1 Potion of Healing
        payload = {
            'character_id': self.character.id,
            'item_name': 'Potion of Healing'
        }
        res = self.client.post(f'/api/shops/{shop.id}/buy', headers=self.headers, json=payload)
        self.assertEqual(res.status_code, 200)
        data = res.get_json()

        # Verify response shop stock decreased
        updated_shop_items = data['shop']['items']
        potion = next(i for i in updated_shop_items if i['name'] == 'Potion of Healing')
        self.assertEqual(potion['quantity'], 1)

        # Verify gold deducted (100 - 50 = 50)
        self.assertEqual(data['character']['currency']['gp'], 50)

        # Verify equipment added in DB
        equip = CharacterEquipment.query.filter_by(character_id=self.character.id, name='Potion of Healing').first()
        self.assertIsNotNone(equip)
        self.assertEqual(equip.quantity, 1)

        # Verify system message purchase announcement
        msgs = SessionMessage.query.filter_by(session_id=self.session.id, role='system').all()
        self.assertEqual(len(msgs), 1)
        self.assertIn('**Garrick** purchased **Potion of Healing** from **General Store** for 50 gp.', msgs[0].content)

    def test_buy_item_insufficient_gold(self):
        # Shop item costs 150 gp, character only has 100 gp
        items = [{'name': 'Plate Armor', 'description': 'Heavy', 'cost_gp': 150, 'quantity': 1}]
        shop = CampaignShop(
            campaign_id=self.campaign.id,
            location_id='market_square',
            location_name='Market Square',
            name='Armory',
            description='Armor sales',
            items_json=json.dumps(items)
        )
        db.session.add(shop)
        db.session.commit()

        payload = {
            'character_id': self.character.id,
            'item_name': 'Plate Armor'
        }
        res = self.client.post(f'/api/shops/{shop.id}/buy', headers=self.headers, json=payload)
        self.assertEqual(res.status_code, 400)
        data = res.get_json()
        self.assertIn('Insufficient gold', data['error'])

    def test_buy_item_out_of_stock(self):
        # Shop item is out of stock (quantity = 0)
        items = [{'name': 'Rope', 'description': '50 ft', 'cost_gp': 1, 'quantity': 0}]
        shop = CampaignShop(
            campaign_id=self.campaign.id,
            location_id='market_square',
            location_name='Market Square',
            name='General Store',
            description='All goods',
            items_json=json.dumps(items)
        )
        db.session.add(shop)
        db.session.commit()

        payload = {
            'character_id': self.character.id,
            'item_name': 'Rope'
        }
        res = self.client.post(f'/api/shops/{shop.id}/buy', headers=self.headers, json=payload)
        self.assertEqual(res.status_code, 400)
        data = res.get_json()
        self.assertIn('out of stock', data['error'])

    def test_buy_item_rejects_shop_at_different_location(self):
        items = [{'name': 'Lantern', 'description': 'Hooded lantern', 'cost_gp': 5, 'quantity': 1}]
        shop = CampaignShop(
            campaign_id=self.campaign.id,
            location_id='dock_ward',
            location_name='Dock Ward',
            name='Dock Chandler',
            description='Ship supplies',
            items_json=json.dumps(items)
        )
        db.session.add(shop)
        db.session.commit()

        payload = {
            'character_id': self.character.id,
            'item_name': 'Lantern'
        }
        res = self.client.post(f'/api/shops/{shop.id}/buy', headers=self.headers, json=payload)
        self.assertEqual(res.status_code, 409)
        data = res.get_json()
        self.assertIn("party's current location", data['error'])

import json
import os
import sys
import unittest

from flask import Flask

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from models import Campaign, CampaignMember, Character, CharacterClass, User, db
from openrouter import WORLD_GENESIS_SECTION_SPECS, build_world_genesis_messages
from services.entity_types import WORLD_ENTITY_TYPE_HINT, normalize_world_entity_type
from services.world_service import build_world_identity_pairs, normalize_entities, normalize_world_package


class WorldEntityTypeTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        owner = User(username='owner', email='owner@example.com')
        owner.set_password('password')
        second_player = User(username='second', email='second@example.com')
        second_player.set_password('password')
        db.session.add_all([owner, second_player])
        db.session.flush()

        self.campaign = Campaign(name='Roster World', user_id=owner.id)
        db.session.add(self.campaign)
        db.session.flush()

        zoro = Character(
            campaign_id=self.campaign.id,
            user_id=owner.id,
            name='Roronoa Zoro',
            race='Human',
        )
        amun = Character(
            campaign_id=self.campaign.id,
            user_id=second_player.id,
            name='Amun',
            race='Human',
        )
        db.session.add_all([zoro, amun])
        db.session.flush()
        db.session.add(CharacterClass(character_id=zoro.id, class_name='Fighter', level=1))
        db.session.add_all([
            CampaignMember(
                campaign_id=self.campaign.id,
                user_id=owner.id,
                selected_character_id=zoro.id,
            ),
            CampaignMember(
                campaign_id=self.campaign.id,
                user_id=second_player.id,
                selected_character_id=amun.id,
            ),
        ])
        db.session.commit()

    def tearDown(self):
        db.session.rollback()
        db.drop_all()
        self.ctx.pop()

    def _raw_package(self):
        return {
            'public_intro': {'title': 'Roster World'},
            'knowledge_graph': {
                'entities': [
                    {
                        'id': 'pc_roronoa',
                        'type': 'npc',
                        'name': 'Roronoa Zoro',
                        'summary': 'A bounty hunter fighter.',
                        'visibility': 'dm_private',
                        'tags': ['fighter'],
                    },
                    {
                        'id': 'the_moth',
                        'type': 'ship',
                        'name': 'The Moth',
                        'summary': 'A battered tug.',
                        'visibility': 'public',
                    },
                ],
                'relations': [
                    {
                        'id': 'zoro_pilots_moth',
                        'source_id': 'pc_roronoa',
                        'target_id': 'the_moth',
                        'type': 'pilots',
                        'summary': 'Zoro pilots the tug.',
                        'visibility': 'public',
                    },
                ],
                'facts': [
                    {
                        'id': 'zoro_aboard',
                        'entity_ids': ['pc_roronoa', 'the_moth'],
                        'text': 'Zoro is aboard the Moth.',
                        'visibility': 'public',
                    },
                ],
            },
        }

    def test_selected_roster_characters_are_authoritative_graph_entities(self):
        package = normalize_world_package(self._raw_package(), self.campaign)
        graph = package['knowledge_graph']
        entities = {entity['id']: entity for entity in graph['entities']}

        self.assertEqual(entities['roronoa_zoro']['type'], 'character')
        self.assertEqual(entities['roronoa_zoro']['name'], 'Roronoa Zoro')
        self.assertEqual(entities['roronoa_zoro']['visibility'], 'public')
        self.assertIn('player_character', entities['roronoa_zoro']['tags'])
        self.assertEqual(entities['amun']['type'], 'character')
        self.assertIn('player_character', entities['amun']['tags'])

        relation = graph['relations'][0]
        self.assertEqual(relation['source_id'], 'roronoa_zoro')
        self.assertEqual(relation['target_id'], 'the_moth')
        self.assertEqual(graph['facts'][0]['entity_ids'], ['roronoa_zoro', 'the_moth'])

    def test_roster_characters_are_not_paired_with_npc_actor_rows(self):
        package = normalize_world_package(self._raw_package(), self.campaign)
        package['npc_actors'] = [
            {'id': 'npc_zoro', 'name': 'Roronoa Zoro'},
        ]
        self.assertEqual(build_world_identity_pairs(package), [])

    def test_entity_type_aliases_collapse_to_canonical_vocabulary(self):
        entities = normalize_entities([
            {'id': 'moth', 'type': 'ship', 'name': 'Moth'},
            {'id': 'guild', 'type': 'organization', 'name': 'Guild'},
            {'id': 'crew', 'type': 'family', 'name': 'Crew'},
            {'id': 'key', 'type': 'item/device', 'name': 'Key'},
            {'id': 'oddity', 'type': 'unrecognized_new_type', 'name': 'Oddity'},
        ])
        self.assertEqual(
            {entity['id']: entity['type'] for entity in entities},
            {
                'moth': 'vehicle',
                'guild': 'faction',
                'crew': 'group',
                'key': 'item',
                'oddity': 'other',
            },
        )
        self.assertEqual(normalize_world_entity_type('player_character'), 'character')

    def test_world_genesis_prompts_share_the_canonical_type_vocabulary(self):
        full_prompt = json.loads(build_world_genesis_messages({})[-1]['content'])
        full_hint = full_prompt['return_shape']['knowledge_graph']['entities'][0]['type']
        section_shapes = dict(WORLD_GENESIS_SECTION_SPECS)
        section_hint = section_shapes['knowledge_graph']['knowledge_graph']['entities'][0]['type']

        self.assertEqual(full_hint, WORLD_ENTITY_TYPE_HINT)
        self.assertEqual(section_hint, WORLD_ENTITY_TYPE_HINT)
        self.assertIn('character', WORLD_ENTITY_TYPE_HINT.split(' | '))
        self.assertIn('vehicle', WORLD_ENTITY_TYPE_HINT.split(' | '))
        self.assertIn('group', WORLD_ENTITY_TYPE_HINT.split(' | '))


if __name__ == '__main__':
    unittest.main()

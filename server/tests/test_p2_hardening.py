import os
import sys
import json
import unittest
from flask import Flask

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from models import (
    db,
    User,
    Campaign,
    CampaignSession,
    CampaignWorld
)
from services.dm_tools import apply_compiled_session_memory_patch
from services.session_memory_agent import MemoryPipelineError, compile_staged_memory_patch
from services.memory_resolver_schemas import SOURCE_CONTRACT_COMPILED_V2

class P2HardeningTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SECRET_KEY'] = 'test-secret'
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        # Seed basic models
        self.user = User(username='testuser', email='test@example.com', password_hash='hash')
        db.session.add(self.user)
        db.session.flush()

        self.campaign = Campaign(name='P2 Test Campaign', user_id=self.user.id)
        db.session.add(self.campaign)
        db.session.flush()

        self.session = CampaignSession(campaign_id=self.campaign.id, running_summary="Summary")
        db.session.add(self.session)
        db.session.flush()

        # Seed CampaignWorld for resolver context
        self.world = CampaignWorld(
            campaign_id=self.campaign.id,
            public_intro='{}',
            world_state='{}',
            dm_private='{}',
            knowledge_graph=json.dumps({
                'entities': [
                    {'id': 'waterdeep', 'name': 'Waterdeep', 'type': 'location'},
                    {'id': 'neverwinter', 'name': 'Neverwinter', 'type': 'location'}
                ],
                'relations': [],
                'facts': []
            })
        )
        db.session.add(self.world)
        db.session.flush()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_memory_anchors_persistence_via_compiled_contract(self):
        anchors = {
            "current_goal": "Find the lost mine",
            "current_scene": "Inside the tavern",
            "open_clues": ["Map fragment"],
            "unresolved_questions": ["Where is Gundren?"],
            "npc_observations": ["Sildar looks tired"],
            "recent_offers_promises": ["100gp reward"]
        }
        patch = {
            'source_contract': SOURCE_CONTRACT_COMPILED_V2,
            'base_memory_revision': self.world.memory_revision or 0,
            'running_summary': 'Summary updated',
            'memory_anchors': anchors,
            'upsert_graph_entities': [],
            'upsert_graph_relations': [],
            'upsert_graph_facts': [],
            'update_npc_actors': [],
            'record_events': [],
        }
        apply_compiled_session_memory_patch(self.campaign, self.session, patch)
        db.session.commit()

        session_db = db.session.get(CampaignSession, self.session.id)
        self.assertEqual(session_db.memory_anchors, anchors)

    def test_to_dict_returns_normalized_anchor_shape(self):
        self.session.memory_anchors = None
        db.session.commit()
        
        d = self.session.to_dict()
        self.assertIn('memory_anchors', d)
        self.assertEqual(d['memory_anchors']['current_goal'], None)
        self.assertEqual(d['memory_anchors']['open_clues'], [])

        self.session.memory_anchors = {"current_goal": "Goal"}
        db.session.commit()
        d2 = self.session.to_dict()
        self.assertEqual(d2['memory_anchors']['current_goal'], "Goal")
        self.assertEqual(d2['memory_anchors']['open_clues'], [])

    def test_compile_missing_campaign_raises_error(self):
        with self.assertRaises(MemoryPipelineError) as ctx:
            compile_staged_memory_patch({}, {}, {})
        self.assertEqual(ctx.exception.stage, 'compilation')
        self.assertEqual(ctx.exception.code, 'missing_campaign')


if __name__ == '__main__':
    unittest.main()

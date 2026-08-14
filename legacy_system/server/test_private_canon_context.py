import unittest
from unittest.mock import patch

from services import dm_tools


class PrivateCanonContextTests(unittest.TestCase):
    def test_public_fact_referencing_hidden_entity_is_not_projected(self):
        graph = {
            'entities': [
                {'id': 'bram_harlow', 'visibility': 'party_known'},
                {'id': 'lockhouse_ledger', 'visibility': 'dm_private'},
            ],
            'facts': [
                {
                    'id': 'unsafe',
                    'visibility': 'party_known',
                    'text': 'Bram lied about the forged ledger.',
                    'entity_ids': ['bram_harlow', 'lockhouse_ledger'],
                },
                {
                    'id': 'safe',
                    'visibility': 'party_known',
                    'text': 'Bram denied wrongdoing.',
                    'entity_ids': ['bram_harlow'],
                },
            ],
        }
        with patch.object(dm_tools, '_world_json', return_value=(object(), graph, {}, {})), patch.object(
            dm_tools, '_explicit_revealed_facet_ids', return_value=set()
        ):
            facts = dm_tools._established_public_facts(object())
        self.assertEqual([fact['id'] for fact in facts], ['safe'])

    def test_legacy_string_threads_are_not_public_projections(self):
        state = {
            'open_threads': [
                'Investigate the lockhouse [private: hidden explanation]',
                {
                    'id': 'thread_1',
                    'visibility': 'party_known',
                    'text': 'Investigate the lockhouse.',
                    'source_facet_ids': ['fact:public:clue'],
                },
            ],
        }
        with patch.object(dm_tools, '_world_json', return_value=(object(), {}, state, {})):
            threads = dm_tools._open_public_threads(object())
        self.assertEqual(threads, [{
            'id': 'thread_1',
            'text': 'Investigate the lockhouse.',
            'source_facet_ids': ['fact:public:clue'],
        }])

    def test_private_scene_interpretation_does_not_enter_public_projection(self):
        scene = {
            'location_id': 'ferry_landing',
            'location_name': 'Glassmere Ferry Landing',
            'immediate_tension': 'Bram looks at the lockhouse because of a forged ledger.',
        }
        projection = dm_tools._public_scene_projection(scene)
        self.assertEqual(projection['location_id'], 'ferry_landing')
        self.assertNotIn('immediate_tension', projection)

    def test_private_facets_remain_available_as_canonical_dm_facts(self):
        facets = [
            {
                'id': 'fact:private:ledger',
                'kind': 'fact_text',
                'canonical_text': 'The lockhouse ledger was forged.',
                'subject_id': 'ledger',
                'visibility': 'dm_private',
            },
            {
                'id': 'fact:public:landing',
                'kind': 'fact_text',
                'canonical_text': 'Water rises at the landing.',
                'subject_id': 'landing',
                'visibility': 'party_known',
            },
        ]
        with patch.object(dm_tools, '_disclosure_item_facets', return_value=facets):
            private = dm_tools._canonical_private_facts(object())
        self.assertEqual(len(private), 1)
        self.assertEqual(private[0]['truth'], 'The lockhouse ledger was forged.')
        self.assertEqual(private[0]['disclosure_state'], 'hidden')


if __name__ == '__main__':
    unittest.main()

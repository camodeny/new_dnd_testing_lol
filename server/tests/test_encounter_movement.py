import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from services.encounter_movement import evaluate_cover


class EncounterMovementTest(unittest.TestCase):
    def test_cover_depends_on_direction_and_intervening_obstacle(self):
        setup = {
            'terrain_zones': [],
            'obstacles': [{
                'label': 'Round Table',
                'kind': 'cover',
                'movement_effect': 'provides_cover',
                'cover_type': 'half',
                'rect': {'col': 4, 'row': 4, 'width': 2, 'height': 2},
            }],
        }

        east_to_west = evaluate_cover(setup, attacker_col=8, attacker_row=5, target_col=2, target_row=5)
        west_to_south = evaluate_cover(setup, attacker_col=1, attacker_row=1, target_col=2, target_row=8)

        self.assertEqual(east_to_west['cover_type'], 'half')
        self.assertEqual(east_to_west['providers'][0]['label'], 'Round Table')
        self.assertEqual(west_to_south['cover_type'], 'none')

    def test_blocking_wall_grants_full_cover(self):
        setup = {
            'terrain_zones': [],
            'obstacles': [{
                'label': 'Stone Wall',
                'kind': 'wall',
                'movement_effect': 'blocks_movement',
                'rect': {'col': 4, 'row': 1, 'width': 1, 'height': 6},
            }],
        }

        result = evaluate_cover(setup, attacker_col=1, attacker_row=3, target_col=7, target_row=3)

        self.assertEqual(result['cover_type'], 'full')
        self.assertEqual(result['providers'][0]['label'], 'Stone Wall')

    def test_broad_cover_terrain_zone_is_ignored_as_imprecise(self):
        setup = {
            'terrain_zones': [{
                'label': 'Crowded Tavern Floor',
                'kind': 'cover',
                'rect': {'col': 1, 'row': 2, 'width': 8, 'height': 5},
            }],
            'obstacles': [],
        }

        result = evaluate_cover(setup, attacker_col=0, attacker_row=4, target_col=10, target_row=4)

        self.assertEqual(result['cover_type'], 'none')


if __name__ == '__main__':
    unittest.main()

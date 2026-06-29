import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ.setdefault('DATABASE_URL', 'sqlite:///:memory:')

from services.world_service import sanitize_public_intro


class SanitizePublicIntroTest(unittest.TestCase):
    def test_party_hook_is_not_trimmed(self):
        campaign = SimpleNamespace(name='Test Campaign', description='Short description.')
        long_hook = (
            'The party meets in the lantern market after a public call for skilled help. '
            + ('A courier keeps weaving through the crowd without stopping ' * 12)
        ).strip()
        raw_intro = {
            'party_hook': long_hook,
        }

        intro = sanitize_public_intro(raw_intro, campaign)

        self.assertEqual(intro['party_hook'], long_hook)

    def test_party_hook_without_sentence_boundary_uses_fallback(self):
        campaign = SimpleNamespace(name='Test Campaign', description='Short description.')
        raw_intro = {
            'party_hook': ' '.join(['breathless'] * 100),
        }

        intro = sanitize_public_intro(raw_intro, campaign)

        self.assertEqual(intro['party_hook'], raw_intro['party_hook'])


if __name__ == '__main__':
    unittest.main()

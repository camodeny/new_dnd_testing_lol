import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ['DATABASE_URL'] = 'sqlite:///:memory:'

from app import app


class AppRouteTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_root_path = app.root_path
        app.root_path = self.temp_dir.name

        static_dir = Path(self.temp_dir.name) / 'static'
        (static_dir / 'assets').mkdir(parents=True)
        (static_dir / 'index.html').write_text('<!doctype html><div id="root"></div>', encoding='utf-8')
        (static_dir / 'assets' / 'app.js').write_text('console.log("ok")', encoding='utf-8')

        self.client = app.test_client()

    def tearDown(self):
        app.root_path = self.old_root_path
        self.temp_dir.cleanup()

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

    def test_missing_api_routes_stay_json_404s(self):
        response = self.client.get('/api/not-real')

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json(), {'error': 'Not found'})


if __name__ == '__main__':
    unittest.main()

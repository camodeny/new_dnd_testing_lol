import os
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ['DATABASE_URL'] = 'sqlite:///:memory:'

from app import app
from auth import generate_token
from models import db, Campaign, CampaignInvite, CampaignMember, User


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
        app.root_path = self.old_root_path
        with app.app_context():
            db.session.remove()
        self.temp_dir.cleanup()

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
            campaign = Campaign.query.get(campaign_id)
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
            owner_id = Campaign.query.get(campaign_id).user_id
            owner_token = generate_token(owner_id)

        response = self.client.get(
            f'/api/campaigns/{campaign_id}/invites',
            headers={'Authorization': f'Bearer {owner_token}'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['invite']['code'], 'COWJVBID')
        with app.app_context():
            self.assertEqual(CampaignInvite.query.filter_by(campaign_id=campaign_id).count(), 1)


if __name__ == '__main__':
    unittest.main()

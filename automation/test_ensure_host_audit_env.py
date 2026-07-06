import importlib.util
import os
import pathlib
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER_DIR = ROOT / 'server'
HELPER_PATH = ROOT / 'automation' / 'ensure_host_audit_env.py'


def load_helper():
    spec = importlib.util.spec_from_file_location('ensure_host_audit_env', HELPER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EnsureHostAuditEnvTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db_path = pathlib.Path(self.tempdir.name) / 'test.db'
        self.original_env = {
            'DATABASE_URL': os.environ.get('DATABASE_URL'),
            'SECRET_KEY': os.environ.get('SECRET_KEY'),
            'LLM_CAMPAIGN_ENV_FILE': os.environ.get('LLM_CAMPAIGN_ENV_FILE'),
        }
        os.environ['DATABASE_URL'] = f'sqlite:///{self.db_path}'
        os.environ['SECRET_KEY'] = 'test-secret'
        os.environ['LLM_CAMPAIGN_ENV_FILE'] = str(pathlib.Path(self.tempdir.name) / 'host.env')
        if str(SERVER_DIR) not in os.sys.path:
            os.sys.path.insert(0, str(SERVER_DIR))
        from app import create_app
        from models import User, db

        self.create_app = create_app
        self.User = User
        self.db = db
        self.helper = load_helper()
        self.app = self.create_app()
        with self.app.app_context():
            self.db.drop_all()
            self.db.create_all()
            user = self.User(username='owner', email='owner@example.com')
            user.set_password('pw')
            self.db.session.add(user)
            self.db.session.commit()

    def tearDown(self):
        for key, value in self.original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_writes_env_file_and_rotates_same_label(self):
        env_path = pathlib.Path(self.tempdir.name) / 'audit.env'
        with self.app.app_context():
            first = self.helper.ensure_host_audit_env(
                username='owner',
                label='Deployed Host Audit',
                env_file=env_path,
                api_base='http://127.0.0.1:5001',
            )
            second = self.helper.ensure_host_audit_env(
                username='owner',
                label='Deployed Host Audit',
                env_file=env_path,
                api_base='http://127.0.0.1:5001',
            )
            rows = self.helper.UserAutomationKey.query.filter_by(user_id=first['user_id']).all()

        self.assertEqual(len(rows), 1)
        self.assertEqual(first['rotated_keys'], 0)
        self.assertEqual(second['rotated_keys'], 1)
        content = env_path.read_text(encoding='utf-8')
        self.assertIn('DND_API_BASE=http://127.0.0.1:5001', content)
        self.assertIn('DND_OWNER_API_KEY=dndauto_', content)


if __name__ == '__main__':
    unittest.main()

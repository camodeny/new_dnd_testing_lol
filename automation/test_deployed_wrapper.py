import os
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
WRAPPER = ROOT / 'automation' / 'automationctl.sh'


def run_wrapper(wrapper, args, *, cwd=None, env=None):
    return subprocess.run(
        ['sh', str(wrapper)] + args,
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        capture_output=True,
    )


class DeployedWrapperTest(unittest.TestCase):
    """Installed-path wrapper root resolution for the deployed image.

    The image installs `/usr/local/bin/dnd-automationctl` as a symlink to
    `/app/automation/automationctl.sh`. The wrapper must resolve the real
    application root instead of deriving it from the symlink destination.
    """

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.temp = pathlib.Path(self.tempdir.name)
        self.app = self.temp / 'app'
        self.app.mkdir(parents=True)
        self.bin = self.temp / 'usr' / 'local' / 'bin'
        self.bin.mkdir(parents=True)
        (self.app / 'automation').symlink_to(
            ROOT / 'automation', target_is_directory=True,
        )
        (self.bin / 'dnd-automationctl').symlink_to(
            self.app / 'automation' / 'automationctl.sh',
        )

    def env(self, **overrides):
        env = {
            'PATH': '/usr/bin:/bin:/usr/local/bin',
            'HOME': str(self.temp),
        }
        env.update(overrides)
        return env

    def test_installed_wrapper_resolves_root_outside_repo_directory(self):
        result = run_wrapper(
            self.bin / 'dnd-automationctl',
            ['--help'],
            cwd=self.temp,
            env=self.env(),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('usage:', result.stdout)
        self.assertNotIn('cannot open', result.stderr)
        self.assertNotIn('No such file', result.stderr)

    def test_installed_wrapper_loads_real_loader_not_cwd_dependent(self):
        env = self.env()
        env['LLM_CAMPAIGN_ENV_FILE'] = str(self.temp / 'deployed' / 'llm_campaign.env')
        pathlib.Path(self.temp / 'deployed').mkdir()
        (self.temp / 'deployed' / 'llm_campaign.env').write_text(
            'DND_OWNER_API_KEY=owner-key-from-deployed-env\n',
            encoding='utf-8',
        )
        result = run_wrapper(
            self.bin / 'dnd-automationctl',
            ['run', 'status', '--run-id', '40', '--api-base', 'http://127.0.0.1:1'],
            cwd=self.temp,
            env=env,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('failed:', result.stderr)
        self.assertNotIn('automation control env file not found', result.stderr)
        self.assertNotIn('cannot open', result.stderr)

    def test_missing_env_diagnostic(self):
        missing = str(self.temp / 'missing' / 'llm_campaign.env')
        result = run_wrapper(
            self.bin / 'dnd-automationctl',
            ['--help'],
            cwd=self.temp,
            env=self.env(LLM_CAMPAIGN_ENV_FILE=missing),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('automation control env file not found', result.stderr)
        self.assertIn(missing, result.stderr)
        self.assertIn('dnd-ensure-host-audit-env', result.stderr)

    def test_local_repository_invocation_still_works(self):
        result = run_wrapper(WRAPPER, ['--help'], cwd=ROOT, env=self.env())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('usage:', result.stdout)

    def test_automation_root_override_is_honored(self):
        fake = self.temp / 'fake-root'
        fake_automation = fake / 'automation'
        fake_automation.mkdir(parents=True)
        (fake_automation / 'load_llm_campaign_env.sh').write_text('', encoding='utf-8')
        (fake_automation / 'automationctl.py').write_text(
            'import sys\nprint("AUTOMATION_ROOT_OVERRIDE_OK")\nsys.exit(0)\n',
            encoding='utf-8',
        )
        result = run_wrapper(
            self.bin / 'dnd-automationctl',
            [],
            cwd=self.temp,
            env=self.env(AUTOMATION_ROOT=str(fake)),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('AUTOMATION_ROOT_OVERRIDE_OK', result.stdout)


if __name__ == '__main__':
    unittest.main()

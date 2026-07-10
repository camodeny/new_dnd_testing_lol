import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
LOADER = ROOT / 'automation' / 'load_llm_campaign_env.sh'


class LoadLlmCampaignEnvTest(unittest.TestCase):
    def test_loads_project_provider_settings_before_automation_control_settings(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = pathlib.Path(temp_dir)
            app_env = temp_path / 'app.env'
            automation_env = temp_path / 'automation.env'
            app_env.write_text(
                'LLM_PROVIDER=opencode_go\n'
                'OPENCODE_GO_API_KEY=test-opencode-key\n'
                'OPENCODE_GO_MODEL=deepseek-v4-flash\n',
                encoding='utf-8',
            )
            automation_env.write_text(
                'DND_API_BASE=http://127.0.0.1:5889\n'
                'DND_OWNER_API_KEY=test-owner-key\n',
                encoding='utf-8',
            )
            result = subprocess.run(
                [
                    'sh',
                    '-c',
                    '. "$1"; printf "%s|%s|%s|%s|%s" '
                    '"$LLM_PROVIDER" "$OPENCODE_GO_API_KEY" "$OPENCODE_GO_MODEL" '
                    '"$DND_API_BASE" "$DND_OWNER_API_KEY"',
                    'sh',
                    str(LOADER),
                ],
                env={
                    'PATH': '/usr/bin:/bin',
                    'ROOT': str(ROOT),
                    'DND_APP_ENV_FILE': str(app_env),
                    'LLM_CAMPAIGN_ENV_FILE': str(automation_env),
                },
                text=True,
                capture_output=True,
                check=True,
            )

        self.assertEqual(
            result.stdout,
            'opencode_go|test-opencode-key|deepseek-v4-flash|http://127.0.0.1:5889|test-owner-key',
        )


if __name__ == '__main__':
    unittest.main()

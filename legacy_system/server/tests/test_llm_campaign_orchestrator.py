import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch


SERVER_DIR = os.path.dirname(os.path.dirname(__file__))
AUTOMATION_DIR = os.path.abspath(os.path.join(SERVER_DIR, "..", "automation"))
sys.path.insert(0, AUTOMATION_DIR)

from run_llm_campaign_orchestrator import SYSTEM_PROMPT, build_prompt  # noqa: E402
from run_autonomous_llm_campaign import wait_for_opening_dm  # noqa: E402


class LlmCampaignOrchestratorPromptTests(unittest.TestCase):
    def test_prior_session_proposal_memory_is_explicitly_non_actionable(self):
        manifest = {
            "llm_players": [{
                "llm_player": {"id": 14, "user_id": 15, "label": "Audit Player"},
                "character": {"id": 21, "name": "Grashnak"},
            }],
        }
        prompt = build_prompt(
            manifest,
            {
                "id": 14,
                "name": "Audit",
                "description": "SHEET TEST: pay for an old writ and request another deduction",
                "seed": "seed",
            },
            {"world": {"world_state": {"immediate_tension": "Old writ payment proposal pending"}}},
            {
                "id": 15,
                "started_at": "now",
                "messages": [{"id": 124, "role": "dm", "content": "The Exchange begins. What do you do?"}],
                "pending_roll_requests": [],
            },
            manifest["llm_players"][0],
            [],
            16,
        )

        self.assertIn("Only pending_sheet_proposals below are actionable", prompt)
        self.assertIn('"pending_sheet_proposals": []', prompt)
        self.assertNotIn("Old writ payment proposal pending", prompt)
        self.assertNotIn("SHEET TEST", prompt)
        self.assertIn("References to prior transactions or proposals in campaign memory are historical", SYSTEM_PROMPT)

    def test_autonomous_runner_waits_for_visible_opening_dm(self):
        sessions = [
            {"id": 18, "is_active": True, "messages": []},
            {"id": 18, "is_active": True, "messages": [{"id": 137, "role": "dm", "content": "What do you do?"}]},
        ]
        args = SimpleNamespace(session_start_timeout=10, message_window=16, poll_interval=0.1)

        with (
            patch("run_autonomous_llm_campaign.fetch_session", side_effect=sessions) as fetch,
            patch("run_autonomous_llm_campaign.time.sleep") as sleep,
        ):
            result = wait_for_opening_dm(args, {"session": {"id": 18}})

        self.assertEqual(result["messages"][0]["id"], 137)
        self.assertEqual(fetch.call_count, 2)
        sleep.assert_called_once_with(0.1)


if __name__ == "__main__":
    unittest.main()

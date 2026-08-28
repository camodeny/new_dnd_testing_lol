import os
import sys
import unittest


sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from services.session_rolls import normalize_roll_request, normalize_roll_result, parse_roll_message  # noqa: E402


class SessionRollContractTests(unittest.TestCase):
    def test_normalizes_typed_request(self):
        request = normalize_roll_request({
            "request_id": "roll_12",
            "requested_user_id": "7",
            "character_id": 9,
            "roll_kind": "check",
            "ability_or_skill": "Investigation",
            "label": "Investigation check",
            "advantage_state": "advantage",
            "reason_public": "Inspect the seal.",
            "dc_private": "15",
        })
        self.assertEqual(request["requested_user_id"], 7)
        self.assertEqual(request["dc_private"], 15)

    def test_parses_existing_frontend_roll_format(self):
        result = parse_roll_message(
            "[Roll: Investigation check] total: 18 | rolls: 14 | mod: 4 | sides: 20"
        )
        self.assertEqual(result, {
            "label": "Investigation check",
            "total": 18,
            "rolls": [14],
            "modifier": 4,
            "sides": 20,
        })

    def test_rejects_impossible_structured_die_values(self):
        with self.assertRaises(ValueError):
            normalize_roll_result({
                "label": "Investigation check",
                "total": 99,
                "rolls": [99],
                "modifier": 0,
                "sides": 20,
            })

    def test_does_not_ambiguously_correlate_multiple_rolls(self):
        content = (
            "[Roll: Perception check] total: 12 | rolls: 10 | mod: 2 | sides: 20\n"
            "[Roll: Investigation check] total: 18 | rolls: 14 | mod: 4 | sides: 20"
        )
        self.assertIsNone(parse_roll_message(content))


if __name__ == "__main__":
    unittest.main()

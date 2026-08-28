import os
import sys
import unittest


sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from services.session_memory_agent import _allocate_new_fact_id  # noqa: E402


class SessionMemoryFactIdTests(unittest.TestCase):
    def test_new_fact_ids_do_not_restart_at_one_each_turn(self):
        known = {"fact_1", "fact_2", "fact_3", "fact_named_clue"}
        allocated = set()

        first = _allocate_new_fact_id(known, allocated)
        allocated.add(first)
        second = _allocate_new_fact_id(known, allocated)

        self.assertEqual(first, "fact_4")
        self.assertEqual(second, "fact_5")

    def test_named_fact_ids_do_not_break_numeric_allocation(self):
        self.assertEqual(
            _allocate_new_fact_id({"fact_missing_diver", "fact_7"}),
            "fact_1",
        )


if __name__ == "__main__":
    unittest.main()

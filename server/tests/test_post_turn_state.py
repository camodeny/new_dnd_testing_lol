import os
import sys
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from services.post_turn_state import derive_post_turn_state, event_matches_turn
from services import dm_turns


class _Event:
    def __init__(self, trace_id=None, parent_trace_id=None, payload=None):
        import json
        self.trace_id = trace_id
        self.parent_trace_id = parent_trace_id
        self.payload = json.dumps(payload or {})


class PostTurnStateInvariantTest(unittest.TestCase):
    def state(self, post='pending', memory='pending', clock='pending', **kwargs):
        return derive_post_turn_state(
            post,
            memory,
            clock,
            correlation_id=kwargs.pop('correlation_id', 'turn:1'),
            **kwargs,
        )

    def test_full_success_has_one_consistent_completion_representation(self):
        state = self.state('complete', 'complete', 'complete', durable_memory_write=True)
        self.assertTrue(state['post_turn_complete'])
        self.assertTrue(state['post_turn_resolved'])
        self.assertEqual(state['post_turn_status'], 'complete')
        self.assertTrue(state['durable_write_present'])
        self.assertEqual(state['post_turn_invariant_violations'], [])

    def test_memory_and_clock_failures_are_unambiguous(self):
        memory_failure = self.state('error', 'error', 'skipped')
        clock_failure = self.state('error', 'complete', 'error')
        self.assertEqual(memory_failure['post_turn_status'], 'partial')
        self.assertFalse(memory_failure['post_turn_complete'])
        self.assertEqual(clock_failure['post_turn_status'], 'partial')
        self.assertFalse(clock_failure['post_turn_complete'])

    def test_partial_success_and_timeout_are_terminal_but_not_complete(self):
        partial = self.state('complete', 'complete', 'pending')
        timed_out = self.state('timed_out', 'complete', 'pending')
        self.assertEqual(partial['post_turn_status'], 'partial')
        self.assertTrue(partial['post_turn_resolved'])
        self.assertEqual(timed_out['post_turn_status'], 'timed_out')
        self.assertTrue(timed_out['post_turn_resolved'])
        self.assertFalse(timed_out['post_turn_complete'])

    def test_reconciled_retry_is_a_success_terminal_state(self):
        state = self.state('reconciled', 'complete', 'complete')
        self.assertEqual(state['post_turn_status'], 'reconciled')
        self.assertTrue(state['post_turn_complete'])

    def test_committed_memory_patch_v2_counts_as_durable_write(self):
        state = self.state('complete', 'complete', 'complete', durable_memory_write=True)
        self.assertTrue(state['durable_memory_write'])
        self.assertTrue(state['durable_write_present'])

    def test_duplicate_or_late_stage_evidence_cannot_change_terminal_state(self):
        first = self.state('complete', 'complete', 'complete', durable_memory_write=True)
        duplicate = self.state('complete', 'complete', 'complete', durable_memory_write=True)
        self.assertEqual(first, duplicate)

    def test_late_error_and_duplicate_visible_event_cannot_regress_success(self):
        turn = SimpleNamespace(
            campaign_id=1,
            session_id=2,
            player_message_id=10,
            dm_message_id=11,
            trace_id='turn:1',
            status='speak',
            post_turn_status='complete',
            memory_status='complete',
            clock_status='complete',
            error_text=None,
            started_at=datetime(2026, 1, 1),
            visible_completed_at=datetime(2026, 1, 1),
            finished_at=datetime(2026, 1, 1),
            generation_duration_ms=1,
            full_duration_ms=2,
            post_turn_revision=3,
        )
        with patch.object(dm_turns, '_get_turn', return_value=turn):
            dm_turns.mark_session_dm_turn_error(1, 2, 10, 'turn:1', 'late failure')
            dm_turns.mark_session_dm_turn_visible(1, 2, 10, 'turn:1', 'speak', dm_message_id=11)
        self.assertEqual(turn.post_turn_status, 'complete')
        self.assertEqual(turn.memory_status, 'complete')
        self.assertIsNone(turn.error_text)

    def test_retry_can_promote_a_failed_turn_to_complete(self):
        turn = SimpleNamespace(
            dm_message_id=11,
            trace_id='turn:1',
            status='speak',
            post_turn_status='error',
            memory_status='error',
            clock_status='skipped',
            error_text='first attempt failed',
            started_at=datetime(2026, 1, 1),
            finished_at=None,
            full_duration_ms=None,
            post_turn_revision=None,
        )
        with (
            patch.object(dm_turns, '_get_turn', return_value=turn),
            patch.object(dm_turns, '_record_invariant_incident'),
        ):
            dm_turns.mark_session_dm_turn_post_turn_complete(
                10,
                memory_status='complete',
                clock_status='complete',
                post_turn_revision=4,
            )
        self.assertEqual(turn.post_turn_status, 'complete')
        self.assertEqual(turn.post_turn_revision, 4)
        self.assertIsNone(turn.error_text)

    def test_missing_correlation_id_is_an_invariant_violation(self):
        state = self.state('complete', 'complete', 'complete', correlation_id=None)
        self.assertIn('missing post-turn correlation id', state['post_turn_invariant_violations'])

    def test_child_trace_and_message_identity_correlation(self):
        child = _Event(trace_id='memory:child', parent_trace_id='turn:1')
        retry = _Event(trace_id='retry:2', payload={'source_player_message_id': 10})
        self.assertTrue(event_matches_turn(child, trace_id='turn:1', player_message_id=10))
        self.assertTrue(event_matches_turn(retry, trace_id='turn:1', player_message_id=10))
        self.assertFalse(event_matches_turn(retry, trace_id='turn:1', player_message_id=11))


if __name__ == '__main__':
    unittest.main()

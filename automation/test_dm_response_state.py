import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
import dm_response_state


class DmResponseStateUnitTests(unittest.TestCase):
    def test_visible_output_state_matrix(self):
        cases = [
            ({'status': 'speak'}, True),
            ({'status': 'silent'}, True),
            ({'status': 'empty'}, True),
            ({'status': 'pending', 'dm_message_id': 42}, True),
            ({'status': 'pending'}, False),
            (None, False),
            ([], False),
        ]
        for status, expected in cases:
            with self.subTest(status=status):
                self.assertIs(
                    dm_response_state.dm_turn_has_visible_output(status),
                    expected,
                )

    def test_post_turn_resolution_state_matrix(self):
        cases = [
            ({'status': 'silent'}, True),
            ({'status': 'empty'}, True),
            ({'status': 'speak', 'post_turn_status': 'complete'}, True),
            ({'status': 'speak', 'post_turn_status': 'error'}, True),
            ({'status': 'speak', 'post_turn_complete': True}, True),
            ({'status': 'speak', 'post_turn_complete': False}, False),
            ({'status': 'speak', 'post_turn_status': 'pending'}, False),
            ({'status': 'speak'}, True),
            (None, False),
        ]
        for status, expected in cases:
            with self.subTest(status=status):
                self.assertIs(
                    dm_response_state.dm_turn_post_turn_resolved(status),
                    expected,
                )

    def test_full_resolution_state_matrix(self):
        cases = [
            ({'status': 'pending'}, False),
            ({'status': 'silent'}, True),
            ({'status': 'empty'}, True),
            ({'status': 'speak', 'post_turn_status': 'complete'}, True),
            ({'status': 'speak', 'post_turn_status': 'error'}, True),
            ({'status': 'speak', 'post_turn_status': 'pending'}, False),
            ({'status': 'speak'}, True),
        ]
        for status, expected in cases:
            with self.subTest(status=status):
                self.assertIs(
                    dm_response_state.dm_turn_fully_resolved(status),
                    expected,
                )

    def test_timeout_classification_matrix(self):
        cases = [
            ({'status': 'pending'}, 'visible', 'dm_visible_response_timeout'),
            (
                {'status': 'speak', 'post_turn_status': 'pending'},
                'post_turn',
                'dm_post_turn_timeout',
            ),
            (
                {'status': 'speak', 'post_turn_status': 'error'},
                'post_turn',
                'dm_post_turn_error',
            ),
            ({}, 'unknown', 'dm_response_timeout'),
        ]
        for status, phase, expected in cases:
            with self.subTest(phase=phase, status=status):
                self.assertEqual(
                    dm_response_state.classify_timeout(status, phase),
                    expected,
                )

    def test_build_timeout_evidence_includes_all_fields(self):
        status = {
            'player_message_id': 100,
            'dm_message_id': 200,
            'status': 'speak',
            'post_turn_status': 'pending',
            'memory_status': 'pending',
            'clock_status': 'pending',
            'started_at': '2026-01-01T00:00:00',
            'visible_completed_at': '2026-01-01T00:05:00',
            'finished_at': None,
            'generation_duration_ms': 300000,
            'full_duration_ms': None,
        }
        evidence = dm_response_state.build_timeout_evidence(status, 'post_turn')
        self.assertEqual(evidence['player_message_id'], 100)
        self.assertEqual(evidence['dm_message_id'], 200)
        self.assertEqual(evidence['status'], 'speak')
        self.assertEqual(evidence['post_turn_status'], 'pending')
        self.assertEqual(evidence['started_at'], '2026-01-01T00:00:00')
        self.assertEqual(evidence['visible_completed_at'], '2026-01-01T00:05:00')
        self.assertEqual(evidence['generation_duration_ms'], 300000)

    def test_timeout_resolution_matrix(self):
        cases = [
            ((200.0, 100.0, 300.0), (200.0, 100.0)),
            ((200.0, None, 300.0), (200.0, 720.0)),
            ((None, 100.0, 300.0), (300.0, 100.0)),
            ((None, None, 120.0), (120.0, 720.0)),
            ((None, None, None), (720.0, 720.0)),
            ((None, 600.0, 120.0), (120.0, 600.0)),
            ((600.0, None, None), (600.0, 720.0)),
            ((None, 600.0, None), (720.0, 600.0)),
        ]
        for values, expected in cases:
            with self.subTest(values=values):
                args = SimpleNamespace(
                    dm_visible_response_timeout=values[0],
                    dm_post_turn_timeout=values[1],
                    dm_response_timeout=values[2],
                )
                self.assertEqual(
                    dm_response_state.resolve_dm_response_timeouts(args),
                    expected,
                )


class DmResponseStatePhasedTests(unittest.TestCase):
    """Integration-style tests for the phased wait_for_dm_response state machine."""

    @staticmethod
    def _monotonic_counter(start=0.0, step=1.0):
        """Return a callable that yields incrementing timestamps (never runs out)."""
        val = [start]
        def tick():
            current = val[0]
            val[0] = current + step
            return current
        return tick

    def test_visible_output_and_post_turn_complete_before_deadline(self):
        """Normal success: visible output then post-turn completion, both within deadlines."""
        statuses = iter([
            {'status': 'speak', 'player_message_id': 1, 'dm_message_id': 2},
            {'status': 'speak', 'player_message_id': 1, 'dm_message_id': 2,
             'post_turn_status': 'complete'},
        ])

        with patch.object(time, 'sleep') as sleep, \
                patch.object(time, 'monotonic', side_effect=self._monotonic_counter()):
            result, timed_out, timeout_phase = dm_response_state.wait_for_dm_response(
                fetch_status_fn=lambda: next(statuses),
                maybe_heartbeat_fn=lambda: None,
                visible_timeout=300.0,
                post_turn_timeout=180.0,
                poll_interval=5.0,
            )

        self.assertFalse(timed_out)
        self.assertIsNone(timeout_phase)
        self.assertEqual(result['status'], 'speak')
        self.assertEqual(result['post_turn_status'], 'complete')

    def test_visible_output_completes_near_deadline_post_turn_within_new_deadline(self):
        """Visible output appears; post-turn finishes under its own budget even if combined time exceeds old limit."""
        phase1_pending_then_speak = iter([
            {'status': 'pending', 'player_message_id': 1},
            {'status': 'speak', 'player_message_id': 1, 'dm_message_id': 2},
        ])
        phase2_pending_then_complete = iter([
            {'status': 'speak', 'player_message_id': 1, 'dm_message_id': 2,
             'post_turn_status': 'pending'},
            {'status': 'speak', 'player_message_id': 1, 'dm_message_id': 2,
             'post_turn_status': 'complete'},
        ])
        phase_tracker = {'phase': 0}
        def fetch_status():
            if phase_tracker['phase'] == 0:
                try:
                    return next(phase1_pending_then_speak)
                except StopIteration:
                    phase_tracker['phase'] = 1
            return next(phase2_pending_then_complete)

        with patch.object(time, 'sleep') as sleep, \
                patch.object(time, 'monotonic', side_effect=self._monotonic_counter()):
            result, timed_out, timeout_phase = dm_response_state.wait_for_dm_response(
                fetch_status_fn=fetch_status,
                maybe_heartbeat_fn=lambda: None,
                visible_timeout=300.0,
                post_turn_timeout=180.0,
                poll_interval=5.0,
            )

        self.assertFalse(timed_out)
        self.assertIsNone(timeout_phase)
        self.assertEqual(result['post_turn_status'], 'complete')

    def test_visible_output_never_appears(self):
        """Phase 1 times out because no visible output is ever produced."""
        pending = {'status': 'pending', 'player_message_id': 1}

        with patch.object(time, 'sleep') as sleep, \
                patch.object(time, 'monotonic', side_effect=self._monotonic_counter()):
            result, timed_out, timeout_phase = dm_response_state.wait_for_dm_response(
                fetch_status_fn=lambda: pending,
                maybe_heartbeat_fn=lambda: None,
                visible_timeout=0.001,
                post_turn_timeout=180.0,
                poll_interval=1.0,
            )

        self.assertTrue(timed_out)
        self.assertEqual(timeout_phase, 'visible')
        self.assertEqual(result['status'], 'pending')

    def test_visible_output_appears_but_post_turn_never_completes(self):
        """Phase 2 times out because post-turn work never finishes."""
        speak_status = {'status': 'speak', 'player_message_id': 1, 'dm_message_id': 2,
                        'post_turn_status': 'pending'}

        with patch.object(time, 'sleep') as sleep, \
                patch.object(time, 'monotonic', side_effect=self._monotonic_counter()):
            result, timed_out, timeout_phase = dm_response_state.wait_for_dm_response(
                fetch_status_fn=lambda: speak_status,
                maybe_heartbeat_fn=lambda: None,
                visible_timeout=300.0,
                post_turn_timeout=0.001,
                poll_interval=1.0,
            )

        self.assertTrue(timed_out)
        self.assertEqual(timeout_phase, 'post_turn')
        self.assertEqual(result['status'], 'speak')
        self.assertEqual(result['dm_message_id'], 2)

    def test_final_authoritative_read_avoids_false_visible_timeout(self):
        """If the visible deadline fires but the final authoritative read shows visible output, proceed."""
        pending_then_speak = iter([
            {'status': 'pending', 'player_message_id': 1},
            {'status': 'speak', 'player_message_id': 1, 'dm_message_id': 2},
            {'status': 'speak', 'player_message_id': 1, 'dm_message_id': 2,
             'post_turn_status': 'complete'},
        ])

        with patch.object(time, 'sleep') as sleep, \
                patch.object(time, 'monotonic', side_effect=self._monotonic_counter()):
            result, timed_out, timeout_phase = dm_response_state.wait_for_dm_response(
                fetch_status_fn=lambda: next(pending_then_speak),
                maybe_heartbeat_fn=lambda: None,
                visible_timeout=0.001,
                post_turn_timeout=180.0,
                poll_interval=1.0,
            )

        self.assertFalse(timed_out)
        self.assertIsNone(timeout_phase)
        self.assertEqual(result['post_turn_status'], 'complete')

    def test_final_authoritative_read_avoids_false_post_turn_timeout(self):
        """If post-turn deadline expires but final authoritative read shows completion, succeed."""
        visible_then_pending_then_complete = iter([
            {'status': 'speak', 'player_message_id': 1, 'dm_message_id': 2},
            {'status': 'speak', 'player_message_id': 1, 'dm_message_id': 2,
             'post_turn_status': 'pending'},
            {'status': 'speak', 'player_message_id': 1, 'dm_message_id': 2,
             'post_turn_status': 'complete'},
        ])

        with patch.object(time, 'sleep') as sleep, \
                patch.object(time, 'monotonic', side_effect=self._monotonic_counter()):
            result, timed_out, timeout_phase = dm_response_state.wait_for_dm_response(
                fetch_status_fn=lambda: next(visible_then_pending_then_complete),
                maybe_heartbeat_fn=lambda: None,
                visible_timeout=300.0,
                post_turn_timeout=0.001,
                poll_interval=1.0,
            )

        self.assertFalse(timed_out)
        self.assertIsNone(timeout_phase)
        self.assertEqual(result['post_turn_status'], 'complete')

    def test_post_turn_error_classified_as_resolved_not_timeout(self):
        """post_turn_status='error' is a resolved state, not a timeout."""
        statuses = iter([
            {'status': 'speak', 'player_message_id': 1, 'dm_message_id': 2},
            {'status': 'speak', 'player_message_id': 1, 'dm_message_id': 2,
             'post_turn_status': 'error', 'turn_error': 'memory write failed'},
        ])

        with patch.object(time, 'sleep') as sleep, \
                patch.object(time, 'monotonic', side_effect=self._monotonic_counter()):
            result, timed_out, timeout_phase = dm_response_state.wait_for_dm_response(
                fetch_status_fn=lambda: next(statuses),
                maybe_heartbeat_fn=lambda: None,
                visible_timeout=300.0,
                post_turn_timeout=180.0,
                poll_interval=5.0,
            )

        self.assertFalse(timed_out)
        self.assertIsNone(timeout_phase)
        self.assertEqual(result['post_turn_status'], 'error')

    def test_api_errors_during_polling_are_tolerated(self):
        """Transient API errors should be tolerated and retried until the deadline."""
        call_count = [0]

        def flaky_fetch():
            call_count[0] += 1
            if call_count[0] <= 2:
                raise RuntimeError('transient API error')
            return {'status': 'speak', 'player_message_id': 1, 'post_turn_status': 'complete'}

        with patch.object(time, 'sleep') as sleep, \
                patch.object(time, 'monotonic', side_effect=self._monotonic_counter()):
            result, timed_out, timeout_phase = dm_response_state.wait_for_dm_response(
                fetch_status_fn=flaky_fetch,
                maybe_heartbeat_fn=lambda: None,
                visible_timeout=300.0,
                post_turn_timeout=180.0,
                poll_interval=5.0,
            )

        self.assertFalse(timed_out)
        self.assertGreaterEqual(call_count[0], 3)

    def test_maybe_heartbeat_called_during_polling(self):
        """The heartbeat function is called during the polling loop."""
        heartbeat_calls = []

        statuses = iter([
            {'status': 'speak', 'player_message_id': 1},
            {'status': 'speak', 'player_message_id': 1, 'post_turn_status': 'complete'},
        ])

        def track_heartbeat():
            heartbeat_calls.append(1)

        with patch.object(time, 'sleep') as sleep, \
                patch.object(time, 'monotonic', side_effect=self._monotonic_counter()):
            dm_response_state.wait_for_dm_response(
                fetch_status_fn=lambda: next(statuses),
                maybe_heartbeat_fn=track_heartbeat,
                visible_timeout=300.0,
                post_turn_timeout=180.0,
                poll_interval=5.0,
            )

        self.assertGreaterEqual(len(heartbeat_calls), 2)


class DmResponseStateReviewRegressionTests(unittest.TestCase):
    def test_generation_error_is_terminal_without_waiting_for_timeout(self):
        status = {'status': 'error', 'turn_error': 'provider failed'}
        result, timed_out, phase = dm_response_state.wait_for_dm_response(
            lambda: status,
            lambda: None,
            300,
            180,
            1,
        )
        self.assertFalse(timed_out)
        self.assertIsNone(phase)
        self.assertEqual(result, status)

    def test_non_transient_programming_error_propagates(self):
        with self.assertRaises(ValueError):
            dm_response_state.wait_for_dm_response(
                lambda: (_ for _ in ()).throw(ValueError('bug')),
                lambda: None,
                300,
                180,
                1,
                transient_error_types=(RuntimeError,),
            )

    def test_transient_error_is_reported_with_phase(self):
        attempts = 0
        complete = {'status': 'speak', 'post_turn_status': 'complete'}
        reported = []

        def fetch():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError('temporary')
            return complete

        with patch.object(time, 'sleep'):
            result, timed_out, phase = dm_response_state.wait_for_dm_response(
                fetch,
                lambda: None,
                300,
                180,
                1,
                transient_error_types=(RuntimeError,),
                on_poll_error_fn=lambda exc, poll_phase: reported.append((str(exc), poll_phase)),
            )

        self.assertFalse(timed_out)
        self.assertIsNone(phase)
        self.assertEqual(result['post_turn_status'], 'complete')
        self.assertEqual(reported, [('temporary', 'visible')])


if __name__ == '__main__':
    unittest.main()

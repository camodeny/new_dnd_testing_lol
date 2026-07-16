import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
import dm_response_state


class DmResponseStateUnitTests(unittest.TestCase):
    def test_dm_turn_has_visible_output_speak(self):
        self.assertTrue(dm_response_state.dm_turn_has_visible_output({'status': 'speak'}))

    def test_dm_turn_has_visible_output_silent(self):
        self.assertTrue(dm_response_state.dm_turn_has_visible_output({'status': 'silent'}))

    def test_dm_turn_has_visible_output_empty(self):
        self.assertTrue(dm_response_state.dm_turn_has_visible_output({'status': 'empty'}))

    def test_dm_turn_has_visible_output_dm_message_id(self):
        self.assertTrue(dm_response_state.dm_turn_has_visible_output({'status': 'pending', 'dm_message_id': 42}))

    def test_dm_turn_has_visible_output_pending_no_message(self):
        self.assertFalse(dm_response_state.dm_turn_has_visible_output({'status': 'pending'}))

    def test_dm_turn_has_visible_output_none_input(self):
        self.assertFalse(dm_response_state.dm_turn_has_visible_output(None))

    def test_dm_turn_has_visible_output_non_dict(self):
        self.assertFalse(dm_response_state.dm_turn_has_visible_output([]))

    def test_dm_turn_post_turn_resolved_silent(self):
        self.assertTrue(dm_response_state.dm_turn_post_turn_resolved({'status': 'silent'}))

    def test_dm_turn_post_turn_resolved_empty(self):
        self.assertTrue(dm_response_state.dm_turn_post_turn_resolved({'status': 'empty'}))

    def test_dm_turn_post_turn_resolved_complete(self):
        self.assertTrue(dm_response_state.dm_turn_post_turn_resolved(
            {'status': 'speak', 'post_turn_status': 'complete'}
        ))

    def test_dm_turn_post_turn_resolved_error(self):
        self.assertTrue(dm_response_state.dm_turn_post_turn_resolved(
            {'status': 'speak', 'post_turn_status': 'error'}
        ))

    def test_dm_turn_post_turn_resolved_post_turn_complete_flag(self):
        self.assertTrue(dm_response_state.dm_turn_post_turn_resolved(
            {'status': 'speak', 'post_turn_complete': True}
        ))

    def test_dm_turn_post_turn_resolved_pending(self):
        self.assertFalse(dm_response_state.dm_turn_post_turn_resolved(
            {'status': 'speak', 'post_turn_status': 'pending'}
        ))

    def test_dm_turn_post_turn_resolved_no_post_turn_info(self):
        self.assertTrue(dm_response_state.dm_turn_post_turn_resolved({'status': 'speak'}))

    def test_dm_turn_post_turn_resolved_none(self):
        self.assertFalse(dm_response_state.dm_turn_post_turn_resolved(None))

    def test_dm_turn_fully_resolved_pending(self):
        self.assertFalse(dm_response_state.dm_turn_fully_resolved({'status': 'pending'}))

    def test_dm_turn_fully_resolved_silent(self):
        self.assertTrue(dm_response_state.dm_turn_fully_resolved({'status': 'silent'}))

    def test_dm_turn_fully_resolved_empty(self):
        self.assertTrue(dm_response_state.dm_turn_fully_resolved({'status': 'empty'}))

    def test_dm_turn_fully_resolved_speak_complete(self):
        self.assertTrue(dm_response_state.dm_turn_fully_resolved(
            {'status': 'speak', 'post_turn_status': 'complete'}
        ))

    def test_dm_turn_fully_resolved_speak_error(self):
        self.assertTrue(dm_response_state.dm_turn_fully_resolved(
            {'status': 'speak', 'post_turn_status': 'error'}
        ))

    def test_dm_turn_fully_resolved_speak_pending(self):
        self.assertFalse(dm_response_state.dm_turn_fully_resolved(
            {'status': 'speak', 'post_turn_status': 'pending'}
        ))

    def test_dm_turn_fully_resolved_backward_compat_no_post_turn(self):
        self.assertTrue(dm_response_state.dm_turn_fully_resolved({'status': 'speak'}))

    def test_classify_timeout_visible(self):
        self.assertEqual(
            dm_response_state.classify_timeout({'status': 'pending'}, 'visible'),
            'dm_visible_response_timeout',
        )

    def test_classify_timeout_post_turn_pending(self):
        self.assertEqual(
            dm_response_state.classify_timeout(
                {'status': 'speak', 'post_turn_status': 'pending'}, 'post_turn'
            ),
            'dm_post_turn_timeout',
        )

    def test_classify_timeout_post_turn_error(self):
        self.assertEqual(
            dm_response_state.classify_timeout(
                {'status': 'speak', 'post_turn_status': 'error'}, 'post_turn'
            ),
            'dm_post_turn_error',
        )

    def test_classify_timeout_unknown_phase(self):
        self.assertEqual(
            dm_response_state.classify_timeout({}, 'unknown'),
            'dm_response_timeout',
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

    def test_resolve_dm_response_timeouts_both_specified(self):
        args = SimpleNamespace(
            dm_visible_response_timeout=200.0,
            dm_post_turn_timeout=100.0,
            dm_response_timeout=300.0,
        )
        visible, post_turn = dm_response_state.resolve_dm_response_timeouts(args)
        self.assertEqual(visible, 200.0)
        self.assertEqual(post_turn, 100.0)

    def test_resolve_dm_response_timeouts_only_visible(self):
        args = SimpleNamespace(
            dm_visible_response_timeout=200.0,
            dm_post_turn_timeout=None,
            dm_response_timeout=300.0,
        )
        visible, post_turn = dm_response_state.resolve_dm_response_timeouts(args)
        self.assertEqual(visible, 200.0)
        self.assertEqual(post_turn, 720.0)

    def test_resolve_dm_response_timeouts_only_post_turn(self):
        args = SimpleNamespace(
            dm_visible_response_timeout=None,
            dm_post_turn_timeout=100.0,
            dm_response_timeout=300.0,
        )
        visible, post_turn = dm_response_state.resolve_dm_response_timeouts(args)
        self.assertEqual(visible, 300.0)
        self.assertEqual(post_turn, 100.0)

    def test_resolve_dm_response_timeouts_legacy_fallback(self):
        args = SimpleNamespace(
            dm_visible_response_timeout=None,
            dm_post_turn_timeout=None,
            dm_response_timeout=120.0,
        )
        visible, post_turn = dm_response_state.resolve_dm_response_timeouts(args)
        self.assertEqual(visible, 120.0)
        self.assertEqual(post_turn, 720.0)

    def test_resolve_dm_response_timeouts_all_none_defaults(self):
        args = SimpleNamespace(
            dm_visible_response_timeout=None,
            dm_post_turn_timeout=None,
            dm_response_timeout=None,
        )
        visible, post_turn = dm_response_state.resolve_dm_response_timeouts(args)
        self.assertEqual(visible, 720.0)
        self.assertEqual(post_turn, 720.0)

    def test_resolve_dm_response_timeouts_legacy_visible_with_independent_post_turn(self):
        args = SimpleNamespace(
            dm_visible_response_timeout=None,
            dm_post_turn_timeout=600.0,
            dm_response_timeout=120.0,
        )
        visible, post_turn = dm_response_state.resolve_dm_response_timeouts(args)
        self.assertEqual(visible, 120.0)
        self.assertEqual(post_turn, 600.0)

    def test_resolve_dm_response_timeouts_both_specified_via_env_overrides(self):
        args = SimpleNamespace(
            dm_visible_response_timeout=720.0,
            dm_post_turn_timeout=720.0,
            dm_response_timeout=300.0,
        )
        visible, post_turn = dm_response_state.resolve_dm_response_timeouts(args)
        self.assertEqual(visible, 720.0)
        self.assertEqual(post_turn, 720.0)

    def test_resolve_dm_response_timeouts_visible_override_post_turn_defaults(self):
        args = SimpleNamespace(
            dm_visible_response_timeout=600.0,
            dm_post_turn_timeout=None,
            dm_response_timeout=None,
        )
        visible, post_turn = dm_response_state.resolve_dm_response_timeouts(args)
        self.assertEqual(visible, 600.0)
        self.assertEqual(post_turn, 720.0)

    def test_resolve_dm_response_timeouts_post_turn_override_visible_defaults(self):
        args = SimpleNamespace(
            dm_visible_response_timeout=None,
            dm_post_turn_timeout=600.0,
            dm_response_timeout=None,
        )
        visible, post_turn = dm_response_state.resolve_dm_response_timeouts(args)
        self.assertEqual(visible, 720.0)
        self.assertEqual(post_turn, 600.0)


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
        statuses = iter([
            RuntimeError('temporary'),
            {'status': 'speak', 'post_turn_status': 'complete'},
        ])
        reported = []

        def fetch():
            item = next(statuses)
            if isinstance(item, Exception):
                raise item
            return item

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

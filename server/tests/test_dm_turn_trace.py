import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from services.dm_turn_trace import dm_turn_traces_from_audit_events


class DmTurnTraceTest(unittest.TestCase):
    def test_derives_phase_timings_and_visible_result(self):
        events = [
            {
                'id': 1,
                'event_type': 'player_input_stored',
                'created_at': '2026-06-02T12:00:00',
                'payload': {'message': {'id': 42, 'role': 'player', 'content': 'I open the door.'}},
            },
            {
                'id': 2,
                'event_type': 'session_hot_context_read',
                'created_at': '2026-06-02T12:00:01',
                'trace_id': 'session_dm:session_7:message_42',
                'trace_label': 'session_dm: session 7',
                'actor': 'server',
                'payload': {},
            },
            {
                'id': 3,
                'event_type': 'model_request',
                'created_at': '2026-06-02T12:00:03',
                'trace_id': 'session_dm:session_7:message_42',
                'trace_label': 'session_dm: session 7',
                'actor': 'session_dm',
                'payload': {'operation': 'session_dm_response'},
            },
            {
                'id': 4,
                'event_type': 'dm_tool_execution',
                'created_at': '2026-06-02T12:00:04',
                'trace_id': 'session_dm:session_7:message_42',
                'trace_label': 'session_dm: session 7',
                'actor': 'session_dm',
                'payload': {'tool_name': 'read_character_sheet', 'result': {'ok': True}},
            },
            {
                'id': 5,
                'event_type': 'dm_output_stored',
                'created_at': '2026-06-02T12:00:06',
                'trace_id': 'session_dm:session_7:message_42',
                'trace_label': 'session_dm: session 7',
                'actor': 'session_dm',
                'payload': {'session_id': 7, 'message': {'role': 'dm', 'content': 'The door groans open.'}},
            },
        ]

        traces = dm_turn_traces_from_audit_events(events)

        self.assertEqual(len(traces), 1)
        trace = traces[0]
        self.assertEqual(trace['session_id'], 7)
        self.assertEqual(trace['player_message_id'], 42)
        self.assertEqual(trace['player_input']['content'], 'I open the door.')
        self.assertEqual(trace['visible_result']['mode'], 'speak')
        self.assertEqual(trace['visible_result']['content'], 'The door groans open.')
        self.assertEqual(trace['total_ms'], 6000)
        self.assertIn('read_character_sheet', trace['tool_names'])
        phases = {phase['phase']: phase for phase in trace['phases']}
        self.assertEqual(phases['player_input']['duration_ms'], 1000)
        self.assertEqual(phases['hot_context']['duration_ms'], 2000)
        self.assertEqual(phases['main_model']['duration_ms'], 1000)
        self.assertEqual(phases['tools']['duration_ms'], 2000)
        self.assertEqual(phases['visible_output']['duration_ms'], 0)
        timeline = {event['event_id']: event for event in trace['timeline']}
        self.assertEqual(timeline[3]['delta_ms'], 2000)
        self.assertEqual(timeline[3]['duration_ms'], 1000)

    def test_infers_session_support_events_without_trace_ids(self):
        events = [
            {
                'id': 1,
                'event_type': 'player_input_stored',
                'created_at': '2026-06-02T12:00:00',
                'payload': {'message': {'id': 42, 'role': 'player', 'content': 'I listen.'}},
            },
            {
                'id': 2,
                'event_type': 'session_hot_context_read',
                'created_at': '2026-06-02T12:00:01',
                'actor': 'server',
                'payload': {},
            },
            {
                'id': 3,
                'event_type': 'model_request',
                'created_at': '2026-06-02T12:00:02',
                'trace_id': 'session_dm:session_7:message_42',
                'trace_label': 'session_dm: session 7',
                'actor': 'session_dm',
                'payload': {'operation': 'session_dm_response'},
            },
            {
                'id': 4,
                'event_type': 'dm_output_stored',
                'created_at': '2026-06-02T12:00:04',
                'trace_id': 'session_dm:session_7:message_42',
                'trace_label': 'session_dm: session 7',
                'actor': 'session_dm',
                'payload': {'session_id': 7, 'message': {'role': 'dm', 'content': 'You hear distant water.'}},
            },
            {
                'id': 5,
                'event_type': 'client_response_sent',
                'source': 'session_messages',
                'created_at': '2026-06-02T12:00:05',
                'actor': 'server',
                'payload': {},
            },
        ]

        traces = dm_turn_traces_from_audit_events(events)

        self.assertEqual(len(traces), 1)
        trace = traces[0]
        self.assertEqual(trace['event_ids'], [1, 2, 3, 4, 5])
        phases = {phase['phase']: phase for phase in trace['phases']}
        self.assertIn('hot_context', phases)
        self.assertIn('client_response', phases)

    def test_does_not_duplicate_child_session_traces_as_roots(self):
        events = [
            {
                'id': 1,
                'event_type': 'player_input_stored',
                'created_at': '2026-06-02T12:00:00',
                'payload': {'message': {'id': 42, 'role': 'player', 'content': 'Check the lock.'}},
            },
            {
                'id': 2,
                'event_type': 'model_request',
                'created_at': '2026-06-02T12:00:01',
                'trace_id': 'session_dm:session_7:message_42',
                'trace_label': 'session_dm: session 7',
                'actor': 'session_dm',
                'payload': {'operation': 'session_dm_response'},
            },
            {
                'id': 3,
                'event_type': 'model_request',
                'created_at': '2026-06-02T12:00:02',
                'trace_id': 'session_dm:session_7:message_42:session_7:message_42',
                'parent_trace_id': 'session_dm:session_7:message_42',
                'trace_label': 'child session trace',
                'actor': 'session_dm',
                'payload': {'operation': 'guard_check'},
            },
            {
                'id': 4,
                'event_type': 'dm_output_stored',
                'created_at': '2026-06-02T12:00:03',
                'trace_id': 'session_dm:session_7:message_42',
                'trace_label': 'session_dm: session 7',
                'actor': 'session_dm',
                'payload': {'session_id': 7, 'message': {'role': 'dm', 'content': 'It is locked.'}},
            },
        ]

        traces = dm_turn_traces_from_audit_events(events)

        self.assertEqual(len(traces), 1)
        self.assertEqual(traces[0]['child_trace_ids'], ['session_dm:session_7:message_42:session_7:message_42'])
        self.assertEqual(traces[0]['event_ids'], [1, 2, 3, 4])

    def test_handles_mixed_naive_and_aware_timestamps(self):
        events = [
            {
                'id': 1,
                'event_type': 'player_input_stored',
                'created_at': '2026-06-02T12:00:00',
                'payload': {'message': {'id': 42, 'role': 'player', 'content': 'Wait.'}},
            },
            {
                'id': 2,
                'event_type': 'model_request',
                'created_at': '2026-06-02T12:00:01Z',
                'trace_id': 'session_dm:session_7:message_42',
                'trace_label': 'session_dm: session 7',
                'actor': 'session_dm',
                'payload': {'operation': 'session_dm_response'},
            },
            {
                'id': 3,
                'event_type': 'dm_silence_chosen',
                'created_at': '2026-06-02T12:00:03+00:00',
                'trace_id': 'session_dm:session_7:message_42',
                'trace_label': 'session_dm: session 7',
                'actor': 'session_dm',
                'payload': {'decision': {'reason': 'No response needed.'}},
            },
        ]

        traces = dm_turn_traces_from_audit_events(events)

        self.assertEqual(len(traces), 1)
        self.assertEqual(traces[0]['total_ms'], 3000)
        phases = {phase['phase']: phase for phase in traces[0]['phases']}
        self.assertEqual(phases['player_input']['duration_ms'], 1000)
        self.assertEqual(phases['main_model']['duration_ms'], 2000)


if __name__ == '__main__':
    unittest.main()

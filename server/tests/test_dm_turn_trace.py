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
        self.assertIn('hot_context', phases)
        self.assertIn('main_model', phases)
        self.assertIn('tools', phases)
        self.assertIn('visible_output', phases)


if __name__ == '__main__':
    unittest.main()

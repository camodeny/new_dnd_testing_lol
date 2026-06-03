import re
from collections import defaultdict
from datetime import datetime, timezone


SESSION_DM_TRACE_RE = re.compile(r'(?:^|:)session_(?P<session_id>\d+):(?P<kind>message|opening)(?:_(?P<message_id>\d+))?')

GUARD_EVENT_HINTS = (
    'guard',
    'checker',
    'spoiler',
    'mechanics',
    'format',
    'pc_control',
    'private_output',
    'json_contract',
)


def _event_dict(event):
    if isinstance(event, dict):
        return event
    return event.to_dict()


def _parse_dt(value):
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        except ValueError:
            return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _ms_between(start, end):
    if not start or not end:
        return 0
    return max(0, int((end - start).total_seconds() * 1000))


def _payload(event):
    payload = event.get('payload')
    return payload if isinstance(payload, dict) else {}


def _operation(event):
    return str(_payload(event).get('operation') or '').strip()


def _phase_for_event(event):
    event_type = str(event.get('event_type') or '')
    actor = str(event.get('actor') or '')
    source = str(event.get('source') or '')
    operation = _operation(event)
    text = ' '.join([event_type, actor, source, operation]).lower()

    if event_type == 'player_input_stored':
        return 'player_input'
    if event_type == 'session_hot_context_read':
        return 'hot_context'
    if event_type == 'dm_tool_execution':
        return 'tools'
    if event_type in {'dm_output_stored', 'dm_silence_chosen', 'dm_output_empty'}:
        return 'visible_output'
    if event_type == 'client_response_sent':
        return 'client_response'
    if 'memory' in text:
        return 'memory'
    if event_type in {'model_request', 'model_response'}:
        if 'preflight' in text:
            return 'preflight'
        if any(hint in text for hint in GUARD_EVENT_HINTS):
            return 'guards'
        if actor == 'session_dm' or operation == 'session_dm_response':
            return 'main_model'
        return 'model_other'
    if any(hint in text for hint in GUARD_EVENT_HINTS):
        return 'guards'
    return 'other'


def _phase_label(phase):
    return {
        'player_input': 'Player input',
        'hot_context': 'Hot context',
        'preflight': 'Preflight',
        'main_model': 'Main DM model',
        'tools': 'Tools',
        'guards': 'Guards',
        'visible_output': 'Visible output',
        'client_response': 'Client response',
        'memory': 'Memory writer',
        'model_other': 'Other model call',
        'other': 'Other',
    }.get(phase, phase.replace('_', ' ').title())


def _trace_match(trace_id):
    return SESSION_DM_TRACE_RE.search(str(trace_id or ''))


def _message_id_from_player_input(event):
    payload = _payload(event)
    message = payload.get('message') if isinstance(payload.get('message'), dict) else {}
    return message.get('id') or payload.get('player_message_id')


def _is_session_support_event(entry):
    event_type = entry.get('event_type')
    source = entry.get('source')
    if event_type in {'session_hot_context_read', 'knowledge_graph_read'}:
        return True
    if event_type == 'client_response_sent' and source in {'session_messages', 'campaign_sessions'}:
        return True
    return False


def _with_inferred_session_trace_links(entries):
    linked = [dict(entry) for entry in (entries or [])]
    pending = []
    active_session_trace = None
    active_session_label = None

    def apply_trace(entry, trace_id, trace_label):
        entry['trace_id'] = trace_id
        entry['trace_label'] = trace_label or entry.get('trace_label')

    for entry in linked:
        trace_id = entry.get('trace_id')
        if _trace_match(trace_id):
            active_session_trace = trace_id
            active_session_label = entry.get('trace_label')
            for pending_entry in pending:
                apply_trace(pending_entry, active_session_trace, active_session_label)
            pending = []
            continue

        if trace_id or not _is_session_support_event(entry):
            continue

        if active_session_trace and entry.get('event_type') == 'client_response_sent':
            apply_trace(entry, active_session_trace, active_session_label)
        else:
            pending.append(entry)

    return linked


def _extract_tool_names(events):
    names = []
    for event in events:
        payload = _payload(event)
        if payload.get('tool_name'):
            names.append(payload.get('tool_name'))
        raw_response = payload.get('raw_response') if isinstance(payload.get('raw_response'), dict) else {}
        choices = raw_response.get('choices') if isinstance(raw_response, dict) else []
        first = choices[0] if choices and isinstance(choices[0], dict) else {}
        message = first.get('message') if isinstance(first.get('message'), dict) else {}
        tool_calls = payload.get('tool_calls') or message.get('tool_calls') or []
        for tool_call in tool_calls:
            function = tool_call.get('function') if isinstance(tool_call, dict) else {}
            name = function.get('name') if isinstance(function, dict) else None
            if name:
                names.append(name)
    result = []
    seen = set()
    for name in names:
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    return result


def _extract_visible_result(events):
    for event in reversed(events):
        event_type = event.get('event_type')
        payload = _payload(event)
        if event_type == 'dm_output_stored':
            message = payload.get('message') if isinstance(payload.get('message'), dict) else {}
            return {
                'mode': 'speak',
                'content': message.get('content') or payload.get('content') or '',
            }
        if event_type == 'dm_silence_chosen':
            decision = payload.get('decision') if isinstance(payload.get('decision'), dict) else {}
            return {
                'mode': 'silent',
                'reason': decision.get('reason') or '',
            }
        if event_type == 'dm_output_empty':
            return {'mode': 'empty', 'reason': 'DM returned no visible content.'}
    return {'mode': 'unknown'}


def _extract_player_input(events):
    for event in events:
        if event.get('event_type') != 'player_input_stored':
            continue
        payload = _payload(event)
        message = payload.get('message') if isinstance(payload.get('message'), dict) else {}
        return {
            'message_id': _message_id_from_player_input(event),
            'role': message.get('role'),
            'content': message.get('content') or '',
            'username': message.get('username'),
        }
    return None


def _timeline_and_totals(events):
    timeline = []
    phase_totals = defaultdict(int)
    phase_counts = defaultdict(int)
    parsed_times = [_parse_dt(event.get('created_at')) for event in events]

    for index, event in enumerate(events):
        at = parsed_times[index]
        previous_at = parsed_times[index - 1] if index > 0 else None
        next_at = parsed_times[index + 1] if index + 1 < len(parsed_times) else None
        phase = _phase_for_event(event)
        delta_ms = _ms_between(previous_at, at) if previous_at else 0
        phase_duration_ms = _ms_between(at, next_at) if next_at else 0

        phase_totals[phase] += phase_duration_ms
        phase_counts[phase] += 1
        timeline.append({
            'event_id': event.get('id'),
            'event_type': event.get('event_type'),
            'phase': phase,
            'phase_label': _phase_label(phase),
            'delta_ms': delta_ms,
            'duration_ms': phase_duration_ms,
            'created_at': event.get('created_at'),
            'actor': event.get('actor'),
            'source': event.get('source'),
            'summary': event.get('summary'),
            'operation': _operation(event),
        })

    phases = [
        {
            'phase': phase,
            'label': _phase_label(phase),
            'duration_ms': duration,
            'event_count': phase_counts[phase],
        }
        for phase, duration in sorted(phase_totals.items(), key=lambda item: item[1], reverse=True)
    ]
    return timeline, phases


def _descendant_trace_ids(root_trace_id, children_by_parent):
    found = []
    stack = list(children_by_parent.get(root_trace_id, []))
    seen = set()
    while stack:
        trace_id = stack.pop(0)
        if trace_id in seen:
            continue
        seen.add(trace_id)
        found.append(trace_id)
        stack.extend(children_by_parent.get(trace_id, []))
    return found


def dm_turn_traces_from_audit_events(audit_events, limit=50):
    raw_entries = sorted(
        [_event_dict(event) for event in audit_events],
        key=lambda event: (event.get('id') or 0),
    )
    entries = _with_inferred_session_trace_links(raw_entries)

    by_trace = defaultdict(list)
    children_by_parent = defaultdict(list)
    parent_by_trace = {}
    player_inputs_by_message_id = {}

    for entry in entries:
        trace_id = entry.get('trace_id')
        if trace_id:
            by_trace[trace_id].append(entry)
        parent_trace_id = entry.get('parent_trace_id')
        if trace_id and parent_trace_id and trace_id != parent_trace_id:
            children_by_parent[parent_trace_id].append(trace_id)
            parent_by_trace.setdefault(trace_id, parent_trace_id)
        if entry.get('event_type') == 'player_input_stored':
            message_id = _message_id_from_player_input(entry)
            if message_id is not None:
                player_inputs_by_message_id[int(message_id)] = entry

    root_trace_ids = []
    for trace_id in by_trace:
        match = _trace_match(trace_id)
        if not match:
            continue
        parent_trace_id = parent_by_trace.get(trace_id)
        if parent_trace_id and parent_trace_id in by_trace:
            continue
        if str(by_trace[trace_id][0].get('actor') or '') == 'session_memory_writer':
            continue
        if trace_id not in root_trace_ids:
            root_trace_ids.append(trace_id)

    traces = []
    for trace_id in root_trace_ids:
        match = _trace_match(trace_id)
        child_trace_ids = _descendant_trace_ids(trace_id, children_by_parent)
        trace_events = []
        player_message_id = int(match.group('message_id')) if match and match.group('message_id') else None
        if player_message_id in player_inputs_by_message_id:
            trace_events.append(player_inputs_by_message_id[player_message_id])
        for related_id in [trace_id, *child_trace_ids]:
            trace_events.extend(by_trace.get(related_id, []))
        trace_events.sort(key=lambda event: (event.get('id') or 0))
        if not trace_events:
            continue

        start_at = _parse_dt(trace_events[0].get('created_at'))
        end_at = _parse_dt(trace_events[-1].get('created_at'))
        timeline, phases = _timeline_and_totals(trace_events)
        model_request_count = sum(1 for event in trace_events if event.get('event_type') == 'model_request')
        guard_count = sum(1 for event in trace_events if _phase_for_event(event) == 'guards')
        memory_event_count = sum(1 for event in trace_events if _phase_for_event(event) == 'memory')
        tool_names = _extract_tool_names(trace_events)

        traces.append({
            'trace_id': trace_id,
            'trace_label': trace_events[0].get('trace_label') or trace_id,
            'session_id': int(match.group('session_id')) if match and match.group('session_id') else None,
            'player_message_id': player_message_id,
            'turn_kind': match.group('kind') if match else 'unknown',
            'started_at': trace_events[0].get('created_at'),
            'ended_at': trace_events[-1].get('created_at'),
            'total_ms': _ms_between(start_at, end_at),
            'event_count': len(trace_events),
            'model_request_count': model_request_count,
            'guard_event_count': guard_count,
            'memory_event_count': memory_event_count,
            'tool_names': tool_names,
            'player_input': _extract_player_input(trace_events),
            'visible_result': _extract_visible_result(trace_events),
            'phases': phases,
            'timeline': timeline,
            'child_trace_ids': child_trace_ids,
            'event_ids': [event.get('id') for event in trace_events],
        })

    traces.sort(key=lambda trace: trace.get('started_at') or '', reverse=True)
    return traces[:limit]

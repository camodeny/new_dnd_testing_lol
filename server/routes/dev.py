import json
import random
import re

from flask import Blueprint, jsonify, request

from auth import token_required
from dev_data import DEV_CHARACTER_TEMPLATES
from models import (
    Campaign,
    CampaignAuditEvent,
    CampaignClock,
    CampaignSession,
    Character,
    CharacterPlanningMessage,
    NPCActor,
    WorldEvent,
    db,
)
from openrouter import (
    build_opening_scene_messages,
    build_planning_dm_messages,
    build_planning_summary_messages,
    build_session_dm_tool_messages,
    build_world_genesis_messages,
    get_openrouter_settings,
    reset_openrouter_model,
    set_openrouter_model,
)
from services.character_service import (
    build_character_from_data,
    character_full_dict,
    update_character_relations,
)
from services.audit_service import AGENT_ACTORS, infer_audit_role
from services.campaign_service import get_or_404
from services.dm_tools import DM_TOOL_DEFINITIONS, build_session_hot_context, context_manifest
from services.planning_service import (
    get_campaign_members,
    planning_context,
    summary_dict_for_read,
    visible_planning_payload,
)
from services.world_service import dm_world_context, world_public_payload

dev_bp = Blueprint('dev', __name__)


@dev_bp.route('/api/dev/character', methods=['POST'])
@token_required
def create_dev_character(current_user):
    template = random.choice(DEV_CHARACTER_TEMPLATES)
    character = Character(user_id=current_user.id)
    build_character_from_data(character, template)

    db.session.add(character)
    db.session.flush()
    update_character_relations(character, template)
    db.session.commit()

    return jsonify({'message': 'Dev character created', 'character': character_full_dict(character)}), 201


@dev_bp.route('/api/dev/model', methods=['GET'])
@token_required
def get_dev_model_settings(current_user):
    return jsonify({'settings': get_openrouter_settings()}), 200


@dev_bp.route('/api/dev/model', methods=['PUT'])
@token_required
def update_dev_model_settings(current_user):
    data = request.get_json() or {}
    if data.get('reset'):
        return jsonify({
            'message': 'LLM model reset to environment value',
            'settings': reset_openrouter_model(),
        }), 200

    try:
        settings = set_openrouter_model(data.get('model'))
    except ValueError as err:
        return jsonify({'error': str(err)}), 400

    return jsonify({
        'message': 'LLM model updated',
        'settings': settings,
    }), 200


def _serialize_session(session):
    data = session.to_dict()
    data['messages'] = [message.to_dict() for message in session.messages]
    return data


def _format_event_type(event_type):
    return (event_type or 'event').replace('_', ' ')


def _first_choice_message(raw_response):
    if not isinstance(raw_response, dict):
        return {}
    choices = raw_response.get('choices') or []
    if not choices or not isinstance(choices[0], dict):
        return {}
    message = choices[0].get('message') or {}
    return message if isinstance(message, dict) else {}


def _reasoning_payload(payload):
    raw_response = payload.get('raw_response') if isinstance(payload, dict) else {}
    message = _first_choice_message(raw_response)
    reasoning = payload.get('reasoning') or message.get('reasoning') or message.get('reasoning_content')
    reasoning_details = payload.get('reasoning_details') or message.get('reasoning_details')
    usage = payload.get('reasoning_usage') or {}
    if not usage and isinstance(raw_response, dict):
        usage_data = raw_response.get('usage') or {}
        completion_details = usage_data.get('completion_tokens_details') or {}
        output_details = usage_data.get('output_tokens_details') or {}
        usage = {
            'reasoning_tokens': completion_details.get('reasoning_tokens') or output_details.get('reasoning_tokens'),
            'completion_tokens_details': completion_details,
            'output_tokens_details': output_details,
        }
    return {
        'returned': bool(reasoning or reasoning_details),
        'reasoning': reasoning,
        'reasoning_details': reasoning_details,
        'usage': usage,
    }


def _fallback_trace_id(event_data):
    actor = event_data.get('actor')
    event_type = event_data.get('event_type')
    if event_data.get('trace_id'):
        return event_data.get('trace_id')
    if actor in AGENT_ACTORS or event_type in {'model_request', 'model_response', 'model_error'}:
        return f'actor:{actor or "model"}'
    return None


def _audit_stream_entry(event):
    event_data = event.to_dict()
    payload = event_data.get('payload') or {}
    role = infer_audit_role(event_data.get('event_type'), actor=event_data.get('actor'), audit_role=event_data.get('audit_role'))
    trace_id = _fallback_trace_id(event_data)
    messages = payload.get('messages') if isinstance(payload.get('messages'), list) else []
    reasoning = _reasoning_payload(payload) if event_data.get('event_type') == 'model_response' else None
    raw_response = payload.get('raw_response') if isinstance(payload, dict) else {}
    first_message = _first_choice_message(raw_response)
    tool_calls = payload.get('tool_calls') or first_message.get('tool_calls') or []
    provider = payload.get('provider') or event_data.get('source')
    model = payload.get('model')
    if not model and isinstance(raw_response, dict):
        model = raw_response.get('model')

    label = event_data.get('actor') or role
    if role == 'tools':
        label = _format_event_type(event_data.get('event_type'))

    return {
        'id': event_data['id'],
        'campaign_id': event_data['campaign_id'],
        'event_type': event_data['event_type'],
        'role': role,
        'label': label,
        'source': event_data.get('source'),
        'actor': event_data.get('actor'),
        'summary': event_data.get('summary'),
        'provider': provider,
        'model': model,
        'content': payload.get('content') if isinstance(payload, dict) else None,
        'payload': payload,
        'messages': messages,
        'message_count': len(messages),
        'reasoning': reasoning,
        'finish_reason': payload.get('finish_reason') if isinstance(payload, dict) else None,
        'tool_calls': tool_calls,
        'tool_names': payload.get('tool_names') if isinstance(payload, dict) else [],
        'context_manifest': payload.get('context_manifest') if isinstance(payload, dict) else {},
        'token_estimate': payload.get('token_estimate') if isinstance(payload, dict) else {},
        'usage': payload.get('usage') if isinstance(payload, dict) else {},
        'trace_id': trace_id,
        'parent_trace_id': event_data.get('parent_trace_id'),
        'trace_label': event_data.get('trace_label') or event_data.get('actor') or trace_id,
        'created_at': event_data.get('created_at'),
    }


def _agent_runs_from_stream(stream):
    nodes = {}
    for entry in stream:
        trace_id = entry.get('trace_id')
        if not trace_id:
            continue
        node = nodes.setdefault(trace_id, {
            'trace_id': trace_id,
            'parent_trace_id': entry.get('parent_trace_id'),
            'trace_label': entry.get('trace_label') or trace_id,
            'actor': entry.get('actor'),
            'events': [],
            'children': [],
        })
        if (
            not node.get('parent_trace_id')
            and entry.get('parent_trace_id')
            and entry.get('parent_trace_id') != trace_id
        ):
            node['parent_trace_id'] = entry.get('parent_trace_id')
        if entry.get('actor') in AGENT_ACTORS and node.get('actor') not in AGENT_ACTORS:
            node['actor'] = entry.get('actor')
        elif not node.get('actor') and entry.get('actor'):
            node['actor'] = entry.get('actor')
        node['events'].append(entry)

    for node in nodes.values():
        node['events'].sort(key=lambda item: item.get('id') or 0)

    roots = []
    for node in nodes.values():
        parent_id = node.get('parent_trace_id')
        if parent_id and parent_id in nodes and parent_id != node.get('trace_id'):
            nodes[parent_id]['children'].append(node)
        else:
            roots.append(node)

    def sort_node(node):
        node['children'].sort(key=lambda child: child['events'][0]['id'] if child['events'] else 0)
        for child in node['children']:
            sort_node(child)

    roots.sort(key=lambda node: node['events'][0]['id'] if node['events'] else 0)
    for root in roots:
        sort_node(root)
    return roots


def _with_inferred_session_trace_links(stream):
    linked = [dict(entry) for entry in (stream or [])]
    pending = []
    active_session_trace = None
    active_session_label = None

    def is_session_trace(trace_id):
        return bool(trace_id and re.search(r'(?:^|:)session_\d+:(?:message_\d+|opening)', trace_id))

    def is_session_support_event(entry):
        event_type = entry.get('event_type')
        source = entry.get('source')
        if event_type in {'session_hot_context_read', 'knowledge_graph_read'}:
            return True
        if event_type == 'client_response_sent' and source in {'session_messages', 'campaign_sessions'}:
            return True
        return False

    def apply_trace(entry, trace_id, trace_label):
        entry['trace_id'] = trace_id
        entry['trace_label'] = trace_label or entry.get('trace_label')

    for entry in linked:
        trace_id = entry.get('trace_id')
        if is_session_trace(trace_id):
            active_session_trace = trace_id
            active_session_label = entry.get('trace_label')
            for pending_entry in pending:
                apply_trace(pending_entry, active_session_trace, active_session_label)
            pending = []
            continue

        if trace_id:
            continue

        if not is_session_support_event(entry):
            continue

        if active_session_trace and entry.get('event_type') == 'client_response_sent':
            apply_trace(entry, active_session_trace, active_session_label)
        else:
            pending.append(entry)

    return linked


def _message_link_from_trace(trace_id):
    if not trace_id:
        return None

    session_match = re.search(r'(?:^|:)session_(\d+):message_(\d+)', trace_id)
    if session_match:
        return {
            'lane_id': f'session-{session_match.group(1)}',
            'message_key': f'session-message-{session_match.group(2)}',
        }

    planning_match = re.search(r'(?:^|:)campaign_(\d+):message_(\d+)', trace_id)
    if planning_match:
        return {
            'lane_id': f'planning-user-message-{planning_match.group(2)}',
            'message_key': f'planning-message-{planning_match.group(2)}',
        }

    return None


def _normalize_visible_message(source, message, lane_id, username=None):
    return {
        'key': f'{source}-message-{message.id}',
        'id': message.id,
        'source': source,
        'lane_id': lane_id,
        'role': message.role,
        'user_id': getattr(message, 'user_id', None),
        'username': username,
        'content': message.content,
        'created_at': message.created_at.isoformat() if message.created_at else None,
        'branches': [],
    }


def _branch_operation(events):
    for event in events:
        payload = event.get('payload') or {}
        operation = payload.get('operation')
        if operation:
            return operation
    return None


def _branch_messages(events):
    for event in events:
        if event.get('messages'):
            return event.get('messages') or []
        payload = event.get('payload') or {}
        if isinstance(payload.get('messages'), list):
            return payload.get('messages') or []
    return []


def _branch_response(events):
    for event in events:
        if event.get('event_type') in {
            'model_response',
            'memory_writer_response',
            'draft_output_sent',
            'dm_output_stored',
            'dm_silence_chosen',
            'dm_output_empty',
        }:
            payload = event.get('payload') or {}
            content = event.get('content') or payload.get('content')
            if content:
                return content
            message = payload.get('message') if isinstance(payload.get('message'), dict) else {}
            if message.get('content'):
                return message.get('content')
            decision = payload.get('decision') if isinstance(payload.get('decision'), dict) else {}
            if decision.get('reason'):
                return decision.get('reason')
            if payload.get('patch'):
                return payload.get('patch')
            if payload.get('draft'):
                return payload.get('draft')
    return None


def _json_loads_or_value(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value
    return value


def _event_step_category(event):
    event_type = event.get('event_type') or ''
    actor = event.get('actor') or ''
    if event_type == 'dm_tool_execution':
        return 'tools'
    if 'memory' in actor or event_type.startswith('memory_') or event_type in {'planning_memory_write', 'memory_patch_applied'}:
        return 'memory'
    if event_type in {
        'model_request',
        'model_response',
        'dm_output_stored',
        'dm_silence_chosen',
        'dm_output_empty',
        'draft_output_sent',
    }:
        return 'agents'
    if event.get('role') == 'tools':
        return 'tools'
    return 'agents'


def _reasoning_step(event):
    reasoning = event.get('reasoning') or {}
    if not reasoning.get('returned'):
        return None
    content = reasoning.get('reasoning') or reasoning.get('reasoning_details')
    if not content:
        return None
    return {
        'id': f"{event.get('id')}-reasoning",
        'event_id': event.get('id'),
        'kind': 'model_reasoning',
        'category': _event_step_category(event),
        'title': 'Model reasoning',
        'summary': 'returned by provider',
        'actor': event.get('actor'),
        'provider': event.get('provider'),
        'model': event.get('model'),
        'created_at': event.get('created_at'),
        'content': content,
        'usage': reasoning.get('usage') or {},
    }


def _model_tool_request_step(event):
    tool_calls = event.get('tool_calls') or []
    if not tool_calls:
        return None
    names = []
    for tool_call in tool_calls:
        function = tool_call.get('function') if isinstance(tool_call, dict) else {}
        name = function.get('name') if isinstance(function, dict) else None
        if name:
            names.append(name)
    return {
        'id': f"{event.get('id')}-model-tool-request",
        'event_id': event.get('id'),
        'kind': 'model_tool_request',
        'category': 'agents',
        'title': 'Model tool request',
        'summary': ', '.join(names) if names else f"{len(tool_calls)} tool request{'s' if len(tool_calls) != 1 else ''}",
        'actor': event.get('actor'),
        'provider': event.get('provider'),
        'model': event.get('model'),
        'created_at': event.get('created_at'),
        'tool_calls': tool_calls,
    }


def _tool_call_step(event):
    payload = event.get('payload') or {}
    return {
        'id': f"{event.get('id')}-tool-call",
        'event_id': event.get('id'),
        'kind': 'tool_call',
        'category': 'tools',
        'title': payload.get('tool_name') or 'Tool call',
        'summary': 'called by server',
        'actor': event.get('actor'),
        'created_at': event.get('created_at'),
        'tool_name': payload.get('tool_name'),
        'arguments': payload.get('arguments'),
        'payload': payload,
    }


def _tool_result_step(event):
    payload = event.get('payload') or {}
    return {
        'id': f"{event.get('id')}-tool-result",
        'event_id': event.get('id'),
        'kind': 'tool_result',
        'category': 'tools',
        'title': f"{payload.get('tool_name') or 'Tool'} result",
        'summary': 'mutated state' if payload.get('mutated') else 'read result',
        'actor': event.get('actor'),
        'created_at': event.get('created_at'),
        'tool_name': payload.get('tool_name'),
        'result': payload.get('result'),
        'mutated': payload.get('mutated'),
        'affected_ids': payload.get('affected_ids'),
        'payload': payload,
    }


def _event_step_title(event):
    event_type = event.get('event_type')
    payload = event.get('payload') or {}
    if event_type == 'dm_tool_execution':
        return payload.get('tool_name') or 'Tool execution'
    if event_type == 'model_request':
        return 'Model request'
    if event_type == 'model_response':
        return 'Model response'
    if event_type == 'dm_output_stored':
        return 'Visible chat message'
    if event_type == 'dm_silence_chosen':
        return 'DM stayed silent'
    if event_type == 'dm_output_empty':
        return 'Empty DM output'
    if event_type == 'memory_patch_applied':
        return 'Memory patch applied'
    if event_type == 'memory_writer_request':
        return 'Memory writer request'
    if event_type == 'memory_writer_response':
        return 'Memory writer response'
    if event_type == 'session_hot_context_read':
        return 'Session context read'
    if event_type == 'knowledge_graph_read':
        return 'World context read'
    if event_type == 'client_response_sent':
        return 'Client response sent'
    return _format_event_type(event_type)


def _prompt_message_title(message):
    role = message.get('role') if isinstance(message, dict) else None
    content = str(message.get('content') or '') if isinstance(message, dict) else ''
    if role == 'system':
        if content.startswith('Compact hot context.'):
            return 'Session context'
        if content.startswith('Use the private campaign world memory'):
            return 'Opening instruction'
        return 'System prompt'
    if role == 'assistant':
        return 'Assistant context'
    if role == 'user':
        return 'User message'
    if role == 'tool':
        return 'Tool message'
    return 'Prompt message'


def _prompt_message_step(event, message, index, summary=None):
    role = message.get('role') or 'message'
    return {
        'id': f"{event.get('id')}-prompt-message-{index}",
        'event_id': event.get('id'),
        'kind': 'prompt_message',
        'category': 'agents',
        'title': _prompt_message_title(message),
        'summary': summary or f"#{index + 1} sent to model",
        'actor': event.get('actor'),
        'created_at': event.get('created_at'),
        'content': message.get('content'),
        'prompt_role': role,
        'name': message.get('name'),
        'tool_call_id': message.get('tool_call_id'),
        'tool_calls': message.get('tool_calls') or [],
        'source_message_index': index,
    }


def _prompt_message_steps(event):
    steps = []
    for index, message in enumerate(event.get('messages') or []):
        if not isinstance(message, dict):
            continue
        steps.append(_prompt_message_step(event, message, index))
    return steps


def _agent_setup_prompt_steps(event):
    steps = []
    for index, message in enumerate(event.get('messages') or []):
        if not isinstance(message, dict):
            continue
        if message.get('role') != 'system':
            break
        steps.append(_prompt_message_step(
            event,
            message,
            index,
            summary=f"agent setup from message #{index + 1}",
        ))
    return steps


def _event_step_summary(event):
    event_type = event.get('event_type')
    payload = event.get('payload') or {}
    if event_type == 'dm_tool_execution':
        return 'mutated state' if payload.get('mutated') else 'executed'
    if event.get('tool_calls'):
        return f"{len(event.get('tool_calls') or [])} tool request{'s' if len(event.get('tool_calls') or []) != 1 else ''}"
    operation = payload.get('operation')
    if operation:
        return _format_event_type(operation)
    return event.get('summary')


def _event_step_content(event):
    payload = event.get('payload') or {}
    content = event.get('content') or payload.get('content')
    if content:
        return content
    message = payload.get('message') if isinstance(payload.get('message'), dict) else {}
    if message.get('content'):
        return message.get('content')
    decision = payload.get('decision') if isinstance(payload.get('decision'), dict) else {}
    if decision.get('reason'):
        return decision.get('reason')
    if payload.get('patch'):
        return payload.get('patch')
    if payload.get('draft'):
        return payload.get('draft')
    return None


def _branch_steps(events):
    steps = []
    setup_prompts_added = False
    for event in events:
        reasoning = _reasoning_step(event)
        if reasoning:
            steps.append(reasoning)

        if event.get('event_type') == 'model_request':
            if not setup_prompts_added:
                steps.extend(_agent_setup_prompt_steps(event))
                setup_prompts_added = True

        if event.get('event_type') == 'model_response' and event.get('tool_calls'):
            tool_request = _model_tool_request_step(event)
            if tool_request:
                steps.append(tool_request)
            if not _event_step_content(event):
                continue

        if event.get('event_type') == 'dm_tool_execution':
            steps.append(_tool_call_step(event))
            steps.append(_tool_result_step(event))
            continue

        payload = event.get('payload') or {}
        category = _event_step_category(event)
        step = {
            'id': f"event-{event.get('id')}",
            'event_id': event.get('id'),
            'kind': event.get('event_type'),
            'category': category,
            'title': _event_step_title(event),
            'summary': _event_step_summary(event),
            'actor': event.get('actor'),
            'provider': event.get('provider'),
            'model': event.get('model'),
            'created_at': event.get('created_at'),
            'content': _event_step_content(event),
            'messages': event.get('messages') or [],
            'usage': event.get('usage') or {},
            'reasoning': event.get('reasoning'),
            'finish_reason': event.get('finish_reason'),
            'tool_calls': event.get('tool_calls') or [],
            'tool_name': payload.get('tool_name'),
            'arguments': payload.get('arguments'),
            'result': payload.get('result'),
            'mutated': payload.get('mutated'),
            'affected_ids': payload.get('affected_ids'),
            'payload': payload,
        }
        steps.append(step)
    return steps


def _branch_tool_events(events):
    result = []
    for event in events:
        payload = event.get('payload') or {}
        if event.get('tool_calls'):
            result.append({
                'event_id': event.get('id'),
                'event_type': event.get('event_type'),
                'tool_calls': event.get('tool_calls'),
            })
        if payload.get('tool_name'):
            result.append({
                'event_id': event.get('id'),
                'event_type': event.get('event_type'),
                'tool_name': payload.get('tool_name'),
                'arguments': payload.get('arguments'),
                'result': payload.get('result'),
                'mutated': payload.get('mutated'),
                'affected_ids': payload.get('affected_ids'),
            })
    return result


def _branch_memory_events(events):
    return [
        {
            'event_id': event.get('id'),
            'event_type': event.get('event_type'),
            'summary': event.get('summary'),
            'payload': event.get('payload') or {},
        }
        for event in events
        if str(event.get('event_type') or '').startswith('memory_')
        or event.get('event_type') in {'planning_memory_write', 'memory_patch_applied'}
    ]


def _branch_reasoning(events):
    for event in events:
        reasoning = event.get('reasoning')
        if reasoning and reasoning.get('returned'):
            return reasoning
    return None


def _branch_model_metadata(events):
    for event in events:
        if event.get('model') or event.get('provider'):
            return {
                'provider': event.get('provider'),
                'model': event.get('model'),
            }
    return {'provider': None, 'model': None}


def _branch_from_run(run):
    events = run.get('events') or []
    first_event = events[0] if events else {}
    trace_id = run.get('trace_id')
    model_metadata = _branch_model_metadata(events)
    return {
        'id': trace_id or f'audit-run-{first_event.get("id")}',
        'trace_id': trace_id,
        'parent_trace_id': run.get('parent_trace_id'),
        'trace_label': run.get('trace_label') or trace_id,
        'actor': run.get('actor') or first_event.get('actor'),
        'provider': model_metadata.get('provider'),
        'model': model_metadata.get('model'),
        'operation': _branch_operation(events),
        'role': first_event.get('role') or 'tools',
        'summary': first_event.get('summary') or run.get('trace_label') or trace_id,
        'created_at': first_event.get('created_at'),
        'event_ids': [event.get('id') for event in events],
        'events': events,
        'steps': _branch_steps(events),
        'messages': _branch_messages(events),
        'response': _branch_response(events),
        'tool_events': _branch_tool_events(events),
        'memory_events': _branch_memory_events(events),
        'reasoning': _branch_reasoning(events),
        'link': _message_link_from_trace(trace_id),
        'children': [_branch_from_run(child) for child in (run.get('children') or [])],
    }


def _branch_from_event(event):
    return {
        'id': f'audit-event-{event.get("id")}',
        'trace_id': event.get('trace_id'),
        'parent_trace_id': event.get('parent_trace_id'),
        'trace_label': event.get('trace_label') or event.get('label'),
        'actor': event.get('actor'),
        'provider': event.get('provider'),
        'model': event.get('model'),
        'operation': (event.get('payload') or {}).get('operation'),
        'role': event.get('role') or 'tools',
        'summary': event.get('summary'),
        'created_at': event.get('created_at'),
        'event_ids': [event.get('id')],
        'events': [event],
        'steps': _branch_steps([event]),
        'messages': event.get('messages') or [],
        'response': event.get('content'),
        'tool_events': _branch_tool_events([event]),
        'memory_events': _branch_memory_events([event]),
        'reasoning': event.get('reasoning'),
        'link': _message_link_from_trace(event.get('trace_id')),
        'children': [],
    }


def _attach_branch(lanes_by_id, branch):
    link = branch.get('link') or {}
    lane = lanes_by_id.get(link.get('lane_id'))
    if lane:
        for message in lane.get('messages') or []:
            if message.get('key') == link.get('message_key'):
                message.setdefault('branches', []).append(branch)
                return True

    for event in branch.get('events') or []:
        if event.get('event_type') != 'dm_output_stored':
            continue
        payload = event.get('payload') or {}
        session_id = payload.get('session_id')
        message_payload = payload.get('message') if isinstance(payload.get('message'), dict) else {}
        content = message_payload.get('content')
        role = message_payload.get('role') or 'dm'
        if not session_id or not content:
            continue
        lane = lanes_by_id.get(f'session-{session_id}')
        if not lane:
            continue
        for message in lane.get('messages') or []:
            if message.get('role') == role and message.get('content') == content:
                message.setdefault('branches', []).append(branch)
                return True
    return False


def _branch_count(branches):
    return sum(1 + _branch_count(branch.get('children') or []) for branch in branches)


def _chat_flow_payload(campaign_id, planning_messages, sessions, members, audit_stream, agent_runs):
    user_names = {member.user_id: member.user.username if member.user else None for member in members}
    lanes = []
    lanes_by_id = {}

    planning_lane_by_user = {}
    planning_message_lane_ids = {}
    for message in planning_messages:
        username = user_names.get(message.user_id) or f'User {message.user_id}'
        lane_id = f'planning-user-{message.user_id}'
        lane = planning_lane_by_user.get(message.user_id)
        if not lane:
            lane = {
                'id': lane_id,
                'type': 'planning',
                'title': f'Planning chat - {username}',
                'subtitle': f'Campaign {campaign_id} character planning',
                'messages': [],
                'branches': [],
            }
            planning_lane_by_user[message.user_id] = lane
            lanes.append(lane)
            lanes_by_id[lane_id] = lane
        normalized = _normalize_visible_message('planning', message, lane_id, username=username)
        lane['messages'].append(normalized)
        planning_message_lane_ids[str(message.id)] = lane_id

    for message_id, lane_id in planning_message_lane_ids.items():
        lanes_by_id[f'planning-user-message-{message_id}'] = lanes_by_id[lane_id]

    for session in sessions:
        lane_id = f'session-{session.id}'
        lane = {
            'id': lane_id,
            'type': 'session',
            'title': f'Session #{session.id}',
            'subtitle': 'Active session' if session.is_active else 'Past session',
            'messages': [],
            'branches': [],
        }
        session_messages = sorted(session.messages or [], key=lambda item: item.created_at or item.id)
        for message in session_messages:
            username = message.user.username if message.user else None
            lane['messages'].append(_normalize_visible_message('session', message, lane_id, username=username))
        lanes.append(lane)
        lanes_by_id[lane_id] = lane

    system_lane = {
        'id': 'system-unlinked',
        'type': 'system',
        'title': 'Unlinked agent runs',
        'subtitle': 'World generation and audit events that cannot be tied to one visible message',
        'messages': [],
        'branches': [],
    }

    linked_event_ids = set()
    unlinked_branches = []
    for run in agent_runs:
        branch = _branch_from_run(run)
        linked_event_ids.update(branch.get('event_ids') or [])
        if _attach_branch(lanes_by_id, branch):
            continue
        unlinked_branches.append(branch)

    standalone_event_types = {
        'player_input_stored',
        'dm_output_stored',
        'dm_silence_chosen',
        'dm_output_empty',
    }
    for event in audit_stream:
        if event.get('id') in linked_event_ids:
            continue
        if event.get('trace_id'):
            continue
        if event.get('event_type') in standalone_event_types:
            continue
        unlinked_branches.append(_branch_from_event(event))

    unlinked_branches.sort(key=lambda branch: branch.get('created_at') or '')
    system_lane['branches'] = unlinked_branches
    if unlinked_branches:
        lanes.append(system_lane)
        lanes_by_id[system_lane['id']] = system_lane

    for lane in lanes:
        for message in lane.get('messages') or []:
            message.get('branches', []).sort(key=lambda branch: branch.get('created_at') or '')
        lane.get('branches', []).sort(key=lambda branch: branch.get('created_at') or '')

    return {
        'lanes': lanes,
        'unlinked_branches': unlinked_branches,
        'stats': {
            'lane_count': len(lanes),
            'visible_message_count': sum(len(lane.get('messages') or []) for lane in lanes),
            'linked_branch_count': sum(
                _branch_count(message.get('branches') or [])
                for lane in lanes
                for message in (lane.get('messages') or [])
            ),
            'unlinked_branch_count': _branch_count(unlinked_branches),
        },
    }


@dev_bp.route('/api/campaigns/<int:campaign_id>/dev', methods=['GET'])
@token_required
def get_campaign_dev_audit(current_user, campaign_id):
    campaign = get_or_404(Campaign, campaign_id)
    members = get_campaign_members(campaign)
    member_user_ids = {member.user_id for member in members}
    if current_user.id not in member_user_ids and campaign.user_id != current_user.id:
        return jsonify({'error': 'Forbidden'}), 403

    campaign_data = campaign.to_dict()
    campaign_data['owner_username'] = campaign.owner.username if campaign.owner else None

    characters = [character_full_dict(character) for character in campaign.characters]
    sessions = CampaignSession.query.filter_by(campaign_id=campaign_id).order_by(CampaignSession.started_at.desc()).all()
    active_session = CampaignSession.query.filter_by(campaign_id=campaign_id, is_active=True).first()

    planning_ctx = planning_context(campaign, current_user)
    visible_planning = visible_planning_payload(campaign, current_user)
    planning_summary = summary_dict_for_read(campaign.id, include_private=True, current_user_id=current_user.id)
    planning_messages = CharacterPlanningMessage.query.filter_by(campaign_id=campaign_id).order_by(
        CharacterPlanningMessage.created_at.asc(),
    ).all()
    current_user_planning_messages = CharacterPlanningMessage.query.filter_by(
        campaign_id=campaign_id,
        user_id=current_user.id,
    ).order_by(CharacterPlanningMessage.created_at.asc()).all()
    latest_player_message = next(
        (message.content for message in reversed(current_user_planning_messages) if message.role == 'player'),
        '',
    )
    latest_dm_message = next(
        (message.content for message in reversed(current_user_planning_messages) if message.role == 'dm'),
        '',
    )

    world_public = world_public_payload(campaign)
    world_context = dm_world_context(campaign)
    world_events = WorldEvent.query.filter_by(campaign_id=campaign_id).order_by(WorldEvent.created_at.asc()).all()
    npcs = NPCActor.query.filter_by(campaign_id=campaign_id).order_by(NPCActor.id.asc()).all()
    clocks = CampaignClock.query.filter_by(campaign_id=campaign_id).order_by(CampaignClock.id.asc()).all()
    audit_events = CampaignAuditEvent.query.filter_by(campaign_id=campaign_id).order_by(
        CampaignAuditEvent.id.asc(),
    ).all()
    audit_events_payload = [event.to_dict() for event in audit_events]
    audit_stream = _with_inferred_session_trace_links([_audit_stream_entry(event) for event in audit_events])
    agent_runs = _agent_runs_from_stream(audit_stream)
    chat_flow = _chat_flow_payload(campaign_id, planning_messages, sessions, members, audit_stream, agent_runs)
    latest_session = active_session or (sessions[0] if sessions else None)

    latest_session_hot_context = None
    latest_session_context_manifest = None
    if latest_session:
        latest_session_hot_context = build_session_hot_context(campaign, latest_session, current_user)
        latest_session_context_manifest = context_manifest(latest_session_hot_context, DM_TOOL_DEFINITIONS)

    return jsonify({
        'campaign': campaign_data,
        'members': [member.to_dict() for member in members],
        'characters': characters,
        'sessions': [_serialize_session(session) for session in sessions],
        'active_session': _serialize_session(active_session) if active_session else None,
        'latest_session': _serialize_session(latest_session) if latest_session else None,
        'planning': {
            'context': planning_ctx,
            'visible': visible_planning,
            'summary': planning_summary,
            'messages': [message.to_dict() for message in planning_messages],
            'current_user_messages': [message.to_dict() for message in current_user_planning_messages],
            'prompt_traces': {
                'dm_response': build_planning_dm_messages(
                    planning_ctx,
                    current_user_planning_messages,
                    draft_character=None,
                    active_page=None,
                ),
                'summary_update': build_planning_summary_messages(
                    planning_ctx,
                    latest_player_message,
                    latest_dm_message,
                ),
            },
        },
        'world': {
            'public': world_public,
            'context': world_context,
            'npc_actors': [npc.to_dict(include_private=True) for npc in npcs],
            'clocks': [clock.to_dict(include_private=True) for clock in clocks],
            'events': [event.to_dict(include_private=True) for event in world_events],
            'prompt_traces': {
                'world_genesis': build_world_genesis_messages(planning_ctx),
                'opening_scene': build_opening_scene_messages(planning_ctx, world_context),
                'session_dm_response': build_session_dm_tool_messages(
                    latest_session_hot_context,
                ) if latest_session_hot_context else [],
            },
            'context_strategy': {
                'hot_context': latest_session_hot_context,
                'manifest': latest_session_context_manifest,
                'tools': DM_TOOL_DEFINITIONS,
                'session_prompt_available': bool(latest_session_hot_context),
            },
        },
        'audit_events': audit_events_payload,
        'audit_stream': audit_stream,
        'agent_runs': agent_runs,
        'chat_flow': chat_flow,
        'audit_notes': {
            'tool_calls': 'Chat requests and responses are persisted as model_request/model_response events. Provider-internal tool calls are not returned unless the provider response includes them.',
            'thinking': 'Reasoning-capable models may return reasoning or reasoning_details fields in model responses. Some models and providers do not return reasoning, and provider-hidden reasoning is not available unless it appears in the response payload.',
            'message': 'The audit stream shows exact app inputs, system prompts, prompt payloads, raw model responses, returned reasoning when provided, database reads/writes, and client response payloads in persisted order.',
        },
    }), 200

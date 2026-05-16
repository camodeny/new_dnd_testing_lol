import json

from models import CampaignAuditEvent, db

AGENT_ACTORS = {
    'planning_dm',
    'session_dm',
    'world_architect',
    'character_draft_agent',
    'planning_memory_writer',
    'session_memory_writer',
}

PLAYER_EVENT_TYPES = {'player_input_stored'}
AGENT_EVENT_TYPES = {
    'dm_output_stored',
    'dm_output_empty',
    'dm_silence_chosen',
    'draft_output_sent',
    'model_response',
    'memory_writer_response',
}


def _json_safe(value):
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except (TypeError, ValueError):
        return repr(value)


def infer_audit_role(event_type, actor=None, audit_role=None):
    if audit_role in {'player', 'agent', 'tools'}:
        return audit_role
    if event_type in PLAYER_EVENT_TYPES:
        return 'player'
    if event_type in AGENT_EVENT_TYPES:
        return 'agent'
    return 'tools'


def _default_trace_label(actor, payload):
    if not actor:
        return None
    operation = payload.get('operation') if isinstance(payload, dict) else None
    return f'{actor}: {operation}' if operation else actor


def log_audit_event(
    campaign_id,
    event_type,
    summary,
    payload=None,
    source=None,
    actor=None,
    commit=False,
    trace_id=None,
    parent_trace_id=None,
    trace_label=None,
    audit_role=None,
):
    if not campaign_id:
        return None

    payload = payload or {}
    event = CampaignAuditEvent(
        campaign_id=campaign_id,
        event_type=event_type,
        source=source,
        actor=actor,
        trace_id=trace_id,
        parent_trace_id=parent_trace_id,
        trace_label=trace_label or _default_trace_label(actor, payload),
        audit_role=infer_audit_role(event_type, actor=actor, audit_role=audit_role),
        summary=summary,
        payload=json.dumps(_json_safe(payload), ensure_ascii=False),
    )
    db.session.add(event)
    db.session.flush()
    if commit:
        db.session.commit()
    return event


def log_model_request(
    campaign_id,
    operation,
    actor,
    messages,
    model,
    json_mode=False,
    commit=True,
    trace_id=None,
    parent_trace_id=None,
    trace_label=None,
    tools=None,
    tool_choice=None,
    parallel_tool_calls=None,
    context_manifest=None,
    token_estimate=None,
    provider='openrouter',
    reasoning_requested_by_app=False,
    reasoning_note=None,
):
    tool_names = [
        tool.get('function', {}).get('name')
        for tool in (tools or [])
        if isinstance(tool, dict) and tool.get('function')
    ]
    return log_audit_event(
        campaign_id,
        'model_request',
        f'{actor} request: {operation}',
        {
            'operation': operation,
            'provider': provider,
            'model': model,
            'json_mode': json_mode,
            'messages': messages,
            'tools': tools or [],
            'tool_names': [name for name in tool_names if name],
            'tool_choice': tool_choice,
            'parallel_tool_calls': parallel_tool_calls,
            'context_manifest': context_manifest or {},
            'token_estimate': token_estimate or {},
            'reasoning_requested_by_app': reasoning_requested_by_app,
            'reasoning_note': reasoning_note or (
                'This app does not force reasoning for every request. Reasoning-capable '
                'models may still return reasoning fields in the model response.'
            ),
        },
        source=provider,
        actor=actor,
        trace_id=trace_id,
        parent_trace_id=parent_trace_id,
        trace_label=trace_label,
        audit_role='tools',
        commit=commit,
    )


def _first_choice_message(response):
    if not isinstance(response, dict):
        return {}
    choices = response.get('choices') or []
    if not choices or not isinstance(choices[0], dict):
        return {}
    message = choices[0].get('message') or {}
    return message if isinstance(message, dict) else {}


def _reasoning_usage(response):
    if not isinstance(response, dict):
        return {}
    usage = response.get('usage') or {}
    if not isinstance(usage, dict):
        return {}
    completion_details = usage.get('completion_tokens_details') or {}
    output_details = usage.get('output_tokens_details') or {}
    details = completion_details if isinstance(completion_details, dict) else {}
    output_details = output_details if isinstance(output_details, dict) else {}
    reasoning_tokens = details.get('reasoning_tokens')
    if reasoning_tokens is None:
        reasoning_tokens = output_details.get('reasoning_tokens')
    return {
        'reasoning_tokens': reasoning_tokens,
        'completion_tokens_details': details,
        'output_tokens_details': output_details,
    }


def log_model_response(
    campaign_id,
    operation,
    actor,
    response,
    commit=True,
    trace_id=None,
    parent_trace_id=None,
    trace_label=None,
    provider='openrouter',
):
    message = _first_choice_message(response)
    choices = response.get('choices') if isinstance(response, dict) else []
    first_choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    reasoning = message.get('reasoning') or message.get('reasoning_content')
    reasoning_details = message.get('reasoning_details')
    return log_audit_event(
        campaign_id,
        'model_response',
        f'{actor} response: {operation}',
        {
            'operation': operation,
            'provider': provider,
            'model': response.get('model') if isinstance(response, dict) else None,
            'raw_response': response,
            'content': message.get('content'),
            'finish_reason': first_choice.get('finish_reason'),
            'tool_calls': message.get('tool_calls') or [],
            'usage': response.get('usage') if isinstance(response, dict) else {},
            'reasoning': reasoning,
            'reasoning_details': reasoning_details,
            'reasoning_returned': bool(reasoning or reasoning_details),
            'reasoning_usage': _reasoning_usage(response),
        },
        source=provider,
        actor=actor,
        trace_id=trace_id,
        parent_trace_id=parent_trace_id,
        trace_label=trace_label,
        audit_role='agent',
        commit=commit,
    )


def log_model_error(
    campaign_id,
    operation,
    actor,
    error,
    commit=True,
    trace_id=None,
    parent_trace_id=None,
    trace_label=None,
    provider='openrouter',
):
    return log_audit_event(
        campaign_id,
        'model_error',
        f'{actor} error: {operation}',
        {
            'operation': operation,
            'provider': provider,
            'error': repr(error),
        },
        source=provider,
        actor=actor,
        trace_id=trace_id,
        parent_trace_id=parent_trace_id,
        trace_label=trace_label,
        audit_role='tools',
        commit=commit,
    )

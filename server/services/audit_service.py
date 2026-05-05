import json

from models import CampaignAuditEvent, db

AGENT_ACTORS = {
    'planning_dm',
    'session_dm',
    'world_architect',
    'character_draft_agent',
    'planning_memory_writer',
}

PLAYER_EVENT_TYPES = {'player_input_stored'}
AGENT_EVENT_TYPES = {'dm_output_stored', 'draft_output_sent', 'model_response'}


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
):
    return log_audit_event(
        campaign_id,
        'model_request',
        f'{actor} request: {operation}',
        {
            'operation': operation,
            'model': model,
            'json_mode': json_mode,
            'messages': messages,
            'reasoning_requested_by_app': False,
            'reasoning_note': (
                'This app does not force reasoning for every request. OpenRouter reasoning-capable '
                'models may still return reasoning fields in the model response.'
            ),
        },
        source='openrouter',
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
):
    message = _first_choice_message(response)
    reasoning = message.get('reasoning') or message.get('reasoning_content')
    reasoning_details = message.get('reasoning_details')
    return log_audit_event(
        campaign_id,
        'model_response',
        f'{actor} response: {operation}',
        {
            'operation': operation,
            'raw_response': response,
            'content': message.get('content'),
            'reasoning': reasoning,
            'reasoning_details': reasoning_details,
            'reasoning_returned': bool(reasoning or reasoning_details),
            'reasoning_usage': _reasoning_usage(response),
        },
        source='openrouter',
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
):
    return log_audit_event(
        campaign_id,
        'model_error',
        f'{actor} error: {operation}',
        {
            'operation': operation,
            'error': repr(error),
        },
        source='openrouter',
        actor=actor,
        trace_id=trace_id,
        parent_trace_id=parent_trace_id,
        trace_label=trace_label,
        audit_role='tools',
        commit=commit,
    )

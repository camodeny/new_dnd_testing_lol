import json

from models import CampaignAuditEvent, db


def _json_safe(value):
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except (TypeError, ValueError):
        return repr(value)


def log_audit_event(campaign_id, event_type, summary, payload=None, source=None, actor=None, commit=False):
    if not campaign_id:
        return None

    event = CampaignAuditEvent(
        campaign_id=campaign_id,
        event_type=event_type,
        source=source,
        actor=actor,
        summary=summary,
        payload=json.dumps(_json_safe(payload or {}), ensure_ascii=False),
    )
    db.session.add(event)
    db.session.flush()
    if commit:
        db.session.commit()
    return event


def log_model_request(campaign_id, operation, actor, messages, model, json_mode=False, commit=True):
    return log_audit_event(
        campaign_id,
        'model_request',
        f'{actor} request: {operation}',
        {
            'operation': operation,
            'model': model,
            'json_mode': json_mode,
            'messages': messages,
            'reasoning_available': False,
            'reasoning_note': (
                'Provider-hidden chain-of-thought is not returned to or stored by this app. '
                'This payload is the complete message input the app sent.'
            ),
        },
        source='openrouter',
        actor=actor,
        commit=commit,
    )


def log_model_response(campaign_id, operation, actor, response, commit=True):
    return log_audit_event(
        campaign_id,
        'model_response',
        f'{actor} response: {operation}',
        {
            'operation': operation,
            'raw_response': response,
            'content': (
                response.get('choices', [{}])[0].get('message', {}).get('content')
                if isinstance(response, dict)
                else None
            ),
        },
        source='openrouter',
        actor=actor,
        commit=commit,
    )


def log_model_error(campaign_id, operation, actor, error, commit=True):
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
        commit=commit,
    )

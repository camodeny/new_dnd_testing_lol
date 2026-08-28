"""Typed roll-request lifecycle and legacy roll-message correlation."""

import re

from models import db, SessionRollRequest
from time_utils import utcnow


ROLL_MESSAGE_RE = re.compile(
    r"\[Roll:\s*([^\]]+)\]\s*total:\s*(-?\d+)\s*\|\s*rolls:\s*([\d,\s-]+)\s*\|\s*mod:\s*(-?\d+)\s*\|\s*sides:\s*(\d+)",
    re.IGNORECASE,
)


def normalize_roll_request(raw):
    if not isinstance(raw, dict):
        raise ValueError('roll_request must be an object.')
    request_id = str(raw.get('request_id') or '').strip()[:160]
    roll_kind = str(raw.get('roll_kind') or 'check').strip().lower()[:40]
    ability_or_skill = str(raw.get('ability_or_skill') or '').strip()[:120]
    label = str(raw.get('label') or '').strip()[:160]
    advantage_state = str(raw.get('advantage_state') or 'normal').strip().lower()[:20]
    reason_public = str(raw.get('reason_public') or '').strip()[:600]
    if not request_id or not re.fullmatch(r'[A-Za-z0-9_-]+', request_id):
        raise ValueError('roll_request.request_id must be a stable identifier.')
    if roll_kind not in {'check', 'save', 'attack', 'ability', 'initiative', 'other'}:
        raise ValueError('roll_request.roll_kind is invalid.')
    if not ability_or_skill or not label or not reason_public:
        raise ValueError('roll_request requires ability_or_skill, label, and reason_public.')
    if advantage_state not in {'normal', 'advantage', 'disadvantage'}:
        raise ValueError('roll_request.advantage_state is invalid.')
    normalized = {
        'request_id': request_id,
        'roll_kind': roll_kind,
        'ability_or_skill': ability_or_skill,
        'label': label,
        'advantage_state': advantage_state,
        'reason_public': reason_public,
        'requested_user_id': raw.get('requested_user_id'),
        'character_id': raw.get('character_id'),
        'dc_private': raw.get('dc_private'),
    }
    for key in ('requested_user_id', 'character_id', 'dc_private'):
        if normalized[key] is not None:
            try:
                normalized[key] = int(normalized[key])
            except (TypeError, ValueError):
                raise ValueError(f'roll_request.{key} must be an integer or null.')
    if normalized['dc_private'] is not None and not 1 <= normalized['dc_private'] <= 40:
        raise ValueError('roll_request.dc_private must be between 1 and 40.')
    return normalized


def create_roll_request(campaign, session, source_player_message_id, dm_message_id, raw):
    request = normalize_roll_request(raw)
    existing = SessionRollRequest.query.filter_by(request_id=request['request_id']).first()
    if existing:
        request['request_id'] = f"{request['request_id']}_{source_player_message_id}"
        if SessionRollRequest.query.filter_by(request_id=request['request_id']).first():
            raise ValueError(f"Duplicate roll request ID: {request['request_id']}")
    row = SessionRollRequest(
        campaign_id=campaign.id,
        session_id=session.id,
        source_player_message_id=source_player_message_id,
        requesting_dm_message_id=dm_message_id,
        **request,
    )
    db.session.add(row)
    return row


def parse_roll_message(content):
    matches = list(ROLL_MESSAGE_RE.finditer(str(content or '')))
    if len(matches) != 1:
        return None
    match = matches[0]
    rolls = [int(value.strip()) for value in match.group(3).split(',') if value.strip()]
    return {
        'label': match.group(1).strip()[:160],
        'total': int(match.group(2)),
        'rolls': rolls,
        'modifier': int(match.group(4)),
        'sides': int(match.group(5)),
    }


def normalize_roll_result(raw):
    if not isinstance(raw, dict):
        raise ValueError('roll_result must be an object.')
    try:
        result = {
            'label': str(raw.get('label') or '').strip()[:160],
            'total': int(raw.get('total')),
            'rolls': [int(value) for value in (raw.get('rolls') or [])],
            'modifier': int(raw.get('modifier') or 0),
            'sides': int(raw.get('sides') or 20),
        }
    except (TypeError, ValueError):
        raise ValueError('roll_result contains invalid numeric values.')
    if not result['label'] or not result['rolls'] or not 2 <= result['sides'] <= 100:
        raise ValueError('roll_result requires a label, rolls, and valid die size.')
    if any(value < 1 or value > result['sides'] for value in result['rolls']):
        raise ValueError('roll_result contains a die result outside its valid range.')
    return result


def fulfill_roll_request(session_id, user_id, message, request_id, raw_result):
    result = normalize_roll_result(raw_result)
    row = SessionRollRequest.query.filter_by(
        session_id=session_id,
        request_id=str(request_id or '').strip(),
        status='pending',
    ).first()
    if row is None:
        raise ValueError('The referenced roll request is not pending in this session.')
    if row.requested_user_id is not None and row.requested_user_id != user_id:
        raise ValueError('This roll request belongs to another player.')
    if row.advantage_state == 'advantage':
        expected_total = max(result['rolls']) + result['modifier']
    elif row.advantage_state == 'disadvantage':
        expected_total = min(result['rolls']) + result['modifier']
    else:
        expected_total = sum(result['rolls']) + result['modifier']
    if result['total'] != expected_total:
        raise ValueError('roll_result total does not match its dice and modifier.')
    row.status = 'fulfilled'
    row.result_message_id = message.id
    row.result_json = result
    row.fulfilled_at = utcnow()
    return row


def fulfill_matching_roll_request(session_id, user_id, message):
    """Correlate one posted roll to the newest compatible pending request."""
    result = parse_roll_message(getattr(message, 'content', ''))
    if not result:
        return None
    pending = (
        SessionRollRequest.query
        .filter_by(session_id=session_id, status='pending')
        .filter(
            (SessionRollRequest.requested_user_id.is_(None))
            | (SessionRollRequest.requested_user_id == user_id)
        )
        .order_by(SessionRollRequest.id.desc())
        .all()
    )
    if not pending:
        return None
    normalized_label = result['label'].lower()
    row = next((item for item in pending if item.label.lower() in normalized_label or normalized_label in item.label.lower()), pending[0])
    row.status = 'fulfilled'
    row.result_message_id = message.id
    row.result_json = result
    row.fulfilled_at = utcnow()
    return row


def roll_context_for_session(session_id, limit=6):
    rows = (
        SessionRollRequest.query.filter_by(session_id=session_id)
        .order_by(SessionRollRequest.id.desc())
        .limit(limit)
        .all()
    )
    return [row.to_dict(include_private=True) for row in reversed(rows)]


def pending_roll_requests_for_player(session_id, user_id):
    rows = (
        SessionRollRequest.query
        .filter_by(session_id=session_id, status='pending')
        .filter(
            (SessionRollRequest.requested_user_id.is_(None))
            | (SessionRollRequest.requested_user_id == user_id)
        )
        .order_by(SessionRollRequest.id.asc())
        .all()
    )
    return [row.to_dict(include_private=False) for row in rows]


def pending_roll_requests(session_id):
    rows = (
        SessionRollRequest.query
        .filter_by(session_id=session_id, status='pending')
        .order_by(SessionRollRequest.id.asc())
        .all()
    )
    return [row.to_dict(include_private=False) for row in rows]

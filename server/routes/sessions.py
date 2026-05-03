from datetime import datetime

from flask import Blueprint, jsonify, request

from auth import token_required
from models import db, Campaign, CampaignSession, SessionMessage
from openrouter import get_dm_response
from services.campaign_service import ensure_member

sessions_bp = Blueprint('sessions', __name__)


@sessions_bp.route('/api/campaigns/<int:campaign_id>/sessions', methods=['POST'])
@token_required
def start_session(current_user, campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    active = CampaignSession.query.filter_by(campaign_id=campaign_id, is_active=True).first()
    if active:
        return jsonify({'error': 'An active session already exists'}), 400

    session = CampaignSession(campaign_id=campaign_id)
    db.session.add(session)
    campaign.last_played_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'session': session.to_dict()}), 201


@sessions_bp.route('/api/campaigns/<int:campaign_id>/sessions', methods=['GET'])
@token_required
def list_sessions(current_user, campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    sessions = CampaignSession.query.filter_by(campaign_id=campaign_id).order_by(CampaignSession.started_at.desc()).all()
    return jsonify({'sessions': [s.to_dict() for s in sessions]}), 200


@sessions_bp.route('/api/sessions/<int:session_id>', methods=['GET'])
@token_required
def get_session(current_user, session_id):
    session = CampaignSession.query.get_or_404(session_id)
    campaign = Campaign.query.get(session.campaign_id)
    if not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    data = session.to_dict()
    data['messages'] = [m.to_dict() for m in session.messages]
    return jsonify({'session': data}), 200


@sessions_bp.route('/api/sessions/<int:session_id>', methods=['PUT'])
@token_required
def end_session(current_user, session_id):
    session = CampaignSession.query.get_or_404(session_id)
    campaign = Campaign.query.get(session.campaign_id)
    if not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    data = request.get_json()
    session.is_active = False
    session.ended_at = datetime.utcnow()
    if data and 'recap' in data:
        session.recap = data['recap']

    db.session.commit()
    return jsonify({'session': session.to_dict()}), 200


@sessions_bp.route('/api/sessions/<int:session_id>/messages', methods=['GET'])
@token_required
def get_messages(current_user, session_id):
    session = CampaignSession.query.get_or_404(session_id)
    campaign = Campaign.query.get(session.campaign_id)
    if not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    messages = SessionMessage.query.filter_by(session_id=session_id).order_by(SessionMessage.created_at).all()
    return jsonify({'messages': [m.to_dict() for m in messages]}), 200


@sessions_bp.route('/api/sessions/<int:session_id>/messages', methods=['POST'])
@token_required
def send_message(current_user, session_id):
    session = CampaignSession.query.get_or_404(session_id)
    campaign = Campaign.query.get(session.campaign_id)
    if not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    data = request.get_json()
    if not data or not data.get('content'):
        return jsonify({'error': 'Missing content'}), 400

    msg = SessionMessage(
        session_id=session_id,
        role=data.get('role', 'player'),
        content=data['content'],
    )
    db.session.add(msg)
    db.session.commit()
    result_messages = [msg.to_dict()]

    ai_text = get_dm_response(session.messages)
    if ai_text:
        ai_msg = SessionMessage(
            session_id=session_id,
            role='dm',
            content=ai_text,
        )
        db.session.add(ai_msg)
        db.session.commit()
        result_messages.append(ai_msg.to_dict())

    return jsonify({'messages': result_messages}), 201

from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request

from auth import token_required
from models import db, Campaign, CampaignInvite, CampaignMember
from services.campaign_service import (
    current_invite_for_campaign,
    ensure_member,
    generate_invite_code,
    get_or_404,
    invite_code_matches,
)

members_bp = Blueprint('members', __name__)


@members_bp.route('/api/invites/lookup', methods=['GET'])
@token_required
def lookup_invite_code(current_user):
    code = request.args.get('code')
    if not code:
        return jsonify({'error': 'Missing invite code'}), 400

    normalized = code.strip().upper()
    campaign = Campaign.query.filter(Campaign.invite_code == normalized).first()
    if not campaign:
        invite = CampaignInvite.query.filter_by(code=normalized, is_used=False).first()
        if invite:
            campaign = Campaign.query.get(invite.campaign_id)

    if not campaign:
        return jsonify({'error': 'Invalid invite code'}), 404

    return jsonify({
        'campaign_id': campaign.id,
        'campaign_name': campaign.name
    }), 200


@members_bp.route('/api/campaigns/<int:campaign_id>/members', methods=['GET'])
@token_required
def list_members(current_user, campaign_id):
    campaign = get_or_404(Campaign, campaign_id)
    if not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    members = CampaignMember.query.filter_by(campaign_id=campaign_id).all()
    result = [m.to_dict() for m in members]

    owner_in_list = any(m['user_id'] == campaign.user_id for m in result)
    if not owner_in_list:
        owner = campaign.owner
        if owner:
            result.insert(0, {
                'id': None,
                'campaign_id': campaign_id,
                'user_id': owner.id,
                'username': owner.username,
                'role': 'player',
                'joined_at': campaign.created_at.isoformat() if campaign.created_at else None,
            })

    return jsonify({'members': result}), 200


@members_bp.route('/api/campaigns/<int:campaign_id>/invites', methods=['GET'])
@token_required
def get_current_invite(current_user, campaign_id):
    campaign = get_or_404(Campaign, campaign_id)
    if campaign.user_id != current_user.id:
        return jsonify({'error': 'Only the campaign owner can view invites'}), 403

    invite = current_invite_for_campaign(campaign)
    if not invite:
        return jsonify({'invite': None}), 200

    return jsonify({'invite': invite.to_dict()}), 200


@members_bp.route('/api/campaigns/<int:campaign_id>/invites', methods=['POST'])
@token_required
def create_invite(current_user, campaign_id):
    campaign = get_or_404(Campaign, campaign_id)
    if campaign.user_id != current_user.id:
        return jsonify({'error': 'Only the campaign owner can create invites'}), 403

    code = generate_invite_code()
    invite = CampaignInvite(
        campaign_id=campaign_id,
        code=code,
        created_by=current_user.id,
        expires_at=datetime.utcnow() + timedelta(days=7),
    )
    db.session.add(invite)
    campaign.invite_code = code
    db.session.commit()
    return jsonify({'invite': invite.to_dict()}), 201


@members_bp.route('/api/campaigns/<int:campaign_id>/join', methods=['POST'])
@token_required
def join_campaign(current_user, campaign_id):
    campaign = get_or_404(Campaign, campaign_id)
    data = request.get_json()
    if not data or not data.get('code'):
        return jsonify({'error': 'Missing invite code'}), 400
    if not invite_code_matches(campaign, data['code']):
        return jsonify({'error': 'Invalid invite code'}), 403

    existing = CampaignMember.query.filter_by(campaign_id=campaign_id, user_id=current_user.id).first()
    if existing:
        return jsonify({'error': 'Already a member'}), 400

    member = CampaignMember(campaign_id=campaign_id, user_id=current_user.id, role='player')
    db.session.add(member)
    db.session.commit()
    return jsonify({'member': member.to_dict()}), 201


@members_bp.route('/api/campaigns/<int:campaign_id>/members/<int:user_id>', methods=['PUT'])
@token_required
def update_member_role(current_user, campaign_id, user_id):
    campaign = get_or_404(Campaign, campaign_id)
    if campaign.user_id != current_user.id:
        return jsonify({'error': 'Forbidden'}), 403

    member = CampaignMember.query.filter_by(campaign_id=campaign_id, user_id=user_id).first_or_404()
    data = request.get_json()
    if data and 'role' in data:
        member.role = data['role']

    db.session.commit()
    return jsonify({'member': member.to_dict()}), 200


@members_bp.route('/api/campaigns/<int:campaign_id>/members/<int:user_id>', methods=['DELETE'])
@token_required
def remove_member(current_user, campaign_id, user_id):
    campaign = get_or_404(Campaign, campaign_id)
    if campaign.user_id != current_user.id and current_user.id != user_id:
        return jsonify({'error': 'Forbidden'}), 403

    member = CampaignMember.query.filter_by(campaign_id=campaign_id, user_id=user_id).first_or_404()
    db.session.delete(member)
    db.session.commit()
    return jsonify({'message': 'Member removed'}), 200

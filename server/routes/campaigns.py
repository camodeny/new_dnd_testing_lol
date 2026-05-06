import json
from datetime import datetime

from flask import Blueprint, jsonify, request

from auth import token_required
from models import (
    db,
    Campaign,
    CampaignAuditEvent,
    CampaignInvite,
    CampaignMember,
    CampaignPlanningSummary,
    CampaignSession,
    Character,
    CharacterPlanningMessage,
    PlanningBondProposal,
)
from services.campaign_service import ensure_member
from services.character_service import character_full_dict

campaigns_bp = Blueprint('campaigns', __name__)


@campaigns_bp.route('/api/campaigns', methods=['GET'])
@token_required
def get_campaigns(current_user):
    owned = Campaign.query.filter_by(user_id=current_user.id).all()
    member_of = Campaign.query.join(CampaignMember).filter(
        CampaignMember.user_id == current_user.id
    ).all()
    all_campaigns = list({c.id: c for c in owned + member_of}.values())
    return jsonify({'campaigns': [c.to_dict() for c in all_campaigns]}), 200


@campaigns_bp.route('/api/campaigns/<int:campaign_id>', methods=['GET'])
@token_required
def get_campaign(current_user, campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)

    has_invite = False
    invite_code = request.args.get('code')
    if invite_code:
        valid = CampaignInvite.query.filter_by(
            campaign_id=campaign_id, code=invite_code, is_used=False
        ).first()
        if valid and (valid.expires_at is None or valid.expires_at > datetime.utcnow()):
            has_invite = True

    if not ensure_member(campaign, current_user) and not has_invite:
        return jsonify({'error': 'Forbidden'}), 403

    data = campaign.to_dict()
    data['owner_username'] = campaign.owner.username if campaign.owner else None
    data['characters'] = [character_full_dict(c) for c in campaign.characters]
    data['active_session'] = None

    active = CampaignSession.query.filter_by(campaign_id=campaign_id, is_active=True).first()
    if active:
        data['active_session'] = active.to_dict()

    return jsonify({'campaign': data}), 200


@campaigns_bp.route('/api/campaigns/<int:campaign_id>', methods=['PUT'])
@token_required
def update_campaign(current_user, campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.user_id != current_user.id:
        return jsonify({'error': 'Forbidden'}), 403

    data = request.get_json()
    if 'name' in data:
        campaign.name = data['name']
    if 'description' in data:
        campaign.description = data['description']
    if 'difficulty' in data:
        campaign.difficulty = data['difficulty']
    if 'seed' in data:
        campaign.seed = data['seed']
    if 'status' in data:
        campaign.status = data['status']
    if 'settings' in data:
        campaign.settings = json.dumps(data['settings'])

    db.session.commit()
    return jsonify({'campaign': campaign.to_dict()}), 200


@campaigns_bp.route('/api/campaigns/<int:campaign_id>', methods=['DELETE'])
@token_required
def delete_campaign(current_user, campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.user_id != current_user.id:
        return jsonify({'error': 'Forbidden'}), 403

    Character.query.filter_by(campaign_id=campaign_id).update(
        {Character.campaign_id: None},
        synchronize_session=False,
    )
    CharacterPlanningMessage.query.filter_by(campaign_id=campaign_id).delete(synchronize_session=False)
    CampaignPlanningSummary.query.filter_by(campaign_id=campaign_id).delete(synchronize_session=False)
    PlanningBondProposal.query.filter_by(campaign_id=campaign_id).delete(synchronize_session=False)
    CampaignAuditEvent.query.filter_by(campaign_id=campaign_id).delete(synchronize_session=False)

    db.session.delete(campaign)
    db.session.commit()
    return jsonify({'message': 'Campaign deleted'}), 200


@campaigns_bp.route('/api/campaigns', methods=['POST'])
@token_required
def create_campaign(current_user):
    data = request.get_json()

    if not data or not data.get('name'):
        return jsonify({'error': 'Missing required field: name'}), 400

    required = data.get('required_players')
    settings = {}
    if required is not None:
        try:
            required = int(required)
            if required < 1:
                required = 1
            elif required > 10:
                required = 10
            settings['required_players'] = required
        except (ValueError, TypeError):
            pass

    campaign = Campaign(
        name=data['name'],
        description=data.get('description', ''),
        difficulty=data.get('difficulty', ''),
        seed=data.get('seed', ''),
        user_id=current_user.id,
        settings=json.dumps(settings) if settings else None,
    )

    db.session.add(campaign)
    db.session.commit()

    member = CampaignMember(campaign_id=campaign.id, user_id=current_user.id, role='player')
    db.session.add(member)
    db.session.commit()

    return jsonify({'message': 'Campaign created successfully', 'campaign': campaign.to_dict()}), 201


@campaigns_bp.route('/api/campaigns/<int:campaign_id>/characters', methods=['GET'])
@token_required
def get_campaign_characters(current_user, campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    characters = Character.query.filter_by(campaign_id=campaign_id).order_by(Character.party_order).all()
    return jsonify({'characters': [character_full_dict(c) for c in characters]}), 200


@campaigns_bp.route('/api/campaigns/<int:campaign_id>/characters', methods=['POST'])
@token_required
def add_campaign_character(current_user, campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    data = request.get_json()
    if not data or not data.get('character_id'):
        return jsonify({'error': 'Missing character_id'}), 400

    character = Character.query.get_or_404(data['character_id'])
    if character.user_id != current_user.id:
        return jsonify({'error': 'Forbidden'}), 403

    character.campaign_id = campaign_id
    member = CampaignMember.query.filter_by(campaign_id=campaign_id, user_id=current_user.id).first()
    if member:
        member.selected_character_id = character.id
        member.character_ready_at = None
    db.session.commit()
    return jsonify({'character': character_full_dict(character)}), 200

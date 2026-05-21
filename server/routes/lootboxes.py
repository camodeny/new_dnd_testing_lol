import json
from datetime import datetime
from flask import Blueprint, jsonify, request
from auth import token_required
from models import db, Campaign, CampaignMember, CampaignSession, Character, LootBox, SessionMessage
from services.campaign_service import ensure_member, get_or_404
from services.lootbox_service import get_campaign_stash, open_loot_box

lootboxes_bp = Blueprint('lootboxes', __name__)


@lootboxes_bp.route('/api/campaigns/<int:campaign_id>/lootboxes', methods=['GET'])
@token_required
def list_lootboxes(current_user, campaign_id):
    campaign = get_or_404(Campaign, campaign_id)
    if not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    is_dm = campaign.user_id == current_user.id
    boxes = get_campaign_stash(campaign_id, current_user=current_user, is_dm=is_dm)
    return jsonify({'loot_boxes': boxes}), 200


@lootboxes_bp.route('/api/lootboxes/<int:loot_box_id>', methods=['GET'])
@token_required
def get_lootbox(current_user, loot_box_id):
    loot_box = get_or_404(LootBox, loot_box_id)
    campaign = db.session.get(Campaign, loot_box.campaign_id)
    if not campaign or not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    is_dm = campaign.user_id == current_user.id
    return jsonify({'loot_box': loot_box.to_dict(current_user=current_user, is_dm=is_dm)}), 200


@lootboxes_bp.route('/api/lootboxes/<int:loot_box_id>/open', methods=['POST'])
@token_required
def open_lootbox(current_user, loot_box_id):
    loot_box = get_or_404(LootBox, loot_box_id)
    campaign = db.session.get(Campaign, loot_box.campaign_id)
    if not campaign or not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    if loot_box.status != 'unopened':
        return jsonify({'error': 'This loot box has already been opened.'}), 400

    session = None
    if loot_box.session_id:
        session = db.session.get(CampaignSession, loot_box.session_id)
    if not session:
        active = CampaignSession.query.filter_by(campaign_id=campaign.id, is_active=True).first()
        if not active:
            return jsonify({'error': 'No active session to create sheet proposals in.'}), 400
        session = active

    try:
        proposals = open_loot_box(loot_box, session, current_user.id)
    except Exception as err:
        return jsonify({'error': f'Failed to open loot box: {repr(err)}'}), 500

    return jsonify({
        'loot_box': loot_box.to_dict(current_user=current_user, is_dm=(campaign.user_id == current_user.id)),
        'proposals': [p.to_dict() for p in proposals],
    }), 200

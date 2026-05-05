from flask import Blueprint, jsonify

from auth import token_required
from models import Campaign
from services.campaign_service import ensure_member
from services.world_service import ensure_world_generated, world_public_payload

world_bp = Blueprint('world', __name__)


@world_bp.route('/api/campaigns/<int:campaign_id>/world', methods=['GET'])
@token_required
def get_world(current_user, campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    return jsonify(world_public_payload(campaign)), 200


@world_bp.route('/api/campaigns/<int:campaign_id>/world', methods=['POST'])
@token_required
def generate_world(current_user, campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    world, error = ensure_world_generated(campaign, current_user)
    if error:
        return jsonify({key: value for key, value in error.items() if key != 'status'}), error.get('status', 500)

    return jsonify({
        'world': world.to_public_dict(),
        'is_ready': True,
        'can_generate': False,
        'planning': None,
    }), 201

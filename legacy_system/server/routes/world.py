from flask import Blueprint, jsonify

from auth import token_required
from models import Campaign
from services.audit_service import log_audit_event
from services.campaign_service import ensure_member, get_or_404
from services.world_service import ensure_world_generated, world_public_payload

world_bp = Blueprint('world', __name__)


@world_bp.route('/api/campaigns/<int:campaign_id>/world', methods=['GET'])
@token_required
def get_world(current_user, campaign_id):
    campaign = get_or_404(Campaign, campaign_id)
    if not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    return jsonify(world_public_payload(campaign)), 200


@world_bp.route('/api/campaigns/<int:campaign_id>/world', methods=['POST'])
@token_required
def generate_world(current_user, campaign_id):
    campaign = get_or_404(Campaign, campaign_id)
    if not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403
    log_audit_event(
        campaign_id,
        'world_generation_requested',
        'Client requested campaign world generation.',
        {'campaign_id': campaign_id},
        source='world.route',
        actor=current_user.username,
        commit=True,
    )

    world, error = ensure_world_generated(campaign, current_user)
    if error:
        return jsonify({key: value for key, value in error.items() if key != 'status'}), error.get('status', 500)

    response_payload = {
        'world': world.to_public_dict(),
        'is_ready': True,
        'can_generate': False,
        'planning': None,
    }
    log_audit_event(
        campaign_id,
        'client_response_sent',
        'Sent world generation response payload to client.',
        response_payload,
        source='world.route',
        actor='server',
        commit=True,
    )
    return jsonify(response_payload), 201

from flask import Blueprint, jsonify, send_file

from auth import token_required
from models import Campaign, EncounterMap, db
from services.campaign_service import ensure_member, get_or_404
from services.encounter_map_service import encounter_map_labeled_path, encounter_map_path, latest_encounter_map

encounter_maps_bp = Blueprint('encounter_maps', __name__)


@encounter_maps_bp.route('/api/campaigns/<int:campaign_id>/encounter-maps/current', methods=['GET'])
@token_required
def get_current_encounter_map(current_user, campaign_id):
    campaign = get_or_404(Campaign, campaign_id)
    if not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    encounter_map = latest_encounter_map(campaign_id)
    is_dm = campaign.user_id == current_user.id
    return jsonify({'encounter_map': encounter_map.to_dict(include_private=is_dm) if encounter_map else None}), 200


@encounter_maps_bp.route('/api/encounter-maps/<int:encounter_map_id>/image', methods=['GET'])
@token_required
def get_encounter_map_image(current_user, encounter_map_id):
    encounter_map = get_or_404(EncounterMap, encounter_map_id)
    campaign = db.session.get(Campaign, encounter_map.campaign_id)
    if not campaign or not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    path = encounter_map_path(encounter_map)
    if not path.exists() or not path.is_file():
        return jsonify({'error': 'Encounter map image not found.'}), 404

    return send_file(path, mimetype='image/png', max_age=0)


@encounter_maps_bp.route('/api/encounter-maps/<int:encounter_map_id>/labeled-image', methods=['GET'])
@token_required
def get_encounter_map_labeled_image(current_user, encounter_map_id):
    encounter_map = get_or_404(EncounterMap, encounter_map_id)
    campaign = db.session.get(Campaign, encounter_map.campaign_id)
    if not campaign or not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    path = encounter_map_labeled_path(encounter_map)
    if not path or not path.exists() or not path.is_file():
        return jsonify({'error': 'Encounter map labeled image not found.'}), 404

    return send_file(path, mimetype='image/png', max_age=0)

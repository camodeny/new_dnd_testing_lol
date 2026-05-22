from flask import Blueprint, jsonify, request, send_file

from auth import token_required
from models import Campaign, CampaignMember, Character, EncounterMap, EncounterMapPlacement, db
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


@encounter_maps_bp.route('/api/encounter-maps/<int:encounter_map_id>/placements/me', methods=['PATCH'])
@token_required
def move_current_user_encounter_map_token(current_user, encounter_map_id):
    encounter_map = get_or_404(EncounterMap, encounter_map_id)
    campaign = db.session.get(Campaign, encounter_map.campaign_id)
    if not campaign or not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    data = request.get_json(silent=True) or {}
    try:
        grid_col = int(data.get('col'))
        grid_row = int(data.get('row'))
    except (TypeError, ValueError):
        return jsonify({'error': 'Token move requires integer col and row values.'}), 400

    grid = EncounterMap._json_value(encounter_map.grid_json, {})
    columns = grid.get('columns') if isinstance(grid, dict) else None
    rows = grid.get('rows') if isinstance(grid, dict) else None
    if grid_col < 0 or grid_row < 0:
        return jsonify({'error': 'Token move col and row must be 0 or greater.'}), 400
    if isinstance(columns, int) and grid_col >= columns:
        return jsonify({'error': f'Token move col must be less than map columns ({columns}).'}), 400
    if isinstance(rows, int) and grid_row >= rows:
        return jsonify({'error': f'Token move row must be less than map rows ({rows}).'}), 400

    member = CampaignMember.query.filter_by(
        campaign_id=campaign.id,
        user_id=current_user.id,
    ).first()
    character = member.selected_character if member and member.selected_character else None
    if not character:
        character = Character.query.filter_by(
            campaign_id=campaign.id,
            user_id=current_user.id,
        ).first()
    if not character:
        return jsonify({'error': 'No character sheet is assigned to your campaign member.'}), 400

    placement = EncounterMapPlacement.query.filter_by(
        encounter_map_id=encounter_map.id,
        actor_type='player',
        actor_id=str(current_user.id),
    ).first()
    if not placement:
        return jsonify({'error': 'No player token is placed for your character on this encounter map.'}), 404

    speed = max(int(character.speed or 0), 0)
    max_squares = speed // 5
    moved_squares = max(
        abs(grid_col - placement.grid_col),
        abs(grid_row - placement.grid_row),
    )
    if moved_squares > max_squares:
        return jsonify({
            'error': f'That move is {moved_squares} squares, but your character speed allows {max_squares} squares.',
            'movement': {
                'speed': speed,
                'max_squares': max_squares,
                'attempted_squares': moved_squares,
            },
        }), 400

    placement.grid_col = grid_col
    placement.grid_row = grid_row
    db.session.commit()

    include_private = campaign.user_id == current_user.id
    return jsonify({
        'encounter_map': encounter_map.to_dict(include_private=include_private),
        'placement': placement.to_dict(),
        'movement': {
            'speed': speed,
            'max_squares': max_squares,
            'moved_squares': moved_squares,
        },
    }), 200


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

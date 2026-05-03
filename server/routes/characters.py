from flask import Blueprint, jsonify, request

from auth import token_required
from models import db, Character
from services.character_service import (
    build_character_from_data,
    character_full_dict,
    update_character_relations,
)

characters_bp = Blueprint('characters', __name__)


@characters_bp.route('/api/characters', methods=['GET'])
@token_required
def get_characters(current_user):
    characters = Character.query.filter_by(user_id=current_user.id).all()
    return jsonify({'characters': [c.to_dict() for c in characters]}), 200


@characters_bp.route('/api/characters/<int:character_id>', methods=['GET'])
@token_required
def get_character(current_user, character_id):
    character = Character.query.get_or_404(character_id)
    if character.user_id != current_user.id:
        return jsonify({'error': 'Forbidden'}), 403

    return jsonify({'character': character_full_dict(character)}), 200


@characters_bp.route('/api/characters', methods=['POST'])
@token_required
def create_character(current_user):
    data = request.get_json()
    if not data or not data.get('name') or not data.get('race'):
        return jsonify({'error': 'Missing required fields: name and race'}), 400

    character = Character(user_id=current_user.id)
    build_character_from_data(character, data)
    character.campaign_id = data.get('campaign_id')

    db.session.add(character)
    db.session.flush()
    update_character_relations(character, data)
    db.session.commit()

    return jsonify({'message': 'Character created successfully', 'character': character_full_dict(character)}), 201


@characters_bp.route('/api/characters/<int:character_id>', methods=['PUT'])
@token_required
def update_character(current_user, character_id):
    character = Character.query.get_or_404(character_id)
    if character.user_id != current_user.id:
        return jsonify({'error': 'Forbidden'}), 403

    data = request.get_json()
    build_character_from_data(character, data)
    if 'campaign_id' in data:
        character.campaign_id = data['campaign_id']

    update_character_relations(character, data)
    db.session.commit()

    return jsonify({'message': 'Character updated successfully', 'character': character_full_dict(character)}), 200


@characters_bp.route('/api/characters/<int:character_id>', methods=['DELETE'])
@token_required
def delete_character(current_user, character_id):
    character = Character.query.get_or_404(character_id)
    if character.user_id != current_user.id:
        return jsonify({'error': 'Forbidden'}), 403

    db.session.delete(character)
    db.session.commit()
    return jsonify({'message': 'Character deleted successfully'}), 200

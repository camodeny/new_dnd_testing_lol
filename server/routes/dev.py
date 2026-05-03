import random

from flask import Blueprint, jsonify

from auth import token_required
from dev_data import DEV_CHARACTER_TEMPLATES
from models import db, Character
from services.character_service import (
    build_character_from_data,
    character_full_dict,
    update_character_relations,
)

dev_bp = Blueprint('dev', __name__)


@dev_bp.route('/api/dev/character', methods=['POST'])
@token_required
def create_dev_character(current_user):
    template = random.choice(DEV_CHARACTER_TEMPLATES)
    character = Character(user_id=current_user.id)
    build_character_from_data(character, template)

    db.session.add(character)
    db.session.flush()
    update_character_relations(character, template)
    db.session.commit()

    return jsonify({'message': 'Dev character created', 'character': character_full_dict(character)}), 201

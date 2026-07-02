from datetime import datetime
import random
import secrets

from flask import Blueprint, jsonify, request
from werkzeug.security import generate_password_hash

from auth import token_required
from dev_data import DEV_CHARACTER_TEMPLATES
from models import Campaign, CampaignMember, CampaignSession, Character, LLMPlayer, SheetProposal, User, db
from services.campaign_service import get_or_404
from services.character_service import build_character_from_data, character_full_dict, update_character_relations

llm_players_bp = Blueprint('llm_players', __name__)


def _owner_campaign_or_403(current_user, campaign_id):
    campaign = get_or_404(Campaign, campaign_id)
    if campaign.user_id != current_user.id:
        return None, (jsonify({'error': 'Forbidden'}), 403)
    return campaign, None


def _unique_llm_username(label):
    base = ' '.join((label or 'LLM Player').split())[:60] or 'LLM Player'
    username = base
    suffix = 2
    while User.query.filter_by(username=username).first():
        username = f'{base} {suffix}'
        suffix += 1
    return username


def _build_llm_character(user_id, campaign_id, label):
    template = random.choice(DEV_CHARACTER_TEMPLATES)
    character = Character(user_id=user_id, campaign_id=campaign_id, player_name=label)
    build_character_from_data(character, template)
    character.player_name = label
    db.session.add(character)
    db.session.flush()
    update_character_relations(character, template)
    return character


def _generate_llm_api_key():
    api_key = f'dndllm_{secrets.token_urlsafe(32)}'
    return api_key, generate_password_hash(api_key), api_key[:24]


def _next_safe_llm_user_id():
    max_user_id = db.session.query(db.func.max(User.id)).scalar() or 0
    max_llm_user_id = db.session.query(db.func.max(LLMPlayer.user_id)).scalar() or 0
    return max(max_user_id, max_llm_user_id) + 1


def _serialize_llm_player(campaign, llm_player):
    member = CampaignMember.query.filter_by(campaign_id=campaign.id, user_id=llm_player.user_id).first()
    character = member.selected_character if member and member.selected_character else None
    return {
        'llm_player': llm_player.to_dict(),
        'member': member.to_dict() if member else None,
        'character': character_full_dict(character) if character else None,
    }


def _llm_players_for_owner(owner_id):
    return (
        LLMPlayer.query
        .join(Campaign, LLMPlayer.campaign_id == Campaign.id)
        .filter(Campaign.user_id == owner_id)
        .order_by(LLMPlayer.created_at.asc())
        .all()
    )


@llm_players_bp.route('/api/campaigns/<int:campaign_id>/llm-players', methods=['GET'])
@token_required
def list_llm_players(current_user, campaign_id):
    campaign, error = _owner_campaign_or_403(current_user, campaign_id)
    if error:
        return error

    all_llm_players = _llm_players_for_owner(current_user.id)
    assigned = []
    available = []
    for llm_player in all_llm_players:
        payload = _serialize_llm_player(
            campaign if llm_player.campaign_id == campaign.id else get_or_404(Campaign, llm_player.campaign_id),
            llm_player,
        )
        source_campaign = db.session.get(Campaign, llm_player.campaign_id)
        payload['assigned_campaign'] = {
            'id': source_campaign.id,
            'name': source_campaign.name,
            'has_active_session': CampaignSession.query.filter_by(campaign_id=source_campaign.id, is_active=True).first() is not None,
        } if source_campaign else None
        if llm_player.campaign_id == campaign.id:
            assigned.append(payload)
        else:
            available.append(payload)

    return jsonify({
        'llm_players': assigned,
        'available_llm_players': available,
    }), 200


@llm_players_bp.route('/api/campaigns/<int:campaign_id>/llm-players', methods=['POST'])
@token_required
def create_llm_player(current_user, campaign_id):
    campaign, error = _owner_campaign_or_403(current_user, campaign_id)
    if error:
        return error

    data = request.get_json(silent=True) or {}
    existing_count = LLMPlayer.query.filter_by(campaign_id=campaign.id).count()
    label = str(data.get('label') or f'LLM Player {existing_count + 1}').strip()[:120] or f'LLM Player {existing_count + 1}'

    username = _unique_llm_username(label)
    email = f'llm-player-{campaign.id}-{secrets.token_hex(8)}@local.llm'
    llm_user = User(id=_next_safe_llm_user_id(), username=username, email=email)
    llm_user.set_password(secrets.token_urlsafe(32))
    db.session.add(llm_user)
    db.session.flush()

    character = _build_llm_character(llm_user.id, campaign.id, label)
    member = CampaignMember(
        campaign_id=campaign.id,
        user_id=llm_user.id,
        role='player',
        selected_character_id=character.id,
        character_ready_at=datetime.utcnow(),
    )
    db.session.add(member)

    api_key, api_key_hash, api_key_prefix = _generate_llm_api_key()
    llm_player = LLMPlayer(
        campaign_id=campaign.id,
        user_id=llm_user.id,
        label=label,
        api_key_hash=api_key_hash,
        api_key_prefix=api_key_prefix,
    )
    db.session.add(llm_player)
    db.session.commit()

    return jsonify({
        'llm_player': llm_player.to_dict(),
        'member': member.to_dict(),
        'character': character_full_dict(character),
        'api_key': api_key,
    }), 201


@llm_players_bp.route('/api/campaigns/<int:campaign_id>/llm-players/assign', methods=['POST'])
@token_required
def assign_existing_llm_player(current_user, campaign_id):
    campaign, error = _owner_campaign_or_403(current_user, campaign_id)
    if error:
        return error

    data = request.get_json(silent=True) or {}
    llm_player_id = data.get('llm_player_id')
    if not llm_player_id:
        return jsonify({'error': 'Missing llm_player_id'}), 400

    llm_player = (
        LLMPlayer.query
        .join(Campaign, LLMPlayer.campaign_id == Campaign.id)
        .filter(LLMPlayer.id == llm_player_id, Campaign.user_id == current_user.id)
        .first_or_404()
    )
    if llm_player.campaign_id == campaign.id:
        return jsonify({'error': 'That LLM player is already in this campaign'}), 400

    source_campaign = db.session.get(Campaign, llm_player.campaign_id)
    if CampaignSession.query.filter_by(campaign_id=source_campaign.id, is_active=True).first():
        return jsonify({'error': 'Cannot reassign an LLM player from a campaign with an active session'}), 400

    existing_target_member = CampaignMember.query.filter_by(campaign_id=campaign.id, user_id=llm_player.user_id).first()
    if existing_target_member:
        return jsonify({'error': 'That LLM player already has a member record in this campaign'}), 400

    source_member = CampaignMember.query.filter_by(campaign_id=source_campaign.id, user_id=llm_player.user_id).first()
    if not source_member:
        return jsonify({'error': 'LLM player member record not found'}), 404

    character = source_member.selected_character or Character.query.filter_by(campaign_id=source_campaign.id, user_id=llm_player.user_id).first()
    if not character:
        return jsonify({'error': 'LLM player character not found'}), 404

    llm_player.campaign_id = campaign.id
    source_member.campaign_id = campaign.id
    source_member.character_ready_at = datetime.utcnow()
    source_member.selected_character_id = character.id
    character.campaign_id = campaign.id
    character.player_name = llm_player.label
    db.session.commit()

    return jsonify(_serialize_llm_player(campaign, llm_player)), 200


@llm_players_bp.route('/api/campaigns/<int:campaign_id>/llm-players/<int:llm_player_id>/rotate-key', methods=['POST'])
@token_required
def rotate_llm_player_key(current_user, campaign_id, llm_player_id):
    campaign, error = _owner_campaign_or_403(current_user, campaign_id)
    if error:
        return error

    llm_player = LLMPlayer.query.filter_by(id=llm_player_id, campaign_id=campaign.id).first_or_404()
    api_key, api_key_hash, api_key_prefix = _generate_llm_api_key()
    llm_player.api_key_hash = api_key_hash
    llm_player.api_key_prefix = api_key_prefix
    db.session.commit()

    member = CampaignMember.query.filter_by(campaign_id=campaign.id, user_id=llm_player.user_id).first()
    character = member.selected_character if member and member.selected_character else None
    return jsonify({
        'llm_player': llm_player.to_dict(),
        'member': member.to_dict() if member else None,
        'character': character_full_dict(character) if character else None,
        'api_key': api_key,
    }), 200


@llm_players_bp.route('/api/campaigns/<int:campaign_id>/llm-players/<int:llm_player_id>', methods=['DELETE'])
@token_required
def delete_llm_player(current_user, campaign_id, llm_player_id):
    campaign, error = _owner_campaign_or_403(current_user, campaign_id)
    if error:
        return error

    llm_player = LLMPlayer.query.filter_by(id=llm_player_id, campaign_id=campaign.id).first_or_404()
    if CampaignSession.query.filter_by(campaign_id=campaign.id, is_active=True).first():
        return jsonify({'error': 'Cannot delete an LLM player while this campaign has an active session'}), 400

    member = CampaignMember.query.filter_by(campaign_id=campaign.id, user_id=llm_player.user_id).first()
    character = member.selected_character if member and member.selected_character else Character.query.filter_by(
        campaign_id=campaign.id,
        user_id=llm_player.user_id,
    ).first()

    if member:
        member.selected_character_id = None
        db.session.flush()
        db.session.delete(member)

    if character:
        SheetProposal.query.filter_by(character_id=character.id).delete(synchronize_session=False)
        db.session.delete(character)

    db.session.delete(llm_player)
    db.session.commit()
    return jsonify({'message': 'LLM player deleted'}), 200

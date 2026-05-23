import json
from datetime import datetime
from flask import Blueprint, jsonify, request

from auth import token_required
from models import db, Campaign, CampaignSession, CampaignWorld, Character, CampaignShop, SessionMessage, CharacterEquipment
from services.campaign_service import ensure_member, get_or_404

shops_bp = Blueprint('shops', __name__)


def _current_scene(campaign_id):
    world = CampaignWorld.query.filter_by(campaign_id=campaign_id).first()
    if not world:
        return {}
    try:
        world_state = json.loads(world.world_state) if world.world_state else {}
    except (TypeError, ValueError):
        world_state = {}
    current_scene = world_state.get('current_scene', {}) if isinstance(world_state, dict) else {}
    return current_scene if isinstance(current_scene, dict) else {}


def _same_scene(shop, current_scene):
    scene_location_id = current_scene.get('location_id')
    if shop.location_id and scene_location_id:
        return shop.location_id == scene_location_id

    scene_location_name = (current_scene.get('location_name') or '').strip().casefold()
    shop_location_name = (shop.location_name or '').strip().casefold()
    return bool(shop_location_name and scene_location_name and shop_location_name == scene_location_name)


def _shop_available_here(shop):
    return bool(shop.is_open) and _same_scene(shop, _current_scene(shop.campaign_id))


@shops_bp.route('/api/campaigns/<int:campaign_id>/shops', methods=['GET'])
@token_required
def list_shops(current_user, campaign_id):
    campaign = get_or_404(Campaign, campaign_id)
    if not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    current_scene = _current_scene(campaign_id)
    shops = CampaignShop.query.filter_by(
        campaign_id=campaign_id,
        is_open=True,
    ).order_by(CampaignShop.created_at.desc()).all()
    local_shops = [shop for shop in shops if _same_scene(shop, current_scene)]
    return jsonify({
        'current_scene': current_scene,
        'shops': [shop.to_dict() for shop in local_shops],
    }), 200


@shops_bp.route('/api/shops/<int:shop_id>/buy', methods=['POST'])
@token_required
def buy_item(current_user, shop_id):
    shop = get_or_404(CampaignShop, shop_id)
    campaign = db.session.get(Campaign, shop.campaign_id)
    if not campaign or not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    if not _shop_available_here(shop):
        return jsonify({'error': "This merchant is not available at the party's current location."}), 409

    data = request.get_json()
    if not data or not data.get('character_id') or not data.get('item_name'):
        return jsonify({'error': 'Missing character_id or item_name'}), 400

    character_id = data['character_id']
    item_name = data['item_name']

    character = Character.query.filter_by(id=character_id, campaign_id=campaign.id).first()
    if not character:
        return jsonify({'error': 'Character not found'}), 404

    is_dm = campaign.user_id == current_user.id
    is_owner = character.user_id == current_user.id
    is_npc = character.user_id is None

    if not is_owner and not (is_dm and is_npc):
        return jsonify({'error': 'Forbidden'}), 403

    try:
        items = json.loads(shop.items_json) if shop.items_json else []
    except Exception:
        items = []

    target_item = None
    for item in items:
        if item.get('name') == item_name:
            target_item = item
            break

    if not target_item:
        return jsonify({'error': 'Item not found in shop'}), 404

    cost_gp = target_item.get('cost_gp', 0)
    quantity = target_item.get('quantity')

    if (character.gp or 0) < cost_gp:
        return jsonify({'error': f'Insufficient gold. Cost is {cost_gp} gp, but character has {(character.gp or 0)} gp.'}), 400

    if quantity is not None:
        if quantity <= 0:
            return jsonify({'error': 'Item is out of stock'}), 400
        target_item['quantity'] = quantity - 1
        shop.items_json = json.dumps(items)

    character.gp = (character.gp or 0) - cost_gp

    equipment = CharacterEquipment.query.filter_by(character_id=character.id, name=item_name).first()
    if equipment:
        equipment.quantity = (equipment.quantity or 0) + 1
    else:
        equipment = CharacterEquipment(
            character_id=character.id,
            name=item_name,
            description=target_item.get('description', ''),
            quantity=1
        )
        db.session.add(equipment)

    character.updated_at = datetime.utcnow()

    active_session = CampaignSession.query.filter_by(campaign_id=campaign.id, is_active=True).first()
    if active_session:
        announcement = f'**{character.name}** purchased **{item_name}** from **{shop.name}** for {cost_gp} gp.'
        announcement_msg = SessionMessage(
            session_id=active_session.id,
            role='system',
            content=announcement
        )
        db.session.add(announcement_msg)

    db.session.commit()

    from services.character_service import character_full_dict
    return jsonify({
        'shop': shop.to_dict(),
        'character': character_full_dict(character)
    }), 200

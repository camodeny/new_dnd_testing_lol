from flask import Blueprint, jsonify, request, send_file
import json
import random

from auth import token_required
from models import Campaign, CampaignMember, Character, EncounterMap, EncounterMapPlacement, CampaignMonster, NPCActor, db
from services.campaign_service import ensure_member, get_or_404
from services.encounter_map_service import encounter_map_labeled_path, encounter_map_path, latest_encounter_map
from services.encounter_movement import reachable_cells

def build_initial_encounter_state(encounter_map, campaign):
    placements = encounter_map.placements
    turn_order = []
    
    for placement in placements:
        init_bonus = 0
        max_hp = 10
        current_hp = 10
        ac = 10
        speed = 30
        
        if placement.actor_type == 'player':
            character = Character.query.filter_by(campaign_id=campaign.id, user_id=int(placement.actor_id)).first()
            if character:
                init_bonus = int(character.initiative_bonus or 0)
                max_hp = int(character.max_hp or 10)
                current_hp = int(character.current_hp or 10)
                ac = int(character.armor_class or 10)
                speed = int(character.speed or 30)
        elif placement.actor_type == 'monster':
            monster = CampaignMonster.query.filter_by(campaign_id=campaign.id, monster_id=placement.actor_id).first()
            if monster:
                stat_block = json.loads(monster.stat_block) if monster.stat_block else {}
                init_bonus = stat_block.get('initiative_bonus') or stat_block.get('initiative')
                if init_bonus is None:
                    dex = stat_block.get('abilities', {}).get('dexterity') or stat_block.get('dexterity')
                    if isinstance(dex, int):
                        init_bonus = (dex - 10) // 2
                try:
                    init_bonus = int(init_bonus or 0)
                except (TypeError, ValueError):
                    init_bonus = 0
                max_hp = stat_block.get('max_hp') or stat_block.get('hp') or 10
                current_hp = max_hp
                ac = stat_block.get('armor_class') or stat_block.get('ac') or 10
                speed = stat_block.get('speed') or 30
        elif placement.actor_type == 'npc':
            npc = NPCActor.query.filter_by(campaign_id=campaign.id, actor_id=placement.actor_id).first()
            if npc:
                dossier = json.loads(npc.dossier) if npc.dossier else {}
                init_bonus = dossier.get('initiative_bonus') or dossier.get('initiative') or 0
                max_hp = dossier.get('max_hp') or dossier.get('hp') or 10
                current_hp = max_hp
                ac = dossier.get('armor_class') or dossier.get('ac') or 10
                speed = dossier.get('speed') or 30
                
        initiative_value = None
        if placement.actor_type in ('monster', 'npc'):
            initiative_value = random.randint(1, 20) + init_bonus
            
        turn_order.append({
            'placement_id': placement.id,
            'actor_type': placement.actor_type,
            'actor_id': placement.actor_id,
            'label': placement.label,
            'initiative': initiative_value,
            'initiative_bonus': init_bonus,
            'max_hp': max_hp,
            'current_hp': current_hp,
            'armor_class': ac,
            'speed': speed,
            'actions': {
                'action': True,
                'bonus_action': True,
                'reaction': True,
                'movement_remaining': speed
            }
        })
        
    return {
        'active': True,
        'round': 1,
        'active_turn_index': None,
        'turn_order': turn_order
    }

def check_and_start_turns(encounter_state):
    turn_order = encounter_state.get('turn_order', [])
    if not turn_order:
        return
    if any(item.get('initiative') is None for item in turn_order):
        return
        
    turn_order.sort(key=lambda x: x.get('placement_id', 0))
    turn_order.sort(key=lambda x: x.get('initiative_bonus', 0), reverse=True)
    turn_order.sort(key=lambda x: x.get('initiative', 0), reverse=True)
    
    encounter_state['turn_order'] = turn_order
    encounter_state['active_turn_index'] = 0
    if len(turn_order) > 0:
        active_combatant = turn_order[0]
        speed = active_combatant.get('speed', 30)
        active_combatant['actions'] = {
            'action': True,
            'bonus_action': True,
            'reaction': True,
            'movement_remaining': speed
        }

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

    state = EncounterMap._json_value(encounter_map.encounter_state_json, {})
    is_encounter_active = state.get('active', False) if isinstance(state, dict) else False

    if is_encounter_active:
        active_idx = state.get('active_turn_index')
        if active_idx is None:
            return jsonify({'error': 'Combat is active, but initiative rolling is not yet complete.'}), 400

        turn_order = state.get('turn_order', [])
        if not isinstance(turn_order, list) or active_idx < 0 or active_idx >= len(turn_order):
            return jsonify({'error': 'Invalid combat turn state.'}), 400

        active_combatant = turn_order[active_idx]
        if active_combatant.get('placement_id') != placement.id:
            return jsonify({'error': f"It is not your turn. It is currently {active_combatant.get('label')}'s turn."}), 400

        movement_remaining = active_combatant.get('actions', {}).get('movement_remaining', 0)
        speed = max(int(movement_remaining), 0)
    else:
        speed = max(int(character.speed or 0), 0)

    max_squares = speed // 5
    attempted_squares = max(
        abs(grid_col - placement.grid_col),
        abs(grid_row - placement.grid_row),
    )
    setup = EncounterMap._json_value(encounter_map.vtt_setup_json, {})
    if isinstance(columns, int) and isinstance(rows, int):
        reachable = reachable_cells(
            setup,
            columns,
            rows,
            placement.grid_col,
            placement.grid_row,
            max_squares,
        )
        movement_cost = reachable.get((grid_col, grid_row))
    else:
        reachable = {}
        movement_cost = attempted_squares

    if attempted_squares > max_squares and movement_cost is None:
        return jsonify({
            'error': f'That move is {attempted_squares} squares, but your speed allows {max_squares} squares.',
            'movement': {
                'speed': speed,
                'max_squares': max_squares,
                'attempted_squares': attempted_squares,
            },
        }), 400
    if movement_cost is None:
        return jsonify({
            'error': 'That destination is not reachable with your current speed and the map terrain.',
            'movement': {
                'speed': speed,
                'max_squares': max_squares,
                'attempted_squares': attempted_squares,
                'reachable_squares': len(reachable),
            },
        }), 400

    placement.grid_col = grid_col
    placement.grid_row = grid_row

    if is_encounter_active:
        movement_cost_feet = movement_cost * 5
        active_combatant['actions']['movement_remaining'] = max(0, speed - movement_cost_feet)
        encounter_map.encounter_state_json = json.dumps(state)

    db.session.commit()

    include_private = campaign.user_id == current_user.id
    return jsonify({
        'encounter_map': encounter_map.to_dict(include_private=include_private),
        'placement': placement.to_dict(),
        'movement': {
            'speed': speed,
            'max_squares': max_squares,
            'moved_squares': movement_cost,
            'attempted_squares': attempted_squares,
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


@encounter_maps_bp.route('/api/encounter-maps/<int:encounter_map_id>/encounter/roll-initiative', methods=['POST'])
@token_required
def roll_encounter_initiative(current_user, encounter_map_id):
    encounter_map = get_or_404(EncounterMap, encounter_map_id)
    campaign = db.session.get(Campaign, encounter_map.campaign_id)
    if not campaign or not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    data = request.get_json(silent=True) or {}
    actor_type = data.get('actor_type')
    actor_id = str(data.get('actor_id', ''))
    try:
        initiative = int(data.get('initiative'))
    except (TypeError, ValueError):
        return jsonify({'error': 'Initiative must be an integer.'}), 400

    is_dm = campaign.user_id == current_user.id
    if not is_dm:
        if actor_type != 'player' or actor_id != str(current_user.id):
            return jsonify({'error': 'Forbidden: You can only roll initiative for your own player character.'}), 403

    state = EncounterMap._json_value(encounter_map.encounter_state_json, {})
    if not state or not state.get('active'):
        return jsonify({'error': 'Combat is not active.'}), 400

    turn_order = state.get('turn_order', [])
    updated = False
    for combatant in turn_order:
        if combatant.get('actor_type') == actor_type and str(combatant.get('actor_id')) == actor_id:
            combatant['initiative'] = initiative
            updated = True
            break

    if not updated:
        return jsonify({'error': f'Combatant not found in turn order for {actor_type} {actor_id}.'}), 404

    check_and_start_turns(state)
    
    encounter_map.encounter_state_json = json.dumps(state)
    db.session.commit()

    return jsonify({
        'encounter_map': encounter_map.to_dict(include_private=is_dm)
    }), 200


@encounter_maps_bp.route('/api/encounter-maps/<int:encounter_map_id>/encounter/next-turn', methods=['POST'])
@token_required
def next_encounter_turn(current_user, encounter_map_id):
    encounter_map = get_or_404(EncounterMap, encounter_map_id)
    campaign = db.session.get(Campaign, encounter_map.campaign_id)
    if not campaign or not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    state = EncounterMap._json_value(encounter_map.encounter_state_json, {})
    if not state or not state.get('active'):
        return jsonify({'error': 'Combat is not active.'}), 400

    active_turn_index = state.get('active_turn_index')
    if active_turn_index is None:
        return jsonify({'error': 'Initiative is not fully rolled yet.'}), 400

    turn_order = state.get('turn_order', [])
    if not turn_order:
        return jsonify({'error': 'Turn order is empty.'}), 400

    active_combatant = turn_order[active_turn_index]
    if active_combatant.get('actor_type') != 'player' or str(active_combatant.get('actor_id')) != str(current_user.id):
        return jsonify({'error': 'Forbidden: Only the active player combatant can end their turn.'}), 403

    next_index = (active_turn_index + 1) % len(turn_order)
    if next_index == 0:
        state['round'] = state.get('round', 1) + 1

    state['active_turn_index'] = next_index

    next_combatant = turn_order[next_index]
    speed = next_combatant.get('speed', 30)
    next_combatant['actions'] = {
        'action': True,
        'bonus_action': True,
        'reaction': True,
        'movement_remaining': speed
    }

    encounter_map.encounter_state_json = json.dumps(state)
    db.session.commit()

    is_dm = campaign.user_id == current_user.id
    return jsonify({
        'encounter_map': encounter_map.to_dict(include_private=is_dm)
    }), 200



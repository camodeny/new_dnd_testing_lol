from datetime import datetime

from flask import Blueprint, jsonify, request

from auth import token_required
from models import db, Campaign, Character, CharacterPlanningMessage, PlanningBondProposal
from openrouter import (
    get_character_draft,
    get_planning_dm_response,
    get_planning_summary_update,
)
from services.campaign_service import ensure_member
from services.character_service import character_full_dict
from services.planning_service import (
    apply_bond_suggestions,
    get_campaign_members,
    get_member,
    get_or_create_summary,
    json_dumps,
    json_loads,
    merge_summary_update,
    planning_context,
    visible_planning_payload,
)

planning_bp = Blueprint('planning', __name__)


def require_planning_member(current_user, campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if not ensure_member(campaign, current_user):
        return None, (jsonify({'error': 'Forbidden'}), 403)

    get_campaign_members(campaign)
    member = get_member(campaign_id, current_user.id)
    if not member:
        return None, (jsonify({'error': 'Campaign member not found'}), 404)

    return (campaign, member), None


@planning_bp.route('/api/campaigns/<int:campaign_id>/planning', methods=['GET'])
@token_required
def get_planning(current_user, campaign_id):
    result, error = require_planning_member(current_user, campaign_id)
    if error:
        return error

    campaign, _member = result
    db.session.commit()
    return jsonify({'planning': visible_planning_payload(campaign, current_user)}), 200


@planning_bp.route('/api/campaigns/<int:campaign_id>/planning/messages', methods=['POST'])
@token_required
def send_planning_message(current_user, campaign_id):
    result, error = require_planning_member(current_user, campaign_id)
    if error:
        return error

    campaign, _member = result
    data = request.get_json()
    content = (data.get('content') if data else '').strip()
    if not content:
        return jsonify({'error': 'Missing content'}), 400

    player_msg = CharacterPlanningMessage(
        campaign_id=campaign_id,
        user_id=current_user.id,
        role='player',
        content=content,
    )
    db.session.add(player_msg)
    db.session.commit()

    messages = CharacterPlanningMessage.query.filter_by(
        campaign_id=campaign_id,
        user_id=current_user.id,
    ).order_by(CharacterPlanningMessage.created_at.asc()).all()
    context = planning_context(campaign, current_user)
    ai_result = get_planning_dm_response(
        context,
        messages,
        draft_character=data.get('draft_character') if data else None,
        active_page=data.get('active_page') if data else None,
    )
    if not ai_result:
        return jsonify({
            'error': 'The planning DM could not respond',
            'planning': visible_planning_payload(campaign, current_user),
        }), 500
    ai_text = ai_result.get('message') or ''

    summary_payload = get_planning_summary_update(
        planning_context(campaign, current_user),
        content,
        ai_text,
    )
    summary = get_or_create_summary(campaign_id)
    merge_summary_update(summary, summary_payload.get('summary_update', {}))
    apply_bond_suggestions(campaign_id, summary_payload.get('bond_suggestions', []))

    dm_msg = CharacterPlanningMessage(
        campaign_id=campaign_id,
        user_id=current_user.id,
        role='dm',
        content=ai_text,
    )
    db.session.add(dm_msg)

    db.session.commit()
    return jsonify({
        'planning': visible_planning_payload(campaign, current_user),
        'active_page': ai_result.get('active_page'),
        'form_patch': ai_result.get('form_patch') or {},
    }), 201


@planning_bp.route('/api/campaigns/<int:campaign_id>/planning/draft', methods=['POST'])
@token_required
def generate_character_draft(current_user, campaign_id):
    result, error = require_planning_member(current_user, campaign_id)
    if error:
        return error

    campaign, _member = result
    messages = CharacterPlanningMessage.query.filter_by(
        campaign_id=campaign_id,
        user_id=current_user.id,
    ).order_by(CharacterPlanningMessage.created_at.asc()).all()
    if not messages:
        return jsonify({'error': 'Chat with the DM before generating a draft'}), 400

    draft = get_character_draft(planning_context(campaign, current_user), messages)
    if not draft:
        return jsonify({'error': 'The planning DM could not generate a character draft'}), 500

    draft['campaign_id'] = campaign_id
    draft.setdefault('player_name', current_user.username)
    return jsonify({'draft': draft}), 200


@planning_bp.route('/api/campaigns/<int:campaign_id>/planning/character', methods=['PUT'])
@token_required
def select_planning_character(current_user, campaign_id):
    result, error = require_planning_member(current_user, campaign_id)
    if error:
        return error

    campaign, member = result
    data = request.get_json()
    character_id = data.get('character_id') if data else None
    if not character_id:
        member.selected_character_id = None
        member.character_ready_at = None
        db.session.commit()
        return jsonify({'planning': visible_planning_payload(campaign, current_user)}), 200

    character = Character.query.get_or_404(character_id)
    if character.user_id != current_user.id:
        return jsonify({'error': 'Forbidden'}), 403

    character.campaign_id = campaign_id
    member.selected_character_id = character.id
    member.character_ready_at = None
    db.session.commit()
    return jsonify({
        'character': character_full_dict(character),
        'planning': visible_planning_payload(campaign, current_user),
    }), 200


@planning_bp.route('/api/campaigns/<int:campaign_id>/planning/ready', methods=['PUT'])
@token_required
def update_planning_ready(current_user, campaign_id):
    result, error = require_planning_member(current_user, campaign_id)
    if error:
        return error

    campaign, member = result
    data = request.get_json() or {}
    ready = bool(data.get('ready'))
    if ready and not member.selected_character_id:
        return jsonify({'error': 'Select a character before marking ready'}), 400

    if ready:
        character = Character.query.get_or_404(member.selected_character_id)
        if character.user_id != current_user.id or character.campaign_id != campaign_id:
            member.selected_character_id = None
            member.character_ready_at = None
            db.session.commit()
            return jsonify({'error': 'Selected character is no longer available'}), 400
        member.character_ready_at = datetime.utcnow()
    else:
        member.character_ready_at = None

    db.session.commit()
    return jsonify({'planning': visible_planning_payload(campaign, current_user)}), 200


@planning_bp.route('/api/campaigns/<int:campaign_id>/planning/bonds/<int:bond_id>', methods=['PUT'])
@token_required
def update_planning_bond(current_user, campaign_id, bond_id):
    result, error = require_planning_member(current_user, campaign_id)
    if error:
        return error

    campaign, _member = result
    bond = PlanningBondProposal.query.filter_by(id=bond_id, campaign_id=campaign_id).first_or_404()
    data = request.get_json() or {}
    action = data.get('action')
    bond_data = bond.to_dict()
    involved = bond_data['involved_user_ids']
    if current_user.id not in involved:
        return jsonify({'error': 'Only involved players can update this bond'}), 403
    if bond.status != 'pending':
        return jsonify({'error': 'This bond is no longer pending'}), 400

    approvals = json_loads(bond.approval_states, {})
    if action == 'edit':
        if data.get('title'):
            bond.title = data['title'][:200]
        if data.get('description'):
            bond.description = data['description']
        approvals = {str(user_id): 'pending' for user_id in involved}
        approvals[str(current_user.id)] = 'accepted'
    elif action == 'accept':
        approvals[str(current_user.id)] = 'accepted'
    elif action == 'decline':
        approvals[str(current_user.id)] = 'declined'
        bond.status = 'declined'
    else:
        return jsonify({'error': 'Action must be accept, edit, or decline'}), 400

    if bond.status == 'pending' and all(approvals.get(str(user_id)) == 'accepted' for user_id in involved):
        bond.status = 'confirmed'
        summary = get_or_create_summary(campaign_id)
        accepted_hooks = json_loads(summary.accepted_hooks, [])
        hook = f'{bond.title}: {bond.description}'
        if hook not in accepted_hooks:
            accepted_hooks.append(hook)
        summary.accepted_hooks = json_dumps(accepted_hooks)

    bond.approval_states = json_dumps(approvals)
    db.session.commit()
    return jsonify({'planning': visible_planning_payload(campaign, current_user)}), 200

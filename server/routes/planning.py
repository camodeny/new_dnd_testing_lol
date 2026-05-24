from datetime import datetime
import json
import queue

from flask import Blueprint, current_app, jsonify, request

from auth import token_required
from models import db, Campaign, Character, CharacterPlanningMessage, PlanningBondProposal, User
from openrouter import (
    get_planning_dm_response,
    get_planning_summary_update,
)
from services.campaign_service import ensure_member, get_or_404
from services.character_service import character_full_dict
from services.audit_service import log_audit_event
from services.planning_stream import planning_stream_manager
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
    campaign = get_or_404(Campaign, campaign_id)
    if not ensure_member(campaign, current_user):
        return None, (jsonify({'error': 'Forbidden'}), 403)

    get_campaign_members(campaign)
    member = get_member(campaign_id, current_user.id)
    if not member:
        return None, (jsonify({'error': 'Campaign member not found'}), 404)

    return (campaign, member), None


def pending_bonds_for_user(campaign_id, user_id):
    pending_bonds = PlanningBondProposal.query.filter_by(
        campaign_id=campaign_id,
        status='pending',
    ).all()
    return [
        bond for bond in pending_bonds
        if user_id in json_loads(bond.involved_user_ids, [])
    ]


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
    log_audit_event(
        campaign_id,
        'player_input_stored',
        'Stored character-planning player message.',
        {'message': player_msg.to_dict(), 'request_body': data or {}},
        source='planning.messages',
        actor=current_user.username,
        commit=True,
    )

    # In test mode, run synchronously (same as before)
    if (
        current_app.config.get('TESTING')
        or current_app.testing
        or current_app.config.get('SQLALCHEMY_DATABASE_URI') == 'sqlite:///:memory:'
    ):
        dm_trace_id = f'planning_dm:campaign_{campaign_id}:message_{player_msg.id}'
        memory_trace_id = f'planning_memory_writer:campaign_{campaign_id}:message_{player_msg.id}'
        messages = CharacterPlanningMessage.query.filter_by(
            campaign_id=campaign_id,
            user_id=current_user.id,
        ).order_by(CharacterPlanningMessage.created_at.asc()).all()
        context = planning_context(campaign, current_user)
        log_audit_event(
            campaign_id,
            'planning_context_read',
            'Read planning context for planning DM response.',
            {'context': context, 'message_count': len(messages)},
            source='planning_context',
            actor='server',
            commit=True,
        )
        ai_result = get_planning_dm_response(
            context,
            messages,
            draft_character=data.get('draft_character') if data else None,
            active_page=data.get('active_page') if data else None,
            audit_context={
                'campaign_id': campaign_id,
                'operation': 'planning_dm_response',
                'actor': 'planning_dm',
                'trace_id': dm_trace_id,
                'trace_label': f'planning_dm: campaign {campaign_id}',
            },
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
            audit_context={
                'campaign_id': campaign_id,
                'operation': 'planning_summary_update',
                'actor': 'planning_memory_writer',
                'trace_id': memory_trace_id,
                'parent_trace_id': dm_trace_id,
                'trace_label': f'planning_memory_writer: campaign {campaign_id}',
            },
        )
        summary = get_or_create_summary(campaign_id)
        merge_summary_update(summary, summary_payload.get('summary_update', {}))
        apply_bond_suggestions(campaign_id, summary_payload.get('bond_suggestions', []))
        log_audit_event(
            campaign_id,
            'planning_memory_write',
            'Updated campaign planning memory from player and DM exchange.',
            {
                'latest_player_message': content,
                'latest_dm_message': ai_text,
                'summary_payload': summary_payload,
            },
            source='campaign_planning_summaries',
            actor='planning_memory_writer',
            trace_id=memory_trace_id,
            parent_trace_id=dm_trace_id,
            trace_label=f'planning_memory_writer: campaign {campaign_id}',
            commit=False,
        )

        dm_msg = CharacterPlanningMessage(
            campaign_id=campaign_id,
            user_id=current_user.id,
            role='dm',
            content=ai_text,
        )
        db.session.add(dm_msg)
        log_audit_event(
            campaign_id,
            'dm_output_stored',
            'Stored visible planning DM response.',
            {
                'message': {
                    'campaign_id': campaign_id,
                    'user_id': current_user.id,
                    'role': 'dm',
                    'content': ai_text,
                },
                'active_page': ai_result.get('active_page'),
                'form_patch': ai_result.get('form_patch') or {},
            },
            source='character_planning_messages',
            actor='planning_dm',
            trace_id=dm_trace_id,
            trace_label=f'planning_dm: campaign {campaign_id}',
            commit=False,
        )

        db.session.commit()
        planning_payload = visible_planning_payload(campaign, current_user)
        return jsonify({
            'planning': planning_payload,
            'active_page': ai_result.get('active_page'),
            'form_patch': ai_result.get('form_patch') or {},
        }), 201

    # Production: kick off async streaming generation
    planning_stream_manager.start_generation(
        campaign_id,
        current_user.id,
        player_msg.id,
        content,
        draft_character=data.get('draft_character') if data else None,
        active_page=data.get('active_page') if data else None,
    )
    return jsonify({'streaming': True}), 202


@planning_bp.route('/api/campaigns/<int:campaign_id>/planning/stream', methods=['GET'])
def stream_planning(campaign_id):
    """SSE endpoint for streaming planning DM responses."""
    import jwt

    token_str = request.args.get('token')
    if not token_str:
        return jsonify({'error': 'Unauthorized'}), 401

    try:
        token_data = jwt.decode(token_str, current_app.config['SECRET_KEY'], algorithms=['HS256'])
        token_user_id = token_data.get('user_id')
    except Exception:
        return jsonify({'error': 'Unauthorized'}), 401

    if not token_user_id:
        return jsonify({'error': 'Unauthorized'}), 401

    current_user = db.session.get(User, token_user_id)
    if not current_user:
        return jsonify({'error': 'Unauthorized'}), 401

    campaign = db.session.get(Campaign, campaign_id)
    if not campaign or not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    worker = planning_stream_manager.get_worker(campaign_id, current_user.id)
    if not worker:
        def idle_stream():
            yield 'data: ' + json.dumps({'type': 'idle'}) + '\n\n'
        return current_app.response_class(idle_stream(), mimetype='text/event-stream')

    q = worker.add_listener()

    def event_stream():
        try:
            while True:
                try:
                    event = q.get(timeout=30)
                    yield f'data: {json.dumps(event)}\n\n'
                    if event.get('type') in ('done', 'error'):
                        break
                except queue.Empty:
                    yield ': ping\n\n'
        finally:
            worker.remove_listener(q)

    response = current_app.response_class(event_stream(), mimetype='text/event-stream')
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['X-Accel-Buffering'] = 'no'
    return response


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

    character = get_or_404(Character, character_id)
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
    if ready and pending_bonds_for_user(campaign_id, current_user.id):
        return jsonify({'error': 'Resolve pending bond proposals before marking ready'}), 400

    if ready:
        character = get_or_404(Character, member.selected_character_id)
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

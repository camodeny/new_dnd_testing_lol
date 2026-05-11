from datetime import datetime

from flask import Blueprint, jsonify, request

from auth import token_required
from models import db, Campaign, CampaignSession, SessionMessage
from openrouter import get_opening_scene_response, get_session_dm_response_with_tools, get_session_memory_patch
from services.audit_service import log_audit_event
from services.campaign_service import ensure_member
from services.dm_tools import (
    DM_TOOL_DEFINITIONS,
    apply_memory_patch,
    build_session_hot_context,
    build_session_memory_context,
    context_manifest,
    execute_dm_tool,
)
from services.planning_service import can_start_session, planning_context
from services.world_service import approve_world, dm_world_context, ensure_world_generated

sessions_bp = Blueprint('sessions', __name__)


@sessions_bp.route('/api/campaigns/<int:campaign_id>/sessions', methods=['POST'])
@token_required
def start_session(current_user, campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    active = CampaignSession.query.filter_by(campaign_id=campaign_id, is_active=True).first()
    if active:
        return jsonify({'error': 'An active session already exists'}), 400

    ready, details = can_start_session(campaign)
    if not ready:
        db.session.commit()
        return jsonify({
            'error': 'Every party member must select and ready a character before starting a session',
            'planning': details,
        }), 400

    world, world_error = ensure_world_generated(campaign, current_user)
    if world_error:
        return jsonify({key: value for key, value in world_error.items() if key != 'status'}), world_error.get('status', 500)

    session = CampaignSession(campaign_id=campaign_id)
    db.session.add(session)
    approve_world(world)
    campaign.last_played_at = datetime.utcnow()
    db.session.flush()
    log_audit_event(
        campaign_id,
        'session_started',
        'Created active campaign session and approved the world package.',
        {
            'session': session.to_dict(),
            'world': world.to_public_dict(),
        },
        source='campaign_sessions',
        actor=current_user.username,
        commit=True,
    )

    context = planning_context(campaign, current_user)
    world_context = dm_world_context(campaign, audit=True, reason='opening_scene_context')
    opening_text = get_opening_scene_response(
        context,
        world_context,
        audit_context={
            'campaign_id': campaign_id,
            'operation': 'opening_scene',
            'actor': 'session_dm',
        },
    )
    if opening_text:
        opening_msg = SessionMessage(
            session_id=session.id,
            role='dm',
            content=opening_text,
        )
        db.session.add(opening_msg)
        log_audit_event(
            campaign_id,
            'dm_output_stored',
            'Stored opening visible DM message.',
            {
                'session_id': session.id,
                'message': {
                    'role': 'dm',
                    'content': opening_text,
                },
            },
            source='session_messages',
            actor='session_dm',
            commit=False,
        )

    db.session.commit()
    data = session.to_dict()
    data['messages'] = [m.to_dict() for m in session.messages]
    log_audit_event(
        campaign_id,
        'client_response_sent',
        'Sent started session payload to client.',
        {'session': data},
        source='campaign_sessions',
        actor='server',
        commit=True,
    )
    return jsonify({'session': data}), 201


@sessions_bp.route('/api/campaigns/<int:campaign_id>/sessions', methods=['GET'])
@token_required
def list_sessions(current_user, campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    sessions = CampaignSession.query.filter_by(campaign_id=campaign_id).order_by(CampaignSession.started_at.desc()).all()
    return jsonify({'sessions': [s.to_dict() for s in sessions]}), 200


@sessions_bp.route('/api/sessions/<int:session_id>', methods=['GET'])
@token_required
def get_session(current_user, session_id):
    session = CampaignSession.query.get_or_404(session_id)
    campaign = Campaign.query.get(session.campaign_id)
    if not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    data = session.to_dict()
    data['messages'] = [m.to_dict() for m in session.messages]
    return jsonify({'session': data}), 200


@sessions_bp.route('/api/sessions/<int:session_id>', methods=['PUT'])
@token_required
def end_session(current_user, session_id):
    session = CampaignSession.query.get_or_404(session_id)
    campaign = Campaign.query.get(session.campaign_id)
    if not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    data = request.get_json()
    session.is_active = False
    session.ended_at = datetime.utcnow()
    if data and 'recap' in data:
        session.recap = data['recap']

    db.session.commit()
    return jsonify({'session': session.to_dict()}), 200


@sessions_bp.route('/api/sessions/<int:session_id>/messages', methods=['GET'])
@token_required
def get_messages(current_user, session_id):
    session = CampaignSession.query.get_or_404(session_id)
    campaign = Campaign.query.get(session.campaign_id)
    if not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    messages = SessionMessage.query.filter_by(session_id=session_id).order_by(SessionMessage.created_at).all()
    return jsonify({'messages': [m.to_dict() for m in messages]}), 200


@sessions_bp.route('/api/sessions/<int:session_id>/messages', methods=['POST'])
@token_required
def send_message(current_user, session_id):
    session = CampaignSession.query.get_or_404(session_id)
    campaign = Campaign.query.get(session.campaign_id)
    if not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    data = request.get_json()
    if not data or not data.get('content'):
        return jsonify({'error': 'Missing content'}), 400

    content = data['content']
    msg = SessionMessage(
        session_id=session_id,
        user_id=current_user.id,
        role=data.get('role', 'player'),
        content=content,
    )
    db.session.add(msg)
    db.session.commit()
    result_messages = [msg.to_dict()]
    log_audit_event(
        campaign.id,
        'player_input_stored',
        'Stored session player message.',
        {'session_id': session_id, 'message': msg.to_dict(), 'request_body': data},
        source='session_messages',
        actor=current_user.username,
        commit=True,
    )

    try:
        hot_context = build_session_hot_context(campaign, session, current_user)
        manifest = context_manifest(hot_context, DM_TOOL_DEFINITIONS)
        log_audit_event(
            campaign.id,
            'session_hot_context_read',
            'Read compact hot context for session DM response.',
            {'context': hot_context, 'context_manifest': manifest},
            source='session_context',
            actor='server',
            commit=True,
        )
        recent_messages = SessionMessage.query.filter_by(session_id=session_id).order_by(
            SessionMessage.created_at.asc(),
        ).all()[-8:]
        trace_id = f'session_dm:session_{session_id}:message_{msg.id}'
        ai_text = get_session_dm_response_with_tools(
            hot_context,
            recent_messages,
            DM_TOOL_DEFINITIONS,
            lambda name, args, tool_audit: execute_dm_tool(campaign, session, current_user, name, args, tool_audit),
            audit_context={
                'campaign_id': campaign.id,
                'operation': 'session_dm_response',
                'actor': 'session_dm',
                'trace_id': trace_id,
                'trace_label': f'session_dm: session {session_id}',
                'context_manifest': manifest,
                'full_world_graph_included': False,
            },
        )
    except RuntimeError as err:
        return jsonify({'error': str(err), 'messages': result_messages}), 500
    except Exception as err:
        return jsonify({'error': repr(err), 'messages': result_messages}), 500

    if ai_text:
        ai_msg = SessionMessage(
            session_id=session_id,
            role='dm',
            content=ai_text,
        )
        db.session.add(ai_msg)
        db.session.flush()
        log_audit_event(
            campaign.id,
            'dm_output_stored',
            'Stored visible session DM response.',
            {
                'session_id': session_id,
                'message': {
                    'role': 'dm',
                    'content': ai_text,
                },
            },
            source='session_messages',
            actor='session_dm',
            commit=False,
        )
        memory_trace_id = f'session_memory_writer:session_{session_id}:message_{msg.id}'
        try:
            memory_context = build_session_memory_context(
                campaign,
                session,
                current_user,
                content,
                ai_text,
                hot_context,
            )
            memory_patch = get_session_memory_patch(
                memory_context,
                audit_context={
                    'campaign_id': campaign.id,
                    'operation': 'session_memory_update',
                    'actor': 'session_memory_writer',
                    'trace_id': memory_trace_id,
                    'parent_trace_id': trace_id,
                    'trace_label': f'session_memory_writer: session {session_id}',
                },
            )
            if memory_patch:
                apply_memory_patch(
                    campaign,
                    session,
                    memory_patch,
                    audit_context={
                        'trace_id': memory_trace_id,
                        'parent_trace_id': trace_id,
                        'trace_label': f'session_memory_writer: session {session_id}',
                    },
                )
        except Exception as err:
            log_audit_event(
                campaign.id,
                'memory_update_error',
                'Post-turn memory update failed after visible DM response.',
                {'session_id': session_id, 'error': repr(err)},
                source='session_memory',
                actor='session_memory_writer',
                trace_id=memory_trace_id,
                parent_trace_id=trace_id,
                trace_label=f'session_memory_writer: session {session_id}',
                audit_role='tools',
                commit=False,
            )
        db.session.commit()
        result_messages.append(ai_msg.to_dict())
    else:
        db.session.rollback()

    log_audit_event(
        campaign.id,
        'client_response_sent',
        'Sent session message response payload to client.',
        {'session_id': session_id, 'messages': result_messages},
        source='session_messages',
        actor='server',
        commit=True,
    )
    return jsonify({'messages': result_messages}), 201

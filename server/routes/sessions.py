from datetime import datetime
import json
import queue

from flask import Blueprint, current_app, jsonify, request

from auth import authenticate_request, token_required
from models import (
    db,
    Campaign,
    CampaignAuditEvent,
    CampaignSession,
    SessionMessage,
    SheetProposal,
    Character,
    User,
)
from openrouter import (
    get_opening_scene_response,
    get_session_clock_updates,
    get_session_dm_response_with_tools,
    get_session_memory_patch,
    normalize_session_dm_turn_decision,
)
from services.stream_manager import stream_manager
from services.planning_stream import planning_stream_manager
from services.audit_service import log_audit_event
from services.campaign_service import ensure_member, get_or_404
from services.character_service import character_full_dict
from services.dm_tools import (
    get_dm_tool_definitions,
    SHEET_SCALAR_FIELDS,
    apply_clock_adjudication,
    apply_memory_patch,
    build_session_hot_context,
    build_session_clock_context,
    build_session_memory_context,
    context_manifest,
    execute_dm_tool,
)
from services.dm_turns import (
    begin_session_dm_turn,
    mark_session_dm_turn_error,
    mark_session_dm_turn_post_turn_complete,
    mark_session_dm_turn_visible,
    session_dm_trace_id,
    session_dm_turn_status_payload,
)
from services.dev_combat_sandbox import is_combat_sandbox_campaign, start_combat_sandbox_session
from services.planning_service import can_start_session, planning_context
from services.world_service import approve_world, dm_world_context, ensure_world_generated, world_public_payload

sessions_bp = Blueprint('sessions', __name__)

DEFAULT_MESSAGE_PAGE_SIZE = 50
MAX_MESSAGE_PAGE_SIZE = 100


def _message_page(session_id, before_id=None, limit=DEFAULT_MESSAGE_PAGE_SIZE):
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = DEFAULT_MESSAGE_PAGE_SIZE
    limit = max(1, min(limit, MAX_MESSAGE_PAGE_SIZE))

    query = SessionMessage.query.filter_by(session_id=session_id)
    if before_id:
        try:
            before_id = int(before_id)
        except (TypeError, ValueError):
            before_id = None
        if before_id:
            query = query.filter(SessionMessage.id < before_id)

    rows = query.order_by(SessionMessage.id.desc()).limit(limit + 1).all()
    has_more = len(rows) > limit
    messages = list(reversed(rows[:limit]))
    return {
        'messages': [message.to_dict() for message in messages],
        'has_more_messages': has_more,
    }


def _session_dm_turn_decision(raw_result):
    decision = normalize_session_dm_turn_decision(raw_result)
    if decision.get('mode') == 'silent':
        return {
            'mode': 'silent',
            'content': '',
            'reason': decision.get('reason') or 'The DM intentionally stayed silent.',
        }
    return {
        'mode': 'speak',
        'content': decision.get('content') or '',
    }


def _member_record(campaign_id, user_id):
    from models import CampaignMember

    return CampaignMember.query.filter_by(campaign_id=campaign_id, user_id=user_id).first()


def _run_session_memory_update(
    campaign_id,
    session_id,
    user_id,
    player_message_id,
    player_content,
    ai_text,
    hot_context,
    parent_trace_id,
    dm_message_id=None,
):
    from uuid import uuid4
    memory_run_id = f"memrun_{uuid4().hex[:12]}"
    memory_trace_id = f'session_memory_writer:session_{session_id}:message_{player_message_id}'
    trace_label = f'session_memory_writer: session {session_id}'
    clock_trace_id = f'session_clock_adjudicator:session_{session_id}:message_{player_message_id}'
    clock_trace_label = f'session_clock_adjudicator: session {session_id}'
    memory_complete = False
    clock_complete = False
    try:
        campaign = db.session.get(Campaign, campaign_id)
        session = db.session.get(CampaignSession, session_id)
        current_user = db.session.get(User, user_id)
        if not campaign or not session or not current_user:
            return

        world_before = world_public_payload(campaign).get('world') or {}
        current_scene_before = world_before.get('current_scene')

        memory_context = build_session_memory_context(
            campaign,
            session,
            current_user,
            player_content,
            ai_text,
            hot_context,
        )
        memory_audit_context = {
            'campaign_id': campaign.id,
            'operation': 'session_memory_update',
            'actor': 'session_memory_writer',
            'trace_id': memory_trace_id,
            'parent_trace_id': parent_trace_id,
            'trace_label': trace_label,
            'memory_run_id': memory_run_id,
            'source_player_message_id': player_message_id,
            'source_dm_message_id': dm_message_id,
            'latest_player_message': player_content,
            'latest_dm_message': ai_text,
        }

        memory_patch = get_session_memory_patch(
            memory_context,
            audit_context=memory_audit_context,
        )
        if memory_patch:
            apply_memory_patch(
                campaign,
                session,
                memory_patch,
                audit_context=memory_audit_context,
            )
            memory_complete = True
        world_after_memory = world_public_payload(campaign).get('world') or {}
        current_scene_after_memory = world_after_memory.get('current_scene')
        clock_context = build_session_clock_context(
            campaign,
            session,
            current_user,
            player_content,
            ai_text,
            current_scene_before,
            current_scene_after_memory,
        )
        clock_updates = get_session_clock_updates(
            clock_context,
            audit_context={
                'campaign_id': campaign.id,
                'operation': 'session_clock_adjudication',
                'actor': 'session_clock_adjudicator',
                'trace_id': clock_trace_id,
                'parent_trace_id': parent_trace_id,
                'trace_label': clock_trace_label,
            },
        )
        if clock_updates:
            apply_clock_adjudication(
                campaign,
                clock_updates,
                audit_context={
                    'trace_id': clock_trace_id,
                    'parent_trace_id': parent_trace_id,
                    'trace_label': clock_trace_label,
                },
            )
            clock_complete = True
        db.session.commit()

        mark_session_dm_turn_post_turn_complete(
            player_message_id,
            dm_message_id=dm_message_id,
        )
        db.session.commit()

        world_after = world_public_payload(campaign).get('world') or {}
        current_scene_after = world_after.get('current_scene')
        if current_scene_after != current_scene_before:
            stream_manager.broadcast_event(session_id, {
                'type': 'scene_updated',
                'current_scene': current_scene_after,
            })
    except Exception as err:
        db.session.rollback()
        log_audit_event(
            campaign_id,
            'memory_update_error',
            'Post-turn memory update failed after visible DM response.',
            {'session_id': session_id, 'error': repr(err), 'telemetry': memory_audit_context.get('telemetry') if 'memory_audit_context' in locals() else None},
            source='session_memory',
            actor='session_memory_writer',
            trace_id=memory_trace_id,
            parent_trace_id=parent_trace_id,
            trace_label=trace_label,
            audit_role='tools',
            commit=True,
        )
        mark_session_dm_turn_error(
            campaign_id,
            session_id,
            player_message_id,
            parent_trace_id,
            repr(err),
            dm_message_id=dm_message_id,
            memory_status='complete' if memory_complete else 'error',
            clock_status='error' if memory_complete and not clock_complete else 'skipped',
        )
        db.session.commit()


def _post_turn_status_for_player(campaign_id, session_id, player_message_id):
    memory_trace_id = f'session_memory_writer:session_{session_id}:message_{player_message_id}'
    clock_trace_id = f'session_clock_adjudicator:session_{session_id}:message_{player_message_id}'

    memory_error = CampaignAuditEvent.query.filter_by(
        campaign_id=campaign_id,
        trace_id=memory_trace_id,
        event_type='memory_update_error',
    ).order_by(CampaignAuditEvent.id.desc()).first()
    if memory_error is not None:
        return {
            'post_turn_complete': True,
            'post_turn_status': 'error',
            'memory_status': 'error',
            'clock_status': 'skipped',
        }

    memory_applied = CampaignAuditEvent.query.filter_by(
        campaign_id=campaign_id,
        trace_id=memory_trace_id,
        event_type='memory_patch_applied',
    ).order_by(CampaignAuditEvent.id.desc()).first()
    if memory_applied is None:
        return {
            'post_turn_complete': False,
            'post_turn_status': 'pending',
            'memory_status': 'pending',
            'clock_status': 'pending',
        }

    clock_applied = CampaignAuditEvent.query.filter_by(
        campaign_id=campaign_id,
        trace_id=clock_trace_id,
        event_type='clock_adjudication_applied',
    ).order_by(CampaignAuditEvent.id.desc()).first()
    if clock_applied is None:
        return {
            'post_turn_complete': False,
            'post_turn_status': 'pending',
            'memory_status': 'complete',
            'clock_status': 'pending',
        }

    return {
        'post_turn_complete': True,
        'post_turn_status': 'complete',
        'memory_status': 'complete',
        'clock_status': 'complete',
    }


def _dm_turn_status_for_player(campaign_id, session_id, player_message_id=None):
    """Return the most recent completed session-DM turn decision."""
    query = (
        CampaignAuditEvent.query
        .filter_by(
            campaign_id=campaign_id,
            source='session_messages',
            actor='session_dm',
        )
        .filter(CampaignAuditEvent.event_type.in_([
            'dm_output_stored',
            'dm_silence_chosen',
            'dm_output_empty',
        ]))
        .order_by(CampaignAuditEvent.id.desc())
    )
    for event in query.limit(64).all():
        try:
            payload = json.loads(event.payload) if event.payload else {}
        except (TypeError, ValueError):
            payload = {}
        event_player_message_id = payload.get('player_message_id')
        if player_message_id is not None and event_player_message_id != player_message_id:
            continue
        if event.event_type == 'dm_output_stored':
            status = {
                'status': 'speak',
                'player_message_id': event_player_message_id,
                'dm_message_id': payload.get('dm_message_id'),
            }
            if event_player_message_id is not None:
                status.update(_post_turn_status_for_player(campaign_id, session_id, event_player_message_id))
                status.update(session_dm_turn_status_payload(event_player_message_id))
            return status
        if event.event_type == 'dm_silence_chosen':
            decision = payload.get('decision') if isinstance(payload.get('decision'), dict) else {}
            status = {
                'status': 'silent',
                'player_message_id': event_player_message_id,
                'reason': decision.get('reason') or '',
                'post_turn_complete': True,
                'post_turn_status': 'complete',
                'memory_status': 'skipped',
                'clock_status': 'skipped',
            }
            if event_player_message_id is not None:
                status.update(session_dm_turn_status_payload(event_player_message_id))
            return status
        if event.event_type == 'dm_output_empty':
            status = {
                'status': 'empty',
                'player_message_id': event_player_message_id,
                'decision': payload.get('decision'),
                'post_turn_complete': True,
                'post_turn_status': 'complete',
                'memory_status': 'skipped',
                'clock_status': 'skipped',
            }
            if event_player_message_id is not None:
                status.update(session_dm_turn_status_payload(event_player_message_id))
            return status
    status = {
        'status': 'pending',
        'player_message_id': player_message_id,
        'post_turn_complete': False,
        'post_turn_status': 'pending',
        'memory_status': 'pending',
        'clock_status': 'pending',
    }
    if player_message_id is not None:
        status.update(session_dm_turn_status_payload(player_message_id))
    return status


@sessions_bp.route('/api/sessions/<int:session_id>/dm-turn-status', methods=['GET'])
@token_required
def get_dm_turn_status(current_user, session_id):
    session = get_or_404(CampaignSession, session_id)
    campaign = db.session.get(Campaign, session.campaign_id)
    if not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    after_message_id = request.args.get('after_message_id', type=int)
    status = _dm_turn_status_for_player(campaign.id, session_id, player_message_id=after_message_id)
    return jsonify(status)


@sessions_bp.route('/api/campaigns/<int:campaign_id>/sessions', methods=['POST'])
@token_required
def start_session(current_user, campaign_id):
    campaign = get_or_404(Campaign, campaign_id)
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

    if is_combat_sandbox_campaign(campaign):
        try:
            started = start_combat_sandbox_session(campaign, current_user)
        except ValueError as err:
            db.session.rollback()
            return jsonify({'error': str(err)}), 400
        except RuntimeError as err:
            db.session.rollback()
            return jsonify({'error': str(err)}), 500

        session = started['session']
        data = session.to_dict()
        data['messages'] = [message.to_dict() for message in session.messages]
        stream_manager.broadcast_event(session.id, {"type": "refresh"})
        planning_stream_manager.broadcast_campaign_event(campaign_id, {"type": "session_started"})
        return jsonify({'session': data}), 201

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

    opening_trace_id = f'session_dm:session_{session.id}:opening'
    opening_trace_label = f'session_dm: session {session.id} opening'
    context = planning_context(campaign, current_user)
    world_context = dm_world_context(
        campaign,
        audit=True,
        reason='opening_scene_context',
        audit_context={
            'trace_id': opening_trace_id,
            'trace_label': opening_trace_label,
        },
    )
    opening_text = get_opening_scene_response(
        context,
        world_context,
        audit_context={
            'campaign_id': campaign_id,
            'operation': 'opening_scene',
            'actor': 'session_dm',
            'trace_id': opening_trace_id,
            'trace_label': opening_trace_label,
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
            trace_id=opening_trace_id,
            trace_label=opening_trace_label,
            commit=False,
        )

    db.session.commit()
    stream_manager.broadcast_event(session.id, {"type": "refresh"})
    planning_stream_manager.broadcast_campaign_event(campaign_id, {"type": "session_started"})
    data = session.to_dict()
    data['messages'] = [m.to_dict() for m in session.messages]
    log_audit_event(
        campaign_id,
        'client_response_sent',
        'Sent started session payload to client.',
        {'session': data},
        source='campaign_sessions',
        actor='server',
        trace_id=opening_trace_id,
        trace_label=opening_trace_label,
        commit=True,
    )
    return jsonify({'session': data}), 201


@sessions_bp.route('/api/campaigns/<int:campaign_id>/sessions', methods=['GET'])
@token_required
def list_sessions(current_user, campaign_id):
    campaign = get_or_404(Campaign, campaign_id)
    if not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    sessions = CampaignSession.query.filter_by(campaign_id=campaign_id).order_by(CampaignSession.started_at.desc()).all()
    return jsonify({'sessions': [s.to_dict() for s in sessions]}), 200


@sessions_bp.route('/api/sessions/<int:session_id>', methods=['GET'])
@token_required
def get_session(current_user, session_id):
    session = get_or_404(CampaignSession, session_id)
    campaign = db.session.get(Campaign, session.campaign_id)
    if not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    data = session.to_dict()
    data.update(_message_page(
        session_id,
        before_id=request.args.get('before_id'),
        limit=request.args.get('limit'),
    ))
    return jsonify({'session': data}), 200


@sessions_bp.route('/api/sessions/<int:session_id>', methods=['PUT'])
@token_required
def end_session(current_user, session_id):
    session = get_or_404(CampaignSession, session_id)
    campaign = db.session.get(Campaign, session.campaign_id)
    if not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    data = request.get_json()
    session.is_active = False
    session.ended_at = datetime.utcnow()
    if data and 'recap' in data:
        session.recap = data['recap']

    db.session.commit()
    stream_manager.broadcast_event(session_id, {"type": "refresh"})
    return jsonify({'session': session.to_dict()}), 200


@sessions_bp.route('/api/sessions/<int:session_id>/messages', methods=['GET'])
@token_required
def get_messages(current_user, session_id):
    session = get_or_404(CampaignSession, session_id)
    campaign = db.session.get(Campaign, session.campaign_id)
    if not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    return jsonify(_message_page(
        session_id,
        before_id=request.args.get('before_id'),
        limit=request.args.get('limit'),
    )), 200


@sessions_bp.route('/api/sessions/<int:session_id>/messages', methods=['POST'])
@token_required
def send_message(current_user, session_id):
    session = get_or_404(CampaignSession, session_id)
    campaign = db.session.get(Campaign, session.campaign_id)
    if not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    member = _member_record(campaign.id, current_user.id)
    if member and (member.role or 'player') == 'spectator':
        return jsonify({'error': 'Spectators can read this campaign but cannot send messages'}), 403

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
    stream_manager.broadcast_event(session_id, {"type": "message", "message": msg.to_dict()})
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
    trace_id = session_dm_trace_id(session_id, msg.id)
    trace_label = f'session_dm: session {session_id}'
    db.session.add(begin_session_dm_turn(campaign.id, session_id, msg.id, trace_id))
    db.session.commit()

    # Start generation asynchronously
    if (
        current_app.config.get('TESTING')
        or current_app.testing
        or current_app.config.get('SQLALCHEMY_DATABASE_URI') == 'sqlite:///:memory:'
    ):
        recent_messages = SessionMessage.query.filter_by(session_id=session_id).order_by(
            SessionMessage.created_at.asc(),
        ).all()[-8:]

        hot_context = build_session_hot_context(campaign, session, current_user)
        dm_tools_filtered = get_dm_tool_definitions(campaign)
        manifest = context_manifest(hot_context, dm_tools_filtered)
        log_audit_event(
            campaign.id,
            'session_hot_context_read',
            'Read compact hot context for session DM response.',
            {'context': hot_context, 'context_manifest': manifest},
            source='session_context',
            actor='server',
            trace_id=trace_id,
            trace_label=trace_label,
            commit=True,
        )

        try:
            ai_result = get_session_dm_response_with_tools(
                hot_context,
                recent_messages,
                dm_tools_filtered,
                lambda name, args, tool_audit: execute_dm_tool(campaign, session, current_user, name, args, tool_audit),
                audit_context={
                    'campaign_id': campaign.id,
                    'operation': 'session_dm_response',
                    'actor': 'session_dm',
                    'trace_id': trace_id,
                    'trace_label': trace_label,
                    'context_manifest': manifest,
                    'full_world_graph_included': False,
                },
            )
        except Exception as err:
            db.session.add(mark_session_dm_turn_error(
                campaign.id,
                session_id,
                msg.id,
                trace_id,
                repr(err),
            ))
            db.session.commit()
            return jsonify({'error': repr(err), 'messages': result_messages}), 500

        ai_turn = _session_dm_turn_decision(ai_result)
        ai_text = ai_turn.get('content') or ''

        if ai_turn.get('mode') == 'speak' and ai_text:
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
                    'player_message_id': msg.id,
                    'dm_message_id': ai_msg.id,
                    'message': {
                        'role': 'dm',
                        'content': ai_text,
                    },
                },
                source='session_messages',
                actor='session_dm',
                trace_id=trace_id,
                trace_label=trace_label,
                commit=False,
            )
            pending_proposals = SheetProposal.query.filter_by(
                session_id=session_id, message_id=None, status='pending',
            ).all()
            for proposal in pending_proposals:
                proposal.message_id = ai_msg.id
            db.session.add(mark_session_dm_turn_visible(
                campaign.id,
                session_id,
                msg.id,
                trace_id,
                status='speak',
                dm_message_id=ai_msg.id,
            ))
            db.session.commit()
            result_messages.append(ai_msg.to_dict())

            # Synchronous memory update
            _run_session_memory_update(
                campaign.id,
                session_id,
                current_user.id,
                msg.id,
                content,
                ai_text,
                hot_context,
                trace_id,
                dm_message_id=ai_msg.id,
            )
        elif ai_turn.get('mode') == 'silent':
            log_audit_event(
                campaign.id,
                'dm_silence_chosen',
                'Session DM intentionally sent no visible response.',
                {
                    'session_id': session_id,
                    'player_message_id': msg.id,
                    'decision': {
                        'mode': 'silent',
                        'reason': ai_turn.get('reason') or '',
                    },
                },
                source='session_messages',
                actor='session_dm',
                trace_id=trace_id,
                trace_label=trace_label,
                audit_role='agent',
                commit=False,
            )
            db.session.add(mark_session_dm_turn_visible(
                campaign.id,
                session_id,
                msg.id,
                trace_id,
                status='silent',
            ))
            db.session.commit()
        else:
            log_audit_event(
                campaign.id,
                'dm_output_empty',
                'Session DM returned no visible content; no DM message was stored.',
                {
                    'session_id': session_id,
                    'player_message_id': msg.id,
                    'decision': ai_turn,
                },
                source='session_messages',
                actor='session_dm',
                trace_id=trace_id,
                trace_label=trace_label,
                audit_role='agent',
                commit=False,
            )
            db.session.add(mark_session_dm_turn_visible(
                campaign.id,
                session_id,
                msg.id,
                trace_id,
                status='empty',
            ))
            db.session.commit()

        log_audit_event(
            campaign.id,
            'client_response_sent',
            'Sent session turn messages payload to client.',
            {'messages': result_messages},
            source='session_messages',
            actor='server',
            trace_id=trace_id,
            trace_label=trace_label,
            commit=True,
        )
    else:
        stream_manager.start_generation(campaign.id, session_id, current_user.id, content, msg.id)
    return jsonify({'messages': result_messages}), 201


@sessions_bp.route('/api/sessions/<int:session_id>/stream', methods=['GET'])
def stream_session(session_id):
    token_str = request.args.get('token')
    api_key = request.args.get('api_key')

    current_user, error_response = authenticate_request(token=token_str, api_key=api_key)
    if error_response is not None:
        return error_response

    session = get_or_404(CampaignSession, session_id)
    campaign = db.session.get(Campaign, session.campaign_id)
    if not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    q = stream_manager.add_listener(session_id)

    def event_stream():
        try:
            while True:
                try:
                    event = q.get(timeout=20) # Keep-alive ping / timeout
                    yield f"data: {json.dumps(event)}\n\n"
                except queue.Empty:
                    # Keep-alive comment
                    yield ": ping\n\n"
        finally:
            stream_manager.remove_listener(session_id, q)

    response = current_app.response_class(event_stream(), mimetype='text/event-stream')
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['X-Accel-Buffering'] = 'no'
    return response


@token_required
def _get_session_proposals(current_user, session_id):
    session = get_or_404(CampaignSession, session_id)
    campaign = db.session.get(Campaign, session.campaign_id)
    if not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    is_dm = campaign.user_id == current_user.id
    proposals = SheetProposal.query.filter_by(session_id=session_id, status='pending').all()
    session_message_ids = {
        message_id
        for (message_id,) in db.session.query(SessionMessage.id)
        .filter_by(session_id=session_id)
        .all()
    }
    proposals = [
        proposal for proposal in proposals
        if proposal.message_id is None or proposal.message_id in session_message_ids
    ]

    if is_dm:
        result = [p.to_dict() for p in proposals]
    else:
        user_char_ids = {c.id for c in Character.query.filter_by(user_id=current_user.id).all()}
        result = [p.to_dict() for p in proposals if p.character_id in user_char_ids]

    return jsonify({'sheet_proposals': result}), 200


@token_required
def _apply_sheet_proposal(current_user, session_id, proposal_id):
    session = get_or_404(CampaignSession, session_id)
    campaign = db.session.get(Campaign, session.campaign_id)
    proposal = get_or_404(SheetProposal, proposal_id)

    if proposal.status != 'pending':
        return jsonify({'error': 'Proposal is not pending.'}), 400

    character = db.session.get(Character, proposal.character_id)
    if not character:
        return jsonify({'error': 'Character not found.'}), 404

    is_dm = campaign.user_id == current_user.id
    is_owner = character.user_id == current_user.id
    is_npc = character.user_id is None

    if not is_owner and not (is_dm and is_npc):
        return jsonify({'error': 'Forbidden'}), 403

    from models import CharacterCondition, CharacterEquipment

    for change in proposal.changes:
        field = change['field']
        after = change['after']

        if ':' in field:
            prefix, item_name = field.split(':', 1)
            prefix = prefix.strip().lower()
            item_name = item_name.strip()

            if prefix == 'condition':
                existing = CharacterCondition.query.filter_by(
                    character_id=character.id, condition_name=item_name,
                ).first()
                if isinstance(after, dict) and after.get('count', 0) > 0:
                    if not existing:
                        db.session.add(CharacterCondition(
                            character=character, condition_name=item_name,
                        ))
                elif existing:
                    db.session.delete(existing)

            elif prefix == 'equipment':
                if isinstance(after, dict) and after.get('count', 0) > 0:
                    existing_equip = CharacterEquipment.query.filter_by(
                        character_id=character.id, name=item_name,
                    ).first()
                    if existing_equip:
                        existing_equip.quantity = (existing_equip.quantity or 0) + 1
                    else:
                        db.session.add(CharacterEquipment(
                            character=character, name=item_name, quantity=1,
                        ))
        else:
            config = SHEET_SCALAR_FIELDS.get(field)
            if not config:
                continue
            if config['type'] == 'bool':
                setattr(character, field, bool(after))
            else:
                setattr(character, field, int(after))

    character.updated_at = datetime.utcnow()
    proposal.status = 'applied'
    proposal.applied_at = datetime.utcnow()
    db.session.commit()

    stream_manager.broadcast_event(session_id, {
        "type": "proposal_applied",
        "proposal": proposal.to_dict(),
        "character": character_full_dict(character)
    })

    log_audit_event(
        campaign.id,
        'sheet_proposal_applied',
        f'Sheet proposal {proposal_id} applied.',
        {'session_id': session_id, 'proposal_id': proposal_id, 'changes': proposal.changes},
        source='session_messages',
        actor=current_user.username,
        commit=True,
    )

    return jsonify({'proposal': proposal.to_dict(), 'character': character_full_dict(character)}), 200


@token_required
def _dismiss_sheet_proposal(current_user, session_id, proposal_id):
    session = get_or_404(CampaignSession, session_id)
    campaign = db.session.get(Campaign, session.campaign_id)
    proposal = get_or_404(SheetProposal, proposal_id)

    if proposal.status != 'pending':
        return jsonify({'error': 'Proposal is not pending.'}), 400

    character = db.session.get(Character, proposal.character_id)
    is_dm = campaign.user_id == current_user.id
    is_owner = character and character.user_id == current_user.id

    if not is_owner and not is_dm:
        return jsonify({'error': 'Forbidden'}), 403

    proposal.status = 'dismissed'
    db.session.commit()

    stream_manager.broadcast_event(session_id, {
        "type": "proposal_dismissed",
        "proposal": proposal.to_dict()
    })

    log_audit_event(
        campaign.id,
        'sheet_proposal_dismissed',
        f'Sheet proposal {proposal_id} dismissed.',
        {'session_id': session_id, 'proposal_id': proposal_id},
        source='session_messages',
        actor=current_user.username,
        commit=True,
    )

    return jsonify({'proposal': proposal.to_dict()}), 200


sessions_bp.add_url_rule(
    '/api/sessions/<int:session_id>/proposals',
    view_func=_get_session_proposals,
    methods=['GET'],
)
sessions_bp.add_url_rule(
    '/api/sessions/<int:session_id>/proposals/<int:proposal_id>/apply',
    view_func=_apply_sheet_proposal,
    methods=['POST'],
)
sessions_bp.add_url_rule(
    '/api/sessions/<int:session_id>/proposals/<int:proposal_id>/dismiss',
    view_func=_dismiss_sheet_proposal,
    methods=['POST'],
)

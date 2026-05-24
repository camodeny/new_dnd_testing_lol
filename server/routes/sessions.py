from datetime import datetime
from threading import Thread
import json
import queue

from flask import Blueprint, current_app, jsonify, request

from auth import token_required
from models import db, Campaign, CampaignSession, SessionMessage, SheetProposal, Character, User
from openrouter import (
    get_opening_scene_response,
    get_session_dm_response_with_tools,
    get_session_memory_patch,
    normalize_session_dm_turn_decision,
)
from services.stream_manager import stream_manager
from services.audit_service import log_audit_event
from services.campaign_service import ensure_member, get_or_404
from services.character_service import character_full_dict
from services.dm_tools import (
    get_dm_tool_definitions,
    SHEET_SCALAR_FIELDS,
    apply_memory_patch,
    build_session_hot_context,
    build_session_memory_context,
    context_manifest,
    execute_dm_tool,
)
from services.planning_service import can_start_session, planning_context
from services.world_service import approve_world, dm_world_context, ensure_world_generated

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


def _run_session_memory_update(
    campaign_id,
    session_id,
    user_id,
    player_message_id,
    player_content,
    ai_text,
    hot_context,
    parent_trace_id,
):
    memory_trace_id = f'session_memory_writer:session_{session_id}:message_{player_message_id}'
    trace_label = f'session_memory_writer: session {session_id}'
    try:
        campaign = db.session.get(Campaign, campaign_id)
        session = db.session.get(CampaignSession, session_id)
        current_user = db.session.get(User, user_id)
        if not campaign or not session or not current_user:
            return

        memory_context = build_session_memory_context(
            campaign,
            session,
            current_user,
            player_content,
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
                'parent_trace_id': parent_trace_id,
                'trace_label': trace_label,
            },
        )
        if memory_patch:
            apply_memory_patch(
                campaign,
                session,
                memory_patch,
                audit_context={
                    'trace_id': memory_trace_id,
                    'parent_trace_id': parent_trace_id,
                    'trace_label': trace_label,
                },
            )
        db.session.commit()
    except Exception as err:
        db.session.rollback()
        log_audit_event(
            campaign_id,
            'memory_update_error',
            'Post-turn memory update failed after visible DM response.',
            {'session_id': session_id, 'error': repr(err)},
            source='session_memory',
            actor='session_memory_writer',
            trace_id=memory_trace_id,
            parent_trace_id=parent_trace_id,
            trace_label=trace_label,
            audit_role='tools',
            commit=True,
        )


def _schedule_session_memory_update(
    campaign_id,
    session_id,
    user_id,
    player_message_id,
    player_content,
    ai_text,
    hot_context,
    parent_trace_id,
):
    app = current_app._get_current_object()

    def run_with_app_context():
        with app.app_context():
            try:
                _run_session_memory_update(
                    campaign_id,
                    session_id,
                    user_id,
                    player_message_id,
                    player_content,
                    ai_text,
                    hot_context,
                    parent_trace_id,
                )
            finally:
                db.session.remove()

    if (
        app.config.get('TESTING')
        or app.testing
        or app.config.get('SQLALCHEMY_DATABASE_URI') == 'sqlite:///:memory:'
    ):
        _run_session_memory_update(
            campaign_id,
            session_id,
            user_id,
            player_message_id,
            player_content,
            ai_text,
            hot_context,
            parent_trace_id,
        )
        return

    Thread(
        target=run_with_app_context,
        name=f'session-memory-{session_id}',
        daemon=True,
    ).start()


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

    # Start generation asynchronously
    if (
        current_app.config.get('TESTING')
        or current_app.testing
        or current_app.config.get('SQLALCHEMY_DATABASE_URI') == 'sqlite:///:memory:'
    ):
        recent_messages = SessionMessage.query.filter_by(session_id=session_id).order_by(
            SessionMessage.created_at.asc(),
        ).all()[-8:]
        trace_id = f'session_dm:session_{session_id}:message_{msg.id}'
        trace_label = f'session_dm: session {session_id}'

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
    # Retrieve token from query params since EventSource doesn't support headers natively
    token_str = request.args.get('token')
    if not token_str:
        return jsonify({'error': 'Unauthorized'}), 401

    import jwt
    try:
        data = jwt.decode(token_str, current_app.config['SECRET_KEY'], algorithms=['HS256'])
        token_user_id = data.get('user_id')
    except Exception:
        return jsonify({'error': 'Unauthorized'}), 401

    if not token_user_id:
        return jsonify({'error': 'Unauthorized'}), 401

    current_user = db.session.get(User, token_user_id)
    if not current_user:
        return jsonify({'error': 'Unauthorized'}), 401

    session = get_or_404(CampaignSession, session_id)
    campaign = db.session.get(Campaign, session.campaign_id)
    if not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    worker = stream_manager.get_worker(session_id)
    if not worker:
        # If no generation is running, return a brief EventStream indicating idle
        def idle_stream():
            yield "data: " + json.dumps({"type": "status", "status": "idle"}) + "\n\n"
        return current_app.response_class(idle_stream(), mimetype='text/event-stream')

    q = worker.add_listener()

    def event_stream():
        try:
            while True:
                try:
                    event = q.get(timeout=20) # Keep-alive ping / timeout
                    yield f"data: {json.dumps(event)}\n\n"
                    if event.get("type") in ("done", "error"):
                        break
                except queue.Empty:
                    # Keep-alive comment
                    yield ": ping\n\n"
        finally:
            worker.remove_listener(q)

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

import random

from flask import Blueprint, jsonify

from auth import token_required
from dev_data import DEV_CHARACTER_TEMPLATES
from models import (
    Campaign,
    CampaignAuditEvent,
    CampaignClock,
    CampaignSession,
    Character,
    CharacterPlanningMessage,
    NPCActor,
    WorldEvent,
    db,
)
from openrouter import (
    build_dm_response_messages,
    build_opening_scene_messages,
    build_planning_dm_messages,
    build_planning_summary_messages,
    build_world_genesis_messages,
)
from services.character_service import (
    build_character_from_data,
    character_full_dict,
    update_character_relations,
)
from services.planning_service import (
    get_campaign_members,
    planning_context,
    summary_dict_for_read,
    visible_planning_payload,
)
from services.world_service import dm_world_context, world_public_payload

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

def _serialize_session(session):
    data = session.to_dict()
    data['messages'] = [message.to_dict() for message in session.messages]
    return data


@dev_bp.route('/api/campaigns/<int:campaign_id>/dev', methods=['GET'])
@token_required
def get_campaign_dev_audit(current_user, campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    members = get_campaign_members(campaign)
    member_user_ids = {member.user_id for member in members}
    if current_user.id not in member_user_ids and campaign.user_id != current_user.id:
        return jsonify({'error': 'Forbidden'}), 403

    campaign_data = campaign.to_dict()
    campaign_data['owner_username'] = campaign.owner.username if campaign.owner else None

    characters = [character_full_dict(character) for character in campaign.characters]
    sessions = CampaignSession.query.filter_by(campaign_id=campaign_id).order_by(CampaignSession.started_at.desc()).all()
    active_session = CampaignSession.query.filter_by(campaign_id=campaign_id, is_active=True).first()

    planning_ctx = planning_context(campaign, current_user)
    visible_planning = visible_planning_payload(campaign, current_user)
    planning_summary = summary_dict_for_read(campaign.id, include_private=True, current_user_id=current_user.id)
    planning_messages = CharacterPlanningMessage.query.filter_by(campaign_id=campaign_id).order_by(
        CharacterPlanningMessage.created_at.asc(),
    ).all()
    current_user_planning_messages = CharacterPlanningMessage.query.filter_by(
        campaign_id=campaign_id,
        user_id=current_user.id,
    ).order_by(CharacterPlanningMessage.created_at.asc()).all()
    latest_player_message = next(
        (message.content for message in reversed(current_user_planning_messages) if message.role == 'player'),
        '',
    )
    latest_dm_message = next(
        (message.content for message in reversed(current_user_planning_messages) if message.role == 'dm'),
        '',
    )

    world_public = world_public_payload(campaign)
    world_context = dm_world_context(campaign)
    world_events = WorldEvent.query.filter_by(campaign_id=campaign_id).order_by(WorldEvent.created_at.asc()).all()
    npcs = NPCActor.query.filter_by(campaign_id=campaign_id).order_by(NPCActor.id.asc()).all()
    clocks = CampaignClock.query.filter_by(campaign_id=campaign_id).order_by(CampaignClock.id.asc()).all()
    audit_events = CampaignAuditEvent.query.filter_by(campaign_id=campaign_id).order_by(
        CampaignAuditEvent.id.asc(),
    ).all()
    latest_session = active_session or (sessions[0] if sessions else None)

    session_context = dict(planning_ctx)
    if world_context:
        session_context['world'] = world_context

    return jsonify({
        'campaign': campaign_data,
        'members': [member.to_dict() for member in members],
        'characters': characters,
        'sessions': [_serialize_session(session) for session in sessions],
        'active_session': _serialize_session(active_session) if active_session else None,
        'latest_session': _serialize_session(latest_session) if latest_session else None,
        'planning': {
            'context': planning_ctx,
            'visible': visible_planning,
            'summary': planning_summary,
            'messages': [message.to_dict() for message in planning_messages],
            'current_user_messages': [message.to_dict() for message in current_user_planning_messages],
            'prompt_traces': {
                'dm_response': build_planning_dm_messages(
                    planning_ctx,
                    current_user_planning_messages,
                    draft_character=None,
                    active_page=None,
                ),
                'summary_update': build_planning_summary_messages(
                    planning_ctx,
                    latest_player_message,
                    latest_dm_message,
                ),
            },
        },
        'world': {
            'public': world_public,
            'context': world_context,
            'npc_actors': [npc.to_dict(include_private=True) for npc in npcs],
            'clocks': [clock.to_dict(include_private=True) for clock in clocks],
            'events': [event.to_dict(include_private=True) for event in world_events],
            'prompt_traces': {
                'world_genesis': build_world_genesis_messages(planning_ctx),
                'opening_scene': build_opening_scene_messages(planning_ctx, world_context),
                'session_dm_response': build_dm_response_messages(
                    latest_session.messages if latest_session else [],
                    session_context if latest_session else None,
                ) if latest_session else [],
            },
        },
        'audit_events': [event.to_dict() for event in audit_events],
        'audit_notes': {
            'tool_calls': 'OpenRouter chat requests and responses are persisted as model_request/model_response events. Provider-internal tool calls are not returned unless the provider response includes them.',
            'thinking': None,
            'message': 'Provider-hidden chain-of-thought is not returned to or stored by this app. The audit stream shows exact app inputs, system prompts, prompt payloads, raw model responses, database reads/writes, and client response payloads in persisted order.',
        },
    }), 200

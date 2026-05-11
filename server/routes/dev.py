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
    build_session_dm_tool_messages,
    build_world_genesis_messages,
)
from services.character_service import (
    build_character_from_data,
    character_full_dict,
    update_character_relations,
)
from services.audit_service import AGENT_ACTORS, infer_audit_role
from services.dm_tools import DM_TOOL_DEFINITIONS, build_session_hot_context, context_manifest
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


def _format_event_type(event_type):
    return (event_type or 'event').replace('_', ' ')


def _first_choice_message(raw_response):
    if not isinstance(raw_response, dict):
        return {}
    choices = raw_response.get('choices') or []
    if not choices or not isinstance(choices[0], dict):
        return {}
    message = choices[0].get('message') or {}
    return message if isinstance(message, dict) else {}


def _reasoning_payload(payload):
    raw_response = payload.get('raw_response') if isinstance(payload, dict) else {}
    message = _first_choice_message(raw_response)
    reasoning = payload.get('reasoning') or message.get('reasoning') or message.get('reasoning_content')
    reasoning_details = payload.get('reasoning_details') or message.get('reasoning_details')
    usage = payload.get('reasoning_usage') or {}
    if not usage and isinstance(raw_response, dict):
        usage_data = raw_response.get('usage') or {}
        completion_details = usage_data.get('completion_tokens_details') or {}
        output_details = usage_data.get('output_tokens_details') or {}
        usage = {
            'reasoning_tokens': completion_details.get('reasoning_tokens') or output_details.get('reasoning_tokens'),
            'completion_tokens_details': completion_details,
            'output_tokens_details': output_details,
        }
    return {
        'returned': bool(reasoning or reasoning_details),
        'reasoning': reasoning,
        'reasoning_details': reasoning_details,
        'usage': usage,
    }


def _fallback_trace_id(event_data):
    actor = event_data.get('actor')
    event_type = event_data.get('event_type')
    if event_data.get('trace_id'):
        return event_data.get('trace_id')
    if actor in AGENT_ACTORS or event_type in {'model_request', 'model_response', 'model_error'}:
        return f'actor:{actor or "model"}'
    return None


def _audit_stream_entry(event):
    event_data = event.to_dict()
    payload = event_data.get('payload') or {}
    role = infer_audit_role(event_data.get('event_type'), actor=event_data.get('actor'), audit_role=event_data.get('audit_role'))
    trace_id = _fallback_trace_id(event_data)
    messages = payload.get('messages') if isinstance(payload.get('messages'), list) else []
    reasoning = _reasoning_payload(payload) if event_data.get('event_type') == 'model_response' else None
    raw_response = payload.get('raw_response') if isinstance(payload, dict) else {}
    first_message = _first_choice_message(raw_response)
    tool_calls = payload.get('tool_calls') or first_message.get('tool_calls') or []

    label = event_data.get('actor') or role
    if role == 'tools':
        label = _format_event_type(event_data.get('event_type'))

    return {
        'id': event_data['id'],
        'campaign_id': event_data['campaign_id'],
        'event_type': event_data['event_type'],
        'role': role,
        'label': label,
        'source': event_data.get('source'),
        'actor': event_data.get('actor'),
        'summary': event_data.get('summary'),
        'content': payload.get('content') if isinstance(payload, dict) else None,
        'payload': payload,
        'messages': messages,
        'message_count': len(messages),
        'reasoning': reasoning,
        'finish_reason': payload.get('finish_reason') if isinstance(payload, dict) else None,
        'tool_calls': tool_calls,
        'tool_names': payload.get('tool_names') if isinstance(payload, dict) else [],
        'context_manifest': payload.get('context_manifest') if isinstance(payload, dict) else {},
        'token_estimate': payload.get('token_estimate') if isinstance(payload, dict) else {},
        'usage': payload.get('usage') if isinstance(payload, dict) else {},
        'trace_id': trace_id,
        'parent_trace_id': event_data.get('parent_trace_id'),
        'trace_label': event_data.get('trace_label') or event_data.get('actor') or trace_id,
        'created_at': event_data.get('created_at'),
    }


def _agent_runs_from_stream(stream):
    nodes = {}
    for entry in stream:
        trace_id = entry.get('trace_id')
        if not trace_id:
            continue
        node = nodes.setdefault(trace_id, {
            'trace_id': trace_id,
            'parent_trace_id': entry.get('parent_trace_id'),
            'trace_label': entry.get('trace_label') or trace_id,
            'actor': entry.get('actor'),
            'events': [],
            'children': [],
        })
        if not node.get('parent_trace_id') and entry.get('parent_trace_id'):
            node['parent_trace_id'] = entry.get('parent_trace_id')
        if not node.get('actor') and entry.get('actor'):
            node['actor'] = entry.get('actor')
        node['events'].append(entry)

    for node in nodes.values():
        node['events'].sort(key=lambda item: item.get('id') or 0)

    roots = []
    for node in nodes.values():
        parent_id = node.get('parent_trace_id')
        if parent_id and parent_id in nodes:
            nodes[parent_id]['children'].append(node)
        else:
            roots.append(node)

    def sort_node(node):
        node['children'].sort(key=lambda child: child['events'][0]['id'] if child['events'] else 0)
        for child in node['children']:
            sort_node(child)

    roots.sort(key=lambda node: node['events'][0]['id'] if node['events'] else 0)
    for root in roots:
        sort_node(root)
    return roots


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
    audit_events_payload = [event.to_dict() for event in audit_events]
    audit_stream = [_audit_stream_entry(event) for event in audit_events]
    agent_runs = _agent_runs_from_stream(audit_stream)
    latest_session = active_session or (sessions[0] if sessions else None)

    session_context = dict(planning_ctx)
    if world_context:
        session_context['world'] = world_context
    latest_session_hot_context = None
    latest_session_context_manifest = None
    if latest_session:
        latest_session_hot_context = build_session_hot_context(campaign, latest_session, current_user)
        latest_session_context_manifest = context_manifest(latest_session_hot_context, DM_TOOL_DEFINITIONS)

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
                'legacy_full_session_dm_response': build_dm_response_messages(
                    latest_session.messages if latest_session else [],
                    session_context if latest_session else None,
                ) if latest_session else [],
                'session_dm_response': build_session_dm_tool_messages(
                    latest_session_hot_context,
                ) if latest_session_hot_context else [],
            },
            'context_strategy': {
                'hot_context': latest_session_hot_context,
                'manifest': latest_session_context_manifest,
                'tools': DM_TOOL_DEFINITIONS,
                'legacy_full_prompt_available': bool(latest_session),
            },
        },
        'audit_events': audit_events_payload,
        'audit_stream': audit_stream,
        'agent_runs': agent_runs,
        'audit_notes': {
            'tool_calls': 'OpenRouter chat requests and responses are persisted as model_request/model_response events. Provider-internal tool calls are not returned unless the provider response includes them.',
            'thinking': 'OpenRouter reasoning-capable models may return reasoning or reasoning_details fields in model responses. Some models and providers do not return reasoning, and provider-hidden reasoning is not available unless it appears in the response payload.',
            'message': 'The audit stream shows exact app inputs, system prompts, prompt payloads, raw model responses, returned reasoning when provided, database reads/writes, and client response payloads in persisted order.',
        },
    }), 200

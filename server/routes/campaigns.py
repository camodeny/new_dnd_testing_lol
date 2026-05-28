import io
import json
import re
import zipfile
from datetime import datetime

from flask import Blueprint, jsonify, request, send_file

from auth import token_required
from models import (
    db,
    Campaign,
    CampaignAuditEvent,
    CampaignClock,
    CampaignMember,
    CampaignMemoryLog,
    CampaignMemoryRun,
    CampaignMonster,
    CampaignPlanningSummary,
    CampaignSession,
    CampaignShop,
    CampaignWorld,
    Character,
    CharacterPlanningMessage,
    LootBox,
    NPCActor,
    PlanningBondProposal,
    SessionMessage,
)
from services.campaign_service import ensure_member, get_or_404, invite_code_matches
from services.character_service import character_full_dict

campaigns_bp = Blueprint('campaigns', __name__)


@campaigns_bp.route('/api/campaigns', methods=['GET'])
@token_required
def get_campaigns(current_user):
    owned = Campaign.query.filter_by(user_id=current_user.id).all()
    member_of = Campaign.query.join(CampaignMember).filter(
        CampaignMember.user_id == current_user.id
    ).all()
    all_campaigns = list({c.id: c for c in owned + member_of}.values())
    return jsonify({'campaigns': [c.to_dict() for c in all_campaigns]}), 200


@campaigns_bp.route('/api/campaigns/<int:campaign_id>', methods=['GET'])
@token_required
def get_campaign(current_user, campaign_id):
    campaign = get_or_404(Campaign, campaign_id)

    has_invite = False
    invite_code = request.args.get('code')
    if invite_code and invite_code_matches(campaign, invite_code):
        has_invite = True

    if not ensure_member(campaign, current_user) and not has_invite:
        return jsonify({'error': 'Forbidden'}), 403

    data = campaign.to_dict()
    data['owner_username'] = campaign.owner.username if campaign.owner else None
    data['characters'] = [character_full_dict(c) for c in campaign.characters]
    data['active_session'] = None

    active = CampaignSession.query.filter_by(campaign_id=campaign_id, is_active=True).first()
    if active:
        data['active_session'] = active.to_dict()

    return jsonify({'campaign': data}), 200


@campaigns_bp.route('/api/campaigns/<int:campaign_id>', methods=['PUT'])
@token_required
def update_campaign(current_user, campaign_id):
    campaign = get_or_404(Campaign, campaign_id)
    if campaign.user_id != current_user.id:
        return jsonify({'error': 'Forbidden'}), 403

    data = request.get_json()
    if 'name' in data:
        campaign.name = data['name']
    if 'description' in data:
        campaign.description = data['description']
    if 'difficulty' in data:
        campaign.difficulty = data['difficulty']
    if 'seed' in data:
        campaign.seed = data['seed']
    if 'status' in data:
        campaign.status = data['status']
    if 'settings' in data:
        campaign.settings = json.dumps(data['settings'])

    db.session.commit()
    return jsonify({'campaign': campaign.to_dict()}), 200


@campaigns_bp.route('/api/campaigns/<int:campaign_id>', methods=['DELETE'])
@token_required
def delete_campaign(current_user, campaign_id):
    campaign = get_or_404(Campaign, campaign_id)
    if campaign.user_id != current_user.id:
        return jsonify({'error': 'Forbidden'}), 403

    Character.query.filter_by(campaign_id=campaign_id).update(
        {Character.campaign_id: None},
        synchronize_session=False,
    )
    CharacterPlanningMessage.query.filter_by(campaign_id=campaign_id).delete(synchronize_session=False)
    CampaignPlanningSummary.query.filter_by(campaign_id=campaign_id).delete(synchronize_session=False)
    PlanningBondProposal.query.filter_by(campaign_id=campaign_id).delete(synchronize_session=False)
    CampaignAuditEvent.query.filter_by(campaign_id=campaign_id).delete(synchronize_session=False)

    db.session.delete(campaign)
    db.session.commit()
    return jsonify({'message': 'Campaign deleted'}), 200


@campaigns_bp.route('/api/campaigns', methods=['POST'])
@token_required
def create_campaign(current_user):
    data = request.get_json()

    if not data or not data.get('name'):
        return jsonify({'error': 'Missing required field: name'}), 400

    required = data.get('required_players')
    settings = {}
    if required is not None:
        try:
            required = int(required)
            if required < 1:
                required = 1
            elif required > 10:
                required = 10
            settings['required_players'] = required
        except (ValueError, TypeError):
            pass

    loot_mode = data.get('loot_mode')
    if loot_mode in ('frequent_gamble', 'rare_quality'):
        settings['loot_mode'] = loot_mode

    campaign = Campaign(
        name=data['name'],
        description=data.get('description', ''),
        difficulty=data.get('difficulty', ''),
        seed=data.get('seed', ''),
        user_id=current_user.id,
        settings=json.dumps(settings) if settings else None,
    )

    db.session.add(campaign)
    db.session.commit()

    member = CampaignMember(campaign_id=campaign.id, user_id=current_user.id, role='player')
    db.session.add(member)
    db.session.commit()

    return jsonify({'message': 'Campaign created successfully', 'campaign': campaign.to_dict()}), 201


@campaigns_bp.route('/api/campaigns/<int:campaign_id>/characters', methods=['GET'])
@token_required
def get_campaign_characters(current_user, campaign_id):
    campaign = get_or_404(Campaign, campaign_id)
    if not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    characters = Character.query.filter_by(campaign_id=campaign_id).order_by(Character.party_order).all()
    return jsonify({'characters': [character_full_dict(c) for c in characters]}), 200


@campaigns_bp.route('/api/campaigns/<int:campaign_id>/characters', methods=['POST'])
@token_required
def add_campaign_character(current_user, campaign_id):
    campaign = get_or_404(Campaign, campaign_id)
    if not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    data = request.get_json()
    if not data or not data.get('character_id'):
        return jsonify({'error': 'Missing character_id'}), 400

    character = get_or_404(Character, data['character_id'])
    if character.user_id != current_user.id:
        return jsonify({'error': 'Forbidden'}), 403

    character.campaign_id = campaign_id
    member = CampaignMember.query.filter_by(campaign_id=campaign_id, user_id=current_user.id).first()
    if member:
        member.selected_character_id = character.id
        member.character_ready_at = None
    db.session.commit()
    return jsonify({'character': character_full_dict(character)}), 200


@campaigns_bp.route('/api/campaigns/<int:campaign_id>/export', methods=['GET'])
@token_required
def export_campaign(current_user, campaign_id):
    """Bundle all campaign data into a ZIP archive for download."""
    campaign = get_or_404(Campaign, campaign_id)
    if campaign.user_id != current_user.id:
        return jsonify({'error': 'Forbidden'}), 403

    # --- Gather all data ---

    # Campaign core
    campaign_data = campaign.to_dict()
    campaign_data['owner_username'] = campaign.owner.username if campaign.owner else None

    # Members
    members = CampaignMember.query.filter_by(campaign_id=campaign_id).all()
    members_data = [m.to_dict() for m in members]

    # Characters with full relation data
    characters = Character.query.filter_by(campaign_id=campaign_id).order_by(Character.party_order).all()
    characters_data = [character_full_dict(c) for c in characters]

    # Sessions + messages
    sessions = CampaignSession.query.filter_by(campaign_id=campaign_id).order_by(CampaignSession.started_at).all()
    sessions_data = []
    for s in sessions:
        s_dict = s.to_dict()
        messages = SessionMessage.query.filter_by(session_id=s.id).order_by(SessionMessage.created_at).all()
        s_dict['messages'] = [m.to_dict() for m in messages]
        sessions_data.append(s_dict)

    # World
    world = CampaignWorld.query.filter_by(campaign_id=campaign_id).first()
    world_data = None
    if world:
        try:
            public_intro = json.loads(world.public_intro) if world.public_intro else {}
        except (TypeError, ValueError):
            public_intro = {}
        try:
            knowledge_graph = json.loads(world.knowledge_graph) if world.knowledge_graph else {}
        except (TypeError, ValueError):
            knowledge_graph = {}
        try:
            world_state = json.loads(world.world_state) if world.world_state else {}
        except (TypeError, ValueError):
            world_state = {}
        try:
            dm_private = json.loads(world.dm_private) if world.dm_private else {}
        except (TypeError, ValueError):
            dm_private = {}
        world_data = {
            'id': world.id,
            'campaign_id': world.campaign_id,
            'public_intro': public_intro,
            'knowledge_graph': knowledge_graph,
            'world_state': world_state,
            'dm_private': dm_private,
            'approved_at': world.approved_at.isoformat() if world.approved_at else None,
            'created_at': world.created_at.isoformat() if world.created_at else None,
            'updated_at': world.updated_at.isoformat() if world.updated_at else None,
        }

    # NPCs
    npcs = NPCActor.query.filter_by(campaign_id=campaign_id).all()
    npcs_data = [n.to_dict(include_private=True) for n in npcs]

    # Clocks
    clocks = CampaignClock.query.filter_by(campaign_id=campaign_id).all()
    clocks_data = [c.to_dict(include_private=True) for c in clocks if c.to_dict(include_private=True) is not None]

    # Monsters
    monsters = CampaignMonster.query.filter_by(campaign_id=campaign_id).all()
    monsters_data = [m.to_dict() for m in monsters]

    # Shops
    shops = CampaignShop.query.filter_by(campaign_id=campaign_id).all()
    shops_data = [s.to_dict() for s in shops]

    # Loot boxes
    loot_boxes = LootBox.query.filter_by(campaign_id=campaign_id).all()
    loot_boxes_data = [lb.to_dict(is_dm=True) for lb in loot_boxes]

    # Planning
    planning_summary = CampaignPlanningSummary.query.filter_by(campaign_id=campaign_id).first()
    planning_data = planning_summary.to_dict(include_private=True) if planning_summary else None

    planning_messages = CharacterPlanningMessage.query.filter_by(campaign_id=campaign_id).order_by(CharacterPlanningMessage.created_at).all()
    planning_messages_data = [m.to_dict() for m in planning_messages]

    bond_proposals = PlanningBondProposal.query.filter_by(campaign_id=campaign_id).all()
    bond_proposals_data = [b.to_dict() for b in bond_proposals]

    # Audit log
    audit_events = CampaignAuditEvent.query.filter_by(campaign_id=campaign_id).order_by(CampaignAuditEvent.created_at).all()
    audit_data = [e.to_dict() for e in audit_events]

    # Memory runs & logs
    memory_runs = CampaignMemoryRun.query.filter_by(campaign_id=campaign_id).order_by(CampaignMemoryRun.created_at).all()
    memory_runs_data = [r.to_dict() for r in memory_runs]

    memory_logs = CampaignMemoryLog.query.filter_by(campaign_id=campaign_id).order_by(CampaignMemoryLog.created_at).all()
    memory_logs_data = [l.to_dict() for l in memory_logs]

    # --- Build ZIP in memory ---
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        def add_json(filename, data):
            zf.writestr(filename, json.dumps(data, indent=2, ensure_ascii=False, default=str))

        add_json('campaign.json', campaign_data)
        add_json('members.json', members_data)
        add_json('characters.json', characters_data)
        add_json('sessions.json', sessions_data)
        add_json('world.json', world_data)
        add_json('npcs.json', npcs_data)
        add_json('clocks.json', clocks_data)
        add_json('monsters.json', monsters_data)
        add_json('shops.json', shops_data)
        add_json('loot_boxes.json', loot_boxes_data)
        add_json('planning_summary.json', planning_data)
        add_json('planning_messages.json', planning_messages_data)
        add_json('bond_proposals.json', bond_proposals_data)
        add_json('audit_log.json', audit_data)
        add_json('memory_runs.json', memory_runs_data)
        add_json('memory_logs.json', memory_logs_data)

        # Manifest
        manifest = {
            'export_version': 1,
            'exported_at': datetime.utcnow().isoformat(),
            'campaign_id': campaign_id,
            'campaign_name': campaign.name,
            'files': [
                'campaign.json',
                'members.json',
                'characters.json',
                'sessions.json',
                'world.json',
                'npcs.json',
                'clocks.json',
                'monsters.json',
                'shops.json',
                'loot_boxes.json',
                'planning_summary.json',
                'planning_messages.json',
                'bond_proposals.json',
                'audit_log.json',
                'memory_runs.json',
                'memory_logs.json',
            ],
        }
        add_json('manifest.json', manifest)

    buf.seek(0)

    # Build a safe filename
    safe_name = re.sub(r'[^\w\-]', '_', campaign.name)[:40].strip('_') or 'campaign'
    timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    filename = f'{safe_name}_{timestamp}.zip'

    return send_file(
        buf,
        mimetype='application/zip',
        as_attachment=True,
        download_name=filename,
    )

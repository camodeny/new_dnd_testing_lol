from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import random
import shutil
from uuid import uuid4

from dev_data import DEV_CHARACTER_TEMPLATES
from models import (
    Campaign,
    CampaignInvite,
    CampaignMember,
    CampaignMonster,
    CampaignSession,
    Character,
    EncounterMap,
    EncounterMapPlacement,
    NPCActor,
    SessionMessage,
    db,
)
from services.campaign_service import generate_invite_code
from services.character_service import build_character_from_data, update_character_relations
from services.encounter_map_service import (
    create_encounter_map,
    encounter_map_labeled_path,
    encounter_map_path,
    encounter_map_storage_dir,
    latest_encounter_map,
)
from services.encounter_movement import movement_grid


DEFAULT_TRAINING_MONSTERS = [
    {
        'monster_id': 'training-skirmisher-1',
        'name': 'Training Skirmisher',
        'stat_block': {
            'max_hp': 11,
            'hp': 11,
            'armor_class': 13,
            'ac': 13,
            'speed': 30,
            'initiative_bonus': 2,
            'abilities': {'dexterity': 14},
        },
    },
    {
        'monster_id': 'training-skirmisher-2',
        'name': 'Training Skirmisher',
        'stat_block': {
            'max_hp': 11,
            'hp': 11,
            'armor_class': 13,
            'ac': 13,
            'speed': 30,
            'initiative_bonus': 2,
            'abilities': {'dexterity': 14},
        },
    },
    {
        'monster_id': 'training-brute-1',
        'name': 'Training Brute',
        'stat_block': {
            'max_hp': 18,
            'hp': 18,
            'armor_class': 12,
            'ac': 12,
            'speed': 25,
            'initiative_bonus': 0,
            'abilities': {'dexterity': 10},
        },
    },
]


def _utcnow():
    return datetime.now(UTC).replace(tzinfo=None)


def _campaign_settings(campaign):
    try:
        return json.loads(campaign.settings) if campaign.settings else {}
    except (TypeError, ValueError):
        return {}


def _set_campaign_settings(campaign, settings):
    campaign.settings = json.dumps(settings or {})


def is_combat_sandbox_campaign(campaign):
    return _campaign_settings(campaign).get('dev_mode') == 'combat_sandbox'


def _create_dev_character(user_id, campaign_id, player_label=''):
    template = random.choice(DEV_CHARACTER_TEMPLATES)
    character = Character(user_id=user_id, campaign_id=campaign_id, player_name=player_label or None)
    build_character_from_data(character, template)
    if player_label:
        character.player_name = player_label
    db.session.add(character)
    db.session.flush()
    update_character_relations(character, template)
    return character


def _ensure_member_character(campaign, user, auto_ready=False):
    member = CampaignMember.query.filter_by(campaign_id=campaign.id, user_id=user.id).first()
    if not member:
        member = CampaignMember(campaign_id=campaign.id, user_id=user.id, role='player')
        db.session.add(member)
        db.session.flush()

    character = member.selected_character if member.selected_character_id else None
    if character and character.campaign_id == campaign.id:
        if auto_ready and member.character_ready_at is None:
            member.character_ready_at = _utcnow()
        return member, character

    character = _create_dev_character(user.id, campaign.id, player_label=user.username or '')
    member.selected_character_id = character.id
    member.character_ready_at = _utcnow() if auto_ready else None
    db.session.flush()
    return member, character


def _ensure_campaign_invite(campaign, user_id):
    code = generate_invite_code()
    invite = CampaignInvite(
        campaign_id=campaign.id,
        code=code,
        created_by=user_id,
        expires_at=_utcnow() + timedelta(days=7),
    )
    db.session.add(invite)
    campaign.invite_code = code
    db.session.flush()
    return invite


def _encounter_grid(encounter_map):
    grid = EncounterMap._json_value(encounter_map.grid_json, {})
    columns = int(grid.get('columns') or 0)
    rows = int(grid.get('rows') or 0)
    return columns, rows


def _cells_for_area(area):
    rect = area.get('rect') if isinstance(area, dict) else {}
    try:
        col = int(rect.get('col'))
        row = int(rect.get('row'))
        width = int(rect.get('width'))
        height = int(rect.get('height'))
    except (TypeError, ValueError, AttributeError):
        return []
    return [
        (cell_col, cell_row)
        for cell_row in range(row, row + max(height, 0))
        for cell_col in range(col, col + max(width, 0))
    ]


def _open_cells(encounter_map, preferred_groups):
    setup = EncounterMap._json_value(encounter_map.vtt_setup_json, {}) or {}
    columns, rows = _encounter_grid(encounter_map)
    if columns <= 0 or rows <= 0:
        return []

    grid = movement_grid(setup, columns, rows)
    occupied = {
        (placement.grid_col, placement.grid_row)
        for placement in EncounterMapPlacement.query.filter_by(encounter_map_id=encounter_map.id).all()
    }

    seen = set()
    cells = []
    for group in preferred_groups:
        areas = setup.get(group) if isinstance(setup.get(group), list) else []
        for area in areas:
            for col, row in _cells_for_area(area):
                if not (0 <= col < columns and 0 <= row < rows):
                    continue
                if (col, row) in seen or (col, row) in occupied:
                    continue
                seen.add((col, row))
                if grid[row][col]['blocked']:
                    continue
                cells.append((col, row))

    if cells:
        return cells

    for row in range(rows):
        for col in range(columns):
            if (col, row) in occupied:
                continue
            if grid[row][col]['blocked']:
                continue
            cells.append((col, row))
    return cells


def _choose_player_spawn(encounter_map, fallback=None):
    cells = _open_cells(encounter_map, ['player_start_areas', 'friendly_spawn_boxes'])
    if cells:
        return cells[0]
    return fallback or (0, 0)


def _choose_enemy_spawns(encounter_map, count):
    cells = _open_cells(encounter_map, ['enemy_start_areas', 'enemy_spawn_boxes'])
    if len(cells) >= count:
        return cells[:count]
    return cells


def _copy_map_asset(source_path: Path | None, campaign_id, suffix=''):
    if not source_path or not source_path.exists():
        return None
    ext = source_path.suffix or '.png'
    filename = f'campaign_{campaign_id}_map_{uuid4().hex}{suffix}{ext}'
    storage_dir = encounter_map_storage_dir()
    storage_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, storage_dir / filename)
    return filename


def _clone_npc(source_campaign_id, target_campaign_id, actor_id):
    existing = NPCActor.query.filter_by(campaign_id=target_campaign_id, actor_id=actor_id).first()
    if existing:
        return existing
    source = NPCActor.query.filter_by(campaign_id=source_campaign_id, actor_id=actor_id).first()
    if not source:
        return None
    row = NPCActor(
        campaign_id=target_campaign_id,
        actor_id=source.actor_id,
        name=source.name,
        role=source.role,
        public_summary=source.public_summary,
        dossier=source.dossier,
    )
    db.session.add(row)
    db.session.flush()
    return row


def _clone_monster(source_campaign_id, target_campaign_id, monster_id):
    existing = CampaignMonster.query.filter_by(campaign_id=target_campaign_id, monster_id=monster_id).first()
    if existing:
        return existing
    source = CampaignMonster.query.filter_by(campaign_id=source_campaign_id, monster_id=monster_id).first()
    if not source:
        return None
    row = CampaignMonster(
        campaign_id=target_campaign_id,
        monster_id=source.monster_id,
        name=source.name,
        stat_block=source.stat_block,
    )
    db.session.add(row)
    db.session.flush()
    return row


def clone_encounter_map_template(source_map, campaign, session):
    image_filename = _copy_map_asset(encounter_map_path(source_map), campaign.id)
    labeled_filename = _copy_map_asset(encounter_map_labeled_path(source_map), campaign.id, suffix='_labeled')
    encounter_map = EncounterMap(
        campaign_id=campaign.id,
        session_id=session.id if session else None,
        title=source_map.title,
        prompt=source_map.prompt,
        image_filename=image_filename,
        labeled_image_filename=labeled_filename,
        model=source_map.model,
        size=source_map.size,
        quality=source_map.quality,
        grid_json=source_map.grid_json,
        vtt_setup_json=source_map.vtt_setup_json,
        setup_status=source_map.setup_status,
        setup_error=source_map.setup_error,
        created_by_tool=source_map.created_by_tool,
        is_archived=False,
    )
    db.session.add(encounter_map)
    db.session.flush()
    return encounter_map


def _clone_non_player_placements(source_map, target_campaign, target_map):
    cloned = []
    source_placements = EncounterMapPlacement.query.filter_by(encounter_map_id=source_map.id).order_by(EncounterMapPlacement.id.asc()).all()
    for placement in source_placements:
        if placement.actor_type == 'player':
            continue
        if placement.actor_type == 'npc':
            _clone_npc(source_map.campaign_id, target_campaign.id, placement.actor_id)
        elif placement.actor_type == 'monster':
            _clone_monster(source_map.campaign_id, target_campaign.id, placement.actor_id)

        row = EncounterMapPlacement(
            encounter_map_id=target_map.id,
            actor_type=placement.actor_type,
            actor_id=placement.actor_id,
            label=placement.label,
            grid_col=placement.grid_col,
            grid_row=placement.grid_row,
        )
        db.session.add(row)
        cloned.append(row)
    db.session.flush()
    return cloned


def _ensure_training_monsters(campaign, encounter_map):
    existing_monsters = EncounterMapPlacement.query.filter_by(encounter_map_id=encounter_map.id, actor_type='monster').count()
    if existing_monsters:
        return

    spawn_cells = _choose_enemy_spawns(encounter_map, len(DEFAULT_TRAINING_MONSTERS))
    if not spawn_cells:
        return

    for monster_data, (col, row) in zip(DEFAULT_TRAINING_MONSTERS, spawn_cells):
        monster = CampaignMonster.query.filter_by(campaign_id=campaign.id, monster_id=monster_data['monster_id']).first()
        if not monster:
            monster = CampaignMonster(
                campaign_id=campaign.id,
                monster_id=monster_data['monster_id'],
                name=monster_data['name'],
                stat_block=json.dumps(monster_data['stat_block']),
            )
            db.session.add(monster)
            db.session.flush()

        placement = EncounterMapPlacement(
            encounter_map_id=encounter_map.id,
            actor_type='monster',
            actor_id=monster.monster_id,
            label=monster.name,
            grid_col=col,
            grid_row=row,
        )
        db.session.add(placement)
    db.session.flush()


def _build_player_combatant(placement, character, initiative=None):
    bonus = int(character.initiative_bonus or 0)
    max_hp = int(character.max_hp or 10)
    current_hp = int(character.current_hp or max_hp)
    speed = int(character.speed or 30)
    return {
        'placement_id': placement.id,
        'actor_type': 'player',
        'actor_id': str(placement.actor_id),
        'label': placement.label,
        'initiative': initiative,
        'initiative_bonus': bonus,
        'max_hp': max_hp,
        'current_hp': current_hp,
        'armor_class': int(character.armor_class or 10),
        'speed': speed,
        'actions': {
            'action': True,
            'bonus_action': True,
            'reaction': True,
            'movement_remaining': speed,
        },
    }


def _initialize_sandbox_combat(encounter_map, campaign, owner_placement):
    from routes.encounter_maps import build_initial_encounter_state, check_and_start_turns

    state = build_initial_encounter_state(encounter_map, campaign)
    for combatant in state.get('turn_order', []):
        if combatant.get('placement_id') == owner_placement.id:
            combatant['initiative'] = max(20, int(combatant.get('initiative_bonus') or 0) + 12)
    check_and_start_turns(state)
    encounter_map.encounter_state_json = json.dumps(state)

    settings = _campaign_settings(campaign)
    settings['encounter_active'] = True
    settings['sandbox_active_map_id'] = encounter_map.id
    _set_campaign_settings(campaign, settings)
    db.session.flush()
    return state


def _pending_bootstrap_settings(payload, source_map):
    if source_map:
        return {
            'mode': 'clone',
            'source_map_id': source_map.id,
        }

    map_title = str(payload.get('map_title') or '').strip()
    map_prompt = str(payload.get('map_prompt') or '').strip()
    if not map_title or not map_prompt:
        raise ValueError('map_title and map_prompt are required when creating a new sandbox map.')
    return {
        'mode': 'generate',
        'map_title': map_title,
        'map_prompt': map_prompt,
        'terrain': str(payload.get('terrain') or ''),
        'tactical_features': str(payload.get('tactical_features') or ''),
        'mood': str(payload.get('mood') or ''),
        'vtt_setup_notes': str(payload.get('vtt_setup_notes') or ''),
    }


def _ready_members_for_sandbox(campaign):
    required_players = max(1, int(_campaign_settings(campaign).get('required_players') or 1))
    members = (
        CampaignMember.query
        .filter_by(campaign_id=campaign.id)
        .order_by(CampaignMember.joined_at.asc(), CampaignMember.id.asc())
        .all()
    )
    selected = []
    for member in members:
        if not member.selected_character_id or member.character_ready_at is None:
            continue
        character = member.selected_character
        if not character or character.campaign_id != campaign.id:
            continue
        selected.append((member, character))
    return selected[:required_players]


def _place_member_character(encounter_map, member, character):
    existing = EncounterMapPlacement.query.filter_by(
        encounter_map_id=encounter_map.id,
        actor_type='player',
        actor_id=str(member.user_id),
    ).first()
    if existing:
        existing.label = character.name
        db.session.flush()
        return existing

    fallback = None
    source_player = (
        EncounterMapPlacement.query
        .filter_by(encounter_map_id=encounter_map.id, actor_type='player')
        .order_by(EncounterMapPlacement.id.asc())
        .first()
    )
    if source_player:
        fallback = (source_player.grid_col, source_player.grid_row)
    col, row = _choose_player_spawn(encounter_map, fallback=fallback)
    placement = EncounterMapPlacement(
        encounter_map_id=encounter_map.id,
        actor_type='player',
        actor_id=str(member.user_id),
        label=character.name,
        grid_col=col,
        grid_row=row,
    )
    db.session.add(placement)
    db.session.flush()
    return placement


def list_combat_sandbox_maps(current_user):
    campaigns = Campaign.query.filter_by(user_id=current_user.id).order_by(Campaign.created_at.desc()).all()
    sandbox_campaign_ids = [campaign.id for campaign in campaigns if is_combat_sandbox_campaign(campaign)]
    if not sandbox_campaign_ids:
        return []

    campaign_names = {campaign.id: campaign.name for campaign in campaigns}
    maps = (
        EncounterMap.query
        .filter(EncounterMap.campaign_id.in_(sandbox_campaign_ids))
        .order_by(EncounterMap.updated_at.desc(), EncounterMap.id.desc())
        .all()
    )
    result = []
    for encounter_map in maps:
        setup = EncounterMap._json_value(encounter_map.vtt_setup_json, {}) or {}
        result.append({
            'id': encounter_map.id,
            'campaign_id': encounter_map.campaign_id,
            'campaign_name': campaign_names.get(encounter_map.campaign_id),
            'title': encounter_map.title,
            'created_at': encounter_map.created_at.isoformat() if encounter_map.created_at else None,
            'updated_at': encounter_map.updated_at.isoformat() if encounter_map.updated_at else None,
            'image_url': f'/api/encounter-maps/{encounter_map.id}/image',
            'labeled_image_url': f'/api/encounter-maps/{encounter_map.id}/labeled-image' if encounter_map.labeled_image_filename else None,
            'map_summary': setup.get('map_summary') if isinstance(setup, dict) else None,
            'tactical_notes': setup.get('tactical_notes', []) if isinstance(setup, dict) else [],
            'setup_status': encounter_map.setup_status,
            'is_archived': encounter_map.is_archived,
        })
    return result


def start_combat_sandbox_session(campaign, current_user):
    active_session = CampaignSession.query.filter_by(campaign_id=campaign.id, is_active=True).first()
    if active_session:
        raise ValueError('An active session already exists.')
    if not is_combat_sandbox_campaign(campaign):
        raise ValueError('Campaign is not a combat sandbox.')

    settings = _campaign_settings(campaign)
    bootstrap = settings.get('sandbox_bootstrap')
    if not isinstance(bootstrap, dict):
        raise ValueError('Combat sandbox setup is missing its pending bootstrap configuration.')

    session = CampaignSession(campaign_id=campaign.id, is_active=True)
    db.session.add(session)
    db.session.flush()

    mode = str(bootstrap.get('mode') or '').strip().lower()
    if mode == 'clone':
        source_map_id = bootstrap.get('source_map_id')
        try:
            source_map_id = int(source_map_id)
        except (TypeError, ValueError):
            raise ValueError('Sandbox bootstrap source_map_id is invalid.')
        source_map = db.session.get(EncounterMap, source_map_id)
        if not source_map:
            raise ValueError('Sandbox bootstrap source map no longer exists.')
        source_campaign = db.session.get(Campaign, source_map.campaign_id)
        if not source_campaign or source_campaign.user_id != campaign.user_id or not is_combat_sandbox_campaign(source_campaign):
            raise ValueError('Sandbox bootstrap source map is no longer available.')
        encounter_map = clone_encounter_map_template(source_map, campaign, session)
        _clone_non_player_placements(source_map, campaign, encounter_map)
    elif mode == 'generate':
        encounter_map = create_encounter_map(
            campaign,
            session,
            str(bootstrap.get('map_title') or '').strip(),
            str(bootstrap.get('map_prompt') or '').strip(),
            terrain=str(bootstrap.get('terrain') or ''),
            tactical_features=str(bootstrap.get('tactical_features') or ''),
            mood=str(bootstrap.get('mood') or ''),
            vtt_setup_notes=str(bootstrap.get('vtt_setup_notes') or ''),
        )
    else:
        raise ValueError('Combat sandbox bootstrap mode is invalid.')

    ready_members = _ready_members_for_sandbox(campaign)
    owner_placement = None
    for member, character in ready_members:
        placement = _place_member_character(encounter_map, member, character)
        if member.user_id == campaign.user_id:
            owner_placement = placement

    if owner_placement is None:
        owner_member, owner_character = _ensure_member_character(campaign, current_user, auto_ready=True)
        owner_placement = _place_member_character(encounter_map, owner_member, owner_character)

    _ensure_training_monsters(campaign, encounter_map)
    _initialize_sandbox_combat(encounter_map, campaign, owner_placement)

    settings['encounter_active'] = True
    settings['sandbox_active_map_id'] = encounter_map.id
    settings['sandbox_started_at'] = _utcnow().isoformat()
    _set_campaign_settings(campaign, settings)

    opening_text = (
        f'Combat sandbox ready. The AI DM has loaded "{encounter_map.title}" and dropped the party into initiative range.'
    )
    db.session.add(SessionMessage(
        session_id=session.id,
        user_id=None,
        role='dm',
        content=opening_text,
    ))
    campaign.last_played_at = _utcnow()
    db.session.commit()

    return {
        'session': session,
        'encounter_map': encounter_map,
    }


def create_combat_sandbox(current_user, payload):
    payload = payload if isinstance(payload, dict) else {}
    name = str(payload.get('name') or '').strip() or 'Combat Sandbox'
    description = str(payload.get('description') or '').strip()
    required_players = max(1, min(10, int(payload.get('required_players') or 1)))
    source_map_id = payload.get('source_map_id')
    source_map = None
    if source_map_id is not None:
        try:
            source_map_id = int(source_map_id)
        except (TypeError, ValueError):
            raise ValueError('source_map_id must be an integer.')
        source_map = db.session.get(EncounterMap, source_map_id)
        if not source_map:
            raise ValueError('Selected sandbox map was not found.')
        source_campaign = db.session.get(Campaign, source_map.campaign_id)
        if not source_campaign or source_campaign.user_id != current_user.id or not is_combat_sandbox_campaign(source_campaign):
            raise ValueError('Selected sandbox map is not available to clone.')
    bootstrap = _pending_bootstrap_settings(payload, source_map)

    settings = {
        'dev_mode': 'combat_sandbox',
        'encounter_active': False,
        'required_players': required_players,
        'loot_mode': 'frequent_gamble',
        'sandbox_bootstrap': bootstrap,
    }
    if source_map:
        settings['sandbox_seed_map_id'] = source_map.id
    else:
        settings['sandbox_seed_mode'] = 'generated'

    campaign = Campaign(
        name=name,
        description=description,
        difficulty='Sandbox',
        seed=str(payload.get('seed') or '').strip(),
        user_id=current_user.id,
        settings=json.dumps(settings),
    )
    db.session.add(campaign)
    db.session.flush()

    owner_member = CampaignMember(campaign_id=campaign.id, user_id=current_user.id, role='player')
    db.session.add(owner_member)
    db.session.flush()

    invite = _ensure_campaign_invite(campaign, current_user.id)
    _, owner_character = _ensure_member_character(campaign, current_user, auto_ready=(required_players <= 1))

    session = None
    encounter_map = None
    deferred_start = required_players > 1
    if deferred_start:
        db.session.commit()
    else:
        started = start_combat_sandbox_session(campaign, current_user)
        session = started['session']
        encounter_map = started['encounter_map']

    return {
        'campaign': campaign.to_dict(),
        'invite': invite.to_dict(),
        'session': session.to_dict() if session else None,
        'encounter_map': encounter_map.to_dict(include_private=True) if encounter_map else None,
        'character_id': owner_character.id,
        'deferred_start': deferred_start,
    }


def bootstrap_joined_member_into_combat_sandbox(campaign, current_user):
    if not is_combat_sandbox_campaign(campaign):
        return None

    active_session = CampaignSession.query.filter_by(campaign_id=campaign.id, is_active=True).first()
    encounter_map = latest_encounter_map(campaign.id) if active_session else None
    member, character = _ensure_member_character(campaign, current_user, auto_ready=bool(encounter_map))
    if not encounter_map:
        db.session.commit()
        return {'member': member.to_dict(), 'character_id': character.id}

    existing = EncounterMapPlacement.query.filter_by(
        encounter_map_id=encounter_map.id,
        actor_type='player',
        actor_id=str(current_user.id),
    ).first()
    if not existing:
        col, row = _choose_player_spawn(encounter_map)
        existing = EncounterMapPlacement(
            encounter_map_id=encounter_map.id,
            actor_type='player',
            actor_id=str(current_user.id),
            label=character.name,
            grid_col=col,
            grid_row=row,
        )
        db.session.add(existing)
        db.session.flush()

    state = EncounterMap._json_value(encounter_map.encounter_state_json, {})
    if isinstance(state, dict) and state.get('active'):
        turn_order = state.get('turn_order') if isinstance(state.get('turn_order'), list) else []
        already_present = any(combatant.get('placement_id') == existing.id for combatant in turn_order)
        if not already_present:
            initiative_bonus = int(character.initiative_bonus or 0)
            initiative = random.randint(1, 20) + initiative_bonus
            turn_order.append(_build_player_combatant(existing, character, initiative=initiative))
            state['turn_order'] = turn_order
            encounter_map.encounter_state_json = json.dumps(state)

    active_session = CampaignSession.query.filter_by(campaign_id=campaign.id, is_active=True).first()
    if active_session:
        db.session.add(SessionMessage(
            session_id=active_session.id,
            user_id=None,
            role='dm',
            content=f'{current_user.username} joins the combat sandbox and is dropped directly onto the active battle map.',
        ))

    db.session.commit()
    return {
        'member': member.to_dict(),
        'character_id': character.id,
        'placement_id': existing.id,
        'encounter_map_id': encounter_map.id,
    }

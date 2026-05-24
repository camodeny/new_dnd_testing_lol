import json
from datetime import datetime

from sqlalchemy.exc import IntegrityError

from models import (
    db,
    CampaignAuditEvent,
    CampaignMember,
    Character,
    CharacterPlanningMessage,
    CampaignPlanningSummary,
    PlanningBondProposal,
)
from services.character_service import character_full_dict


SUMMARY_LIST_FIELDS = (
    'confirmed_public_facts',
    'unresolved_gaps',
    'accepted_hooks',
)

def json_dumps(value):
    return json.dumps(value, ensure_ascii=False)


def json_loads(value, fallback):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def normalize_summary_point(point):
    return ' '.join(str(point or '').strip().split())


def summary_point_key(point):
    return normalize_summary_point(point).casefold()


def clean_explicit_player_points(points_by_user):
    if not isinstance(points_by_user, dict):
        return {}

    cleaned = {}
    for user_id, points in points_by_user.items():
        key = str(user_id)
        values = points if isinstance(points, list) else [points]
        seen = set()
        user_points = []
        for point in values:
            text = normalize_summary_point(point)
            point_key = summary_point_key(text)
            if not text or not point_key or point_key in seen:
                continue
            seen.add(point_key)
            user_points.append(text)
        if user_points:
            cleaned[key] = user_points

    return cleaned


def get_required_players(campaign):
    settings = campaign.to_dict().get('settings', {})
    try:
        return max(1, int(settings.get('required_players', 1)))
    except (TypeError, ValueError):
        return 1


def ensure_campaign_member_records(campaign):
    owner_member = CampaignMember.query.filter_by(
        campaign_id=campaign.id,
        user_id=campaign.user_id,
    ).first()
    if not owner_member:
        db.session.add(CampaignMember(
            campaign_id=campaign.id,
            user_id=campaign.user_id,
            role='player',
            joined_at=campaign.created_at or datetime.utcnow(),
        ))
        db.session.flush()


def get_campaign_members(campaign):
    ensure_campaign_member_records(campaign)
    return CampaignMember.query.filter_by(campaign_id=campaign.id).order_by(
        CampaignMember.joined_at.asc(),
        CampaignMember.id.asc(),
    ).all()


def get_member(campaign_id, user_id):
    return CampaignMember.query.filter_by(campaign_id=campaign_id, user_id=user_id).first()


def get_or_create_summary(campaign_id):
    summary = CampaignPlanningSummary.query.filter_by(campaign_id=campaign_id).first()
    if summary:
        return summary

    summary = CampaignPlanningSummary(
        campaign_id=campaign_id,
        party_balance=json_dumps('The party is still forming.'),
        confirmed_public_facts=json_dumps([]),
        dm_private_secrets=json_dumps({}),
        explicit_player_points=json_dumps({}),
        unresolved_gaps=json_dumps(['No party roles or backstory bonds have been confirmed yet.']),
        accepted_hooks=json_dumps([]),
    )
    db.session.add(summary)
    try:
        db.session.flush()
        return summary
    except IntegrityError:
        db.session.rollback()
        return CampaignPlanningSummary.query.filter_by(campaign_id=campaign_id).first()


def default_summary_dict(campaign_id, include_private=False, current_user_id=None):
    secrets = {}
    if include_private:
        secrets = {}
    elif current_user_id is not None:
        secrets = {str(current_user_id): []}

    return {
        'id': None,
        'campaign_id': campaign_id,
        'party_balance': 'The party is still forming.',
        'confirmed_public_facts': [],
        'dm_private_secrets': secrets,
        'explicit_player_points': {},
        'unresolved_gaps': ['No party roles or backstory bonds have been confirmed yet.'],
        'accepted_hooks': [],
        'updated_at': None,
    }


def summary_dict_for_read(campaign_id, include_private=False, current_user_id=None):
    summary = CampaignPlanningSummary.query.filter_by(campaign_id=campaign_id).first()
    if not summary:
        return default_summary_dict(campaign_id, include_private, current_user_id)
    data = summary.to_dict(include_private=include_private, current_user_id=current_user_id)
    data['explicit_player_points'] = clean_explicit_player_points(data.get('explicit_player_points', {}))
    return data


def invalid_ready_member_ids(members):
    selected_ids = {
        member.selected_character_id
        for member in members
        if member.selected_character_id
    }
    if not selected_ids:
        return set()

    with db.session.no_autoflush:
        characters = Character.query.filter(Character.id.in_(selected_ids)).all()
    characters_by_id = {character.id: character for character in characters}
    invalid_ids = set()
    for member in members:
        if not member.selected_character_id:
            continue

        character = characters_by_id.get(member.selected_character_id)
        if (
            character is None
            or character.user_id != member.user_id
            or character.campaign_id != member.campaign_id
        ):
            invalid_ids.add(member.id)
    return invalid_ids


def clear_invalid_ready_states(members):
    changed = False
    invalid_ids = invalid_ready_member_ids(members)
    for member in members:
        if not member.selected_character_id:
            if member.character_ready_at is not None:
                member.character_ready_at = None
                changed = True
            continue

        if member.id in invalid_ids:
            member.selected_character_id = None
            member.character_ready_at = None
            changed = True
    return changed


def member_is_ready(member, invalid_member_ids=None):
    if invalid_member_ids and member.id in invalid_member_ids:
        return False
    return bool(member.selected_character_id and member.character_ready_at)


def member_planning_dict(member, invalid_member_ids=None):
    invalid = bool(invalid_member_ids and member.id in invalid_member_ids)
    stale_ready = not member.selected_character_id and member.character_ready_at is not None
    data = member.to_dict()
    if invalid or stale_ready:
        data['selected_character_id'] = None
        data['character_ready_at'] = None
        data['is_character_ready'] = False
        data['selected_character'] = None
    else:
        data['selected_character'] = (
            character_full_dict(member.selected_character)
            if member.selected_character is not None
            else None
        )
    return data


def party_is_full(campaign, members):
    return len(members) >= get_required_players(campaign)


def all_members_ready(campaign, members, invalid_member_ids=None):
    required = get_required_players(campaign)
    relevant_members = members[:required]
    return len(relevant_members) >= required and all(
        member_is_ready(member, invalid_member_ids)
        for member in relevant_members
    )


def can_start_session(campaign, clean_ready_states=True):
    members = get_campaign_members(campaign)
    invalid_member_ids = set()
    if clean_ready_states:
        clear_invalid_ready_states(members)
    else:
        invalid_member_ids = invalid_ready_member_ids(members)
    required = get_required_players(campaign)
    return all_members_ready(campaign, members, invalid_member_ids), {
        'required_players': required,
        'ready_players': sum(
            1
            for member in members[:required]
            if member_is_ready(member, invalid_member_ids)
        ),
        'members': [member_planning_dict(member, invalid_member_ids) for member in members],
    }


def recent_planning_messages(campaign_id, per_user=8):
    members = CampaignMember.query.filter_by(campaign_id=campaign_id).all()
    result = []
    for member in members:
        messages = CharacterPlanningMessage.query.filter_by(
            campaign_id=campaign_id,
            user_id=member.user_id,
        ).order_by(CharacterPlanningMessage.created_at.desc()).limit(per_user).all()
        result.extend(reversed(messages))
    result.sort(key=lambda message: message.created_at or datetime.utcnow())
    return result


def planning_context(campaign, current_user=None, clean_ready_states=True):
    members = get_campaign_members(campaign)
    invalid_member_ids = set()
    if clean_ready_states:
        clear_invalid_ready_states(members)
    else:
        invalid_member_ids = invalid_ready_member_ids(members)
    characters = Character.query.filter_by(campaign_id=campaign.id).all()
    bonds = PlanningBondProposal.query.filter_by(campaign_id=campaign.id).order_by(
        PlanningBondProposal.created_at.asc(),
    ).all()

    context = {
        'campaign': campaign.to_dict(),
        'required_players': get_required_players(campaign),
        'members': [member_planning_dict(member, invalid_member_ids) for member in members],
        'characters': [character_full_dict(character) for character in characters],
        'summary': summary_dict_for_read(campaign.id, include_private=True),
        'pending_bonds': [bond.to_dict() for bond in bonds if bond.status == 'pending'],
        'confirmed_bonds': [bond.to_dict() for bond in bonds if bond.status == 'confirmed'],
        'recent_messages': [message.to_dict() for message in recent_planning_messages(campaign.id)],
    }
    if current_user is not None:
        context['current_user'] = current_user.to_dict()
    return context


def merge_summary_update(summary, update):
    if summary is None or not isinstance(update, dict):
        return

    if 'party_balance' in update:
        summary.party_balance = json_dumps(update.get('party_balance') or '')

    for field in SUMMARY_LIST_FIELDS:
        if field in update and isinstance(update[field], list):
            summary_value = json_loads(getattr(summary, field), [])
            merged = list(summary_value)
            for item in update[field]:
                if item and item not in merged:
                    merged.append(item)
            setattr(summary, field, json_dumps(merged))

    if 'explicit_player_points' in update and isinstance(update['explicit_player_points'], dict):
        existing = clean_explicit_player_points(json_loads(summary.explicit_player_points, {}))
        for user_id, points in update['explicit_player_points'].items():
            key = str(user_id)
            canonical_points = clean_explicit_player_points({key: points}).get(key, [])
            if canonical_points:
                existing[key] = canonical_points
            elif key in existing:
                existing.pop(key)
        existing = clean_explicit_player_points(existing)
        summary.explicit_player_points = json_dumps(existing)

    if 'dm_private_secrets' in update and isinstance(update['dm_private_secrets'], dict):
        existing = json_loads(summary.dm_private_secrets, {})
        for user_id, secrets in update['dm_private_secrets'].items():
            key = str(user_id)
            existing.setdefault(key, [])
            for secret in secrets if isinstance(secrets, list) else [secrets]:
                if secret and secret not in existing[key]:
                    existing[key].append(secret)
        summary.dm_private_secrets = json_dumps(existing)

    summary.updated_at = datetime.utcnow()


def apply_bond_suggestions(campaign_id, suggestions):
    if not isinstance(suggestions, list):
        return []

    existing_keys = {
        _bond_suggestion_key(bond.to_dict())
        for bond in PlanningBondProposal.query.filter_by(campaign_id=campaign_id).all()
    }
    created = []
    for suggestion in suggestions:
        if not isinstance(suggestion, dict):
            continue
        involved = [int(user_id) for user_id in suggestion.get('involved_user_ids', []) if str(user_id).isdigit()]
        title = (suggestion.get('title') or '').strip()
        description = (suggestion.get('description') or '').strip()
        if len(involved) < 2 or not title or not description:
            continue

        suggestion_key = _bond_suggestion_key({
            'title': title,
            'description': description,
            'involved_user_ids': involved,
        })
        if suggestion_key in existing_keys:
            continue

        approvals = {str(user_id): 'pending' for user_id in involved}
        proposal = PlanningBondProposal(
            campaign_id=campaign_id,
            title=title[:200],
            description=description,
            involved_user_ids=json_dumps(involved),
            approval_states=json_dumps(approvals),
            status='pending',
        )
        db.session.add(proposal)
        created.append(proposal)
        existing_keys.add(suggestion_key)

    return created


def _bond_suggestion_key(suggestion):
    involved = tuple(sorted(int(user_id) for user_id in suggestion.get('involved_user_ids', [])))
    title = ' '.join((suggestion.get('title') or '').casefold().split())
    description = ' '.join((suggestion.get('description') or '').casefold().split())
    return involved, title, description


def _merge_draft_patch(base, patch):
    if not isinstance(patch, dict):
        return base

    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = _merge_draft_patch(dict(base[key]), value)
        else:
            base[key] = value
    return base


def accumulated_planning_draft_patch(campaign_id, user_id):
    events = CampaignAuditEvent.query.filter_by(
        campaign_id=campaign_id,
        event_type='dm_output_stored',
        source='character_planning_messages',
        actor='planning_dm',
    ).order_by(CampaignAuditEvent.id.asc()).all()

    patch = {}
    latest_event_id = None
    for event in events:
        payload = json_loads(event.payload, {})
        message = payload.get('message') if isinstance(payload, dict) else None
        if not isinstance(message, dict) or message.get('user_id') != user_id:
            continue
        form_patch = payload.get('form_patch')
        if not isinstance(form_patch, dict) or not form_patch:
            continue
        _merge_draft_patch(patch, form_patch)
        latest_event_id = event.id

    return patch, latest_event_id


def visible_planning_payload(campaign, current_user, clean_ready_states=True):
    members = get_campaign_members(campaign)
    invalid_member_ids = set()
    if clean_ready_states:
        clear_invalid_ready_states(members)
    else:
        invalid_member_ids = invalid_ready_member_ids(members)
    messages = CharacterPlanningMessage.query.filter_by(
        campaign_id=campaign.id,
        user_id=current_user.id,
    ).order_by(CharacterPlanningMessage.created_at.asc()).all()
    bonds = PlanningBondProposal.query.filter_by(campaign_id=campaign.id).order_by(
        PlanningBondProposal.created_at.asc(),
    ).all()
    user_bonds = []
    for bond in bonds:
        data = bond.to_dict()
        if current_user.id in data['involved_user_ids'] or bond.status == 'confirmed':
            user_bonds.append(data)

    required = get_required_players(campaign)
    draft_patch, draft_patch_event_id = accumulated_planning_draft_patch(campaign.id, current_user.id)
    return {
        'required_players': required,
        'party_full': party_is_full(campaign, members),
        'all_ready': all_members_ready(campaign, members, invalid_member_ids),
        'members': [member_planning_dict(member, invalid_member_ids) for member in members],
        'summary': summary_dict_for_read(campaign.id, include_private=False, current_user_id=current_user.id),
        'messages': [message.to_dict() for message in messages],
        'bonds': user_bonds,
        'draft_patch': draft_patch,
        'draft_patch_event_id': draft_patch_event_id,
    }

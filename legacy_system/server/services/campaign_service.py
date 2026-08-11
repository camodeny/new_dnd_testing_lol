import secrets
import string

from flask import abort

from models import db, CampaignInvite, CampaignMember


def get_or_404(model, ident):
    item = db.session.get(model, ident)
    if item is None:
        abort(404)
    return item


def generate_invite_code():
    return ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))


def ensure_member(campaign, user):
    """Return True when the user owns or belongs to the campaign."""
    if campaign.user_id == user.id:
        return True

    return CampaignMember.query.filter_by(
        campaign_id=campaign.id,
        user_id=user.id,
    ).first() is not None


def invite_code_matches(campaign, code):
    if not campaign.invite_code or not code:
        return find_invite_by_code(campaign, code) is not None

    normalized = code.strip().upper()
    if campaign.invite_code.strip().upper() == normalized:
        return True

    return find_invite_by_code(campaign, normalized) is not None


def find_invite_by_code(campaign, code):
    if not code:
        return None

    return CampaignInvite.query.filter_by(
        campaign_id=campaign.id,
        code=code.strip().upper(),
        is_used=False,
    ).first()


def current_invite_for_campaign(campaign):
    if not campaign.invite_code:
        return None

    invite = find_invite_by_code(campaign, campaign.invite_code)
    if invite:
        return invite

    return None

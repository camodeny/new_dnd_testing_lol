import secrets
import string

from models import CampaignMember


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

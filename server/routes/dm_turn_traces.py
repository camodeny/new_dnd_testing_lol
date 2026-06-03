from flask import Blueprint, jsonify, request

from auth import token_required
from models import Campaign, CampaignAuditEvent
from services.campaign_service import get_or_404
from services.dm_turn_trace import dm_turn_traces_from_audit_events
from services.planning_service import get_campaign_members


dm_turn_traces_bp = Blueprint('dm_turn_traces', __name__)


@dm_turn_traces_bp.route('/api/campaigns/<int:campaign_id>/dev/dm-turn-traces', methods=['GET'])
@token_required
def get_campaign_dm_turn_traces(current_user, campaign_id):
    campaign = get_or_404(Campaign, campaign_id)
    members = get_campaign_members(campaign)
    member_user_ids = {member.user_id for member in members}
    if current_user.id not in member_user_ids and campaign.user_id != current_user.id:
        return jsonify({'error': 'Forbidden'}), 403

    try:
        limit = int(request.args.get('limit', 50))
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 200))

    audit_events = CampaignAuditEvent.query.filter_by(campaign_id=campaign_id).order_by(
        CampaignAuditEvent.id.asc(),
    ).all()
    traces = dm_turn_traces_from_audit_events(audit_events, limit=limit)
    return jsonify({
        'campaign_id': campaign_id,
        'traces': traces,
        'trace_count': len(traces),
    }), 200

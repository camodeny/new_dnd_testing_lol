from flask import Blueprint, jsonify, request

from auth import token_required
from models import db, Campaign, CampaignClarificationFork, CampaignSession
from services.clarification_forks import add_message, archive_fork, create_fork, resolve_fork, retry_generation
from services.campaign_service import get_or_404


clarification_forks_bp = Blueprint("clarification_forks", __name__)


def _owned_session(current_user, session_id):
    session = get_or_404(CampaignSession, session_id)
    campaign = db.session.get(Campaign, session.campaign_id)
    if not campaign or campaign.user_id != current_user.id:
        return None, None
    return campaign, session


def _owned_fork(current_user, fork_id):
    fork = get_or_404(CampaignClarificationFork, fork_id)
    campaign = db.session.get(Campaign, fork.campaign_id)
    if not campaign or campaign.user_id != current_user.id:
        return None
    return fork


@clarification_forks_bp.route("/api/sessions/<int:session_id>/clarification-forks", methods=["GET"])
@token_required
def list_clarification_forks(current_user, session_id):
    campaign, session = _owned_session(current_user, session_id)
    if not session:
        return jsonify({"error": "Forbidden"}), 403
    forks = CampaignClarificationFork.query.filter_by(session_id=session.id).order_by(CampaignClarificationFork.id.desc()).all()
    return jsonify({"forks": [fork.to_dict() for fork in forks]}), 200


@clarification_forks_bp.route("/api/sessions/<int:session_id>/clarification-forks", methods=["POST"])
@token_required
def create_clarification_fork(current_user, session_id):
    campaign, session = _owned_session(current_user, session_id)
    if not session:
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json() or {}
    question = str(data.get("question") or "").strip()
    if not question:
        return jsonify({"error": "question is required."}), 400
    try:
        fork = create_fork(
            campaign,
            session,
            current_user,
            question,
            anchor_message_id=data.get("anchor_message_id"),
            clarification_id=data.get("clarification_id"),
        )
    except ValueError as err:
        return jsonify({"error": str(err)}), 400
    return jsonify({"fork": fork.to_dict(include_messages=True)}), (201 if fork.status == "active" else 202)


@clarification_forks_bp.route("/api/clarification-forks/<int:fork_id>", methods=["GET"])
@token_required
def get_clarification_fork(current_user, fork_id):
    fork = _owned_fork(current_user, fork_id)
    if not fork:
        return jsonify({"error": "Forbidden"}), 403
    return jsonify({"fork": fork.to_dict(include_messages=True)}), 200


@clarification_forks_bp.route("/api/clarification-forks/<int:fork_id>/retry", methods=["POST"])
@token_required
def retry_clarification_fork_generation(current_user, fork_id):
    fork = _owned_fork(current_user, fork_id)
    if not fork:
        return jsonify({"error": "Forbidden"}), 403
    try:
        fork = retry_generation(fork)
    except ValueError as err:
        return jsonify({"error": str(err)}), 400
    return jsonify({"fork": fork.to_dict(include_messages=True)}), (200 if fork.status == "active" else 202)


@clarification_forks_bp.route("/api/clarification-forks/<int:fork_id>/messages", methods=["POST"])
@token_required
def send_clarification_fork_message(current_user, fork_id):
    fork = _owned_fork(current_user, fork_id)
    if not fork:
        return jsonify({"error": "Forbidden"}), 403
    content = str((request.get_json() or {}).get("content") or "").strip()
    if not content:
        return jsonify({"error": "content is required."}), 400
    try:
        fork = add_message(fork, content)
    except ValueError as err:
        return jsonify({"error": str(err)}), 400
    return jsonify({"fork": fork.to_dict(include_messages=True)}), (200 if fork.status == "active" else 202)


@clarification_forks_bp.route("/api/clarification-forks/<int:fork_id>/resolve", methods=["POST"])
@token_required
def resolve_clarification_fork(current_user, fork_id):
    fork = _owned_fork(current_user, fork_id)
    if not fork:
        return jsonify({"error": "Forbidden"}), 403
    try:
        fork = resolve_fork(fork, (request.get_json() or {}).get("resolution"))
    except ValueError as err:
        return jsonify({"error": str(err)}), 400
    return jsonify({"fork": fork.to_dict(include_messages=True)}), 200


@clarification_forks_bp.route("/api/clarification-forks/<int:fork_id>", methods=["DELETE"])
@token_required
def archive_clarification_fork(current_user, fork_id):
    fork = _owned_fork(current_user, fork_id)
    if not fork:
        return jsonify({"error": "Forbidden"}), 403
    fork = archive_fork(fork)
    return jsonify({"fork": fork.to_dict(include_messages=True)}), 200

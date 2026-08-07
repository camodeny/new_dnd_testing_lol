"""Recoverable partial-state tracking for failed post-turn memory updates.

When the staged memory pipeline fails validation or application, the post-turn
clock/summary sync is skipped. Leaving that silent leaves clocks, summaries, and
scene state stale while visible play continues. This module records an explicit
recoverable recovery task (the failed compiled patch plus diagnostics) that must
be resolved before the next turn, and provides a bounded retry path.
"""
import json

from time_utils import utcnow

MAX_RECOVERY_ATTEMPTS = 3


def create_memory_recovery_task(campaign_id, session, player_message_id, dm_message_id, err, patch, trace_id=None):
    from models import SessionMemoryRecoveryTask, db

    patch_json = json.dumps(patch, default=str) if isinstance(patch, dict) else None
    task = SessionMemoryRecoveryTask(
        campaign_id=campaign_id,
        session_id=session.id if session else None,
        player_message_id=player_message_id,
        dm_message_id=dm_message_id,
        trace_id=trace_id,
        status='pending',
        error_stage=getattr(err, 'stage', None),
        error_code=getattr(err, 'code', 'unknown'),
        error_text=str(err),
        patch_json=patch_json,
        attempts=0,
    )
    db.session.add(task)
    db.session.commit()
    return task


def pending_memory_recovery_tasks(campaign_id):
    from models import SessionMemoryRecoveryTask

    return (
        SessionMemoryRecoveryTask.query
        .filter_by(campaign_id=campaign_id, status='pending')
        .order_by(SessionMemoryRecoveryTask.id.asc())
        .all()
    )


def has_pending_memory_recovery(campaign_id):
    return len(pending_memory_recovery_tasks(campaign_id)) > 0


def resolve_memory_recovery_tasks(campaign_id, player_message_id, resolved_reason='memory_applied'):
    """Mark any pending recovery task for a turn as resolved.

    Used when a memory patch for the turn later succeeds, so a subsequent retry
    does not replay an already-applied patch.
    """
    from models import SessionMemoryRecoveryTask, db

    tasks = (
        SessionMemoryRecoveryTask.query
        .filter_by(campaign_id=campaign_id, player_message_id=player_message_id, status='pending')
        .all()
    )
    resolved = 0
    for task in tasks:
        task.status = 'resolved'
        task.resolved_at = utcnow()
        task.last_error_text = resolved_reason
        resolved += 1
    if resolved:
        db.session.commit()
    return resolved


def retry_memory_recovery_task(campaign_id, task_id):
    from models import Campaign, CampaignSession, SessionMemoryRecoveryTask, db
    from services.dm_tools import apply_compiled_session_memory_patch

    task = (
        SessionMemoryRecoveryTask.query
        .filter_by(id=task_id, campaign_id=campaign_id)
        .first()
    )
    if task is None:
        return {'ok': False, 'error': 'task_not_found', 'status': None}
    if task.status != 'pending':
        return {'ok': False, 'error': f'task_status_{task.status}', 'status': task.status}
    if not task.patch_json:
        task.status = 'failed'
        task.last_error_text = 'task_missing_patch'
        db.session.commit()
        return {'ok': False, 'error': 'task_missing_patch', 'status': 'failed'}
    try:
        patch = json.loads(task.patch_json)
    except Exception as err:
        task.status = 'failed'
        task.last_error_text = f'invalid_stored_patch: {err}'
        db.session.commit()
        return {'ok': False, 'error': 'invalid_stored_patch', 'status': 'failed'}

    campaign = db.session.get(Campaign, campaign_id)
    session = db.session.get(CampaignSession, task.session_id) if task.session_id else None
    try:
        result = apply_compiled_session_memory_patch(campaign, session, patch)
    except Exception as err:
        db.session.rollback()
        # The rollback above expires the task row; re-load it so the attempt
        # counter and last error survive the failed retry.
        task = db.session.get(SessionMemoryRecoveryTask, task.id)
        task.attempts += 1
        task.status = 'failed' if task.attempts >= MAX_RECOVERY_ATTEMPTS else 'pending'
        task.last_error_text = str(err)
        db.session.commit()
        return {
            'ok': False,
            'error': str(err),
            'status': task.status,
            'attempts': task.attempts,
            'task_id': task.id,
        }

    task = db.session.get(SessionMemoryRecoveryTask, task.id)
    task.attempts += 1
    task.status = 'resolved'
    task.resolved_at = utcnow()
    task.last_error_text = None
    db.session.commit()
    return {
        'ok': True,
        'status': 'resolved',
        'attempts': task.attempts,
        'task_id': task.id,
        'result': result,
    }

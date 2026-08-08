"""Recoverable partial-state tracking for failed post-turn memory updates.

When the staged memory pipeline fails validation or application, the post-turn
clock/summary sync is skipped. Leaving that silent leaves clocks, summaries, and
scene state stale while visible play continues. This module records an explicit
recoverable recovery task (the failed compiled patch plus the inputs needed to
replay the skipped clock adjudication) and provides a bounded retry path that
repairs the whole post-turn pipeline: re-applies memory, re-runs clock
adjudication, and flips the durable SessionDmTurn out of its error state.
"""
import json

from time_utils import utcnow

MAX_RECOVERY_ATTEMPTS = 3


def _serialize_context(context):
    return json.dumps(context, default=str) if isinstance(context, dict) else None


def create_memory_recovery_task(
    campaign_id,
    session,
    player_message_id,
    dm_message_id,
    err,
    patch,
    trace_id=None,
    context=None,
):
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
        context_json=_serialize_context(context),
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


def _replay_clock_adjudication(task, campaign, session):
    """Re-run the clock adjudication that was skipped by the original failure."""
    from models import SessionMessage, User, db
    from openrouter import get_session_clock_updates
    from services.dm_tools import apply_clock_adjudication, build_session_clock_context
    from services.world_service import world_public_payload

    context = {}
    if task.context_json:
        try:
            context = json.loads(task.context_json)
        except (TypeError, ValueError):
            context = {}

    player_row = db.session.get(SessionMessage, task.player_message_id) if task.player_message_id else None
    dm_row = db.session.get(SessionMessage, task.dm_message_id) if task.dm_message_id else None
    player_content = player_row.content if player_row else ''
    dm_content = dm_row.content if dm_row else ''

    current_user = db.session.get(User, context.get('current_user_id')) if context.get('current_user_id') else None
    if current_user is None and campaign.user_id:
        current_user = db.session.get(User, campaign.user_id)

    current_scene_before = context.get('current_scene_before') if isinstance(context.get('current_scene_before'), dict) else {}
    current_scene_after = (world_public_payload(campaign).get('world') or {}).get('current_scene') or {}

    clock_context = build_session_clock_context(
        campaign,
        session,
        current_user,
        player_content,
        dm_content,
        current_scene_before,
        current_scene_after,
        player_message_id=task.player_message_id,
        dm_message_id=task.dm_message_id,
    )
    clock_trace_id = f"session_clock_adjudicator:session_{session.id}:message_{task.player_message_id}:recovery"
    clock_updates = get_session_clock_updates(
        clock_context,
        audit_context={
            'campaign_id': campaign.id,
            'operation': 'session_clock_adjudication_recovery',
            'actor': 'session_clock_adjudicator',
            'trace_id': clock_trace_id,
            'trace_label': f'session_clock_adjudicator recovery: session {session.id}',
            'source_player_message_id': task.player_message_id,
            'source_dm_message_id': task.dm_message_id,
        },
    )
    if clock_updates is None:
        raise RuntimeError('Clock adjudication did not return a tool submission during recovery.')
    return apply_clock_adjudication(
        campaign,
        clock_updates,
        audit_context={
            'trace_id': clock_trace_id,
            'source_player_message_id': task.player_message_id,
            'source_dm_message_id': task.dm_message_id,
        },
        allowed_evidence_sources=clock_context.get('allowed_evidence_sources') or [],
    )


def _recovery_transcript_content(task):
    from models import SessionMessage, db

    player_row = db.session.get(SessionMessage, task.player_message_id) if task.player_message_id else None
    dm_row = db.session.get(SessionMessage, task.dm_message_id) if task.dm_message_id else None
    return (
        player_row.content if player_row else '',
        dm_row.content if dm_row else '',
    )


def _run_recovery_finalization(campaign, session, task, player_content, dm_content):
    """Complete the #107 post-clock finalization pipeline for a recovered turn.

    Mirrors the normal turn's terminal ordering: deterministic clock repair ->
    running-summary finalization against repaired committed state -> leak-guard
    redaction -> read-only semantic verification. Returns
    (terminal_revision, final_summary_or_None, error_or_None). Any failure leaves
    the caller to keep recovery pending (fail closed) rather than marking the
    turn complete with stale or unchecked state.
    """
    from openrouter import get_session_running_summary_finalize
    from routes.sessions import _repair_post_turn_clocks, _verify_post_turn_state
    from services.dm_tools import (
        build_session_summary_finalize_context,
        redact_session_summary_private_terms,
    )
    from models import db

    # 3. Deterministic clock repair BEFORE summary finalization so the running
    # summary is authored against repaired (not merely adjudicated) clock state.
    terminal_revision, repair_incident = _repair_post_turn_clocks(
        campaign,
        session,
        task.player_message_id,
        task.dm_message_id,
        task.trace_id,
        bump_revision=True,
    )
    if repair_incident:
        return terminal_revision, None, f'post_clock_repair_incident: {repair_incident}'

    # 4. Finalize the running summary from the repaired committed state.
    summary_context = None
    finalized_summary = None
    summary_error = None
    summary_trace_id = f"session_summary_finalizer:session_{session.id}:message_{task.player_message_id}:recovery"
    try:
        summary_context = build_session_summary_finalize_context(
            campaign,
            session,
            player_content,
            dm_content,
            player_message_id=task.player_message_id,
            dm_message_id=task.dm_message_id,
        )
        finalized = get_session_running_summary_finalize(
            summary_context,
            audit_context={
                'campaign_id': campaign.id,
                'operation': 'session_summary_finalize_recovery',
                'actor': 'session_summary_finalizer',
                'trace_id': summary_trace_id,
                'trace_label': f'session_summary_finalizer recovery: session {session.id}',
            },
        )
        finalized_summary = (finalized or {}).get('running_summary') if isinstance(finalized, dict) else None
    except Exception as err:
        summary_error = repr(err)
    if not finalized_summary:
        return terminal_revision, None, f'summary_finalize_failed: {summary_error or "no content"}'

    session.running_summary = finalized_summary.strip()
    redacted, _redacted = redact_session_summary_private_terms(
        campaign,
        session.running_summary,
        player_content,
        dm_content,
    )
    session.running_summary = redacted
    db.session.commit()

    # 5. Read-only semantic/state verification AFTER summary finalization.
    _verified, verify_incident = _verify_post_turn_state(
        campaign,
        session,
        task.player_message_id,
        task.dm_message_id,
        task.trace_id,
        summary_text=session.running_summary,
        summary_context=summary_context,
    )
    if verify_incident:
        return terminal_revision, None, f'verify_incident: {verify_incident}'

    return terminal_revision, session.running_summary, None


def retry_memory_recovery_task(campaign_id, task_id):
    from models import Campaign, CampaignSession, SessionMemoryRecoveryTask, db
    from services.audit_service import log_audit_event
    from services.dm_tools import apply_compiled_session_memory_patch
    from services.dm_turns import repair_session_dm_turn

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

    # 1. Re-apply the failed memory patch unless this exact task already applied
    # it on an earlier attempt (tracked explicitly by memory_applied -- never
    # inferred from the world revision, which an unrelated write could advance).
    # Committed as its own transaction so a later clock-replay failure cannot
    # drag partial clock mutations into the same commit.
    memory_result = {"memory_already_applied": bool(task.memory_applied)}
    if not task.memory_applied:
        try:
            memory_result = apply_compiled_session_memory_patch(campaign, session, patch)
            task = db.session.get(SessionMemoryRecoveryTask, task.id)
            task.memory_applied = True
            db.session.commit()
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

    # 2. Replay the clock adjudication the original failure skipped so
    # time-sensitive clocks are not left stale.
    clock_recovered = False
    clock_result = None
    clock_error = None
    if session is not None:
        try:
            clock_result = _replay_clock_adjudication(task, campaign, session)
            clock_recovered = True
        except Exception as err:
            clock_error = str(err)

    # 3. Fail closed when clock replay could not complete. Memory may already be
    # durable, but a stale clock is exactly the silent regression this recovery
    # exists to prevent, so the task stays pending (blocking) and the durable
    # turn stays in its error state instead of being reported repaired.
    if not clock_recovered:
        db.session.rollback()
        task = db.session.get(SessionMemoryRecoveryTask, task.id)
        task.attempts += 1
        task.status = 'pending'
        task.last_error_text = f'clock_replay_failed: {clock_error}'
        db.session.commit()
        return {
            'ok': False,
            'error': f'clock_replay_failed: {clock_error}',
            'status': 'pending',
            'attempts': task.attempts,
            'task_id': task.id,
            'memory_already_applied': bool(task.memory_applied),
        }

    # 4. Complete the #107 post-clock finalization pipeline: deterministic clock
    # repair, running-summary finalization against repaired committed state,
    # leak-guard redaction, then read-only semantic verification. Only a clean
    # finalization may resolve the task and mark the turn complete.
    player_content, dm_content = _recovery_transcript_content(task)
    terminal_revision, final_summary, finalize_error = _run_recovery_finalization(
        campaign,
        session,
        task,
        player_content,
        dm_content,
    )
    if finalize_error:
        db.session.rollback()
        task = db.session.get(SessionMemoryRecoveryTask, task.id)
        task.attempts += 1
        task.status = 'pending'
        task.last_error_text = finalize_error
        db.session.commit()
        return {
            'ok': False,
            'error': finalize_error,
            'status': 'pending',
            'attempts': task.attempts,
            'task_id': task.id,
            'memory_already_applied': bool(task.memory_applied),
        }

    # 5. Resolve the recovery task and repair the durable turn, recording the
    # correlated terminal revision produced by the finalization pipeline.
    task = db.session.get(SessionMemoryRecoveryTask, task.id)
    task.attempts += 1
    task.status = 'resolved'
    task.resolved_at = utcnow()
    task.last_error_text = None
    repair_session_dm_turn(
        task.player_message_id,
        dm_message_id=task.dm_message_id,
        memory_status='complete',
        clock_status='complete',
        post_turn_revision=terminal_revision,
    )
    db.session.commit()

    log_audit_event(
        campaign_id,
        'memory_recovery_applied',
        'Recovered a previously failed post-turn memory update.',
        {
            'recovery_task_id': task.id,
            'player_message_id': task.player_message_id,
            'dm_message_id': task.dm_message_id,
            'memory_status': 'complete',
            'clock_status': 'complete',
            'memory_already_applied': bool(task.memory_applied),
            'post_turn_revision': terminal_revision,
            'memory_result': memory_result,
        },
        source='session_memory',
        actor='session_memory_writer',
        trace_id=task.trace_id,
        audit_role='tools',
        commit=True,
    )

    return {
        'ok': True,
        'status': 'resolved',
        'attempts': task.attempts,
        'task_id': task.id,
        'clock_recovered': True,
        'post_turn_revision': terminal_revision,
        'memory_already_applied': bool(task.memory_applied),
        'result': memory_result,
    }

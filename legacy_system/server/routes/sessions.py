import json
import queue

from flask import Blueprint, current_app, jsonify, request

from auth import authenticate_request, token_required
from models import (
    db,
    Campaign,
    CampaignAuditEvent,
    CampaignSession,
    SessionMessage,
    SheetProposal,
    Character,
    User,
)
from time_utils import utcnow
from openrouter import (
    get_opening_scene_response,
    get_session_clock_updates,
    get_session_dm_response_with_tools,
    get_session_memory_patch,
    get_session_running_summary_finalize,
    normalize_session_dm_turn_decision,
)
from services.stream_manager import stream_manager
from services.planning_stream import planning_stream_manager
from services.audit_service import log_audit_event
from services.campaign_service import ensure_member, get_or_404
from services.character_service import character_full_dict
from services.dm_tools import (
    get_dm_tool_definitions,
    SHEET_SCALAR_FIELDS,
    apply_clock_adjudication,
    apply_compiled_session_memory_patch,
    build_session_hot_context,
    build_session_clock_context,
    build_session_memory_context,
    build_session_retrieval_packet,
    build_session_summary_finalize_context,
    context_manifest,
    execute_dm_tool,
    redact_session_summary_private_terms,
)
from services.dm_turn_commit import commit_accepted_dm_turn
from services.dm_turns import (
    begin_session_dm_turn,
    mark_session_dm_turn_error,
    mark_session_dm_turn_post_turn_complete,
    mark_session_dm_turn_visible,
    session_dm_trace_id,
    session_dm_turn_status_payload,
)
from services.dev_combat_sandbox import is_combat_sandbox_campaign, start_combat_sandbox_session
from services.planning_service import can_start_session, planning_context
from services.session_memory_agent import MemoryPipelineError, _known_ids
from services.world_service import approve_world, dm_world_context, ensure_world_generated, world_public_payload

sessions_bp = Blueprint('sessions', __name__)

DEFAULT_MESSAGE_PAGE_SIZE = 50
MAX_MESSAGE_PAGE_SIZE = 100


def _message_page(session_id, before_id=None, limit=DEFAULT_MESSAGE_PAGE_SIZE):
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = DEFAULT_MESSAGE_PAGE_SIZE
    limit = max(1, min(limit, MAX_MESSAGE_PAGE_SIZE))

    query = SessionMessage.query.filter_by(session_id=session_id)
    if before_id:
        try:
            before_id = int(before_id)
        except (TypeError, ValueError):
            before_id = None
        if before_id:
            query = query.filter(SessionMessage.id < before_id)

    rows = query.order_by(SessionMessage.id.desc()).limit(limit + 1).all()
    has_more = len(rows) > limit
    messages = list(reversed(rows[:limit]))
    return {
        'messages': [message.to_dict() for message in messages],
        'has_more_messages': has_more,
    }


def _session_dm_turn_decision(raw_result):
    decision = normalize_session_dm_turn_decision(raw_result)
    if decision.get('mode') == 'silent':
        return {
            'mode': 'silent',
            'content': '',
            'reason': decision.get('reason') or 'The DM intentionally stayed silent.',
        }
    result = {
        'mode': 'speak',
        'content': decision.get('content') or '',
        'parts': decision.get('parts') if isinstance(decision.get('parts'), list) else [],
        'commit_action_ids': decision.get('commit_action_ids'),
    }
    if isinstance(decision.get('disclose_item_ids'), list):
        result['disclose_item_ids'] = decision['disclose_item_ids']
    if isinstance(decision.get('resolver_packet'), dict):
        result['resolver_packet'] = decision['resolver_packet']
    return result


def _member_record(campaign_id, user_id):
    from models import CampaignMember

    return CampaignMember.query.filter_by(campaign_id=campaign_id, user_id=user_id).first()


def _repair_post_turn_clocks(campaign, session, player_message_id, dm_message_id, parent_trace_id, bump_revision=True):
    """Mutating post-turn clock repair (supersede stale clocks, emit the
    correlated terminal revision). Must run BEFORE the running summary is
    finalized. Commits its repairs and logs any unresolvable contradiction as an
    actionable incident.

    Returns (terminal_revision, incident_text_or_None).
    """
    from services.post_turn_consistency import PostTurnConsistencyIncident, repair_post_turn_clocks
    from services.audit_service import log_audit_event as _log_reconcile_audit

    trace_label = f'post_turn_consistency: session {session.id if session else None}'
    try:
        report = repair_post_turn_clocks(
            campaign,
            session,
            player_message_id=player_message_id,
            dm_message_id=dm_message_id,
            trace_id=parent_trace_id,
            parent_trace_id=parent_trace_id,
            trace_label=trace_label,
            bump_revision=bump_revision,
        )
        db.session.commit()
        return report.get('terminal_revision'), None
    except PostTurnConsistencyIncident as incident:
        # Deterministic repairs made before the incident were intentional and safe;
        # commit them so the durable state is still as coherent as possible.
        db.session.commit()
        _log_reconcile_audit(
            campaign.id,
            'post_turn_consistency_incident',
            incident.summary,
            {
                'player_message_id': player_message_id,
                'dm_message_id': dm_message_id,
                'report': incident.report,
            },
            source='post_turn_consistency',
            actor='session_memory_writer',
            trace_id=parent_trace_id,
            parent_trace_id=parent_trace_id,
            trace_label=trace_label,
            audit_role='tools',
            commit=True,
        )
        return incident.terminal_revision, incident.summary
    except Exception as err:
        db.session.rollback()
        incident_summary = repr(err)
        _log_reconcile_audit(
            campaign.id,
            'post_turn_consistency_incident',
            'Post-turn consistency reconciliation failed.',
            {
                'player_message_id': player_message_id,
                'dm_message_id': dm_message_id,
                'error': incident_summary,
            },
            source='post_turn_consistency',
            actor='session_memory_writer',
            trace_id=parent_trace_id,
            parent_trace_id=parent_trace_id,
            trace_label=trace_label,
            audit_role='tools',
            commit=True,
        )
        return None, incident_summary


def _verify_post_turn_state(campaign, session, player_message_id, dm_message_id, parent_trace_id, summary_text=None, summary_context=None):
    """Read-only post-turn consistency verification, run AFTER the running
    summary has been finalized. Semantically verifies the finalized summary
    against committed clock/scene/fact state and re-checks active clocks. Never
    mutates durable state. Logs and returns an incident text when a
    contradiction remains so the turn is not reported complete.

    Returns (verified, incident_text_or_None).
    """
    from services.post_turn_consistency import PostTurnConsistencyIncident, verify_post_turn_state
    from services.audit_service import log_audit_event as _log_reconcile_audit

    trace_label = f'post_turn_consistency: session {session.id if session else None}'
    try:
        report = verify_post_turn_state(
            campaign,
            session,
            player_message_id=player_message_id,
            dm_message_id=dm_message_id,
            trace_id=parent_trace_id,
            parent_trace_id=parent_trace_id,
            trace_label=trace_label,
            summary_text=summary_text,
            summary_context=summary_context,
        )
        db.session.commit()
        return report.get('verified', True), None
    except PostTurnConsistencyIncident as incident:
        db.session.commit()
        _log_reconcile_audit(
            campaign.id,
            'post_turn_consistency_incident',
            incident.summary,
            {
                'player_message_id': player_message_id,
                'dm_message_id': dm_message_id,
                'report': incident.report,
            },
            source='post_turn_consistency',
            actor='session_memory_writer',
            trace_id=parent_trace_id,
            parent_trace_id=parent_trace_id,
            trace_label=trace_label,
            audit_role='tools',
            commit=True,
        )
        return False, incident.summary
    except Exception as err:
        db.session.rollback()
        incident_summary = repr(err)
        _log_reconcile_audit(
            campaign.id,
            'post_turn_consistency_incident',
            'Post-turn consistency verification failed.',
            {
                'player_message_id': player_message_id,
                'dm_message_id': dm_message_id,
                'error': incident_summary,
            },
            source='post_turn_consistency',
            actor='session_memory_writer',
            trace_id=parent_trace_id,
            parent_trace_id=parent_trace_id,
            trace_label=trace_label,
            audit_role='tools',
            commit=True,
        )
        return False, incident_summary


def _run_session_memory_update(
    campaign_id,
    session_id,
    user_id,
    player_message_id,
    player_content,
    ai_text,
    hot_context,
    parent_trace_id,
    dm_message_id=None,
    response_parts=None,
    resolver_packet=None,
):
    from uuid import uuid4
    memory_run_id = f"memrun_{uuid4().hex[:12]}"
    memory_trace_id = f'session_memory_writer:session_{session_id}:message_{player_message_id}'
    trace_label = f'session_memory_writer: session {session_id}'
    clock_trace_id = f'session_clock_adjudicator:session_{session_id}:message_{player_message_id}'
    clock_trace_label = f'session_clock_adjudicator: session {session_id}'
    memory_complete = False
    clock_complete = False
    memory_patch = None
    try:
        hot_context = dict(hot_context if isinstance(hot_context, dict) else {})
        hot_context['turn_id'] = f"turn_{player_message_id}"

        campaign = db.session.get(Campaign, campaign_id)
        session = db.session.get(CampaignSession, session_id)
        current_user = db.session.get(User, user_id)
        if not campaign or not session or not current_user:
            return

        world_before = world_public_payload(campaign).get('world') or {}
        current_scene_before = world_before.get('current_scene')

        memory_context = build_session_memory_context(
            campaign,
            session,
            current_user,
            player_content,
            ai_text,
            hot_context,
        )
        # Load accepted internal parts from storage; this is the canonical memory source.
        if dm_message_id:
            from models import CampaignDmResponseParts
            try:
                stored = CampaignDmResponseParts.query.filter_by(
                    campaign_id=campaign.id,
                    dm_message_id=dm_message_id,
                ).order_by(CampaignDmResponseParts.id.desc()).first()
            except Exception as e:
                # database read failure fails the run
                raise MemoryPipelineError(
                    stage="ingest_response_parts",
                    code="response_parts_storage_read_error",
                    message=f"Failed to read accepted response parts from database: {e}",
                    telemetry={"dm_message_id": dm_message_id, "error": str(e)},
                )

            if stored:
                if not isinstance(stored.parts_json, list):
                    raise MemoryPipelineError(
                        stage="ingest_response_parts",
                        code="malformed_stored_response_parts",
                        message="Stored response parts are not a valid JSON array.",
                        telemetry={"dm_message_id": dm_message_id},
                    )
                memory_context['latest_dm_response_parts'] = stored.parts_json

                from models import CampaignResolverPacket
                stored_packet = CampaignResolverPacket.query.filter_by(
                    campaign_id=campaign.id, dm_message_id=dm_message_id, status='committed',
                ).order_by(CampaignResolverPacket.id.desc()).first()
                if stored_packet:
                    memory_context['resolver_packet'] = stored_packet.packet_json

                # Check for transient/stored mismatch
                if isinstance(response_parts, list) and response_parts:
                    if stored.parts_json != response_parts:
                        raise MemoryPipelineError(
                            stage="ingest_response_parts",
                            code="response_parts_mismatch",
                            message="Transient response parts do not match the accepted stored response parts.",
                            telemetry={
                                "dm_message_id": dm_message_id,
                                "stored": stored.parts_json,
                                "transient": response_parts,
                            },
                        )
            else:
                if isinstance(response_parts, list) and response_parts:
                    raise MemoryPipelineError(
                        stage="ingest_response_parts",
                        code="response_parts_missing_in_storage",
                        message="Accepted response parts are missing from storage.",
                        telemetry={"dm_message_id": dm_message_id, "transient": response_parts},
                    )
        else:
            if isinstance(response_parts, list):
                memory_context['latest_dm_response_parts'] = response_parts
            if isinstance(resolver_packet, dict):
                memory_context['resolver_packet'] = resolver_packet

        memory_audit_context = {
            'campaign_id': campaign.id,
            'operation': 'session_memory_update',
            'actor': 'session_memory_writer',
            'trace_id': memory_trace_id,
            'parent_trace_id': parent_trace_id,
            'trace_label': trace_label,
            'memory_run_id': memory_run_id,
            'source_player_message_id': player_message_id,
            'source_dm_message_id': dm_message_id,
            'latest_player_message': player_content,
            'latest_dm_message': ai_text,
        }

        memory_patch = get_session_memory_patch(
            memory_context,
            audit_context=memory_audit_context,
        )
        if memory_patch:
            source_contract = memory_patch.get('source_contract', '') if isinstance(memory_patch, dict) else ''
            if source_contract == 'compiled_session_memory_v2':
                apply_compiled_session_memory_patch(
                    campaign,
                    session,
                    memory_patch,
                    audit_context=memory_audit_context,
                )
            else:
                raise MemoryPipelineError(
                    stage="validation",
                    code="invalid_contract",
                    message=f"Memory patch missing required source_contract 'compiled_session_memory_v2'. Got: {source_contract!r}",
                )
            memory_complete = True
            from services.memory_recovery import resolve_memory_recovery_tasks
            resolve_memory_recovery_tasks(campaign.id, player_message_id, resolved_reason='memory_applied')

        # Commit memory transaction independently
        try:
            db.session.commit()
        except Exception as mem_err:
            db.session.rollback()
            raise

        world_after_memory = world_public_payload(campaign).get('world') or {}
        current_scene_after_memory = world_after_memory.get('current_scene')

        # Clock adjudication in a separate transaction
        clock_complete = False
        try:
            clock_context = build_session_clock_context(
                campaign,
                session,
                current_user,
                player_content,
                ai_text,
                current_scene_before,
                current_scene_after_memory,
                player_message_id=player_message_id,
                dm_message_id=dm_message_id,
            )
            clock_updates = get_session_clock_updates(
                clock_context,
                audit_context={
                    'campaign_id': campaign.id,
                    'operation': 'session_clock_adjudication',
                    'actor': 'session_clock_adjudicator',
                    'trace_id': clock_trace_id,
                    'parent_trace_id': parent_trace_id,
                    'trace_label': clock_trace_label,
                },
            )
            if clock_updates is None:
                raise RuntimeError('Clock adjudication did not return a tool submission.')
            apply_clock_adjudication(
                campaign,
                clock_updates,
                audit_context={
                    'trace_id': clock_trace_id,
                    'parent_trace_id': parent_trace_id,
                    'trace_label': clock_trace_label,
                    'source_player_message_id': player_message_id,
                    'source_dm_message_id': dm_message_id,
                },
                allowed_evidence_sources=clock_context.get('allowed_evidence_sources') or [],
            )
            clock_complete = True
            db.session.commit()
        except Exception:
            db.session.rollback()
            clock_complete = False

        # Deterministic clock repair BEFORE summary finalization so the running
        # summary is authored against repaired (not merely adjudicated) clock
        # state: supersede stale clocks and emit the correlated terminal revision.
        terminal_revision, repair_incident = _repair_post_turn_clocks(
            campaign,
            session,
            player_message_id,
            dm_message_id,
            parent_trace_id,
            bump_revision=True,
        )
        if repair_incident:
            mark_session_dm_turn_error(
                campaign.id,
                session_id,
                player_message_id,
                parent_trace_id,
                repair_incident,
                dm_message_id=dm_message_id,
                memory_status='complete' if memory_complete else 'skipped',
                clock_status='complete' if clock_complete else 'error',
                post_turn_revision=terminal_revision,
            )
            db.session.commit()
            world_after = world_public_payload(campaign).get('world') or {}
            current_scene_after = world_after.get('current_scene')
            if current_scene_after != current_scene_before:
                stream_manager.broadcast_event(session_id, {
                    'type': 'scene_updated',
                    'current_scene': current_scene_after,
                })
            return

        # Finalize the running summary against the committed, repaired post-clock
        # state so it can never encode a pre-adjudication clock value. This is a
        # narrow LLM pass (previous summary + current turn + committed scene/
        # facts/clocks), not the full memory compiler. Fail closed on error: do
        # NOT report the turn complete with the old summary still presented current.
        summary_finalize_trace_id = f'session_summary_finalizer:session_{session_id}:message_{player_message_id}'
        summary_finalize_label = f'session_summary_finalizer: session {session_id}'
        summary_finalize_error = None
        try:
            summary_context = build_session_summary_finalize_context(
                campaign,
                session,
                player_content,
                ai_text,
                player_message_id=player_message_id,
                dm_message_id=dm_message_id,
            )
            finalized = get_session_running_summary_finalize(
                summary_context,
                audit_context={
                    'campaign_id': campaign.id,
                    'operation': 'session_summary_finalize',
                    'actor': 'session_summary_finalizer',
                    'trace_id': summary_finalize_trace_id,
                    'parent_trace_id': parent_trace_id,
                    'trace_label': summary_finalize_label,
                },
            )
        except Exception as err:
            finalized = None
            summary_finalize_error = repr(err)
        finalized_summary = (finalized or {}).get('running_summary') if isinstance(finalized, dict) else None
        if not finalized_summary:
            log_audit_event(
                campaign_id,
                'post_turn_summary_finalize_error',
                'Running summary finalization failed after memory and clock commits.',
                {
                    'session_id': session_id,
                    'error': summary_finalize_error or 'summary_finalizer returned no content',
                },
                source='session_memory',
                actor='session_summary_finalizer',
                trace_id=summary_finalize_trace_id,
                parent_trace_id=parent_trace_id,
                trace_label=summary_finalize_label,
                audit_role='tools',
                commit=True,
            )
            # Memory and clock are already committed; only the summary pass is
            # pending, so the turn is error/recoverable rather than complete.
            mark_session_dm_turn_error(
                campaign_id,
                session_id,
                player_message_id,
                parent_trace_id,
                'Running summary finalization failed; the turn is recoverable but not complete.',
                dm_message_id=dm_message_id,
                memory_status='complete',
                clock_status='complete',
                post_turn_revision=terminal_revision,
            )
            db.session.commit()
            world_after = world_public_payload(campaign).get('world') or {}
            current_scene_after = world_after.get('current_scene')
            if current_scene_after != current_scene_before:
                stream_manager.broadcast_event(session_id, {
                    'type': 'scene_updated',
                    'current_scene': current_scene_after,
                })
            return
        session.running_summary = finalized_summary.strip()
        # The finalizer writes the summary directly (bypassing the memory-patch
        # write boundary), so re-apply the leak guard: unrevealed private terms
        # must never reach party-facing session state.
        redacted_summary, _redacted = redact_session_summary_private_terms(
            campaign,
            session.running_summary,
            player_content,
            ai_text,
        )
        session.running_summary = redacted_summary
        db.session.commit()
        log_audit_event(
            campaign_id,
            'summary_finalizer_applied',
            'Committed the finalized post-turn running summary.',
            {
                'session_id': session_id,
                'player_message_id': player_message_id,
                'dm_message_id': dm_message_id,
            },
            source='session_memory',
            actor='session_summary_finalizer',
            trace_id=summary_finalize_trace_id,
            parent_trace_id=parent_trace_id,
            trace_label=summary_finalize_label,
            audit_role='tools',
            commit=True,
        )

        # Read-only final consistency verification AFTER summary finalization.
        # It never mutates state; the finalized summary is semantically checked
        # against committed clock/scene state and any remaining contradiction
        # prevents complete.
        _verified, verify_incident = _verify_post_turn_state(
            campaign,
            session,
            player_message_id,
            dm_message_id,
            parent_trace_id,
            summary_text=session.running_summary,
            summary_context=summary_context,
        )
        if verify_incident:
            mark_session_dm_turn_error(
                campaign.id,
                session_id,
                player_message_id,
                parent_trace_id,
                verify_incident,
                dm_message_id=dm_message_id,
                memory_status='complete' if memory_complete else 'skipped',
                clock_status='complete' if clock_complete else 'error',
                post_turn_revision=terminal_revision,
            )
            db.session.commit()
        else:
            mark_session_dm_turn_post_turn_complete(
                player_message_id,
                dm_message_id=dm_message_id,
                memory_status='complete' if memory_complete else 'skipped',
                clock_status='complete' if clock_complete else 'error',
                post_turn_revision=terminal_revision,
            )
            db.session.commit()

        world_after = world_public_payload(campaign).get('world') or {}
        current_scene_after = world_after.get('current_scene')
        if current_scene_after != current_scene_before:
            stream_manager.broadcast_event(session_id, {
                'type': 'scene_updated',
                'current_scene': current_scene_after,
            })
    except MemoryPipelineError as err:
        db.session.rollback()
        telemetry = err.telemetry or {}
        telemetry.update({
            'pipeline_mode': 'staged_memory_writer',
            'pipeline_error_stage': err.stage,
            'pipeline_error_code': err.code,
            'error_type': type(err).__name__,
        })
        log_audit_event(
            campaign_id,
            'memory_update_error',
            f'Post-turn memory update failed at stage {err.stage}: {err.code}',
            {
                'session_id': session_id,
                'error': repr(err),
                'stage': err.stage,
                'code': err.code,
                'telemetry': telemetry,
            },
            source='session_memory',
            actor='session_memory_writer',
            trace_id=memory_trace_id,
            parent_trace_id=parent_trace_id,
            trace_label=trace_label,
            audit_role='tools',
            commit=True,
        )
        from services.memory_recovery import create_memory_recovery_task
        try:
            create_memory_recovery_task(
                campaign_id,
                session,
                player_message_id,
                dm_message_id,
                err,
                memory_patch,
                trace_id=memory_trace_id,
                context={
                    'current_user_id': current_user.id if current_user else None,
                    'current_scene_before': current_scene_before,
                },
            )
        except Exception:
            db.session.rollback()
        # Reconcile durable surfaces even when memory failed so any pre-existing
        # drift (e.g. a stale active clock) is repaired where deterministically
        # possible. The turn still reports error for the memory failure. The
        # revision is NOT bumped so a stored failed patch with the pre-failure
        # base_memory_revision stays retryable.
        terminal_revision, _incident_text = _repair_post_turn_clocks(
            campaign,
            session,
            player_message_id,
            dm_message_id,
            parent_trace_id,
            bump_revision=False,
        )
        mark_session_dm_turn_error(
            campaign_id,
            session_id,
            player_message_id,
            parent_trace_id,
            str(err),
            dm_message_id=dm_message_id,
            memory_status='error',
            clock_status='skipped',
            post_turn_revision=terminal_revision,
        )
        db.session.commit()
    except Exception as err:
        db.session.rollback()
        telemetry = memory_audit_context.get('telemetry') if 'memory_audit_context' in locals() else None
        if isinstance(telemetry, dict):
            summary = telemetry.setdefault("telemetry_summary", {})
            summary["status"] = "persistence_failure"
            summary["failure_category"] = "persistence"
        log_audit_event(
            campaign_id,
            'memory_update_error',
            'Post-turn memory update failed after visible DM response.',
            {'session_id': session_id, 'error': repr(err), 'telemetry': telemetry},
            source='session_memory',
            actor='session_memory_writer',
            trace_id=memory_trace_id,
            parent_trace_id=parent_trace_id,
            trace_label=trace_label,
            audit_role='tools',
            commit=True,
        )
        from services.memory_recovery import create_memory_recovery_task
        try:
            create_memory_recovery_task(
                campaign_id,
                session,
                player_message_id,
                dm_message_id,
                err,
                memory_patch,
                trace_id=memory_trace_id,
                context={
                    'current_user_id': current_user.id if current_user else None,
                    'current_scene_before': current_scene_before,
                },
            )
        except Exception:
            db.session.rollback()
        terminal_revision, _incident_text = _repair_post_turn_clocks(
            campaign,
            session,
            player_message_id,
            dm_message_id,
            parent_trace_id,
            bump_revision=memory_complete,
        )
        mark_session_dm_turn_error(
            campaign_id,
            session_id,
            player_message_id,
            parent_trace_id,
            repr(err),
            dm_message_id=dm_message_id,
            memory_status='complete' if memory_complete else 'error',
            clock_status='error' if memory_complete and not clock_complete else 'skipped',
            post_turn_revision=terminal_revision,
        )
        db.session.commit()


def _post_turn_status_for_player(campaign_id, session_id, player_message_id):
    memory_trace_id = f'session_memory_writer:session_{session_id}:message_{player_message_id}'
    clock_trace_id = f'session_clock_adjudicator:session_{session_id}:message_{player_message_id}'

    def _with_revision(status):
        from services.dm_turns import session_dm_turn_status_payload
        revision = session_dm_turn_status_payload(player_message_id).get('post_turn_revision')
        if revision is not None:
            status['post_turn_revision'] = revision
        return status

    from models import SessionDmTurn

    # The durable row is the state machine. Audit events only supply committed
    # write evidence to its canonical projection.
    turn = SessionDmTurn.query.filter_by(
        campaign_id=campaign_id,
        player_message_id=player_message_id,
    ).first()
    if turn is not None:
        status = session_dm_turn_status_payload(player_message_id)
        if status.get('post_turn_status') in {'partial', 'failed', 'timed_out'}:
            from models import SessionMemoryRecoveryTask
            pending_recovery = (
                SessionMemoryRecoveryTask.query
                .filter_by(campaign_id=campaign_id, player_message_id=player_message_id, status='pending')
                .order_by(SessionMemoryRecoveryTask.id.desc())
                .first()
            )
            status.update({
                'recoverable': pending_recovery is not None,
                'has_pending_recovery': pending_recovery is not None,
                'recovery_task': {'id': pending_recovery.id} if pending_recovery else None,
            })
        return status

    memory_error = CampaignAuditEvent.query.filter_by(
        campaign_id=campaign_id,
        trace_id=memory_trace_id,
        event_type='memory_update_error',
    ).order_by(CampaignAuditEvent.id.desc()).first()
    if memory_error is not None:
        from models import SessionMemoryRecoveryTask

        pending_recovery = (
            SessionMemoryRecoveryTask.query
            .filter_by(campaign_id=campaign_id, player_message_id=player_message_id, status='pending')
            .order_by(SessionMemoryRecoveryTask.id.desc())
            .first()
        )
        # Player-facing shape stays minimal: the automation worker only needs the
        # opaque task id to retry. Full recovery metadata (error_text, trace_id,
        # dm_message_id, patch info) is exposed only through the privileged
        # DM/owner /memory-recovery routes.
        return _with_revision({
            'post_turn_complete': True,
            'post_turn_status': 'error',
            'memory_status': 'error',
            'clock_status': 'skipped',
            'recoverable': True,
            'has_pending_recovery': pending_recovery is not None,
            'recovery_task': {'id': pending_recovery.id} if pending_recovery else None,
        })

    memory_applied = CampaignAuditEvent.query.filter(
        CampaignAuditEvent.campaign_id == campaign_id,
        CampaignAuditEvent.trace_id == memory_trace_id,
        CampaignAuditEvent.event_type.in_(['memory_patch_applied', 'memory_patch_applied_v2']),
    ).order_by(CampaignAuditEvent.id.desc()).first()
    if memory_applied is None:
        return _with_revision({
            'post_turn_complete': False,
            'post_turn_status': 'pending',
            'memory_status': 'pending',
            'clock_status': 'pending',
        })

    clock_applied = CampaignAuditEvent.query.filter_by(
        campaign_id=campaign_id,
        trace_id=clock_trace_id,
        event_type='clock_adjudication_applied',
    ).order_by(CampaignAuditEvent.id.desc()).first()
    if clock_applied is None:
        return _with_revision({
            'post_turn_complete': False,
            'post_turn_status': 'pending',
            'memory_status': 'complete',
            'clock_status': 'pending',
        })

    return _with_revision({
        'post_turn_complete': True,
        'post_turn_status': 'complete',
        'memory_status': 'complete',
        'clock_status': 'complete',
    })


def _dm_turn_status_for_player(campaign_id, session_id, player_message_id=None):
    """Return the most recent completed session-DM turn decision."""
    query = (
        CampaignAuditEvent.query
        .filter_by(
            campaign_id=campaign_id,
            source='session_messages',
            actor='session_dm',
        )
        .filter(CampaignAuditEvent.event_type.in_([
            'dm_output_stored',
            'dm_silence_chosen',
            'dm_output_empty',
        ]))
        .order_by(CampaignAuditEvent.id.desc())
    )
    for event in query.limit(64).all():
        try:
            payload = json.loads(event.payload) if event.payload else {}
        except (TypeError, ValueError):
            payload = {}
        event_player_message_id = payload.get('player_message_id')
        if player_message_id is not None and event_player_message_id != player_message_id:
            continue
        if event.event_type == 'dm_output_stored':
            status = {
                'status': 'speak',
                'player_message_id': event_player_message_id,
                'dm_message_id': payload.get('dm_message_id'),
            }
            if event_player_message_id is not None:
                status.update(_post_turn_status_for_player(campaign_id, session_id, event_player_message_id))
                status.update(session_dm_turn_status_payload(event_player_message_id))
            return status
        if event.event_type == 'dm_silence_chosen':
            decision = payload.get('decision') if isinstance(payload.get('decision'), dict) else {}
            status = {
                'status': 'silent',
                'player_message_id': event_player_message_id,
                'reason': decision.get('reason') or '',
                'post_turn_complete': True,
                'post_turn_status': 'complete',
                'memory_status': 'skipped',
                'clock_status': 'skipped',
            }
            if event_player_message_id is not None:
                status.update(session_dm_turn_status_payload(event_player_message_id))
            return status
        if event.event_type == 'dm_output_empty':
            status = {
                'status': 'empty',
                'player_message_id': event_player_message_id,
                'decision': payload.get('decision'),
                'post_turn_complete': True,
                'post_turn_status': 'complete',
                'memory_status': 'skipped',
                'clock_status': 'skipped',
            }
            if event_player_message_id is not None:
                status.update(session_dm_turn_status_payload(event_player_message_id))
            return status
    status = {
        'status': 'pending',
        'player_message_id': player_message_id,
        'post_turn_complete': False,
        'post_turn_status': 'pending',
        'memory_status': 'pending',
        'clock_status': 'pending',
    }
    if player_message_id is not None:
        status.update(session_dm_turn_status_payload(player_message_id))
    return status


@sessions_bp.route('/api/sessions/<int:session_id>/dm-turn-status', methods=['GET'])
@token_required
def get_dm_turn_status(current_user, session_id):
    session = get_or_404(CampaignSession, session_id)
    campaign = db.session.get(Campaign, session.campaign_id)
    if not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    after_message_id = request.args.get('after_message_id', type=int)
    status = _dm_turn_status_for_player(campaign.id, session_id, player_message_id=after_message_id)
    return jsonify(status)


def _can_manage_memory_recovery(campaign, current_user):
    """Recovery tasks replay DM-owned memory/clock writes.

    Only the campaign owner (or a DM/co-DM campaign member) may inspect or
    trigger them; ordinary players must not see DM-internal recovery metadata or
    trigger DM-owned replays.
    """
    if campaign.user_id == current_user.id:
        return True
    member = _member_record(campaign.id, current_user.id)
    if member and (member.role or '').lower() in {'dm', 'co_dm'}:
        return True
    return False


@sessions_bp.route('/api/campaigns/<int:campaign_id>/memory-recovery/pending', methods=['GET'])
@token_required
def get_pending_memory_recovery(current_user, campaign_id):
    campaign = get_or_404(Campaign, campaign_id)
    if not _can_manage_memory_recovery(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    from services.memory_recovery import pending_memory_recovery_tasks

    tasks = pending_memory_recovery_tasks(campaign_id)
    return jsonify({
        'campaign_id': campaign_id,
        'count': len(tasks),
        'tasks': [task.to_dict() for task in tasks],
    })


@sessions_bp.route('/api/campaigns/<int:campaign_id>/memory-recovery/<int:task_id>/retry', methods=['POST'])
@token_required
def retry_memory_recovery(current_user, campaign_id, task_id):
    campaign = get_or_404(Campaign, campaign_id)
    if not _can_manage_memory_recovery(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    from services.memory_recovery import retry_memory_recovery_task

    result = retry_memory_recovery_task(campaign_id, task_id)
    status_code = 200 if result.get('ok') else 400
    return jsonify(result), status_code


@sessions_bp.route('/api/campaigns/<int:campaign_id>/sessions', methods=['POST'])
@token_required
def start_session(current_user, campaign_id):
    campaign = get_or_404(Campaign, campaign_id)
    if not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    active = CampaignSession.query.filter_by(campaign_id=campaign_id, is_active=True).first()
    if active:
        return jsonify({'error': 'An active session already exists'}), 400

    ready, details = can_start_session(campaign)
    if not ready:
        db.session.commit()
        return jsonify({
            'error': 'Every party member must select and ready a character before starting a session',
            'planning': details,
        }), 400

    if is_combat_sandbox_campaign(campaign):
        try:
            started = start_combat_sandbox_session(campaign, current_user)
        except ValueError as err:
            db.session.rollback()
            return jsonify({'error': str(err)}), 400
        except RuntimeError as err:
            db.session.rollback()
            return jsonify({'error': str(err)}), 500

        session = started['session']
        data = session.to_dict()
        data['messages'] = [message.to_dict() for message in session.messages]
        stream_manager.broadcast_event(session.id, {"type": "refresh"})
        planning_stream_manager.broadcast_campaign_event(campaign_id, {"type": "session_started"})
        return jsonify({'session': data}), 201

    world, world_error = ensure_world_generated(campaign, current_user)
    if world_error:
        return jsonify({key: value for key, value in world_error.items() if key != 'status'}), world_error.get('status', 500)

    session = CampaignSession(campaign_id=campaign_id)
    db.session.add(session)
    approve_world(world)
    campaign.last_played_at = utcnow()
    db.session.flush()
    log_audit_event(
        campaign_id,
        'session_started',
        'Created active campaign session and approved the world package.',
        {
            'session': session.to_dict(),
            'world': world.to_public_dict(),
        },
        source='campaign_sessions',
        actor=current_user.username,
        commit=True,
    )

    opening_trace_id = f'session_dm:session_{session.id}:opening'
    opening_trace_label = f'session_dm: session {session.id} opening'
    context = planning_context(campaign, current_user)
    world_context = dm_world_context(
        campaign,
        audit=True,
        reason='opening_scene_context',
        audit_context={
            'trace_id': opening_trace_id,
            'trace_label': opening_trace_label,
        },
    )
    opening_text = get_opening_scene_response(
        context,
        world_context,
        audit_context={
            'campaign_id': campaign_id,
            'operation': 'opening_scene',
            'actor': 'session_dm',
            'trace_id': opening_trace_id,
            'trace_label': opening_trace_label,
        },
    )
    if opening_text:
        opening_msg = SessionMessage(
            session_id=session.id,
            role='dm',
            content=opening_text,
        )
        db.session.add(opening_msg)
        log_audit_event(
            campaign_id,
            'dm_output_stored',
            'Stored opening visible DM message.',
            {
                'session_id': session.id,
                'message': {
                    'role': 'dm',
                    'content': opening_text,
                },
            },
            source='session_messages',
            actor='session_dm',
            trace_id=opening_trace_id,
            trace_label=opening_trace_label,
            commit=False,
        )

    db.session.commit()
    stream_manager.broadcast_event(session.id, {"type": "refresh"})
    planning_stream_manager.broadcast_campaign_event(campaign_id, {"type": "session_started"})
    data = session.to_dict()
    data['messages'] = [m.to_dict() for m in session.messages]
    log_audit_event(
        campaign_id,
        'client_response_sent',
        'Sent started session payload to client.',
        {'session': data},
        source='campaign_sessions',
        actor='server',
        trace_id=opening_trace_id,
        trace_label=opening_trace_label,
        commit=True,
    )
    return jsonify({'session': data}), 201


@sessions_bp.route('/api/campaigns/<int:campaign_id>/sessions', methods=['GET'])
@token_required
def list_sessions(current_user, campaign_id):
    campaign = get_or_404(Campaign, campaign_id)
    if not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    sessions = CampaignSession.query.filter_by(campaign_id=campaign_id).order_by(CampaignSession.started_at.desc()).all()
    return jsonify({'sessions': [s.to_dict() for s in sessions]}), 200


def _visible_pending_sheet_proposals(campaign, session, current_user):
    """Pending proposals the requesting user is allowed to see.

    Mirrors the authorization boundary of the dedicated proposals endpoint: the
    DM sees every pending proposal, while other members only see proposals for
    characters they own. Proposals whose source message is no longer part of the
    session are excluded.
    """
    proposals = SheetProposal.query.filter_by(session_id=session.id, status='pending').all()
    session_message_ids = {
        message_id
        for (message_id,) in db.session.query(SessionMessage.id)
        .filter_by(session_id=session.id)
        .all()
    }
    proposals = [
        proposal for proposal in proposals
        if proposal.message_id is None or proposal.message_id in session_message_ids
    ]
    if campaign.user_id == current_user.id:
        return [proposal.to_dict() for proposal in proposals]
    user_char_ids = {c.id for c in Character.query.filter_by(user_id=current_user.id).all()}
    return [
        proposal.to_dict()
        for proposal in proposals
        if proposal.character_id in user_char_ids
    ]


@sessions_bp.route('/api/sessions/<int:session_id>', methods=['GET'])
@token_required
def get_session(current_user, session_id):
    session = get_or_404(CampaignSession, session_id)
    campaign = db.session.get(Campaign, session.campaign_id)
    if not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    data = session.to_dict()
    data.update(_message_page(
        session_id,
        before_id=request.args.get('before_id'),
        limit=request.args.get('limit'),
    ))
    data['pending_sheet_proposals'] = _visible_pending_sheet_proposals(campaign, session, current_user)
    return jsonify({'session': data}), 200


@sessions_bp.route('/api/sessions/<int:session_id>', methods=['PUT'])
@token_required
def end_session(current_user, session_id):
    session = get_or_404(CampaignSession, session_id)
    campaign = db.session.get(Campaign, session.campaign_id)
    if not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    data = request.get_json()
    session.is_active = False
    session.ended_at = utcnow()
    if data and 'recap' in data:
        session.recap = data['recap']

    db.session.commit()
    stream_manager.broadcast_event(session_id, {"type": "refresh"})
    return jsonify({'session': session.to_dict()}), 200


@sessions_bp.route('/api/sessions/<int:session_id>/messages', methods=['GET'])
@token_required
def get_messages(current_user, session_id):
    session = get_or_404(CampaignSession, session_id)
    campaign = db.session.get(Campaign, session.campaign_id)
    if not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    return jsonify(_message_page(
        session_id,
        before_id=request.args.get('before_id'),
        limit=request.args.get('limit'),
    )), 200


@sessions_bp.route('/api/sessions/<int:session_id>/messages', methods=['POST'])
@token_required
def send_message(current_user, session_id):
    session = get_or_404(CampaignSession, session_id)
    campaign = db.session.get(Campaign, session.campaign_id)
    if not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    member = _member_record(campaign.id, current_user.id)
    if member and (member.role or 'player') == 'spectator':
        return jsonify({'error': 'Spectators can read this campaign but cannot send messages'}), 403

    data = request.get_json()
    if not data or not data.get('content'):
        return jsonify({'error': 'Missing content'}), 400

    content = data['content']
    msg = SessionMessage(
        session_id=session_id,
        user_id=current_user.id,
        role=data.get('role', 'player'),
        content=content,
    )
    db.session.add(msg)
    db.session.commit()
    stream_manager.broadcast_event(session_id, {"type": "message", "message": msg.to_dict()})
    result_messages = [msg.to_dict()]
    log_audit_event(
        campaign.id,
        'player_input_stored',
        'Stored session player message.',
        {'session_id': session_id, 'message': msg.to_dict(), 'request_body': data},
        source='session_messages',
        actor=current_user.username,
        commit=True,
    )
    trace_id = session_dm_trace_id(session_id, msg.id)
    trace_label = f'session_dm: session {session_id}'
    db.session.add(begin_session_dm_turn(campaign.id, session_id, msg.id, trace_id))
    db.session.commit()

    # Start generation asynchronously
    if (
        current_app.config.get('TESTING')
        or current_app.testing
        or current_app.config.get('SQLALCHEMY_DATABASE_URI') == 'sqlite:///:memory:'
    ):
        recent_messages = SessionMessage.query.filter_by(session_id=session_id).order_by(
            SessionMessage.created_at.asc(),
        ).all()[-8:]

        hot_context = build_session_hot_context(campaign, session, current_user)
        dm_tools_filtered = get_dm_tool_definitions(campaign)
        manifest = context_manifest(hot_context, dm_tools_filtered)
        log_audit_event(
            campaign.id,
            'session_hot_context_read',
            'Read compact hot context for session DM response.',
            {'context': hot_context, 'context_manifest': manifest},
            source='session_context',
            actor='server',
            trace_id=trace_id,
            trace_label=trace_label,
            commit=True,
        )

        try:
            ai_result = get_session_dm_response_with_tools(
                hot_context,
                recent_messages,
                dm_tools_filtered,
                lambda name, args, tool_audit: execute_dm_tool(campaign, session, current_user, name, args, tool_audit),
                audit_context={
                    'campaign_id': campaign.id,
                    'operation': 'session_dm_response',
                    'actor': 'session_dm',
                    'trace_id': trace_id,
                    'trace_label': trace_label,
                    'context_manifest': manifest,
                    'full_world_graph_included': False,
                },
                build_retrieval_packet=lambda preflight: build_session_retrieval_packet(
                    campaign,
                    current_user,
                    hot_context,
                    preflight,
                    audit_context={
                        'trace_id': trace_id,
                        'trace_label': trace_label,
                        'campaign_id': campaign.id,
                    },
                ),
            )
        except Exception as err:
            db.session.add(mark_session_dm_turn_error(
                campaign.id,
                session_id,
                msg.id,
                trace_id,
                repr(err),
            ))
            db.session.commit()
            return jsonify({'error': repr(err), 'messages': result_messages}), 500

        ai_turn = _session_dm_turn_decision(ai_result)
        ai_text = ai_turn.get('content') or ''

        if ai_turn.get('mode') == 'speak' and ai_text:
            response_parts = ai_turn.get('parts') if isinstance(ai_turn, dict) else None
            resolver_packet = ai_turn.get('resolver_packet') if isinstance(ai_turn, dict) else None
            try:
                ai_msg, pending_proposals, _action_results = commit_accepted_dm_turn(
                    campaign,
                    session,
                    current_user,
                    msg.id,
                    trace_id,
                    trace_label,
                    ai_text,
                    ai_turn.get('commit_action_ids') if isinstance(ai_turn.get('commit_action_ids'), list) else [],
                    {'actions': ai_result.get('_pending_actions')}
                    if isinstance(ai_result, dict) and isinstance(ai_result.get('_pending_actions'), list)
                    else None,
                    response_parts=response_parts,
                    resolver_packet=resolver_packet,
                    disclose_item_ids=ai_turn.get('disclose_item_ids') if isinstance(ai_turn, dict) else None,
                )
            except Exception as err:
                return jsonify({'error': repr(err), 'messages': result_messages}), 500
            result_messages.append(ai_msg.to_dict())

            # Synchronous memory update
            _run_session_memory_update(
                campaign.id,
                session_id,
                current_user.id,
                msg.id,
                content,
                ai_text,
                hot_context,
                trace_id,
                dm_message_id=ai_msg.id,
                response_parts=response_parts,
                resolver_packet=resolver_packet,
            )
        elif ai_turn.get('mode') == 'silent':
            log_audit_event(
                campaign.id,
                'dm_silence_chosen',
                'Session DM intentionally sent no visible response.',
                {
                    'session_id': session_id,
                    'player_message_id': msg.id,
                    'decision': {
                        'mode': 'silent',
                        'reason': ai_turn.get('reason') or '',
                    },
                },
                source='session_messages',
                actor='session_dm',
                trace_id=trace_id,
                trace_label=trace_label,
                audit_role='agent',
                commit=False,
            )
            db.session.add(mark_session_dm_turn_visible(
                campaign.id,
                session_id,
                msg.id,
                trace_id,
                status='silent',
            ))
            db.session.commit()
        else:
            log_audit_event(
                campaign.id,
                'dm_output_empty',
                'Session DM returned no visible content; no DM message was stored.',
                {
                    'session_id': session_id,
                    'player_message_id': msg.id,
                    'decision': ai_turn,
                },
                source='session_messages',
                actor='session_dm',
                trace_id=trace_id,
                trace_label=trace_label,
                audit_role='agent',
                commit=False,
            )
            db.session.add(mark_session_dm_turn_visible(
                campaign.id,
                session_id,
                msg.id,
                trace_id,
                status='empty',
            ))
            db.session.commit()

        log_audit_event(
            campaign.id,
            'client_response_sent',
            'Sent session turn messages payload to client.',
            {'messages': result_messages},
            source='session_messages',
            actor='server',
            trace_id=trace_id,
            trace_label=trace_label,
            commit=True,
        )
    else:
        stream_manager.start_generation(campaign.id, session_id, current_user.id, content, msg.id)
    return jsonify({'messages': result_messages}), 201


@sessions_bp.route('/api/sessions/<int:session_id>/stream', methods=['GET'])
def stream_session(session_id):
    token_str = request.args.get('token')
    api_key = request.args.get('api_key')

    current_user, error_response = authenticate_request(token=token_str, api_key=api_key)
    if error_response is not None:
        return error_response

    session = get_or_404(CampaignSession, session_id)
    campaign = db.session.get(Campaign, session.campaign_id)
    if not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    q = stream_manager.add_listener(session_id)

    def event_stream():
        try:
            while True:
                try:
                    event = q.get(timeout=20) # Keep-alive ping / timeout
                    yield f"data: {json.dumps(event)}\n\n"
                except queue.Empty:
                    # Keep-alive comment
                    yield ": ping\n\n"
        finally:
            stream_manager.remove_listener(session_id, q)

    response = current_app.response_class(event_stream(), mimetype='text/event-stream')
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['X-Accel-Buffering'] = 'no'
    return response


@token_required
def _get_session_proposals(current_user, session_id):
    session = get_or_404(CampaignSession, session_id)
    campaign = db.session.get(Campaign, session.campaign_id)
    if not ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403

    result = _visible_pending_sheet_proposals(campaign, session, current_user)
    return jsonify({'sheet_proposals': result}), 200


@token_required
def _apply_sheet_proposal(current_user, session_id, proposal_id):
    session = get_or_404(CampaignSession, session_id)
    campaign = db.session.get(Campaign, session.campaign_id)
    proposal = get_or_404(SheetProposal, proposal_id)

    if proposal.status != 'pending':
        return jsonify({'error': 'Proposal is not pending.'}), 400

    character = db.session.get(Character, proposal.character_id)
    if not character:
        return jsonify({'error': 'Character not found.'}), 404

    is_dm = campaign.user_id == current_user.id
    is_owner = character.user_id == current_user.id
    is_npc = character.user_id is None

    if not is_owner and not (is_dm and is_npc):
        return jsonify({'error': 'Forbidden'}), 403

    from models import CharacterCondition, CharacterEquipment

    for change in proposal.changes:
        field = change['field']
        after = change['after']

        if ':' in field:
            prefix, item_name = field.split(':', 1)
            prefix = prefix.strip().lower()
            item_name = item_name.strip()

            if prefix == 'condition':
                existing = CharacterCondition.query.filter_by(
                    character_id=character.id, condition_name=item_name,
                ).first()
                if isinstance(after, dict) and after.get('count', 0) > 0:
                    if not existing:
                        db.session.add(CharacterCondition(
                            character=character, condition_name=item_name,
                        ))
                elif existing:
                    db.session.delete(existing)

            elif prefix == 'equipment':
                if isinstance(after, dict) and after.get('count', 0) > 0:
                    existing_equip = CharacterEquipment.query.filter_by(
                        character_id=character.id, name=item_name,
                    ).first()
                    if existing_equip:
                        existing_equip.quantity = (existing_equip.quantity or 0) + 1
                    else:
                        db.session.add(CharacterEquipment(
                            character=character, name=item_name, quantity=1,
                        ))
        else:
            config = SHEET_SCALAR_FIELDS.get(field)
            if not config:
                continue
            if config['type'] == 'bool':
                setattr(character, field, bool(after))
            else:
                setattr(character, field, int(after))

    character.updated_at = utcnow()
    proposal.status = 'applied'
    proposal.applied_at = utcnow()
    db.session.commit()

    stream_manager.broadcast_event(session_id, {
        "type": "proposal_applied",
        "proposal": proposal.to_dict(),
        "character": character_full_dict(character)
    })

    log_audit_event(
        campaign.id,
        'sheet_proposal_applied',
        f'Sheet proposal {proposal_id} applied.',
        {'session_id': session_id, 'proposal_id': proposal_id, 'changes': proposal.changes},
        source='session_messages',
        actor=current_user.username,
        commit=True,
    )

    return jsonify({'proposal': proposal.to_dict(), 'character': character_full_dict(character)}), 200


@token_required
def _dismiss_sheet_proposal(current_user, session_id, proposal_id):
    session = get_or_404(CampaignSession, session_id)
    campaign = db.session.get(Campaign, session.campaign_id)
    proposal = get_or_404(SheetProposal, proposal_id)

    if proposal.status != 'pending':
        return jsonify({'error': 'Proposal is not pending.'}), 400

    character = db.session.get(Character, proposal.character_id)
    is_dm = campaign.user_id == current_user.id
    is_owner = character and character.user_id == current_user.id

    if not is_owner and not is_dm:
        return jsonify({'error': 'Forbidden'}), 403

    proposal.status = 'dismissed'
    db.session.commit()

    stream_manager.broadcast_event(session_id, {
        "type": "proposal_dismissed",
        "proposal": proposal.to_dict()
    })

    log_audit_event(
        campaign.id,
        'sheet_proposal_dismissed',
        f'Sheet proposal {proposal_id} dismissed.',
        {'session_id': session_id, 'proposal_id': proposal_id},
        source='session_messages',
        actor=current_user.username,
        commit=True,
    )

    return jsonify({'proposal': proposal.to_dict()}), 200


sessions_bp.add_url_rule(
    '/api/sessions/<int:session_id>/proposals',
    view_func=_get_session_proposals,
    methods=['GET'],
)
sessions_bp.add_url_rule(
    '/api/sessions/<int:session_id>/proposals/<int:proposal_id>/apply',
    view_func=_apply_sheet_proposal,
    methods=['POST'],
)
sessions_bp.add_url_rule(
    '/api/sessions/<int:session_id>/proposals/<int:proposal_id>/dismiss',
    view_func=_dismiss_sheet_proposal,
    methods=['POST'],
)


@token_required
def _get_campaign_clarifications(current_user, campaign_id):
    from models import CampaignClarification
    campaign = db.session.get(Campaign, campaign_id)
    if not campaign:
        return jsonify({'error': 'Campaign not found.'}), 404

    is_dm = campaign.user_id == current_user.id
    if not is_dm:
        return jsonify({'error': 'Forbidden'}), 403

    clarifications = CampaignClarification.query.filter_by(campaign_id=campaign_id).all()
    return jsonify([c.to_dict() for c in clarifications]), 200


@token_required
def _answer_campaign_clarification(current_user, campaign_id, clarification_id):
    from models import CampaignClarification
    campaign = db.session.get(Campaign, campaign_id)
    if not campaign:
        return jsonify({'error': 'Campaign not found.'}), 404

    is_dm = campaign.user_id == current_user.id
    if not is_dm:
        return jsonify({'error': 'Forbidden'}), 403

    clar = CampaignClarification.query.filter_by(
        campaign_id=campaign_id, clarification_id=clarification_id
    ).first()
    if not clar:
        return jsonify({'error': 'Clarification not found.'}), 404

    if clar.status not in ("pending", "answered"):
        return jsonify({'error': f'Cannot answer clarification in status {clar.status}'}), 400

    data = request.get_json() or {}
    answer = data.get("answer", "")
    resolved_canonical_id = data.get("resolved_canonical_id")
    resolution_action = data.get("resolution_action")
    resolution_patch = data.get("resolution_patch")

    if not resolution_action or resolution_action not in ("same_identity", "new_entity", "ignore"):
        return jsonify({'error': 'Invalid resolution action.'}), 400

    if resolution_action == "same_identity":
        if not resolved_canonical_id:
            return jsonify({'error': 'resolved_canonical_id is required for same_identity.'}), 400
        known = _known_ids(campaign)
        if resolved_canonical_id not in known.get("entity_ids", set()):
            return jsonify({'error': f'Canonical ID {resolved_canonical_id} does not exist in this campaign.'}), 400

    if resolution_patch is not None:
        if not isinstance(resolution_patch, dict):
            return jsonify({'error': 'resolution_patch must be a JSON object.'}), 400
        allowed_keys = {"update_npc_actors", "upsert_graph_entities"}
        for k in resolution_patch.keys():
            if k not in allowed_keys:
                return jsonify({'error': f'Key {k} is not allowed in resolution_patch.'}), 400

        # Scope validation: check allowed keys against blocking_scope
        blocking_scope = clar.blocking_scope or []
        if blocking_scope:
            if "update_npc_actors" in resolution_patch:
                if not any(op in blocking_scope for op in ("npc_update", "identity_merge", "entity_merge")):
                    return jsonify({'error': 'update_npc_actors not allowed by blocking scope.'}), 400
            if "upsert_graph_entities" in resolution_patch:
                if not any(op in blocking_scope for op in ("entity_merge", "identity_merge")):
                    return jsonify({'error': 'upsert_graph_entities not allowed by blocking scope.'}), 400

        # Validate target IDs
        if resolution_action == "same_identity":
            target_id = resolved_canonical_id
        elif resolution_action == "new_entity":
            target_id = clar.mention_entity_id
        else:
            target_id = None

        if "update_npc_actors" in resolution_patch:
            items = resolution_patch["update_npc_actors"]
            if not isinstance(items, list):
                return jsonify({'error': 'update_npc_actors must be a list.'}), 400
            for item in items:
                if not isinstance(item, dict) or item.get("id") != target_id:
                    return jsonify({'error': f'Target ID in update_npc_actors must match {target_id}.'}), 400
        if "upsert_graph_entities" in resolution_patch:
            items = resolution_patch["upsert_graph_entities"]
            if not isinstance(items, list):
                return jsonify({'error': 'upsert_graph_entities must be a list.'}), 400
            for item in items:
                if not isinstance(item, dict) or item.get("id") != target_id:
                    return jsonify({'error': f'Target ID in upsert_graph_entities must match {target_id}.'}), 400

    clar.status = "answered"
    clar.answer = answer
    clar.resolved_canonical_id = resolved_canonical_id
    clar.resolution_action = resolution_action
    clar.resolution_patch_json = resolution_patch
    clar.answered_by = current_user.username
    clar.answered_at = utcnow()

    db.session.commit()
    return jsonify(clar.to_dict()), 200


@token_required
def _dismiss_campaign_clarification(current_user, campaign_id, clarification_id):
    from models import CampaignClarification
    campaign = db.session.get(Campaign, campaign_id)
    if not campaign:
        return jsonify({'error': 'Campaign not found.'}), 404

    is_dm = campaign.user_id == current_user.id
    if not is_dm:
        return jsonify({'error': 'Forbidden'}), 403

    clar = CampaignClarification.query.filter_by(
        campaign_id=campaign_id, clarification_id=clarification_id
    ).first()
    if not clar:
        return jsonify({'error': 'Clarification not found.'}), 404

    if clar.status not in ("pending", "answered"):
        return jsonify({'error': f'Cannot dismiss clarification in status {clar.status}'}), 400

    clar.status = "dismissed"
    clar.dismissed_at = utcnow()

    db.session.commit()
    return jsonify(clar.to_dict()), 200


@token_required
def _obsolete_campaign_clarification(current_user, campaign_id, clarification_id):
    from models import CampaignClarification
    campaign = db.session.get(Campaign, campaign_id)
    if not campaign:
        return jsonify({'error': 'Campaign not found.'}), 404

    is_dm = campaign.user_id == current_user.id
    if not is_dm:
        return jsonify({'error': 'Forbidden'}), 403

    clar = CampaignClarification.query.filter_by(
        campaign_id=campaign_id, clarification_id=clarification_id
    ).first()
    if not clar:
        return jsonify({'error': 'Clarification not found.'}), 404

    if clar.status not in ("pending", "answered"):
        return jsonify({'error': f'Cannot obsolete clarification in status {clar.status}'}), 400

    clar.status = "obsolete"

    db.session.commit()
    return jsonify(clar.to_dict()), 200


sessions_bp.add_url_rule(
    '/api/campaigns/<int:campaign_id>/clarifications',
    view_func=_get_campaign_clarifications,
    methods=['GET'],
)
sessions_bp.add_url_rule(
    '/api/campaigns/<int:campaign_id>/clarifications/<clarification_id>/answer',
    view_func=_answer_campaign_clarification,
    methods=['POST'],
)
sessions_bp.add_url_rule(
    '/api/campaigns/<int:campaign_id>/clarifications/<clarification_id>/dismiss',
    view_func=_dismiss_campaign_clarification,
    methods=['POST'],
)
sessions_bp.add_url_rule(
    '/api/campaigns/<int:campaign_id>/clarifications/<clarification_id>/obsolete',
    view_func=_obsolete_campaign_clarification,
    methods=['POST'],
)

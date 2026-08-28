import json
import os
import queue
import threading
import time
from uuid import uuid4
from flask import current_app
from models import db, Campaign, CampaignSession, SessionMessage, User
from openrouter import (
    normalize_session_dm_turn_decision,
    get_llm_provider,
    _post_chat,
)
from services.dm_tools import (
    build_session_hot_context,
    get_dm_tool_definitions,
    context_manifest,
    execute_dm_tool,
)
from services.dm_turn_commit import commit_accepted_dm_turn
from services.dm_turns import (
    begin_session_dm_turn,
    mark_session_dm_turn_error,
    mark_session_dm_turn_visible,
    session_dm_trace_id,
)
from services.audit_service import log_audit_event

try:
    import redis
except Exception:  # pragma: no cover - redis package may be omitted in some environments
    redis = None

class SessionGeneratorWorker:
    def __init__(self, campaign_id, session_id, user_id, content, player_message_id=None, manager=None):
        self.campaign_id = campaign_id
        self.session_id = session_id
        self.user_id = user_id
        self.content = content
        self.player_message_id = player_message_id
        self.manager = manager
        self.listeners = []
        self.listener_lock = threading.Lock()
        self.status = "Initializing DM response..."
        self.is_done = False
        self.finished_at = None
        self.error = None
        self.messages_result = []
        self.sheet_proposals_result = []
        self.dynamic_summary_lock = threading.Lock()
        self.last_dynamic_summary_at = 0.0
        self.dm_stream_id = None
        self.dm_stream_text = ''

    def add_listener(self):
        q = queue.Queue()
        with self.listener_lock:
            self.listeners.append(q)
            # Catch up new listener with current status
            q.put({"type": "status", "status": self.status})
            if self.dm_stream_id and self.dm_stream_text and not self.is_done:
                q.put({
                    "type": "dm_stream_snapshot",
                    "stream_id": self.dm_stream_id,
                    "content": self.dm_stream_text,
                })
            if self.is_done:
                if self.error:
                    q.put({"type": "error", "error": self.error})
                else:
                    q.put({
                        "type": "done",
                        "messages": self.messages_result,
                        "sheet_proposals": self.sheet_proposals_result
                    })
        return q

    def remove_listener(self, q):
        with self.listener_lock:
            if q in self.listeners:
                self.listeners.remove(q)

    def broadcast(self, payload):
        with self.listener_lock:
            for q in self.listeners:
                q.put(payload)
        stream_manager.broadcast_event(self.session_id, payload)

    def finish_success(self, messages, sheet_proposals):
        self.messages_result = messages
        self.sheet_proposals_result = sheet_proposals
        self.is_done = True
        self.finished_at = time.monotonic()
        self.broadcast({
            "type": "done",
            "messages": self.messages_result,
            "sheet_proposals": self.sheet_proposals_result
        })

    def finish_error(self, error):
        self.error = error
        self.is_done = True
        self.finished_at = time.monotonic()
        self.broadcast({"type": "error", "error": self.error})

    def update_status(self, raw_status_or_data):
        # We can dynamically summarize using a cheap LLM call in a separate thread.
        # To avoid stalling the main generation, we dispatch trace generation asynchronously.
        # But we also set a fallback trace synchronously to make sure there's immediate feedback.
        action_desc = ""
        if isinstance(raw_status_or_data, dict):
            step = raw_status_or_data.get("step")
            if step == "preflight":
                action_desc = "Evaluating the current player message and history context to decide if the DM should speak or stay silent."
                self.status = "Checking safety..."
            elif step == "thinking":
                reasoning = raw_status_or_data.get("reasoning") or ""
                action_desc = f"Thinking about how to respond. Current thoughts: {reasoning[:200]}"
                self.status = "Planning response..."
            elif step == "tool_call":
                tool_name = raw_status_or_data.get("tool_name")
                args = raw_status_or_data.get("arguments") or {}
                action_desc = f"Executing D&D campaign tool: {tool_name} with parameters: {json.dumps(args)}"
                self.status = f"Using tool ({tool_name})..."
            elif step == "data_gather":
                prelude = str(raw_status_or_data.get("prelude") or "").strip()
                action_desc = prelude or "Gathering the evidence needed to resolve the turn."
                self.status = prelude or "Checking the campaign record..."
            elif step == "guard_check":
                action_desc = "Verifying the candidate response against D&D player control limits, rules formatting, and spoiler protection guidelines."
                self.status = "Reviewing rules..."
            elif step == "revising":
                violations = raw_status_or_data.get("violations") or {}
                action_desc = f"Revising response because checks detected issue: {json.dumps(violations)}"
                self.status = "Revising response..."
            else:
                action_desc = "Working on turn response."
        else:
            action_desc = str(raw_status_or_data)
            self.status = action_desc

        self.broadcast({"type": "status", "status": self.status})

        # Keep status text responsive without letting rapid status changes pile up
        # overlapping low-value summary calls.
        now = time.monotonic()
        if now - self.last_dynamic_summary_at < 3:
            return
        if not self.dynamic_summary_lock.acquire(blocking=False):
            return
        self.last_dynamic_summary_at = now

        # Run dynamic summarization asynchronously
        threading.Thread(
            target=self._run_dynamic_summarization,
            args=(action_desc, True),
            daemon=True
        ).start()

    def _run_dynamic_summarization(self, action_desc, release_lock=False):
        try:
            # Setup context and system prompt
            provider = get_llm_provider()

            # Check if the provider/API is configured. If not, don't run.
            from openrouter import _api_key_for_provider
            if not _api_key_for_provider(provider):
                return

            system_prompt = (
                "You are a D&D DM thinking trace generator. Generate a 2-3 word summary of what the DM is currently doing in-game based on the provided rich operational data.\n"
                "The trace must be highly specific to the context provided (e.g. mention character names, items, locations, or actions if present in the data), organic, mysterious, and D&D thematic (e.g., 'Amending Gildor's gold...', 'Reading Gildor's spells...', 'Consulting local shop...', 'Foretelling dragon's move...').\n"
                "Return ONLY the 2-3 word summary, capitalizing only the first word. Do not include quotes, preamble, or formatting."
            )

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Rich context data: {action_desc}"}
            ]

            try:
                # Fast, cheap call to the LLM (using 1 attempt, short timeout)
                res = _post_chat(
                    messages,
                    allow_thinking=False,
                    timeout_seconds=8,
                    max_attempts=1,
                    audit_context={
                        "campaign_id": self.campaign_id,
                        "operation": "dynamic_thinking_trace",
                        "actor": "thinking_trace_agent"
                    }
                )
                summary = res.strip().strip('"').strip("'").rstrip(".")
                if summary and len(summary.split()) <= 4:
                    self.status = summary + "..."
                    self.broadcast({"type": "status", "status": self.status})
            except Exception:
                # Fallback is already broadcasted, so fail silently
                pass
        finally:
            if release_lock:
                self.dynamic_summary_lock.release()

    def run(self, app):
        with app.app_context():
            try:
                self._execute_dm_turn()
            except Exception as e:
                db.session.rollback()
                if self.player_message_id:
                    campaign = db.session.get(Campaign, self.campaign_id)
                    if campaign is not None:
                        db.session.add(mark_session_dm_turn_error(
                            self.campaign_id,
                            self.session_id,
                            self.player_message_id,
                            session_dm_trace_id(self.session_id, self.player_message_id),
                            repr(e),
                        ))
                        db.session.commit()
                self.finish_error(str(e))
            finally:
                if not self.is_done:
                    self.is_done = True
                    self.finished_at = time.monotonic()
                db.session.remove()
                if self.manager is not None:
                    self.manager.on_worker_finished(self)

    def _execute_dm_turn(self):
        campaign = db.session.get(Campaign, self.campaign_id)
        session = db.session.get(CampaignSession, self.session_id)
        current_user = db.session.get(User, self.user_id)

        if not campaign or not session or not current_user:
            raise RuntimeError("Missing campaign, session, or user in background thread context")

        # Player message was already saved by the HTTP route handler.
        # Now we proceed with generating the DM response.
        if self.player_message_id:
            player_msg = db.session.get(SessionMessage, self.player_message_id)
            if player_msg and player_msg.session_id != self.session_id:
                player_msg = None
        else:
            player_msg = SessionMessage.query.filter_by(
                session_id=self.session_id, role='player'
            ).order_by(SessionMessage.id.desc()).first()
        player_msg_id = player_msg.id if player_msg else None

        trace_id = session_dm_trace_id(self.session_id, player_msg_id)
        trace_label = f'session_dm: session {self.session_id}'
        if player_msg_id:
            db.session.add(begin_session_dm_turn(campaign.id, self.session_id, player_msg_id, trace_id))
            db.session.commit()

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

        recent_messages = SessionMessage.query.filter_by(session_id=self.session_id).order_by(
            SessionMessage.created_at.asc(),
        ).all()[-8:]

        ai_result = self._try_data_first_turn(
            campaign,
            session,
            current_user,
            recent_messages,
            hot_context,
            trace_id,
            trace_label,
        )

        ai_turn = _session_dm_turn_decision(ai_result)
        ai_text = ai_turn.get('content') or ''

        sheet_proposals = []
        result_messages = [player_msg.to_dict()] if player_msg else []

        if ai_turn.get('mode') in {'speak', 'table_chat'} and ai_text:
            response_parts = ai_turn.get('parts') if isinstance(ai_turn, dict) else None
            resolver_packet = ai_turn.get('resolver_packet') if isinstance(ai_turn, dict) else None
            ai_msg, pending_proposals, _action_results = commit_accepted_dm_turn(
                campaign,
                session,
                current_user,
                player_msg_id,
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
                roll_request=ai_turn.get('roll_request') if isinstance(ai_turn, dict) else None,
                visible_status=ai_turn.get('mode'),
            )
            result_messages.append(ai_msg.to_dict())
            sheet_proposals = [p.to_dict() for p in pending_proposals]

            if ai_turn.get('roll_request'):
                from services.session_rolls import pending_roll_requests
                self.broadcast({
                    'type': 'roll_request_state',
                    'pending_roll_requests': pending_roll_requests(session.id),
                })

            self.finish_success(result_messages, sheet_proposals)
            if ai_turn.get('mode') == 'table_chat':
                return
            from routes.sessions import _run_session_memory_update
            _run_session_memory_update(
                campaign.id,
                self.session_id,
                current_user.id,
                player_msg_id,
                self.content,
                ai_text,
                hot_context,
                trace_id,
                dm_message_id=ai_msg.id,
                response_parts=response_parts,
                resolver_packet=resolver_packet,
            )
            return
        elif ai_turn.get('mode') == 'silent':
            log_audit_event(
                campaign.id,
                'dm_silence_chosen',
                'Session DM intentionally sent no visible response.',
                {
                    'session_id': self.session_id,
                    'player_message_id': player_msg_id,
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
                self.session_id,
                player_msg_id,
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
                    'session_id': self.session_id,
                    'player_message_id': player_msg_id,
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
                self.session_id,
                player_msg_id,
                trace_id,
                status='empty',
            ))
            db.session.commit()

        self.finish_success(result_messages, sheet_proposals)

    def _try_data_first_turn(self, campaign, session, current_user, recent_messages, hot_context, trace_id, trace_label):
        """Return a data-first decision or raise; legacy turn generation is not a fallback."""
        from services.data_first_turn import (
            DataFirstTurnError,
            authorized_player_fact_texts,
            canonicalize_turn_character_refs,
            data_first_enabled,
            expansion_basis_text,
            generate_turn_attempt,
            guard_turn_actions,
            memory_private_context,
            public_expansion_packet,
            resolve_and_retry_turn_attempt,
            stage_turn_actions,
            stream_turn_expansion,
            validate_expansion_text,
            validate_turn_claim_provenance,
            validate_turn_entity_refs,
        )

        from openrouter import (
            _approved_disclosure_texts,
            _pc_control_violation,
            _private_output_violation,
            _private_text_contains_token_sequence,
            _session_dm_disclosure_validation,
            _session_dm_format_violation,
        )

        started_at = time.monotonic()
        stream_started = False
        audit_base = {
            'campaign_id': campaign.id,
            'trace_id': trace_id,
            'parent_trace_id': trace_id,
            'trace_label': trace_label,
            'full_world_graph_included': False,
        }
        try:
            if not data_first_enabled():
                raise DataFirstTurnError(
                    'data_first_disabled',
                    'Data-first DM generation is disabled and legacy fallback has been removed.',
                )
            combat = hot_context.get('combat_coordinates') if isinstance(hot_context, dict) else None
            if isinstance(combat, dict) and combat.get('active'):
                raise DataFirstTurnError(
                    'unsupported_combat',
                    'Combat turns are not supported by the data-first DM path.',
                )

            self.update_status({'step': 'thinking', 'reasoning': 'Building a structured turn attempt'})
            attempt = generate_turn_attempt(hot_context, recent_messages, audit_context=audit_base)
            plan_ms = round((time.monotonic() - started_at) * 1000)
            log_audit_event(
                campaign.id,
                'data_first_turn_attempt_generated',
                'Generated an MVP structured turn attempt.',
                {'attempt': attempt, 'plan_ms': plan_ms},
                source='session_dm.data_first',
                actor='session_dm_turn_planner',
                trace_id=trace_id,
                trace_label=trace_label,
                audit_role='agent',
                commit=True,
            )
            resolution_ms = 0
            replan_ms = 0
            initial_attempt = attempt
            evidence_bundle = None
            if attempt['mode'] == 'resolve':
                safe_prelude = attempt.get('safe_prelude') or 'Checking the campaign record...'
                prelude_private_violation = _private_output_violation(safe_prelude, hot_context)
                if prelude_private_violation:
                    raise DataFirstTurnError(
                        'unsafe_resolver_prelude',
                        'The resolver prelude failed deterministic privacy validation.',
                        details={'private': prelude_private_violation},
                    )
                self.update_status({'step': 'data_gather', 'prelude': safe_prelude})
                resolution_started = time.monotonic()

                def on_evidence_request(request, arguments):
                    self.update_status({
                        'step': 'tool_call',
                        'tool_name': request.get('tool'),
                        'arguments': arguments,
                    })

                def execute_evidence_tool(name, args, tool_audit):
                    result = execute_dm_tool(campaign, session, current_user, name, args, tool_audit)
                    return result

                evidence_ready_at = [None]

                def on_evidence_ready(_bundle):
                    evidence_ready_at[0] = time.monotonic()

                attempt, evidence_bundle = resolve_and_retry_turn_attempt(
                    hot_context,
                    recent_messages,
                    attempt,
                    execute_evidence_tool,
                    audit_context=audit_base,
                    on_request=on_evidence_request,
                    on_evidence_ready=on_evidence_ready,
                )
                resolved_at = time.monotonic()
                evidence_finished_at = evidence_ready_at[0] or resolved_at
                resolution_ms = round((evidence_finished_at - resolution_started) * 1000)
                replan_ms = round((resolved_at - evidence_finished_at) * 1000)
                plan_ms = round((time.monotonic() - started_at) * 1000)
                log_audit_event(
                    campaign.id,
                    'data_first_turn_resolved',
                    'Gathered read-only evidence and generated a bounded retry attempt.',
                    {
                        'initial_attempt': initial_attempt,
                        'evidence_bundle': evidence_bundle,
                        'attempt': attempt,
                        'resolution_ms': resolution_ms,
                        'replan_ms': replan_ms,
                        'total_plan_ms': plan_ms,
                    },
                    source='session_dm.data_first',
                    actor='session_dm_evidence_resolver',
                    trace_id=trace_id,
                    trace_label=trace_label,
                    audit_role='tools',
                    commit=True,
                )
            if attempt['mode'] == 'fallback':
                raise DataFirstTurnError(
                    'unsupported_turn',
                    'The structured planner could not handle this turn.',
                    details={
                        'reason': attempt.get('reason'),
                        'plan_ms': plan_ms,
                        'resolved_once': evidence_bundle is not None,
                    },
                )
            if attempt['mode'] == 'silent':
                return {'mode': 'silent', 'reason': attempt.get('reason') or 'The DM intentionally stayed silent.'}

            attempt = canonicalize_turn_character_refs(attempt, hot_context)
            attempt = validate_turn_entity_refs(attempt, hot_context)
            attempt = validate_turn_claim_provenance(
                attempt,
                hot_context,
                recent_messages,
                evidence_bundle=evidence_bundle,
            )

            authorized_player_facts = authorized_player_fact_texts(
                attempt,
                hot_context,
                recent_messages,
                _pc_control_violation,
            )

            attempt, action_guard_notes = guard_turn_actions(attempt, hot_context)
            if action_guard_notes:
                log_audit_event(
                    campaign.id,
                    'data_first_action_guard_applied',
                    'Normalized redundant or epistemically unsupported staged actions.',
                    {'notes': action_guard_notes, 'guarded_actions': attempt.get('actions') or []},
                    source='session_dm.data_first',
                    actor='session_dm_action_guard',
                    trace_id=trace_id,
                    trace_label=trace_label,
                    audit_role='guardrail',
                    commit=True,
                )

            action_buffer, commit_action_ids = stage_turn_actions(
                attempt,
                lambda name, args, tool_audit: execute_dm_tool(
                    campaign, session, current_user, name, args, tool_audit,
                ),
                audit_context=audit_base,
            )

            packet = public_expansion_packet(
                attempt,
                authorized_player_facts=authorized_player_facts,
                action_buffer=action_buffer,
            )
            basis = expansion_basis_text(packet)
            from services.dm_tools import _reveal_fact_facet_ids
            proposed_disclosures = []
            for action in attempt.get('actions') or []:
                args = action.get('arguments') or {}
                if action.get('tool') == 'reveal_fact' and args.get('visibility') in {'public', 'party_known'}:
                    proposed_disclosures.extend(_reveal_fact_facet_ids(args.get('item_type'), args.get('item_id')))
            approved_disclosures, disclosure_violations = _session_dm_disclosure_validation(
                {'content': basis, 'disclose_item_ids': proposed_disclosures},
                hot_context,
            )
            if disclosure_violations:
                raise DataFirstTurnError(
                    'invalid_structured_disclosure',
                    'A structured reveal action did not map to a known disclosure facet.',
                    details={'violations': disclosure_violations},
                )
            allowed_disclosure_terms = _approved_disclosure_texts(approved_disclosures, hot_context)
            visible_transcript = "\n".join(
                str(item.get('content') or '')
                for item in (hot_context.get('recent_messages') or [])
                if isinstance(item, dict)
            )
            previously_visible_terms = [
                str(term).strip()
                for term in (hot_context.get('protected_identifier_terms') or [])
                if str(term or '').strip()
                and _private_text_contains_token_sequence(visible_transcript, str(term))
            ]
            allowed_disclosure_terms = list(dict.fromkeys(
                [*allowed_disclosure_terms, *previously_visible_terms]
            ))
            private_violation = _private_output_violation(
                basis, hot_context, allowed_terms=allowed_disclosure_terms,
            )
            agency_violation = _pc_control_violation(
                basis, hot_context, allowed_player_facts=authorized_player_facts,
            )
            if private_violation or agency_violation:
                raise DataFirstTurnError(
                    'public_packet_rejected',
                    'The public expansion packet failed deterministic safety validation.',
                    details={
                        'private': private_violation,
                        'agency': agency_violation,
                    },
                )

            self.dm_stream_id = f'dm-stream-{self.player_message_id or uuid4().hex[:10]}'
            self.dm_stream_text = ''
            self.broadcast({'type': 'dm_stream_start', 'stream_id': self.dm_stream_id})
            stream_started = True
            expansion_started_at = time.monotonic()
            first_token_ms = None

            def on_token(delta):
                nonlocal first_token_ms
                if not delta:
                    return
                if first_token_ms is None:
                    first_token_ms = round((time.monotonic() - started_at) * 1000)
                self.dm_stream_text += delta
                self.broadcast({
                    'type': 'dm_stream_token',
                    'stream_id': self.dm_stream_id,
                    'token': delta,
                })

            expanded = stream_turn_expansion(packet, audit_context=audit_base, on_token=on_token)
            expanded = validate_expansion_text(expanded, packet)
            final_private_violation = _private_output_violation(
                expanded, hot_context, allowed_terms=allowed_disclosure_terms,
            )
            final_agency_violation = _pc_control_violation(
                expanded, hot_context, allowed_player_facts=authorized_player_facts,
            )
            format_violation = _session_dm_format_violation(expanded)
            if final_private_violation or final_agency_violation or format_violation:
                raise DataFirstTurnError(
                    'streamed_expansion_rejected',
                    'The streamed expansion failed deterministic surface validation.',
                    details={
                        'private': final_private_violation,
                        'agency': final_agency_violation,
                        'format': format_violation,
                    },
                )

            expansion_ms = round((time.monotonic() - expansion_started_at) * 1000)
            total_ms = round((time.monotonic() - started_at) * 1000)
            log_audit_event(
                campaign.id,
                'data_first_expansion_completed',
                'Streamed prose from an approved public turn packet.',
                {
                    'attempt': attempt,
                    'public_packet': packet,
                    'expansion': expanded,
                    'plan_ms': plan_ms,
                    'resolution_ms': resolution_ms,
                    'replan_ms': replan_ms,
                    'first_token_ms': first_token_ms,
                    'expansion_ms': expansion_ms,
                    'total_ms': total_ms,
                },
                source='session_dm.data_first',
                actor='session_dm_prose_expander',
                trace_id=trace_id,
                trace_label=trace_label,
                audit_role='agent',
                commit=True,
            )
            response_part = {'type': 'narration', 'content': expanded}
            private_context = memory_private_context(attempt)
            if private_context:
                response_part['dm_private_context'] = private_context
            return {
                'mode': attempt['mode'],
                'content': expanded,
                'parts': [response_part],
                'commit_action_ids': commit_action_ids,
                'disclose_item_ids': approved_disclosures,
                '_pending_actions': action_buffer['actions'],
                '_data_first_turn_attempt': attempt,
                **({'roll_request': attempt['roll_request']} if attempt.get('roll_request') else {}),
            }
        except Exception as err:
            if stream_started:
                self.broadcast({
                    'type': 'dm_stream_reset',
                    'stream_id': self.dm_stream_id,
                    'reason': 'The streamed draft failed validation and was discarded.',
                })
                self.dm_stream_id = None
                self.dm_stream_text = ''
            log_audit_event(
                campaign.id,
                'data_first_turn_failed',
                'Data-first DM generation failed closed; no legacy generation was attempted.',
                {
                    'error': repr(err),
                    'code': getattr(err, 'code', None),
                    'details': getattr(err, 'details', None),
                    'elapsed_ms': round((time.monotonic() - started_at) * 1000),
                },
                source='session_dm.data_first',
                actor='session_dm_turn_planner',
                trace_id=trace_id,
                trace_label=trace_label,
                audit_role='agent',
                commit=True,
            )
            raise

def _session_dm_turn_decision(raw_result):
    normalize = normalize_session_dm_turn_decision
    decision = normalize(raw_result)
    if decision.get('mode') == 'silent':
        return {
            'mode': 'silent',
            'content': '',
            'reason': decision.get('reason') or 'The DM intentionally stayed silent.',
        }
    result = {
        'mode': 'table_chat' if decision.get('mode') == 'table_chat' else 'speak',
        'content': decision.get('content') or '',
        'parts': decision.get('parts') if isinstance(decision.get('parts'), list) else [],
        'commit_action_ids': decision.get('commit_action_ids'),
    }
    if isinstance(decision.get('disclose_item_ids'), list):
        result['disclose_item_ids'] = decision['disclose_item_ids']
    if isinstance(decision.get('resolver_packet'), dict):
        result['resolver_packet'] = decision['resolver_packet']
    if isinstance(decision.get('roll_request'), dict):
        result['roll_request'] = decision['roll_request']
    return result

class SessionStreamManager:
    DONE_WORKER_TTL_SECONDS = 60
    REDIS_RETRY_SECONDS = 10

    def __init__(self):
        self.workers = {}
        self.pending_requests = {}
        self.listeners = {}  # session_id -> list of queue.Queue
        self.lock = threading.Lock()
        self.instance_id = str(uuid4())
        self.redis_url = os.environ.get('REDIS_URL', '').strip()
        self.redis_channel = os.environ.get('SESSION_STREAM_REDIS_CHANNEL', 'dnd:session_stream')
        self.redis_client = None
        self.redis_pubsub = None
        self.redis_thread = None
        self.redis_disabled_until = 0.0

    def _redis_enabled(self):
        return bool(self.redis_url and redis is not None)

    def _close_redis_locked(self):
        if self.redis_thread:
            try:
                self.redis_thread.stop()
            except Exception:
                pass
        if self.redis_pubsub:
            try:
                self.redis_pubsub.close()
            except Exception:
                pass
        if self.redis_client:
            try:
                self.redis_client.close()
            except Exception:
                pass
        self.redis_thread = None
        self.redis_pubsub = None
        self.redis_client = None

    def _on_redis_message(self, message):
        try:
            data = message.get('data') if isinstance(message, dict) else None
            if not data:
                return
            if isinstance(data, bytes):
                data = data.decode('utf-8', errors='replace')
            envelope = json.loads(data)
            if envelope.get('source') == self.instance_id:
                return
            session_id = envelope.get('session_id')
            payload = envelope.get('payload')
            if session_id is None or payload is None:
                return
        except Exception:
            return

        with self.lock:
            listeners = list(self.listeners.get(session_id, []))
        for q in listeners:
            q.put(payload)

    def _ensure_redis_subscription_locked(self):
        if not self._redis_enabled():
            return False
        if self.redis_thread and self.redis_thread.is_alive():
            return True
        if time.monotonic() < self.redis_disabled_until:
            return False

        try:
            self.redis_client = redis.Redis.from_url(
                self.redis_url,
                decode_responses=True,
                socket_connect_timeout=1,
                socket_timeout=1,
                health_check_interval=30,
            )
            self.redis_pubsub = self.redis_client.pubsub(ignore_subscribe_messages=True)
            self.redis_pubsub.subscribe(**{self.redis_channel: self._on_redis_message})
            self.redis_thread = self.redis_pubsub.run_in_thread(sleep_time=0.05, daemon=True)
            return True
        except Exception:
            self._close_redis_locked()
            self.redis_disabled_until = time.monotonic() + self.REDIS_RETRY_SECONDS
            return False

    def _ensure_redis_subscription(self):
        with self.lock:
            return self._ensure_redis_subscription_locked()

    def _spawn_worker_locked(self, campaign_id, session_id, user_id, content, player_message_id=None):
        worker = SessionGeneratorWorker(
            campaign_id,
            session_id,
            user_id,
            content,
            player_message_id,
            manager=self,
        )
        self.workers[session_id] = worker
        app = current_app._get_current_object()
        t = threading.Thread(target=worker.run, args=(app,), daemon=True)
        t.start()
        return worker

    def start_generation(self, campaign_id, session_id, user_id, content, player_message_id=None):
        with self.lock:
            self._ensure_redis_subscription_locked()
            # If there's already a worker running for this session, don't spawn a new one.
            if session_id in self.workers and not self.workers[session_id].is_done:
                if player_message_id is not None:
                    pending = self.pending_requests.get(session_id)
                    pending_message_id = pending.get('player_message_id') if pending else None
                    if pending_message_id is None or player_message_id >= pending_message_id:
                        self.pending_requests[session_id] = {
                            'campaign_id': campaign_id,
                            'session_id': session_id,
                            'user_id': user_id,
                            'content': content,
                            'player_message_id': player_message_id,
                        }
                return self.workers[session_id]
            return self._spawn_worker_locked(campaign_id, session_id, user_id, content, player_message_id)

    def get_worker(self, session_id):
        with self.lock:
            worker = self.workers.get(session_id)
            if worker and worker.is_done:
                finished_at = worker.finished_at or time.monotonic()
                if time.monotonic() - finished_at > self.DONE_WORKER_TTL_SECONDS:
                    # Clean up done workers after clients have had a chance to catch up.
                    del self.workers[session_id]
                    return None
            return worker

    def add_listener(self, session_id):
        q = queue.Queue()
        with self.lock:
            self._ensure_redis_subscription_locked()
            if session_id not in self.listeners:
                self.listeners[session_id] = []
            self.listeners[session_id].append(q)

            # Catch up new listener with current status of active worker if it exists
            worker = self.workers.get(session_id)
            if worker and not worker.is_done:
                q.put({"type": "status", "status": worker.status})
        return q

    def remove_listener(self, session_id, q):
        with self.lock:
            if session_id in self.listeners:
                if q in self.listeners[session_id]:
                    self.listeners[session_id].remove(q)
                if not self.listeners[session_id]:
                    del self.listeners[session_id]

    def broadcast_event(self, session_id, payload):
        self._ensure_redis_subscription()
        with self.lock:
            listeners = list(self.listeners.get(session_id, []))
        for q in listeners:
            q.put(payload)
        if not self._redis_enabled():
            return
        try:
            envelope = json.dumps({
                'source': self.instance_id,
                'session_id': session_id,
                'payload': payload,
            })
            self.redis_client.publish(self.redis_channel, envelope)
        except Exception:
            with self.lock:
                self._close_redis_locked()
                self.redis_disabled_until = time.monotonic() + self.REDIS_RETRY_SECONDS

    def on_worker_finished(self, worker):
        with self.lock:
            current_worker = self.workers.get(worker.session_id)
            if current_worker is not worker:
                return None

            pending = self.pending_requests.pop(worker.session_id, None)
            if not pending:
                return None

            pending_player_message_id = pending.get('player_message_id')
            handled_player_message_id = worker.player_message_id
            if (
                pending_player_message_id is not None
                and handled_player_message_id is not None
                and pending_player_message_id <= handled_player_message_id
            ):
                return None

            return self._spawn_worker_locked(
                pending['campaign_id'],
                pending['session_id'],
                pending['user_id'],
                pending['content'],
                pending_player_message_id,
            )

stream_manager = SessionStreamManager()

import json
import queue
import threading
import time
from uuid import uuid4
from flask import current_app
from models import db, Campaign, CampaignSession, SessionMessage, SheetProposal, User
from openrouter import (
    get_session_dm_response_with_tools,
    normalize_session_dm_turn_decision,
    get_openrouter_model,
    get_llm_provider,
    _post_chat,
)
from services.dm_tools import (
    build_session_hot_context,
    get_dm_tool_definitions,
    context_manifest,
    execute_dm_tool,
)
from services.audit_service import log_audit_event

class SessionGeneratorWorker:
    def __init__(self, campaign_id, session_id, user_id, content, player_message_id=None):
        self.campaign_id = campaign_id
        self.session_id = session_id
        self.user_id = user_id
        self.content = content
        self.player_message_id = player_message_id
        self.listeners = []
        self.listener_lock = threading.Lock()
        self.status = "Initializing DM response..."
        self.is_done = False
        self.finished_at = None
        self.error = None
        self.messages_result = []
        self.sheet_proposals_result = []

    def add_listener(self):
        q = queue.Queue()
        with self.listener_lock:
            self.listeners.append(q)
            # Catch up new listener with current status
            q.put({"type": "status", "status": self.status})
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

        # Run dynamic dynamic summarization asynchronously
        threading.Thread(
            target=self._run_dynamic_summarization,
            args=(action_desc,),
            daemon=True
        ).start()

    def _run_dynamic_summarization(self, action_desc):
        # Setup context and system prompt
        provider = get_llm_provider()
        model = get_openrouter_model()

        # Let's check if the provider/API is configured. If not, don't run.
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

    def run(self, app):
        with app.app_context():
            try:
                self._execute_dm_turn()
            except Exception as e:
                db.session.rollback()
                self.finish_error(str(e))
            finally:
                if not self.is_done:
                    self.is_done = True
                    self.finished_at = time.monotonic()
                db.session.remove()

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

        trace_id = f'session_dm:session_{self.session_id}:message_{player_msg_id}' if player_msg_id else f'session_dm:session_{self.session_id}:message_async'
        trace_label = f'session_dm: session {self.session_id}'

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

        # Execute DM turn with our status update callback
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
            on_status_change=self.update_status,
        )

        ai_turn = _session_dm_turn_decision(ai_result)
        ai_text = ai_turn.get('content') or ''

        sheet_proposals = []
        result_messages = [player_msg.to_dict()] if player_msg else []

        if ai_turn.get('mode') == 'speak' and ai_text:
            ai_msg = SessionMessage(
                session_id=self.session_id,
                role='dm',
                content=ai_text,
            )
            db.session.add(ai_msg)
            db.session.flush()
            log_audit_event(
                campaign.id,
                'dm_output_stored',
                'Stored visible session DM response.',
                {
                    'session_id': self.session_id,
                    'message': {
                        'role': 'dm',
                        'content': ai_text,
                    },
                },
                source='session_messages',
                actor='session_dm',
                trace_id=trace_id,
                trace_label=trace_label,
                commit=False,
            )
            pending_proposals = SheetProposal.query.filter_by(
                session_id=self.session_id, message_id=None, status='pending',
            ).all()
            for proposal in pending_proposals:
                proposal.message_id = ai_msg.id
            db.session.commit()
            result_messages.append(ai_msg.to_dict())
            sheet_proposals = [p.to_dict() for p in pending_proposals]

            self.finish_success(result_messages, sheet_proposals)
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
            db.session.commit()

        self.finish_success(result_messages, sheet_proposals)

def _session_dm_turn_decision(raw_result):
    normalize = normalize_session_dm_turn_decision
    decision = normalize(raw_result)
    if decision.get('mode') == 'silent':
        return {
            'mode': 'silent',
            'content': '',
            'reason': decision.get('reason') or 'The DM intentionally stayed silent.',
        }
    return {
        'mode': 'speak',
        'content': decision.get('content') or '',
    }

class SessionStreamManager:
    DONE_WORKER_TTL_SECONDS = 60

    def __init__(self):
        self.workers = {}
        self.lock = threading.Lock()

    def start_generation(self, campaign_id, session_id, user_id, content, player_message_id=None):
        with self.lock:
            # If there's already a worker running for this session, don't spawn a new one.
            if session_id in self.workers and not self.workers[session_id].is_done:
                return self.workers[session_id]

            worker = SessionGeneratorWorker(campaign_id, session_id, user_id, content, player_message_id)
            self.workers[session_id] = worker
            app = current_app._get_current_object()
            t = threading.Thread(target=worker.run, args=(app,), daemon=True)
            t.start()
            return worker

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

stream_manager = SessionStreamManager()

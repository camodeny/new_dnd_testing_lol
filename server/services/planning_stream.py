"""Planning stream manager for character workshop DM responses.

Manages background workers that stream planning DM responses token-by-token
to connected SSE listeners. Since planning has no spoiler-sensitive content,
the visible message text streams directly without guard checks.
"""
import json
import queue
import re
import threading
import time

from flask import current_app

from models import db, Campaign, CharacterPlanningMessage
from openrouter import get_planning_dm_response_streaming, get_planning_summary_update
from services.audit_service import log_audit_event
from services.planning_service import (
    apply_bond_suggestions,
    get_campaign_members,
    get_member,
    get_or_create_summary,
    merge_summary_update,
    planning_context,
    visible_planning_payload,
)


class PlanningMessageExtractor:
    """Incrementally extracts the 'message' value from streamed JSON tokens.

    The LLM returns JSON like: {"message": "Hello, ...", "active_page": "identity", ...}
    We want to stream only the visible text inside the "message" value.

    Strategy: accumulate raw JSON, and once we detect we're inside the message string
    value, emit each new character of it. We track state with a simple parser.
    """

    def __init__(self):
        self._raw = []
        self._in_message = False
        self._message_started = False
        self._emitted_len = 0

    def feed(self, token):
        """Feed a new token and return the visible message delta (may be empty)."""
        self._raw.append(token)
        text = ''.join(self._raw)

        if not self._message_started:
            # Look for the "message" key's string value opening
            match = re.search(r'"message"\s*:\s*"', text)
            if match:
                self._message_started = True
                self._in_message = True
                # Find where the message value content starts
                value_start = match.end()
                # Try to extract content so far
                return self._extract_delta(text, value_start)
            return ''

        if self._in_message:
            # We know where the message value started, extract new content
            match = re.search(r'"message"\s*:\s*"', text)
            if match:
                value_start = match.end()
                return self._extract_delta(text, value_start)

        return ''

    def _extract_delta(self, text, value_start):
        """Extract new characters from the message string value."""
        # Walk from value_start, handling JSON string escapes
        i = value_start
        chars = []
        while i < len(text):
            ch = text[i]
            if ch == '\\' and i + 1 < len(text):
                next_ch = text[i + 1]
                if next_ch == '"':
                    chars.append('"')
                elif next_ch == 'n':
                    chars.append('\n')
                elif next_ch == 'r':
                    chars.append('\r')
                elif next_ch == 't':
                    chars.append('\t')
                elif next_ch == '\\':
                    chars.append('\\')
                elif next_ch == '/':
                    chars.append('/')
                elif next_ch == 'u' and i + 5 < len(text):
                    hex_str = text[i + 2:i + 6]
                    try:
                        chars.append(chr(int(hex_str, 16)))
                    except ValueError:
                        chars.append(text[i:i + 6])
                    i += 6
                    continue
                else:
                    chars.append(ch + next_ch)
                i += 2
                continue
            elif ch == '"':
                # End of the message string value
                self._in_message = False
                break
            else:
                chars.append(ch)
            i += 1

        decoded = ''.join(chars)
        if len(decoded) > self._emitted_len:
            delta = decoded[self._emitted_len:]
            self._emitted_len = len(decoded)
            return delta
        return ''


class PlanningGeneratorWorker:
    """Background worker that streams a planning DM response."""

    def __init__(self, campaign_id, user_id, player_message_id, content, draft_character, active_page):
        self.campaign_id = campaign_id
        self.user_id = user_id
        self.player_message_id = player_message_id
        self.content = content
        self.draft_character = draft_character
        self.active_page = active_page
        self.listeners = []
        self.listener_lock = threading.Lock()
        self.is_done = False
        self.finished_at = None
        self.error = None

    def add_listener(self):
        q = queue.Queue()
        with self.listener_lock:
            self.listeners.append(q)
            if self.is_done:
                if self.error:
                    q.put({'type': 'error', 'error': self.error})
                else:
                    q.put({'type': 'done'})
        return q

    def remove_listener(self, q):
        with self.listener_lock:
            if q in self.listeners:
                self.listeners.remove(q)

    def broadcast(self, payload):
        with self.listener_lock:
            for q in self.listeners:
                q.put(payload)
        planning_stream_manager.broadcast_event(self.campaign_id, self.user_id, payload)

    def finish_success(self, result):
        self.is_done = True
        self.finished_at = time.monotonic()
        self.broadcast({
            'type': 'done',
            'active_page': result.get('active_page'),
            'form_patch': result.get('form_patch') or {},
        })

    def finish_error(self, error_msg):
        self.error = error_msg
        self.is_done = True
        self.finished_at = time.monotonic()
        self.broadcast({'type': 'error', 'error': error_msg})

    def run(self, app):
        with app.app_context():
            try:
                self._execute(app)
            except Exception as e:
                db.session.rollback()
                self.finish_error(str(e))
            finally:
                if not self.is_done:
                    self.is_done = True
                    self.finished_at = time.monotonic()
                db.session.remove()

    def _execute(self, app):
        from models import User
        campaign = db.session.get(Campaign, self.campaign_id)
        current_user = db.session.get(User, self.user_id)
        if not campaign or not current_user:
            raise RuntimeError('Missing campaign or user in planning stream context')

        get_campaign_members(campaign)
        dm_trace_id = f'planning_dm:campaign_{self.campaign_id}:message_{self.player_message_id}'
        memory_trace_id = f'planning_memory_writer:campaign_{self.campaign_id}:message_{self.player_message_id}'

        messages = CharacterPlanningMessage.query.filter_by(
            campaign_id=self.campaign_id,
            user_id=self.user_id,
        ).order_by(CharacterPlanningMessage.created_at.asc()).all()

        context = planning_context(campaign, current_user)
        log_audit_event(
            self.campaign_id,
            'planning_context_read',
            'Read planning context for streaming planning DM response.',
            {'context': context, 'message_count': len(messages)},
            source='planning_context',
            actor='server',
            commit=True,
        )

        extractor = PlanningMessageExtractor()

        def on_token(delta):
            visible_delta = extractor.feed(delta)
            if visible_delta:
                self.broadcast({'type': 'token', 'token': visible_delta})

        ai_result = get_planning_dm_response_streaming(
            context,
            messages,
            draft_character=self.draft_character,
            active_page=self.active_page,
            audit_context={
                'campaign_id': self.campaign_id,
                'operation': 'planning_dm_response',
                'actor': 'planning_dm',
                'trace_id': dm_trace_id,
                'trace_label': f'planning_dm: campaign {self.campaign_id}',
            },
            on_token=on_token,
        )

        if not ai_result:
            self.finish_error('The planning DM could not respond')
            return

        ai_text = ai_result.get('message') or ''

        # Store the DM message
        dm_msg = CharacterPlanningMessage(
            campaign_id=self.campaign_id,
            user_id=self.user_id,
            role='dm',
            content=ai_text,
        )
        db.session.add(dm_msg)
        log_audit_event(
            self.campaign_id,
            'dm_output_stored',
            'Stored visible planning DM response.',
            {
                'message': {
                    'campaign_id': self.campaign_id,
                    'user_id': self.user_id,
                    'role': 'dm',
                    'content': ai_text,
                },
                'active_page': ai_result.get('active_page'),
                'form_patch': ai_result.get('form_patch') or {},
            },
            source='character_planning_messages',
            actor='planning_dm',
            trace_id=dm_trace_id,
            trace_label=f'planning_dm: campaign {self.campaign_id}',
            commit=False,
        )
        db.session.commit()

        # Finish with result so clients get active_page and form_patch
        self.finish_success(ai_result)

        # Run summary update in same thread (background, non-blocking to client)
        try:
            summary_payload = get_planning_summary_update(
                planning_context(campaign, current_user),
                self.content,
                ai_text,
                audit_context={
                    'campaign_id': self.campaign_id,
                    'operation': 'planning_summary_update',
                    'actor': 'planning_memory_writer',
                    'trace_id': memory_trace_id,
                    'parent_trace_id': dm_trace_id,
                    'trace_label': f'planning_memory_writer: campaign {self.campaign_id}',
                },
            )
            summary = get_or_create_summary(self.campaign_id)
            merge_summary_update(summary, summary_payload.get('summary_update', {}))
            apply_bond_suggestions(self.campaign_id, summary_payload.get('bond_suggestions', []))
            log_audit_event(
                self.campaign_id,
                'planning_memory_write',
                'Updated campaign planning memory from player and DM exchange.',
                {
                    'latest_player_message': self.content,
                    'latest_dm_message': ai_text,
                    'summary_payload': summary_payload,
                },
                source='campaign_planning_summaries',
                actor='planning_memory_writer',
                trace_id=memory_trace_id,
                parent_trace_id=dm_trace_id,
                trace_label=f'planning_memory_writer: campaign {self.campaign_id}',
                commit=False,
            )
            db.session.commit()
        except Exception as err:
            db.session.rollback()
            print(f'[planning_stream] Summary update error: {err}')


class PlanningStreamManager:
    """Manages one planning worker per (campaign_id, user_id) pair."""

    DONE_WORKER_TTL_SECONDS = 30

    def __init__(self):
        self.workers = {}
        self.listeners = {}  # (campaign_id, user_id) -> list of queue.Queue
        self.lock = threading.Lock()

    def _key(self, campaign_id, user_id):
        return (campaign_id, user_id)

    def start_generation(self, campaign_id, user_id, player_message_id, content, draft_character, active_page):
        key = self._key(campaign_id, user_id)
        with self.lock:
            existing = self.workers.get(key)
            if existing and not existing.is_done:
                return existing

            worker = PlanningGeneratorWorker(
                campaign_id, user_id, player_message_id, content, draft_character, active_page,
            )
            self.workers[key] = worker
            app = current_app._get_current_object()
            t = threading.Thread(target=worker.run, args=(app,), daemon=True)
            t.start()
            return worker

    def get_worker(self, campaign_id, user_id):
        key = self._key(campaign_id, user_id)
        with self.lock:
            worker = self.workers.get(key)
            if worker and worker.is_done:
                finished_at = worker.finished_at or time.monotonic()
                if time.monotonic() - finished_at > self.DONE_WORKER_TTL_SECONDS:
                    del self.workers[key]
                    return None
            return worker

    def add_listener(self, campaign_id, user_id):
        key = self._key(campaign_id, user_id)
        q = queue.Queue()
        with self.lock:
            if key not in self.listeners:
                self.listeners[key] = []
            self.listeners[key].append(q)
        return q

    def remove_listener(self, campaign_id, user_id, q):
        key = self._key(campaign_id, user_id)
        with self.lock:
            if key in self.listeners:
                if q in self.listeners[key]:
                    self.listeners[key].remove(q)
                if not self.listeners[key]:
                    del self.listeners[key]

    def broadcast_event(self, campaign_id, user_id, payload):
        key = self._key(campaign_id, user_id)
        with self.lock:
            listeners = list(self.listeners.get(key, []))
        for q in listeners:
            q.put(payload)

    def broadcast_campaign_event(self, campaign_id, payload):
        with self.lock:
            queues = []
            for (c_id, u_id), listeners in self.listeners.items():
                if c_id == campaign_id:
                    queues.extend(listeners)
        for q in queues:
            q.put(payload)


planning_stream_manager = PlanningStreamManager()

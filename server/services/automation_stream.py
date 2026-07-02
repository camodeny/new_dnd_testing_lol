import json
import time

from models import AutomationRunEvent, AutomationWorkspaceEvent


STREAM_POLL_INTERVAL_SECONDS = 1.0


def workspace_stream_cursor():
    latest = AutomationWorkspaceEvent.query.order_by(AutomationWorkspaceEvent.id.desc()).first()
    return latest.id if latest else 0


def run_stream_cursor(run_id):
    latest = AutomationRunEvent.query.filter_by(run_id=run_id).order_by(AutomationRunEvent.id.desc()).first()
    return latest.id if latest else 0


def iter_workspace_events(after_id=0, *, user_id=None, poll_interval=STREAM_POLL_INTERVAL_SECONDS, batch_size=50):
    cursor = max(0, int(after_id or 0))
    while True:
        query = AutomationWorkspaceEvent.query.filter(
            AutomationWorkspaceEvent.id > cursor,
        )
        if user_id is not None:
            query = query.filter(AutomationWorkspaceEvent.user_id == user_id)
        rows = query.order_by(AutomationWorkspaceEvent.id.asc()).limit(batch_size).all()
        if rows:
            for row in rows:
                cursor = row.id
                yield row
            continue
        time.sleep(poll_interval)
        yield None


def iter_run_events(run_id, after_id=0, *, poll_interval=STREAM_POLL_INTERVAL_SECONDS, batch_size=50):
    cursor = max(0, int(after_id or 0))
    while True:
        rows = AutomationRunEvent.query.filter(
            AutomationRunEvent.run_id == run_id,
            AutomationRunEvent.id > cursor,
        ).order_by(AutomationRunEvent.id.asc()).limit(batch_size).all()
        if rows:
            for row in rows:
                cursor = row.id
                yield row
            continue
        time.sleep(poll_interval)
        yield None


def sse_message(payload, *, event_id=None):
    parts = []
    if event_id is not None:
        parts.append(f'id: {event_id}')
    parts.append(f'data: {json.dumps(payload)}')
    return '\n'.join(parts) + '\n\n'

"""DM stream transport — issue #197."""
import logging
import os
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.campaigns.service import is_campaign_member, parse_campaign_id
from app.deps.auth import resolve_profile
from app.runtime.threads import (
    ThreadAuthorizationError,
    ThreadNotFoundError,
    assert_can_read_thread,
    get_campaign_thread,
    parse_thread_id,
    resolve_thread_id,
)
from app.dm_streams.service import (
    abandon_stream,
    append_chunk,
    complete_stream,
    create_stream,
    fail_stream,
    get_stream,
    list_all_streams_for_thread,
    list_chunks,
    list_streams_for_thread,
    reconstruct_text,
)
from app.dm_streams.service import (
    DMStreamConflictError,
    DMStreamNotFoundError,
    DMStreamStateError,
)
from database import get_db
from models.campaigns import Campaign

router = APIRouter()
logger = logging.getLogger(__name__)


def _authorized_campaign(db: Session, campaign_id: str, user_id: uuid.UUID) -> Campaign:
    try:
        cid = parse_campaign_id(campaign_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Invalid campaign id") from exc
    campaign = db.get(Campaign, cid)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.owner_id != user_id and not is_campaign_member(db, cid, user_id):
        raise HTTPException(status_code=403, detail="Not a member of this campaign")
    return campaign


def _is_dm_writer(campaign: Campaign, profile, request: Request) -> bool:
    """DM/server-only writer check — issue #197 review.

    Only the campaign owner (DM) or an internal worker may mutate streams.
    Players — even members of the thread — cannot forge DM narration.
    Worker authentication uses a shared header token if configured, to allow
    the queue worker to act without impersonating the owner.
    """
    if campaign.owner_id == profile.id:
        return True
    # Internal worker token (server-to-server). Env not set = no worker path.
    token = request.headers.get("x-worker-token") or request.headers.get("x-internal-token") or request.headers.get("x-dm-worker-token")
    expected = os.getenv("DM_STREAM_WORKER_TOKEN") or os.getenv("WORKER_INTERNAL_TOKEN") or os.getenv("INTERNAL_WORKER_TOKEN")
    if expected and token and token == expected:
        return True
    return False


def _require_dm_writer(campaign: Campaign, profile, request: Request) -> None:
    if not _is_dm_writer(campaign, profile, request):
        logger.info("dm_stream write denied campaign_id=%s user_id=%s reason=not_dm_writer", campaign.id, profile.id)
        raise HTTPException(status_code=403, detail="Only the DM/campaign owner can mutate DM streams")


def _resolve_thread_for_stream(db: Session, campaign: Campaign, raw_thread_id, profile, request: Request) -> uuid.UUID:
    raw = raw_thread_id if raw_thread_id is not None else "main"
    try:
        # Resolve thread id (creates shared thread if needed). This is the same
        # durable resolution used for player submissions, but DM streams do not
        # reuse player write auth — they require explicit DM/worker authorization.
        tid = resolve_thread_id(db, campaign.id, str(raw) if raw else "main", created_by=profile.id)
        db.commit()
        # Verify thread exists and viewer can at least read it (privacy: owner
        # without private membership still fails closed, per #195 invariant).
        assert_can_read_thread(db, campaign.id, tid, profile.id)
        _require_dm_writer(campaign, profile, request)
        return tid
    except HTTPException:
        raise
    except ThreadNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Thread not found") from exc
    except ThreadAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/api/campaigns/{campaign_id}/dm-streams", status_code=201)
def create_dm_stream(campaign_id: str, payload: dict, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    campaign = _authorized_campaign(db, campaign_id, profile.id)

    raw_thread = payload.get("thread_id", payload.get("threadId", "main"))
    try:
        thread_id = _resolve_thread_for_stream(db, campaign, raw_thread, profile, request)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    thread = get_campaign_thread(db, campaign.id, thread_id)
    audience = "campaign" if thread and thread.thread_type == "campaign" else "private"

    turn_id = str(payload.get("turn_id", payload.get("turnId", ""))).strip()
    attempt_id = str(payload.get("attempt_id", payload.get("attemptId", ""))).strip()
    if not turn_id:
        # auto-generate if not provided
        turn_id = str(uuid.uuid4())
    if not attempt_id:
        attempt_id = str(uuid.uuid4())

    trace_id = payload.get("trace_id") or payload.get("traceId")
    operation_id = payload.get("operation_id") or payload.get("operationId")

    try:
        stream = create_stream(
            db,
            campaign_id=campaign.id,
            thread_id=thread_id,
            turn_id=turn_id,
            attempt_id=attempt_id,
            audience=audience,
            trace_id=trace_id,
            operation_id=operation_id,
        )
        db.commit()
        db.refresh(stream)
    except DMStreamConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Realtime DM status (streaming) — best-effort after commit.
    try:
        from app.realtime.service import publish_dm_status

        publish_dm_status(db, stream, visible_text="")
    except Exception as pub_exc:
        logger.warning("realtime publish after stream create failed stream_id=%s error=%s", stream.id, pub_exc)
    return {"stream": stream.to_dict(), "chunks": [], "visible_text": ""}


@router.post("/api/campaigns/{campaign_id}/dm-streams/{stream_id}/chunks", status_code=201)
def append_dm_chunk(campaign_id: str, stream_id: str, payload: dict, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    campaign = _authorized_campaign(db, campaign_id, profile.id)

    try:
        sid = uuid.UUID(str(stream_id))
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Invalid stream id") from exc

    stream = get_stream(db, sid)
    if stream is None or stream.campaign_id != campaign.id:
        raise HTTPException(status_code=404, detail="Stream not found")

    # Auth: DM stream mutations are DM/worker-only — players cannot forge narration.
    # Read-level thread auth still applies for visibility, but write auth is distinct.
    try:
        assert_can_read_thread(db, campaign.id, stream.thread_id, profile.id)
    except ThreadNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Thread not found") from exc
    except ThreadAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    _require_dm_writer(campaign, profile, request)

    sequence = payload.get("sequence")
    text = payload.get("text", payload.get("chunk", payload.get("content", "")))
    if sequence is None:
        raise HTTPException(status_code=422, detail="sequence is required")
    try:
        sequence = int(sequence)
    except Exception:
        raise HTTPException(status_code=422, detail="sequence must be an integer")
    if not isinstance(text, str):
        raise HTTPException(status_code=422, detail="text must be a string")

    try:
        chunk = append_chunk(db, sid, sequence, text)
        db.commit()
        db.refresh(chunk)
        # refresh stream metrics
        db.refresh(stream)
    except DMStreamConflictError as exc:
        # Log append failures per observability
        logger.warning("dm_stream append conflict campaign_id=%s stream_id=%s sequence=%s error=%s", campaign.id, sid, sequence, exc)
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DMStreamStateError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Realtime chunk projection — best-effort after commit (never rolls back DB).
    try:
        from app.realtime.service import publish_dm_chunk_created

        publish_dm_chunk_created(db, stream, chunk)
    except Exception as pub_exc:
        logger.warning("realtime publish after chunk failed stream_id=%s seq=%s error=%s", sid, sequence, pub_exc)
    # Not reporting as visible until persistence succeeded — commit succeeded.
    return {"chunk": chunk.to_dict(), "stream": stream.to_dict()}


@router.get("/api/campaigns/{campaign_id}/dm-streams/{stream_id}")
def get_dm_stream(campaign_id: str, stream_id: str, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    campaign = _authorized_campaign(db, campaign_id, profile.id)
    try:
        sid = uuid.UUID(str(stream_id))
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Invalid stream id") from exc
    stream = get_stream(db, sid)
    if stream is None or stream.campaign_id != campaign.id:
        raise HTTPException(status_code=404, detail="Stream not found")
    try:
        assert_can_read_thread(db, campaign.id, stream.thread_id, profile.id)
    except ThreadNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Thread not found") from exc
    except ThreadAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    chunks = list_chunks(db, sid)
    visible_text = "".join(c.text for c in chunks)
    # Final text convenience
    final_text = stream.final_text if stream.status == "completed" else None
    return {
        "stream": stream.to_dict(),
        "chunks": [c.to_dict() for c in chunks],
        "visible_text": visible_text,
        "final_text": final_text,
        "final_message": final_text if final_text is not None else visible_text if stream.status == "completed" else None,
    }


@router.post("/api/campaigns/{campaign_id}/dm-streams/{stream_id}/complete")
def complete_dm_stream(campaign_id: str, stream_id: str, payload: dict, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    campaign = _authorized_campaign(db, campaign_id, profile.id)
    try:
        sid = uuid.UUID(str(stream_id))
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Invalid stream id") from exc
    stream = get_stream(db, sid)
    if stream is None or stream.campaign_id != campaign.id:
        raise HTTPException(status_code=404, detail="Stream not found")
    try:
        assert_can_read_thread(db, campaign.id, stream.thread_id, profile.id)
    except ThreadNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Thread not found") from exc
    except ThreadAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    _require_dm_writer(campaign, profile, request)
    reason = str(payload.get("reason", payload.get("completion_reason", "completed"))) if payload else "completed"
    try:
        stream = complete_stream(db, sid, completion_reason=reason)
        db.commit()
        db.refresh(stream)
    except DMStreamStateError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    chunks = list_chunks(db, sid)
    visible_text = "".join(c.text for c in chunks)
    # Realtime status projection — best-effort after commit.
    try:
        from app.realtime.service import publish_dm_status

        publish_dm_status(db, stream, visible_text=visible_text)
    except Exception as pub_exc:
        logger.warning("realtime publish after complete failed stream_id=%s error=%s", sid, pub_exc)
    return {"stream": stream.to_dict(), "chunks": [c.to_dict() for c in chunks], "visible_text": visible_text, "final_text": stream.final_text}


@router.post("/api/campaigns/{campaign_id}/dm-streams/{stream_id}/abandon")
def abandon_dm_stream(campaign_id: str, stream_id: str, payload: dict, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    campaign = _authorized_campaign(db, campaign_id, profile.id)
    try:
        sid = uuid.UUID(str(stream_id))
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Invalid stream id") from exc
    stream = get_stream(db, sid)
    if stream is None or stream.campaign_id != campaign.id:
        raise HTTPException(status_code=404, detail="Stream not found")
    try:
        assert_can_read_thread(db, campaign.id, stream.thread_id, profile.id)
    except ThreadNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Thread not found") from exc
    except ThreadAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    _require_dm_writer(campaign, profile, request)
    reason = str(payload.get("reason", payload.get("abandonment_reason", "abandoned"))) if payload else "abandoned"
    try:
        # allow explicit failed status via payload
        if payload and payload.get("status") == "failed":
            stream = fail_stream(db, sid, reason=reason)
        else:
            stream = abandon_stream(db, sid, reason=reason)
        db.commit()
        db.refresh(stream)
    except DMStreamStateError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    chunks = list_chunks(db, sid)
    visible_text = "".join(c.text for c in chunks)
    return {"stream": stream.to_dict(), "chunks": [c.to_dict() for c in chunks], "visible_text": visible_text}


@router.get("/api/campaigns/{campaign_id}/dm-streams")
def list_dm_streams(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    campaign = _authorized_campaign(db, campaign_id, profile.id)
    raw_thread = request.query_params.get("thread_id", request.query_params.get("threadId"))
    # require thread_id filter for now; don't leak private threads without filter
    if raw_thread:
        try:
            if raw_thread in ("main", "", "null"):
                tid = resolve_thread_id(db, campaign.id, "main", created_by=profile.id)
                db.commit()
            else:
                tid = parse_thread_id(raw_thread)
            assert_can_read_thread(db, campaign.id, tid, profile.id)
        except ThreadNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Thread not found") from exc
        except ThreadAuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        include_abandoned = request.query_params.get("include_abandoned", "false").lower() in ("true", "1")
        if include_abandoned:
            streams = list_all_streams_for_thread(db, campaign.id, tid)
        else:
            streams = list_streams_for_thread(db, campaign.id, tid, include_abandoned=False)
        # For include_abandoned=true we still hide failed/abandoned from non-authorized? Already authorized via thread, so return all
        # But canonical history endpoint should exclude abandoned — that's what list_streams_for_thread does when include_abandoned=False
        result = []
        for s in streams:
            chunks = list_chunks(db, s.id)
            visible_text = "".join(c.text for c in chunks)
            result.append({"stream": s.to_dict(), "chunk_count": len(chunks), "visible_text": visible_text, "final_text": s.final_text})
        return {"streams": result, "thread_id": str(tid)}
    else:
        # No thread filter: return only completed streams across visible threads (canonical history)
        from app.runtime.threads import list_threads_for_user
        visible_threads = list_threads_for_user(db, campaign.id, profile.id)
        all_streams = []
        for t in visible_threads:
            streams = list_streams_for_thread(db, campaign.id, t.id, include_abandoned=False)
            for s in streams:
                chunks = list_chunks(db, s.id)
                visible_text = "".join(c.text for c in chunks)
                all_streams.append({"stream": s.to_dict(), "chunk_count": len(chunks), "visible_text": visible_text, "final_text": s.final_text})
        # Optionally include abandoned if requested globally
        include_abandoned = request.query_params.get("include_abandoned", "false").lower() in ("true", "1")
        if include_abandoned:
            # Return all including abandoned for audit (but still only visible threads)
            all_streams_audit = []
            for t in visible_threads:
                streams = list_all_streams_for_thread(db, campaign.id, t.id)
                for s in streams:
                    chunks = list_chunks(db, s.id)
                    visible_text = "".join(c.text for c in chunks)
                    all_streams_audit.append({"stream": s.to_dict(), "chunk_count": len(chunks), "visible_text": visible_text})
            return {"streams": all_streams_audit}
        return {"streams": all_streams}

import os

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from .service import get_trace

router = APIRouter(prefix="/api/dev/observability", tags=["observability"])


def require_debug_token(x_observability_token: str | None = Header(default=None)):
    expected = os.getenv("OBSERVABILITY_DEBUG_TOKEN")
    if not expected or x_observability_token != expected:
        raise HTTPException(status_code=404, detail="Not found")


@router.get("/traces/{trace_id}", dependencies=[Depends(require_debug_token)])
def inspect_trace(trace_id: str, db: Session = Depends(get_db)):
    result = get_trace(db, trace_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Trace not found")
    return result

"""Runtime transport — session stubs."""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from database import get_db

router = APIRouter()


@router.post("/api/campaigns/{campaign_id}/sessions")
def stub_start_session(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    raise HTTPException(status_code=501, detail="Sessions not yet implemented")


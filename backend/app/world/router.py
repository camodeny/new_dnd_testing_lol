"""World transport — stubs."""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from database import get_db

router = APIRouter()


@router.get("/api/campaigns/{campaign_id}/world")
def stub_get_world(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    return {"world": None}


@router.get("/api/campaigns/{campaign_id}/encounter-maps/current")
def stub_encounter_map(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    return {"map": None}


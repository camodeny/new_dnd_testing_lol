"""BYOK transport — APIRouter. Depends on service layer for domain logic."""

import uuid as uuid_lib

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.byok.errors import ByokError, http_status_for
from app.deps.auth import resolve_profile
from database import get_db

router = APIRouter()


def _byok_error(exc: ByokError) -> HTTPException:
    return HTTPException(status_code=http_status_for(exc.kind), detail=str(exc))


def _parse_uuid(value: str, *, what: str) -> uuid_lib.UUID:
    try:
        return uuid_lib.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=404, detail=f"Invalid {what} id")


@router.post("/api/byok/credentials", status_code=201)
def create_credential(payload: dict, request: Request, db: Session = Depends(get_db)):
    from app.byok import service as _service

    profile = resolve_profile(request, db)
    try:
        row = _service.create_credential(
            db,
            owner_id=profile.id,
            provider=(payload or {}).get("provider") or "",
            secret=(payload or {}).get("secret"),
            label=(payload or {}).get("label"),
        )
    except ByokError as exc:
        raise _byok_error(exc)
    db.commit()
    db.refresh(row)
    return {"credential": row.to_masked_dict()}


@router.get("/api/byok/credentials")
def list_credentials(request: Request, db: Session = Depends(get_db)):
    from app.byok import service as _service

    profile = resolve_profile(request, db)
    rows = _service.list_credentials(db, owner_id=profile.id)
    return {"credentials": [row.to_masked_dict() for row in rows]}


@router.get("/api/byok/credentials/{credential_id}")
def get_credential(credential_id: str, request: Request, db: Session = Depends(get_db)):
    from app.byok import service as _service

    profile = resolve_profile(request, db)
    try:
        row = _service.get_owned_credential(
            db, credential_id=_parse_uuid(credential_id, what="credential"),
            owner_id=profile.id,
        )
    except ByokError as exc:
        raise _byok_error(exc)
    return {"credential": row.to_masked_dict()}


@router.patch("/api/byok/credentials/{credential_id}")
def update_credential(credential_id: str, payload: dict, request: Request, db: Session = Depends(get_db)):
    from app.byok import service as _service

    profile = resolve_profile(request, db)
    data = payload or {}
    # Rotation vs label-only is explicit: only a present "secret" rotates.
    kwargs: dict = {}
    if "label" in data:
        kwargs["label"] = data.get("label")
    if "secret" in data:
        kwargs["secret"] = data.get("secret")
    try:
        row = _service.update_credential(
            db, credential_id=_parse_uuid(credential_id, what="credential"),
            owner_id=profile.id, **kwargs,
        )
    except ByokError as exc:
        raise _byok_error(exc)
    db.commit()
    db.refresh(row)
    return {"credential": row.to_masked_dict()}


@router.delete("/api/byok/credentials/{credential_id}", status_code=204)
def delete_credential(credential_id: str, request: Request, db: Session = Depends(get_db)):
    from app.byok import service as _service

    profile = resolve_profile(request, db)
    try:
        _service.delete_credential(
            db, credential_id=_parse_uuid(credential_id, what="credential"),
            owner_id=profile.id,
        )
    except ByokError as exc:
        raise _byok_error(exc)
    db.commit()
    return None


@router.post("/api/byok/credentials/{credential_id}/test")
def test_credential(credential_id: str, request: Request, db: Session = Depends(get_db)):
    from app.byok import service as _service

    profile = resolve_profile(request, db)
    try:
        result = _service.test_credential(
            db, credential_id=_parse_uuid(credential_id, what="credential"),
            owner_id=profile.id,
        )
    except ByokError as exc:
        # Invalid keys still persist their status change: commit the flag
        # before reporting the rejection.
        try:
            db.commit()
        except Exception:
            db.rollback()
        raise _byok_error(exc)
    db.commit()
    return result


@router.get("/api/byok/routes")
def inspect_routes(
    request: Request,
    db: Session = Depends(get_db),
    role: str = "",
    execution_class: str = "generative",
    decision_mode: str = "primer",
):
    """Approved-route introspection (metadata only, never credentials).

    Development/admin visibility into effective routing: which
    provider/model pairs are approved for an execution role and whether
    BYOK may serve them.
    """
    from app.byok import routing as _routing

    resolve_profile(request, db)
    if execution_class == "decision":
        approvals = [
            {
                "decision_class": approval.decision_class,
                "adapter": approval.adapter,
                "model": approval.model,
                "policy_version": approval.policy_version,
                "candidate_schema_version": approval.candidate_schema_version,
                "allowed_modes": list(approval.allowed_modes),
                "byok_eligible": approval.byok_eligible,
            }
            for approval in _routing._DECISION_APPROVALS.values()
            if not role or approval.decision_class == role
        ]
        return {"execution_class": "decision", "approvals": approvals}
    if role:
        from app.providers import policy as _role_policy

        try:
            policy = _role_policy.get_role_policy(role)
        except RuntimeError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {
            "execution_class": "generative",
            "role": role,
            "primary": {"provider": policy.primary_provider, "model": policy.primary_model},
            "byok_providers": list(_routing._byok_providers_from_env(role, policy.primary_provider)),
            "allowed_fallback_models": [
                {"provider": provider, "model": model}
                for provider, model in policy.allowed_fallback_models
            ],
            "max_attempts": policy.max_attempts,
        }
    return {
        "execution_class": "generative",
        "roles": sorted(_routing.GENERATIVE_ROLE_AREA),
    }


@router.put("/api/campaigns/{campaign_id}/byok-policy")
def set_campaign_policy(campaign_id: str, payload: dict, request: Request, db: Session = Depends(get_db)):
    from app.byok import service as _service

    profile = resolve_profile(request, db)
    credential_ref = (payload or {}).get("credential_id")
    if not credential_ref:
        raise HTTPException(status_code=400, detail="credential_id is required")
    try:
        policy = _service.set_campaign_policy(
            db,
            campaign_id=_parse_uuid(campaign_id, what="campaign"),
            credential_id=_parse_uuid(credential_ref, what="credential"),
            authorized_by=profile.id,
        )
    except ByokError as exc:
        raise _byok_error(exc)
    db.commit()
    credential = db.get(_service.ProviderCredential, policy.credential_id) if policy.credential_id else None
    return {"policy": policy.to_dict(credential=credential)}


@router.get("/api/campaigns/{campaign_id}/byok-policy")
def get_campaign_policy(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    from app.byok import service as _service

    profile = resolve_profile(request, db)
    try:
        policy = _service.get_campaign_policy(
            db, campaign_id=_parse_uuid(campaign_id, what="campaign"),
            viewer_id=profile.id,
        )
    except ByokError as exc:
        raise _byok_error(exc)
    if policy is None or policy.credential_id is None:
        return {"policy": None}
    credential = db.get(_service.ProviderCredential, policy.credential_id)
    return {"policy": policy.to_dict(credential=credential)}


@router.delete("/api/campaigns/{campaign_id}/byok-policy", status_code=204)
def clear_campaign_policy(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    from app.byok import service as _service

    profile = resolve_profile(request, db)
    try:
        _service.clear_campaign_policy(
            db, campaign_id=_parse_uuid(campaign_id, what="campaign"),
            authorized_by=profile.id,
        )
    except ByokError as exc:
        raise _byok_error(exc)
    db.commit()
    return None

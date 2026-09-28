"""BYOK credential + campaign-policy application service — issue #257.

Ownership rules (deterministic, code-owned):

- Credentials are owned by exactly one user. Only the owner can create,
  read (masked), update/rotate, delete, or test them. Any other user —
  including campaign members and the campaign owner — gets ``not_found``
  (no existence leak) and never sees ciphertext or plaintext.
- Campaign use requires an explicit policy row set by the campaign owner
  naming one credential owned by a campaign member (owner or member).
  Execution resolves the policy server-side; the key itself is never
  exposed or shared.
- Rotation only affects new runs: in-flight attempts keep their recorded
  ``credential_id`` trace and their already-resolved transport state.
- Deletion hard-deletes the credential row; referencing policies are
  cleared (FK SET NULL + ``enabled=False``) so routing returns to
  funded/provider policy without corrupting gameplay state.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.byok import crypto as _crypto
from app.byok.errors import (
    FORBIDDEN,
    INVALID_CREDENTIAL,
    MALFORMED,
    NOT_FOUND,
    UNSUPPORTED,
    UPSTREAM_UNREACHABLE,
    ByokError,
)
from app.byok.routing import (
    SUPPORTED_CREDENTIAL_PROVIDERS,
    assert_supported_provider,
    normalize_provider,
)
from models.byok import (
    CREDENTIAL_STATUSES,
    CREDENTIAL_STATUS_ACTIVE,
    CREDENTIAL_STATUS_INVALID,
    CampaignByokPolicy,
    ProviderCredential,
)

# Re-exported for transport consumers (router projections).
__all__ = [
    "ProviderCredential",
    "CampaignByokPolicy",
    "ActiveCredential",
    "create_credential",
    "list_credentials",
    "get_owned_credential",
    "update_credential",
    "delete_credential",
    "decrypt_for_execution",
    "flag_invalid_credential",
    "test_credential",
    "set_campaign_policy",
    "get_campaign_policy",
    "clear_campaign_policy",
    "active_campaign_credential",
    "resolve_byok_execution",
    "resolve_decision_execution",
    "byok_satisfies_role",
]

MAX_LABEL_LENGTH = 128
MIN_SECRET_LENGTH = 8
# Provider API keys are short bearer tokens; an upper bound keeps the
# encrypted blob ( Fernet ~ 1.4x + base64 ) small and rejects accidental
# pastes (certificates, dumps) with a clear 400 instead of storing them.
MAX_SECRET_LENGTH = 1024


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _last4(secret: str) -> str:
    stripped = secret.strip()
    return stripped[-4:] if len(stripped) >= 4 else ""


def _validate_label(label: str | None) -> str:
    if label is not None and not isinstance(label, str):
        raise ByokError("label must be a string", kind=MALFORMED)
    text = (label or "").strip()
    if len(text) > MAX_LABEL_LENGTH:
        raise ByokError(
            f"label must be at most {MAX_LABEL_LENGTH} characters",
            kind=MALFORMED,
        )
    return text


def _validate_secret(secret: str | None) -> str:
    if not isinstance(secret, str) or not secret.strip():
        raise ByokError("provider secret must be a non-empty string", kind=MALFORMED)
    clean = secret.strip()
    if len(clean) < MIN_SECRET_LENGTH:
        raise ByokError(
            f"provider secret is too short (minimum {MIN_SECRET_LENGTH} characters)",
            kind=MALFORMED,
        )
    if len(clean) > MAX_SECRET_LENGTH:
        raise ByokError(
            f"provider secret is too long (maximum {MAX_SECRET_LENGTH} characters)",
            kind=MALFORMED,
        )
    return clean


# ── Credential CRUD (owner-only) ────────────────────────────────────────────


def create_credential(
    db: Session, *, owner_id: uuid.UUID, provider: str, secret: str, label: str | None = None
) -> ProviderCredential:
    normalized = assert_supported_provider(provider)
    clean_secret = _validate_secret(secret)
    clean_label = _validate_label(label)
    row = ProviderCredential(
        owner_user_id=owner_id,
        provider=normalized,
        label=clean_label,
        encrypted_secret=_crypto.encrypt_secret(clean_secret),
        key_fingerprint=_fingerprint(clean_secret),
        key_last4=_last4(clean_secret),
        status=CREDENTIAL_STATUS_ACTIVE,
    )
    db.add(row)
    db.flush()
    return row


def list_credentials(db: Session, *, owner_id: uuid.UUID) -> list[ProviderCredential]:
    return list(
        db.scalars(
            select(ProviderCredential)
            .where(ProviderCredential.owner_user_id == owner_id)
            .order_by(ProviderCredential.created_at)
        ).all()
    )


def get_owned_credential(
    db: Session, *, credential_id: uuid.UUID, owner_id: uuid.UUID
) -> ProviderCredential:
    """Owner-only fetch. Non-owners (and strangers) get ``not_found``."""
    row = db.get(ProviderCredential, credential_id)
    if row is None or str(row.owner_user_id) != str(owner_id):
        raise ByokError("provider credential not found", kind=NOT_FOUND)
    return row


def update_credential(
    db: Session,
    *,
    credential_id: uuid.UUID,
    owner_id: uuid.UUID,
    label: str | None = None,
    secret: str | None = None,
) -> ProviderCredential:
    row = get_owned_credential(db, credential_id=credential_id, owner_id=owner_id)
    if label is not None:
        row.label = _validate_label(label)
    if secret is not None:
        clean_secret = _validate_secret(secret)
        row.encrypted_secret = _crypto.encrypt_secret(clean_secret)
        row.key_fingerprint = _fingerprint(clean_secret)
        row.key_last4 = _last4(clean_secret)
        # Rotation re-arms the credential: a previously invalid key becomes
        # a candidate again; the next test or execution decides its status.
        row.status = CREDENTIAL_STATUS_ACTIVE
        row.last_test_result = None
        row.last_tested_at = None
    db.add(row)
    db.flush()
    return row


def delete_credential(db: Session, *, credential_id: uuid.UUID, owner_id: uuid.UUID) -> None:
    row = get_owned_credential(db, credential_id=credential_id, owner_id=owner_id)
    # Clear campaign assignments first so policy reads never dangle; the FK
    # also nulls on delete as defense in depth.
    policies = db.scalars(
        select(CampaignByokPolicy).where(CampaignByokPolicy.credential_id == row.id)
    ).all()
    for policy in policies:
        policy.credential_id = None
        policy.enabled = False
        db.add(policy)
    db.delete(row)
    db.flush()


def decrypt_for_execution(row: ProviderCredential) -> str:
    """Decrypt server-side for transport injection. Never return or log."""
    return _crypto.decrypt_secret(row.encrypted_secret)


def flag_invalid_credential(session_factory, credential_id: uuid.UUID) -> None:
    """Fail-soft mark of an auth-rejected credential as ``invalid``.

    Runs in an independent transaction (never the gameplay session) so a
    gameplay rollback cannot resurrect a dead key, and a flagging failure
    can never break recovery. Execution-ineligible until rotated.
    """
    if session_factory is None or credential_id is None:
        return
    try:
        with session_factory() as db:
            try:
                row = db.get(ProviderCredential, credential_id)
                if row is not None and row.status == CREDENTIAL_STATUS_ACTIVE:
                    row.status = CREDENTIAL_STATUS_INVALID
                    row.last_test_result = INVALID_CREDENTIAL
                    row.last_tested_at = _utcnow()
                    db.add(row)
                db.commit()
            except Exception:
                db.rollback()
    except Exception:
        return


# ── Test-connection ─────────────────────────────────────────────────────────


def _probe_generative(provider: str, secret: str, *, timeout: float) -> bool:
    """Minimal authenticated probe: invalid keys fail closed as invalid.

    Returns True when a probe actually ran, False when the provider has no
    known probe shape (callers must report ``unverified``, never ``ok``).
    """
    import requests

    from app.providers.registry import provider_registry

    adapter = provider_registry.get(provider)
    base = adapter.default_base_url or ""
    # Probe the provider's models endpoint where the shape is known;
    # otherwise fall back to config validation only (no blind POSTs).
    probe_url = ""
    lowered = base.lower()
    if "openai.com" in lowered:
        probe_url = "https://api.openai.com/v1/models"
    elif "openrouter.ai" in lowered:
        probe_url = "https://openrouter.ai/api/v1/models"
    if not probe_url:
        return False
    try:
        response = requests.get(
            probe_url,
            headers={"Authorization": f"Bearer {secret}"},
            timeout=timeout,
        )
    except Exception as exc:
        raise ByokError(
            "provider is unreachable for credential test; retry later",
            kind=UPSTREAM_UNREACHABLE,
        ) from exc
    if response.status_code in (401, 403):
        raise ByokError("provider rejected the credential", kind=INVALID_CREDENTIAL)
    if response.status_code >= 400:
        raise ByokError(
            f"provider test returned status {response.status_code}",
            kind=UPSTREAM_UNREACHABLE,
        )
    return True


def _probe_decision(provider: str, secret: str, *, timeout: float) -> bool:
    import requests

    from app.decisions.config import base_url as _base_url

    url = _base_url().strip()
    if not url:
        return False
    # Auth-only probe: an unauthenticated-shape GET distinguishes bad keys
    # (401/403) from reachable service without spending a decision call.
    try:
        response = requests.get(
            url, headers={"Authorization": f"Bearer {secret}"}, timeout=timeout
        )
    except Exception as exc:
        raise ByokError(
            "decision provider is unreachable for credential test; retry later",
            kind=UPSTREAM_UNREACHABLE,
        ) from exc
    if response.status_code in (401, 403):
        raise ByokError("provider rejected the credential", kind=INVALID_CREDENTIAL)


def test_credential(
    db: Session, *, credential_id: uuid.UUID, owner_id: uuid.UUID, timeout: float = 10.0
) -> dict:
    """Validate a credential against its provider without exposing the secret.

    Updates ``status``/``last_test_*`` on the row. Invalid keys become
    ``invalid`` (execution-ineligible) rather than deleted, so rotation can
    re-arm them. Upstream outages leave status untouched and report
    ``upstream_unreachable``. Providers with no known probe shape report
    ``unverified`` (config-valid only) instead of a false ``ok``.
    """
    from app.byok.adapters import wrap_decision_adapter, wrap_generative_adapter

    row = get_owned_credential(db, credential_id=credential_id, owner_id=owner_id)
    secret = decrypt_for_execution(row)
    probed = False
    try:
        if row.provider == "jev":
            proxy = wrap_decision_adapter(row.provider, secret)
            proxy.require_config(proxy.default_model())
            probed = _probe_decision(row.provider, secret, timeout=timeout)
        else:
            from app.providers.registry import provider_registry

            candidate_model = (
                provider_registry.get(row.provider).env_model()
                or getattr(provider_registry.get(row.provider), "default_model", "")
                or "connection-test"
            )
            proxy = wrap_generative_adapter(row.provider, secret)
            proxy.require_config(candidate_model)
            probed = _probe_generative(row.provider, secret, timeout=timeout)
    except ByokError as exc:
        if exc.kind == INVALID_CREDENTIAL:
            row.status = CREDENTIAL_STATUS_INVALID
            row.last_test_result = INVALID_CREDENTIAL
            row.last_tested_at = _utcnow()
            db.add(row)
            db.flush()
        raise
    result = "ok" if probed else "unverified"
    row.status = CREDENTIAL_STATUS_ACTIVE
    row.last_test_result = result
    row.last_tested_at = _utcnow()
    db.add(row)
    db.flush()
    return {"credential_id": str(row.id), "provider": row.provider, "result": result}


# ── Campaign policy (owner-authorized member key) ───────────────────────────


def _require_campaign_owner(db: Session, campaign_id: uuid.UUID, user_id: uuid.UUID):
    from models.campaigns import Campaign

    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise ByokError("campaign not found", kind=NOT_FOUND)
    if str(campaign.owner_id) != str(user_id):
        raise ByokError(
            "only the campaign owner can authorize a campaign credential",
            kind=FORBIDDEN,
        )
    return campaign


def _require_member_or_owner(db: Session, campaign_id: uuid.UUID, user_id: uuid.UUID) -> None:
    from app.campaigns.service import is_campaign_member
    from models.campaigns import Campaign

    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise ByokError("campaign not found", kind=NOT_FOUND)
    if str(campaign.owner_id) == str(user_id):
        return
    if not is_campaign_member(db, campaign_id, user_id):
        raise ByokError(
            "credential owner is not a member of this campaign",
            kind=FORBIDDEN,
        )


def set_campaign_policy(
    db: Session,
    *,
    campaign_id: uuid.UUID,
    credential_id: uuid.UUID,
    authorized_by: uuid.UUID,
) -> CampaignByokPolicy:
    """Authorize one member credential for a campaign (owner-only)."""
    _require_campaign_owner(db, campaign_id, authorized_by)
    credential = db.get(ProviderCredential, credential_id)
    if credential is None:
        raise ByokError("provider credential not found", kind=NOT_FOUND)
    _require_member_or_owner(db, campaign_id, credential.owner_user_id)
    if credential.status != CREDENTIAL_STATUS_ACTIVE:
        raise ByokError(
            f"credential is {credential.status}; only active credentials can be authorized",
            kind=MALFORMED,
        )
    policy = db.get(CampaignByokPolicy, campaign_id)
    if policy is None:
        policy = CampaignByokPolicy(campaign_id=campaign_id)
    policy.credential_id = credential.id
    policy.authorized_by = authorized_by
    policy.enabled = True
    db.add(policy)
    db.flush()
    return policy


def get_campaign_policy(
    db: Session, *, campaign_id: uuid.UUID, viewer_id: uuid.UUID
) -> CampaignByokPolicy | None:
    """Member-visible policy read: credential IDs only, never secrets."""
    from models.campaigns import Campaign

    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise ByokError("campaign not found", kind=NOT_FOUND)
    from app.campaigns.service import is_campaign_member

    if str(campaign.owner_id) != str(viewer_id) and not is_campaign_member(
        db, campaign_id, viewer_id
    ):
        raise ByokError("not a member of this campaign", kind=FORBIDDEN)
    return db.get(CampaignByokPolicy, campaign_id)


def clear_campaign_policy(
    db: Session, *, campaign_id: uuid.UUID, authorized_by: uuid.UUID
) -> None:
    _require_campaign_owner(db, campaign_id, authorized_by)
    policy = db.get(CampaignByokPolicy, campaign_id)
    if policy is None:
        return
    policy.credential_id = None
    policy.enabled = False
    db.add(policy)
    db.flush()


@dataclass(frozen=True)
class ActiveCredential:
    row: ProviderCredential
    secret: str


def active_campaign_credential(db: Session, campaign_id: uuid.UUID) -> ActiveCredential | None:
    """Resolve the campaign's authorized credential for execution, if any.

    Returns None when no policy, disabled policy, missing/invalid
    credential — callers fall back to funded/provider routing. Decryption
    failures fail closed to None (never raise secrets-adjacent errors into
    gameplay paths).
    """
    policy = db.get(CampaignByokPolicy, campaign_id)
    if policy is None or not policy.enabled or policy.credential_id is None:
        return None
    row = db.get(ProviderCredential, policy.credential_id)
    if row is None or row.status != CREDENTIAL_STATUS_ACTIVE:
        return None
    try:
        secret = decrypt_for_execution(row)
    except ByokError:
        return None
    if not secret:
        return None
    return ActiveCredential(row=row, secret=secret)


def byok_satisfies_role(
    db: Session, campaign_id: uuid.UUID, role: str, *, model: str | None = None
) -> bool:
    """Whether the campaign's authorized credential offers an approved BYOK
    route for a generative ``role`` (capacity-resume probe; non-raising)."""
    from app.byok.routing import byok_route_available

    active = active_campaign_credential(db, campaign_id)
    if active is None:
        return False
    candidate_model = model
    if candidate_model is None:
        try:
            from app.providers import policy as role_policy

            candidate_model = role_policy.get_role_policy(role).primary_model
        except Exception:
            return False
    return byok_route_available(role, provider=active.row.provider, model=candidate_model)


def resolve_byok_execution(
    db: Session, campaign_id: uuid.UUID, *, role: str
) -> "ByokExecution | None":
    """Build the authorized execution context for a generative ``role``.

    The single server-side authorization boundary for production runs:
    resolves the campaign's active policy, verifies the credential offers
    an approved route for ``role`` *before* decrypting (the secret stays
    at rest unless it is actually usable), then binds the decrypted secret
    for server-to-provider transport. Returns None (fail-soft) when there
    is no usable authorized credential — callers fall back to
    funded/provider routing. Never raises into gameplay paths.

    Runtimes must receive ``byok`` only from this resolver (or its
    decision-role sibling); caller-constructed contexts bypass campaign
    authorization and are test-only.
    """
    from app.byok.accounting import ByokExecution
    from app.byok.routing import resolve_generative_route

    try:
        policy = db.get(CampaignByokPolicy, campaign_id)
        if policy is None or not policy.enabled or policy.credential_id is None:
            return None
        row = db.get(ProviderCredential, policy.credential_id)
        if row is None or row.status != CREDENTIAL_STATUS_ACTIVE:
            return None
        try:
            from app.providers import policy as role_policy

            candidate_model = role_policy.get_role_policy(role).primary_model
        except Exception:
            return None
        try:
            resolve_generative_route(role, provider=row.provider, model=candidate_model)
        except ByokError:
            return None
        try:
            secret = decrypt_for_execution(row)
        except ByokError:
            return None
        if not secret:
            return None
        return ByokExecution(
            credential_id=row.id, campaign_id=campaign_id,
            secret=secret, provider=row.provider,
        )
    except Exception:
        return None


def resolve_decision_execution(
    db: Session, campaign_id: uuid.UUID, *, decision_class: str, mode: str = "primer"
) -> "ByokExecution | None":
    """Build the authorized execution context for a decision role/mode.

    Same authorization boundary as :func:`resolve_byok_execution` for the
    decision plane: active policy + exact class/adapter/model/mode approval
    verified before decryption; None (fail-soft) otherwise.
    """
    from app.byok.accounting import ByokExecution
    from app.byok.routing import get_decision_approval, resolve_decision_route

    try:
        policy = db.get(CampaignByokPolicy, campaign_id)
        if policy is None or not policy.enabled or policy.credential_id is None:
            return None
        row = db.get(ProviderCredential, policy.credential_id)
        if row is None or row.status != CREDENTIAL_STATUS_ACTIVE:
            return None
        try:
            approval = get_decision_approval(decision_class)
        except ByokError:
            return None
        try:
            resolve_decision_route(
                decision_class, provider=row.provider,
                model=approval.model, mode=mode,
            )
        except ByokError:
            return None
        try:
            secret = decrypt_for_execution(row)
        except ByokError:
            return None
        if not secret:
            return None
        return ByokExecution(
            credential_id=row.id, campaign_id=campaign_id,
            secret=secret, provider=row.provider,
        )
    except Exception:
        return None

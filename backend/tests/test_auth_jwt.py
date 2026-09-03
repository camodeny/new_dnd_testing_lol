"""Focused security tests for Supabase JWT claim validation."""

from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwk, jwt

from app.auth.errors import AuthError
from app.auth import jwt as auth_jwt


PROJECT_URL = "https://test-project.supabase.co"
EXPECTED_ISSUER = f"{PROJECT_URL}/auth/v1"


def _signing_key(kid: str) -> tuple[bytes, dict]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_jwk = jwk.construct(public_pem, "RS256").to_dict()
    public_jwk["kid"] = kid
    return private_pem, public_jwk


@pytest.fixture
def keys(monkeypatch):
    first_private, first_public = _signing_key("first")
    second_private, second_public = _signing_key("second")
    monkeypatch.setattr(auth_jwt, "SUPABASE_URL", PROJECT_URL)
    monkeypatch.setattr(auth_jwt, "_fetch_jwks", lambda: {"keys": [first_public, second_public]})
    return {"first": first_private, "second": second_private}


def _token(private_key: bytes, kid: str, *, omit: str | None = None, **claim_overrides) -> str:
    claims = {
        "sub": "23f3b2d1-efb6-4785-9a67-fa7ca57d72a3",
        "aud": "authenticated",
        "iss": EXPECTED_ISSUER,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    claims.update(claim_overrides)
    if omit:
        claims.pop(omit)
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": kid})


@pytest.mark.parametrize("kid", ["first", "second"])
def test_accepts_expected_claims_across_advertised_rotation_keys(keys, kid):
    payload = auth_jwt.verify_supabase_jwt(_token(keys[kid], kid))

    assert payload["aud"] == "authenticated"
    assert payload["iss"] == EXPECTED_ISSUER


@pytest.mark.parametrize(
    ("claim_overrides", "omitted_claim"),
    [
        ({"aud": "anon"}, None),
        ({}, "aud"),
        ({"iss": "https://other-project.supabase.co/auth/v1"}, None),
        ({}, "iss"),
    ],
)
def test_rejects_missing_or_unexpected_audience_and_issuer(
    keys, claim_overrides, omitted_claim
):
    token = _token(keys["first"], "first", omit=omitted_claim, **claim_overrides)

    with pytest.raises(AuthError) as exc_info:
        auth_jwt.verify_supabase_jwt(token)

    assert str(exc_info.value) == "Invalid token"


def test_fails_closed_without_configured_supabase_issuer(monkeypatch):
    monkeypatch.setattr(auth_jwt, "SUPABASE_URL", "")
    monkeypatch.setattr(
        auth_jwt,
        "_fetch_jwks",
        lambda: pytest.fail("JWKS must not be fetched without a trusted issuer"),
    )

    with pytest.raises(AuthError) as exc_info:
        auth_jwt.verify_supabase_jwt("untrusted-token")

    assert str(exc_info.value) == "Invalid token"


def test_does_not_expose_jwt_library_errors(keys):
    with pytest.raises(AuthError) as exc_info:
        auth_jwt.verify_supabase_jwt("not.a.jwt")

    assert str(exc_info.value) == "Invalid token"

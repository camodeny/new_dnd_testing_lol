"""Production-safety tests for the mock authentication escape hatch."""

import pytest

from app.auth.service import is_mock_auth_allowed, resolve_profile_pure


def _clear_auth_environment(monkeypatch):
    for name in ("ALLOW_MOCK_AUTH", "NEXT_PUBLIC_MOCK_USER", "VERCEL_ENV", "NODE_ENV"):
        monkeypatch.delenv(name, raising=False)


def test_vercel_production_refuses_mock_auth(monkeypatch):
    _clear_auth_environment(monkeypatch)
    monkeypatch.setenv("VERCEL_ENV", "production")
    monkeypatch.setenv("ALLOW_MOCK_AUTH", "true")

    assert is_mock_auth_allowed() is False


def test_generic_self_hosted_production_refuses_mock_auth(monkeypatch):
    _clear_auth_environment(monkeypatch)
    monkeypatch.setenv("NODE_ENV", "production")
    monkeypatch.setenv("ALLOW_MOCK_AUTH", "true")

    assert is_mock_auth_allowed() is False


def test_local_development_can_explicitly_enable_mock_auth(monkeypatch):
    _clear_auth_environment(monkeypatch)
    monkeypatch.setenv("NODE_ENV", "development")
    monkeypatch.setenv("ALLOW_MOCK_AUTH", "true")

    assert is_mock_auth_allowed() is True


def test_unknown_public_deployment_fails_closed(monkeypatch):
    _clear_auth_environment(monkeypatch)

    assert is_mock_auth_allowed() is False


def test_frontend_mock_flag_does_not_authorize_backend_requests(monkeypatch):
    _clear_auth_environment(monkeypatch)
    monkeypatch.setenv("NODE_ENV", "development")
    monkeypatch.setenv("NEXT_PUBLIC_MOCK_USER", "true")

    assert is_mock_auth_allowed() is False


# --- Expanded coverage for fail-closed semantics ---


def test_production_case_and_whitespace_variants_fail_closed(monkeypatch):
    for prod in ("production", "Production", "PRODUCTION", " production ", "\tproduction\n"):
        for env_name in ("VERCEL_ENV", "NODE_ENV"):
            _clear_auth_environment(monkeypatch)
            monkeypatch.setenv(env_name, prod)
            monkeypatch.setenv("ALLOW_MOCK_AUTH", "true")
            assert is_mock_auth_allowed() is False, f"{env_name}={prod!r} should deny"


def test_allow_flag_parsing_case_and_whitespace(monkeypatch):
    _clear_auth_environment(monkeypatch)
    monkeypatch.setenv("NODE_ENV", "development")
    for truthy in ("1", "true", "TRUE", " True ", "yes", "YES", " yes ", "on", "ON", " on "):
        monkeypatch.setenv("ALLOW_MOCK_AUTH", truthy)
        assert is_mock_auth_allowed() is True, f"{truthy!r} should allow"
    for falsy in ("", "0", "false", "FALSE", " false ", "no", "off", "2", "null"):
        monkeypatch.setenv("ALLOW_MOCK_AUTH", falsy)
        assert is_mock_auth_allowed() is False, f"{falsy!r} should deny"


def test_accidental_public_deployment_with_flag_but_no_metadata_fails_closed(monkeypatch):
    _clear_auth_environment(monkeypatch)
    monkeypatch.setenv("ALLOW_MOCK_AUTH", "true")
    # Neither VERCEL_ENV nor NODE_ENV set -> unknown deployment
    assert is_mock_auth_allowed() is False
    # Explicit empty strings also fail
    monkeypatch.setenv("VERCEL_ENV", "")
    monkeypatch.setenv("NODE_ENV", "")
    assert is_mock_auth_allowed() is False
    # Whitespace-only also fails
    monkeypatch.setenv("NODE_ENV", "   ")
    assert is_mock_auth_allowed() is False


def test_accidental_public_deployment_with_vercel_preview_and_flag(monkeypatch):
    # Vercel preview is non-prod but still public; we allow it only when
    # explicitly in the supported set (preview is supported per docs).
    _clear_auth_environment(monkeypatch)
    monkeypatch.setenv("VERCEL_ENV", "preview")
    monkeypatch.setenv("ALLOW_MOCK_AUTH", "true")
    assert is_mock_auth_allowed() is True
    # Any other unknown vercel value with flag should fail
    _clear_auth_environment(monkeypatch)
    monkeypatch.setenv("VERCEL_ENV", "staging")
    monkeypatch.setenv("ALLOW_MOCK_AUTH", "true")
    assert is_mock_auth_allowed() is False


def test_ci_test_environment_with_flag_allowed(monkeypatch):
    _clear_auth_environment(monkeypatch)
    monkeypatch.setenv("NODE_ENV", "test")
    monkeypatch.setenv("ALLOW_MOCK_AUTH", "true")
    assert is_mock_auth_allowed() is True
    _clear_auth_environment(monkeypatch)
    monkeypatch.setenv("NODE_ENV", "test")
    monkeypatch.setenv("ALLOW_MOCK_AUTH", "1")
    assert is_mock_auth_allowed() is True


def test_vercel_development_with_flag_allowed(monkeypatch):
    _clear_auth_environment(monkeypatch)
    monkeypatch.setenv("VERCEL_ENV", "development")
    monkeypatch.setenv("ALLOW_MOCK_AUTH", "true")
    assert is_mock_auth_allowed() is True


def test_resolve_profile_pure_without_header_requires_explicit_mock_allow(monkeypatch):
    from unittest.mock import MagicMock

    _clear_auth_environment(monkeypatch)
    mock_db = MagicMock()
    with pytest.raises(ValueError, match="Missing Authorization header"):
        resolve_profile_pure(mock_db, None)
    # Even with frontend flag alone, still requires backend flag+env
    monkeypatch.setenv("NEXT_PUBLIC_MOCK_USER", "true")
    monkeypatch.setenv("NODE_ENV", "development")
    with pytest.raises(ValueError, match="Missing Authorization header"):
        resolve_profile_pure(mock_db, None)
    # With proper backend flag+env, mock succeeds
    _clear_auth_environment(monkeypatch)
    monkeypatch.setenv("ALLOW_MOCK_AUTH", "true")
    monkeypatch.setenv("NODE_ENV", "development")
    import app.auth.service as svc

    orig = svc._get_or_create_mock_profile
    svc._get_or_create_mock_profile = lambda db: MagicMock()
    try:
        prof = resolve_profile_pure(mock_db, None)
        assert prof is not None
    finally:
        svc._get_or_create_mock_profile = orig


def test_resolve_profile_pure_invalid_token_rejected_even_when_mock_allowed(monkeypatch):
    from unittest.mock import MagicMock

    _clear_auth_environment(monkeypatch)
    monkeypatch.setenv("ALLOW_MOCK_AUTH", "true")
    monkeypatch.setenv("NODE_ENV", "development")
    mock_db = MagicMock()
    with pytest.raises(ValueError):
        resolve_profile_pure(mock_db, "Bearer invalid-token")
    with pytest.raises(ValueError, match="Invalid Authorization header"):
        resolve_profile_pure(mock_db, "Basic abc")
    with pytest.raises(ValueError, match="Invalid Authorization header"):
        resolve_profile_pure(mock_db, "Bearer")

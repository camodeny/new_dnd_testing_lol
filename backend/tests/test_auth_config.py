"""Production-safety tests for the mock authentication escape hatch."""

from app.auth.service import is_mock_auth_allowed


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

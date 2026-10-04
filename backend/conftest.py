"""Backend pytest hermeticity: pin stub embeddings for the test session.

The backend suite is designed for the offline stub
(``app.world.semantic.resolve_embedding_model`` falls back to ``stub-hash-v1``
when no embedding-provider env vars are set). But ``database.py`` /
``app.factory`` call ``load_dotenv()``, whose find-up logic loads the
repo-root ``.env`` when no ``backend/.env`` exists — and that file sets
``GEMINI_API_KEY``/``GEMINI_EMBEDDING_MODEL``, flipping model resolution to
``gemini-embedding-2`` and breaking stub-assuming tests (e.g. #213 suite).

So test results must not depend on which ``.env`` files happen to exist or
what the shell exports. This strips embedding selection and Jev key vars:

- at import time (before test modules import ``database`` and trigger
  ``load_dotenv()``), so import-time readers see the offline baseline;
- via an autouse fixture (before each test), so ``load_dotenv()`` refills
  and ambient shell exports cannot leak into call-time reads either.

Tests that deliberately exercise the provider path keep working: they use
``monkeypatch.setenv``/``delenv``, which stacks on top of this fixture and
undoes back to the stripped baseline.
"""

from __future__ import annotations

import os

import pytest

# The only env inputs to semantic model resolution
# (backend/app/world/semantic_index.py::resolve_embedding_model).
_PROVIDER_ENV_VARS = (
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_GENAI_API_KEY",
    "GEMINI_EMBEDDING_MODEL",
    # Automatic rules guidance must not call Jev with a developer's live key.
    # Tests of the adapter use explicit fakes or monkeypatch this themselves.
    "TYPESAFE_API_KEY",
)


def _strip_provider_env() -> None:
    for var in _PROVIDER_ENV_VARS:
        os.environ.pop(var, None)


# Import-time strip: conftest imports before test modules, so this runs
# before ``database.load_dotenv()`` refills from a find-up ``.env``.
_strip_provider_env()


# ── Database hermeticity (issue #481) ──────────────────────────────────────
#
# ``database.py`` calls ``load_dotenv()`` and prefers ``POSTGRES_URL``; a
# developer ``backend/.env`` points that at the hosted Supabase database.
# App code opens its own ``SessionLocal()`` in fallback paths (telemetry,
# rules guidance, recovery), so sqlite-fixture tests could reach it. Every DB
# variable is pinned before anything imports ``database``: to the disposable
# test database when one is configured (URL contains ``ci_test``, the existing
# convention), else to empty. Empty still blocks dotenv, which never
# overrides a variable that is already set, so fallbacks become inert.

_DB_ENV_VARS = (
    "POSTGRES_URL",
    "POSTGRES_PRISMA_URL",
    "POSTGRES_URL_NON_POOLING",
    "DATABASE_URL",
    "SUPABASE_DB_URL",
)


def pin_test_database(environ) -> str:
    """Point every DB variable at the disposable test DB (or nothing); return it."""
    disposable = next((environ[v] for v in _DB_ENV_VARS if "ci_test" in (environ.get(v) or "")), "")
    for var in _DB_ENV_VARS:
        environ[var] = disposable
    return disposable


pin_test_database(os.environ)


@pytest.fixture(autouse=True)
def _pin_stub_embeddings(monkeypatch: pytest.MonkeyPatch):
    """Delete provider vars for each test (undoes to prior state after)."""
    for var in _PROVIDER_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    yield


@pytest.fixture(autouse=True)
def _disable_immediate_dm_execution(monkeypatch: pytest.MonkeyPatch):
    """Keep post-response DM execution out of the default test path.

    Submission/start/retry/roll routes dispatch ``execute_committed_attempt``
    after the response. Left enabled, that would open a real ``SessionLocal``
    session (``backend/.env`` points at a live Supabase pooler) for attempts
    that only exist in a test's in-memory DB.

    Tests that deliberately assert dispatch opt back in with
    ``monkeypatch.setenv("DM_EXECUTE_DISPATCH", "1")`` — which stacks on top of
    this fixture, same as ``_pin_stub_embeddings``.
    """
    monkeypatch.setenv("DM_EXECUTE_DISPATCH", "0")
    yield

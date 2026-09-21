"""Backend pytest hermeticity: pin stub embeddings for the test session.

The backend suite is designed for the offline stub
(``app.world.semantic.resolve_embedding_model`` falls back to ``stub-hash-v1``
when no embedding-provider env vars are set). But ``database.py`` /
``app.factory`` call ``load_dotenv()``, whose find-up logic loads the
repo-root ``.env`` when no ``backend/.env`` exists — and that file sets
``GEMINI_API_KEY``/``GEMINI_EMBEDDING_MODEL``, flipping model resolution to
``gemini-embedding-2`` and breaking stub-assuming tests (e.g. #213 suite).

So test results must not depend on which ``.env`` files happen to exist or
what the shell exports. This strips the four provider-selection vars:

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
# (backend/app/world/semantic.py::resolve_embedding_model).
_PROVIDER_ENV_VARS = (
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_GENAI_API_KEY",
    "GEMINI_EMBEDDING_MODEL",
)


def _strip_provider_env() -> None:
    for var in _PROVIDER_ENV_VARS:
        os.environ.pop(var, None)


# Import-time strip: conftest imports before test modules, so this runs
# before ``database.load_dotenv()`` refills from a find-up ``.env``.
_strip_provider_env()


@pytest.fixture(autouse=True)
def _pin_stub_embeddings(monkeypatch: pytest.MonkeyPatch):
    """Delete provider vars for each test (undoes to prior state after)."""
    for var in _PROVIDER_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    yield

"""Deployment entrypoint — instantiates the modular FastAPI application.

All routers are wired in ``app.factory.create_app()``. This file exists only as
the ``uvicorn main:app`` / Vercel entrypoint; it must not accumulate logic.
"""
from app.factory import create_app, lifespan  # noqa: F401  (lifespan re-exported for tests)

app = create_app()

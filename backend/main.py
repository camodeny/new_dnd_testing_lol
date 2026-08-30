"""Thin wiring layer — re-exports the modular application.

All business logic now lives in `app/*` domain modules. This file exists for
backward compatibility (`from main import app`, `uvicorn main:app`, Vercel
entrypoint) and should not accumulate new gameplay logic.

See `app/README.md` for module boundaries and dependency rules.
See `app/factory.py` for the actual FastAPI wiring.
"""
from app.factory import APP_NAME, APP_VERSION, create_app, lifespan  # noqa: F401

# The single FastAPI instance — all routers are mounted in app.factory.create_app().
app = create_app()

# Re-export shared infrastructure for any legacy imports (e.g. scripts).
# New code should import from `database`, `models`, or `app.*` directly.
try:
    from database import Base, engine, get_db  # noqa: F401
    from models import Campaign, CampaignInvite, CampaignMember, Character  # noqa: F401
except Exception:  # pragma: no cover — import-time side effects may fail in tests without DB
    pass

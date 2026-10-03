"""Application factory — single FastAPI instance, modular routers.

This module is the only place that knows about all routers. The deployed
entrypoint ``backend/main.py`` calls ``create_app()``. See ``app/ARCHITECTURE.md``.
"""
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.adventures.router import router as adventures_router
from app.auth.router import router as auth_router
from app.billing.router import router as billing_router
from app.campaigns.routes import router as campaigns_router
from app.campaigns.service import CampaignCommandError
from app.characters.chat.router import router as chat_router
from app.characters.router import router as characters_router
from app.combat.router import router as combat_router
from app.dm.router import router as dm_router
from app.health.router import APP_NAME, APP_VERSION, router as health_router
from app.observability.router import router as observability_router
from app.observability.tracing import TraceMiddleware
from app.post_turn.router import router as post_turn_cron_router
from app.realtime.router import router as realtime_router
from app.rolls.router import router as rolls_router
from app.rules_corpus.router import router as rules_router
from app.snapshot.router import router as snapshot_router
from app.submissions.router import router as submissions_router
from app.threads.router import router as threads_router

load_dotenv()


def create_app() -> FastAPI:
    app = FastAPI(title=APP_NAME, version=APP_VERSION)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(TraceMiddleware)

    @app.exception_handler(CampaignCommandError)
    async def campaign_command_error(request: Request, exc: CampaignCommandError):
        # Same response shape as HTTPException: {"detail": ...}.
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)

    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(billing_router)
    app.include_router(characters_router)
    app.include_router(chat_router)
    app.include_router(campaigns_router)
    app.include_router(combat_router)
    app.include_router(adventures_router)
    app.include_router(submissions_router)
    app.include_router(threads_router)
    app.include_router(snapshot_router)
    app.include_router(observability_router)
    app.include_router(realtime_router)
    app.include_router(dm_router)
    app.include_router(rolls_router)
    app.include_router(rules_router)
    app.include_router(post_turn_cron_router)

    return app

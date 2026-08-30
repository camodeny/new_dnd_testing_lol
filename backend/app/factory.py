"""Application factory — single FastAPI instance, modular routers.

This module is the only place that knows about all routers; domain/application
modules do not import each other. The deployed entrypoint `backend/main.py`
re-exports `app` from here for backward compat (`from main import app`).
"""
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()


APP_NAME = "dnd-backend"
APP_VERSION = "0.1.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Migrations are applied explicitly via `alembic upgrade head` or
    # `python -m scripts.migrate` — never during application startup.
    # See backend/README.md and backend/scripts/migrate.py (from #187).
    yield


def create_app() -> FastAPI:
    app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Import routers lazily to avoid import cycles at module load time
    # if domain modules ever import factory for typing.
    from app.auth.router import router as auth_router
    from app.campaigns.router import router as campaigns_router
    from app.characters.chat.router import router as chat_router
    from app.characters.router import router as characters_router
    from app.health.router import router as health_router
    from app.runtime.router import router as runtime_router
    from app.world.router import router as world_router

    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(characters_router)
    app.include_router(chat_router)
    app.include_router(campaigns_router)
    app.include_router(world_router)
    app.include_router(runtime_router)

    return app


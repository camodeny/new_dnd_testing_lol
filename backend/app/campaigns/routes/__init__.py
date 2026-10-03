"""Campaign HTTP routes, aggregated into one router for the app factory."""

from fastapi import APIRouter

from app.campaigns.routes import core, invites, lobby, lore, members, party

router = APIRouter()
for _module in (core, lobby, members, lore, invites, party):
    router.include_router(_module.router)

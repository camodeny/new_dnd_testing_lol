"""Campaign HTTP routes, aggregated into one router for the app factory."""

from fastapi import APIRouter

from app.campaigns.routes import core, invites, lobby, lore, members, notes, party

router = APIRouter()
for _module in (core, lobby, members, lore, invites, notes, party):
    router.include_router(_module.router)

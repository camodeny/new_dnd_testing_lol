# Backend application modules

One FastAPI application (`backend/main.py` -> `app/factory.py`) over one Postgres schema
(`backend/models/`, Alembic). The package map, the turn flow, the async model, and
where auth/visibility/dice authority live are in [`ARCHITECTURE.md`](ARCHITECTURE.md).

## Rules

- **Services do not import `fastapi`.** Pure helpers take plain values and raise domain
  errors. Transport adapters (`*/router.py`, `campaigns/routes/`, `deps/`) map those errors
  to HTTP. Example: `app/auth/service.py` (pure) vs `app/deps/auth.py` (transport).
  Campaign commands raise `CampaignCommandError` (status + detail), which `app/factory.py`
  maps to the standard `{"detail": ...}` shape.
- **Provider adapters stay isolated.** Gameplay code imports from `app.providers`
  (`stream_chat`, `execute_chat`, `ProviderRequest`, `policy`). It never branches on
  provider names.
- **Import lower layers at module top.** Use a function-level import only to break a
  genuine package cycle; `ARCHITECTURE.md` lists the ones that remain.
- **One deployable.** All routers mount on the single `FastAPI` instance in `app/factory.py`.

## Adding a route

1. Put the logic in `app/<domain>/service.py` (plain functions, no FastAPI).
2. Expose it from `app/<domain>/router.py`.
   - Campaign-scoped routes use `app.deps.campaign.campaign_for(...)` to load and authorize.
   - Idempotent, revision-guarded commands go through `run_campaign_command`.
3. Register the router in `app/factory.py:create_app()`.

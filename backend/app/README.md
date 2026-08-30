# Backend Application Modules

This directory defines the **modular monolith** layout for the production runtime.
There is still **one FastAPI application** (`backend/main.py` / `app/factory.py`) and
one shared Postgres/database layer (`backend/database.py`, `backend/models.py`).

## Module responsibilities

| Module | Responsibility |
|---|---|
| `app/campaigns` | Campaign aggregate: creation, membership, invites, validation |
| `app/characters` | Character aggregate + sheet persistence |
| `app/characters/chat` | Character-creator chat (SSE) — streams via provider abstraction |
| `app/world` | World / knowledge / memory read stubs (campaign world + encounter maps) |
| `app/runtime` | Turn/session orchestration stubs (live-table sessions) |
| `app/rules` | Rules / combat pure domain (no I/O, no FastAPI) — placeholder for Epic 2 |
| `app/visibility` | Fog / visibility calculations — placeholder |
| `app/repair` | Consistency / repair workflows — placeholder |
| `app/billing` | Billing / entitlement checks — placeholder |
| `app/realtime` | Realtime projections / websocket fan-out — placeholder |
| `app/providers` | LLM provider adapters — re-exports `llm_providers` without workflow branching |
| `app/observability` | Structured logging, tracing hooks, TTFT helpers (see #192) |
| `app/health` | Health / hello / db ping |
| `app/auth` | Auth config + `/api/me` (Supabase JWT -> profile) |
| `app/deps` | Shared FastAPI dependencies (e.g. `resolve_profile`) |
| `app/infrastructure` | Thin re-export / documentation for the shared DB layer |

## Dependency direction

```
transport (app/*/router.py, FastAPI)
   -> application (app/*/service.py, app/deps/*)
   -> domain (app/rules/*, models.py pure helpers, value objects)
   -> infrastructure (database.py, providers, observability)
```

Rules:
- **Domain / application MUST NOT import `fastapi`** (`Request`, `APIRouter`, `HTTPException` is allowed only at the application boundary; pure domain helpers take plain values).
- **Provider adapters remain isolated**: gameplay workflows use `app.providers` / `provider_registry` and `stream_chat`/`execute_chat` abstractions; they never `if provider == "openrouter"` branch (see `llm_providers.py:1`).
- **One deployable**: no new services or ports; all routers are mounted on the single `FastAPI` instance in `app/factory.py`.
- New gameplay modules must be importable without importing any `router` module (no circular `router -> service -> router`).

## Adding a new gameplay route

1. Add domain logic to `app/<domain>/service.py` (plain functions, no FastAPI).
2. Expose it via `app/<domain>/router.py` (`APIRouter`, dependency injection, HTTP mapping).
3. Register the router in `app/factory.py:create_app()`.

## Rollback

Moves are import-safe: `backend/main.py` re-exports `app` for backward compat (`from main import app` still works). If a module move breaks, revert the `include_router` line and restore the function to `main.py` — no data migration required.

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
| `app/auth` | Auth config + `/api/me` (transport) + `app/auth/service.py` pure profile resolution + `app/auth/jwt.py` JWT/JWKS verification (application) |
| `app/deps` | Transport adapters that extract `Request` headers and map errors to HTTP (e.g. `resolve_profile(Request) -> Profile`) |
| `app/infrastructure` | Thin re-export / documentation for the shared DB layer |

## Dependency direction

```
transport (app/*/router.py, app/deps/*, FastAPI Request/APIRouter/HTTPException)
   -> application (app/*/service.py, app/auth/service.py, app/campaigns/service.py)
   -> domain (app/rules/*, models.py pure helpers, value objects)
   -> infrastructure (database.py, app/auth/jwt.py verify_supabase_jwt, providers, observability)
```

Rules:
- **Domain / application MUST NOT import `fastapi`** (`Request`, `APIRouter`, `HTTPException`). Pure helpers take plain values (e.g. `auth_header: str | None`, `token: str`) and raise `app.auth.errors.AuthError` (a `ValueError`); transport adapters translate `AuthError` → `HTTPException`. See `app/auth/service.py` (pure) vs `app/deps/auth.py` (transport).
- **Provider adapters remain isolated**: gameplay workflows import from `app.providers` (`provider_registry`, `stream_chat`, `ProviderRequest`) and never branch on provider names or import `llm_providers` directly. `app.providers` is the mock seam for tests (see `app/characters/chat/service.py:118`).
- **One deployable**: no new services or ports; all routers are mounted on the single `FastAPI` instance in `app/factory.py`.
- New gameplay modules must be importable without importing any `router` or `app/deps/*` module (no circular `router -> service -> router`).

## Adding a new gameplay route

1. Add domain logic to `app/<domain>/service.py` (plain functions, no FastAPI).
2. Expose it via `app/<domain>/router.py` (`APIRouter`, dependency injection, HTTP mapping).
3. Register the router in `app/factory.py:create_app()`.

## Rollback

Moves are import-safe: `backend/main.py` re-exports `app` for backward compat (`from main import app` still works). If a module move breaks, revert the `include_router` line and restore the function to `main.py` — no data migration required.

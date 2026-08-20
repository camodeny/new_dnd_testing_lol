# Backend (FastAPI + SQLAlchemy + Alembic + Supabase)

## Env
Copy `.env.example` to `.env` locally. On Vercel the Supabase integration injects `DATABASE_URL`/`POSTGRES_URL`, `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_JWT_SECRET` automatically.

```
# local .env
DATABASE_URL=postgresql://postgres.<ref>:[pass]@aws-0-...pooler.supabase.com:6543/postgres?sslmode=require
SUPABASE_URL=https://<ref>.supabase.co
SUPABASE_ANON_KEY=eyJ...
SUPABASE_JWT_SECRET=...
```

## DB + Alembic

Alembic is configured in `alembic/env.py` to read `DATABASE_URL`/`POSTGRES_URL`/`POSTGRES_PRISMA_URL`/`SUPABASE_DB_URL` (same as `database.py`).

```bash
# create a new migration after editing models.py
alembic revision --autogenerate -m "add campaigns"

# preview SQL without touching DB
alembic upgrade --sql head

# apply to Supabase (needs DATABASE_URL set)
alembic upgrade head

# check status / history
alembic current
alembic history
```

Initial migration `001_create_profiles` creates `public.profiles` mirroring `auth.users`. It matches `schema.sql` for manual use in Supabase SQL Editor.

**Deploys are self-migrating:** `main.py:lifespan` calls `_run_migrations()` on every cold start (`alembic upgrade head` via `alembic.ini`). No manual `alembic upgrade head` needed — Vercel just needs `DATABASE_URL`/`POSTGRES_URL` in Runtime env. If Alembic fails, it falls back to `Base.metadata.create_all()` and logs a warning. For local dev you can still run `alembic upgrade head` manually.

## Auth
`auth.py` verifies Supabase JWT via JWKS (`{SUPABASE_URL}/auth/v1/.well-known/jwks.json`) with `HS256` fallback. Frontend sends `Authorization: Bearer <supabase access_token>` — see `frontend/lib/supabase.ts`.

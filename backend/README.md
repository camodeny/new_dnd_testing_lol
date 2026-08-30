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

### Local development

```bash
# create a new migration after editing models.py
alembic revision --autogenerate -m "add campaigns"

# preview SQL without touching DB
alembic upgrade --sql head

# apply to Supabase/local DB (needs DATABASE_URL set)
alembic upgrade head
# or: python -m scripts.migrate

# check status / history
alembic current
alembic history
```

Initial migration `001_create_profiles` creates `public.profiles` mirroring `auth.users`. It matches `schema.sql` for manual use in Supabase SQL Editor.

### Production deploys (Vercel + Supabase)

Application startup **never** runs migrations or `Base.metadata.create_all()` — see `main.py:lifespan`.

Deploys must apply migrations explicitly **before** serving traffic. This is wired as a single controlled gate before Vercel production promotion (previews do not race):

- **Vercel gate:** `vercel.json` sets `backend.buildCommand` to `bash scripts/vercel-migrate.sh`. That script checks `VERCEL_ENV=production` — only production runs `python -m scripts.migrate`; preview/development skip. A non-zero exit fails the Vercel build, preventing promotion.
- **GitHub gate:** `.github/workflows/backend-migrate.yml` runs the same `python -m scripts.migrate` on `push` to `main` (with `backend/**` changes) against the production DB and includes a forced-failure verification that the release path stops. It also verifies `vercel-migrate.sh` gates correctly.

Manual equivalent:

```bash
# from backend/ with production DATABASE_URL / POSTGRES_URL set
python -m scripts.migrate
# equivalent: alembic upgrade head

# verify
alembic current
```

`scripts/migrate.py` logs the `alembic_version` before and after, and exits non-zero on failure with full traceback. A failed migration leaves the previous app/DB state intact (migrations run in a transaction) and the deploy is treated as unhealthy — Vercel build fails or the GitHub `production-migrate` job fails, blocking promotion. No fallback to `create_all()` is performed in production. Multiple API instances never attempt DDL concurrently because DDL only runs in this single deploy step (Alembic also acquires a row lock on `alembic_version` if run concurrently).

CI example:

```bash
DATABASE_URL="$POSTGRES_URL" python -m scripts.migrate || exit 1
# only then deploy to Vercel / restart containers
```

## Auth
`auth.py` verifies Supabase JWT via JWKS (`{SUPABASE_URL}/auth/v1/.well-known/jwks.json`) with `HS256` fallback. Frontend sends `Authorization: Bearer <supabase access_token>` — see `frontend/lib/supabase.ts`.

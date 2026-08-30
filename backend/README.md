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
- **GitHub verification:** `.github/workflows/backend-migrate.yml` proves cold starts perform no DDL, forced migration failures return non-zero, and the Vercel gate is wired correctly. It never runs against production, so there is only one production DDL invocation.

Manual equivalent:

```bash
# from backend/ with POSTGRES_URL_NON_POOLING (preferred) or another DB URL set
python -m scripts.migrate
# equivalent: alembic upgrade head

# verify
alembic current
```

`scripts/migrate.py` prefers `POSTGRES_URL_NON_POOLING`/`SUPABASE_DB_URL` over pooled runtime URLs, logs the `alembic_version` before and after, and exits non-zero on failure with full traceback. A failed migration leaves the previous app/DB state intact (migrations run in a transaction) and the Vercel build fails, blocking promotion. No fallback to `create_all()` is performed in production. Multiple API instances never attempt DDL because migrations only run in this single deploy step.

CI example (also see `.github/workflows/ci.yml` and `scripts/ci/`):

```bash
# Equivalent to CI — run locally to reproduce failures without GitHub Actions
./scripts/ci/backend.sh                    # lint + unit/integration tests (no DB)
DATABASE_URL=postgresql://ci_test:ci_test@localhost:5432/ci_test ./scripts/ci/backend.sh  # with disposable Postgres + migrations
./scripts/ci/frontend.sh                   # lint + typecheck + build

# Direct equivalents
DATABASE_URL="$POSTGRES_URL" python -m scripts.migrate || exit 1
ALLOW_MOCK_AUTH=true python -m pytest tests/ -v
# only then deploy to Vercel / restart containers
```

## CI

` .github/workflows/ci.yml` is the required PR CI (jobs `backend`, `frontend`). It provisions a disposable Postgres service, runs `python -m scripts.migrate` against a clean DB, then `pytest`, and runs `eslint`/`tsc`/`next build`. Branch protection should require `backend` + `frontend`.

Canonical local commands mirror CI: `scripts/ci/backend.sh` and `scripts/ci/frontend.sh` — workflow delegates to these scripts so local and CI use the same logic.

Heavier suites (#267 one-shot, #270 fault-injection, #273 combat regression) should be added as separate `workflow_dispatch` / `schedule` jobs or new workflows reusing the same `setup-python`/`setup-node`/postgres service conventions — do not add paid model calls or production credentials to the required PR path.

## Auth
`auth.py` verifies Supabase JWT via JWKS (`{SUPABASE_URL}/auth/v1/.well-known/jwks.json`) with `HS256` fallback. Frontend sends `Authorization: Bearer <supabase access_token>` — see `frontend/lib/supabase.ts`.

# Local development and gameplay testing

Local development runs against a local Supabase stack (Postgres, Auth,
Realtime, Studio, and a mail catcher in Docker), never the hosted project.
Test accounts, campaigns, and turns stay on your machine.

## One-time setup

1. Install and start Docker Desktop.
2. Fill in `backend/.env` and `frontend/.env.local` from the `.env.example`
   files. Only the non-Supabase values matter here (LLM provider keys, etc.);
   the script below writes the Supabase ones.

## Start the stack

```bash
scripts/local-supabase.sh up
```

This:

- starts the stack (`supabase/config.toml`; the CLI runs through `npx`, no
  install needed),
- points `backend/.env` and `frontend/.env.local` at it. The first run saves
  the hosted versions as `backend/.hosted.env` and `frontend/.hosted.env`
  (gitignored); copy them back to point at the hosted project again,
- applies the Alembic schema (`python -m scripts.migrate`),
- schedules the production pg_cron sweeps (DM execute, post-turn, adventure
  closing) against the backend on this machine, so post-turn world updates and
  recovery run locally as they do in production,
- seeds four test players: `player1@local.test` … `player4@local.test`,
  password `password`.

Then restart the backend (`uvicorn main:app --reload --port 8000` in
`backend/`) and frontend (`npm run dev` in `frontend/`) so they load the new
env. It is safe to re-run `up`. Run it from the main checkout:
`scripts/sync-env.sh` copies the env files into worktrees, and every worktree
shares the one local stack.

Other commands:

| Command | Effect |
| --- | --- |
| `scripts/local-supabase.sh down` | Stop the stack. Data survives. |
| `scripts/local-supabase.sh reset` | Wipe the local database, accounts included, then `up` again. |
| `scripts/local-supabase.sh status` | Print local URLs and keys. |

Studio (table browser, SQL editor) is at http://127.0.0.1:54323. Every email
the stack sends, such as sign-up confirmations, lands in Mailpit at
http://127.0.0.1:54324.

## Playing a multiplayer session on one machine

On the local stack, `next dev` keeps each browser tab's sign-in separate, and
the login page shows **Local test players** buttons. Open one tab per player
and click a different player in each. Typical loop:

1. Tab 1, Player 1: create a campaign and copy its invite link.
2. Tabs 2–4: open the invite link and pick Players 2–4.
3. Everyone makes or picks a character and readies up; Player 1 starts.

You can still create other accounts with the normal sign-up form.
Confirmation is off locally, so they sign in immediately.

Against a hosted project, or in a production build, none of this applies:
sessions are shared across tabs and the test-player buttons are hidden.

## How auth works

There is no mock or dev auth bypass anywhere in this repo. Every backend
request bears a real Supabase JWT, including locally:

- The frontend signs in through Supabase (`frontend/lib/supabase.ts`,
  `frontend/hooks/useAuth.ts`) and sends
  `Authorization: Bearer <access_token>`.
- The backend verifies the JWT via JWKS (`backend/app/auth/`) and upserts the
  profile on first request. Unauthenticated requests get 401.
- The local stack signs tokens with ES256 like hosted Supabase, using the
  committed, local-only key `supabase/signing_keys.json`. Only the local
  stack trusts that key. Never import it into a hosted project.
- Realtime private channels need a real session. If the socket is down,
  `useLiveTableRealtime` falls back to 5s snapshot polling.

Mock auth was removed because it was a second auth universe: a fixed profile
UUID with no Supabase session behind it. Anything keyed on real auth silently
degraded under it. Do not reintroduce `ALLOW_MOCK_AUTH`,
`NEXT_PUBLIC_MOCK_USER`, `is_mock_auth_allowed`, or a fixed mock profile.

## Tests

Backend tests never use auth env vars. They monkeypatch `resolve_profile`
per router (`app.deps.auth.resolve_profile` for routes using the
`current_profile` dependency) and use `TEST_USER_ID`
(`backend/app/auth/service.py`) as plain fixture data. It grants nothing.

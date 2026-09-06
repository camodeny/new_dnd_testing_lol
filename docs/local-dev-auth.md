# Local dev auth (mock auth removed)

There is no mock/dev auth bypass anywhere in this repo. Every backend
request must bear a real Supabase JWT, including local development.

## Why no mock

Mock auth was removed because it was a second auth universe: a fixed
profile UUID with no Supabase session behind it. Anything keyed on real
auth silently degraded under it — realtime private channels could never
connect (no session for the broadcast socket), and RLS-gated reads would
be next. Pre-alpha with no users, one canonical auth path is strictly
better. See `AGENTS.md` (no fallbacks/legacy shims rule).

Removed (do not reintroduce): `ALLOW_MOCK_AUTH`, `NEXT_PUBLIC_MOCK_USER`,
`is_mock_auth_allowed`, the fixed mock profile.

## Current behavior

- Frontend signs in via Supabase (`frontend/lib/supabase.ts`,
  `frontend/hooks/useAuth.ts`) and sends
  `Authorization: Bearer <supabase access_token>`.
- Backend verifies the JWT via JWKS (`backend/app/auth/`) and upserts the
  profile on first login. Unauthenticated requests get 401.
- Realtime private channels require a real session. If the socket is down,
  `useLiveTableRealtime` falls back to 5s snapshot polling (degraded but
  usable) — that fallback is prod hardening, not a mock replacement.

## Dev setup

1. Supabase Dashboard > Authentication > Users > create a dev user (email +
   password). Enable email/password sign-in for the project if needed.
2. `backend/.env`: Supabase keys + provider keys (see
   `backend/.env.example`). No auth vars needed.
3. `frontend/.env.local`: Supabase public keys + `BACKEND_URL` pointing at
   the local backend (see `frontend/.env.example`).
4. Optional: put the dev user's email/password in
   `NEXT_PUBLIC_DEV_USER_EMAIL` / `NEXT_PUBLIC_DEV_USER_PASSWORD` in
   `frontend/.env.local` to auto-sign-in during local `next dev` and skip
   the login form. Ignored in production builds even if present — never set
   these vars in a deployed environment (values bake into the client
   bundle).
5. Sign in through the app UI (or rely on auto-login). First login creates
   the profile row.

## Legacy mock-owned rows

Rows created under the old mock UUID
`23f3b2d1-efb6-4785-9a67-fa7ca57d72a3` belong to no real user. Transfer or
recreate them, e.g.:

```sql
UPDATE campaigns SET owner_id = '<real-user-uuid>'
 WHERE owner_id = '23f3b2d1-efb6-4785-9a67-fa7ca57d72a3';
```

## Tests

Backend tests never use auth env vars: they monkeypatch `resolve_profile`
per router and use `TEST_USER_ID` (`backend/app/auth/service.py`) as plain
fixture data. It grants nothing.

# CLI validation — 2026-10-02

Validated against this worktree's FastAPI backend on `127.0.0.1:8020`,
using real Supabase email/password sessions and PostgreSQL. Fixtures used
one disposable campaign, an owner account, and two separate player accounts.

## Live checks passed

- Login, identity, campaign/character discovery, invitation acceptance,
  character selection, and readiness.
- Owner observation refusal and separate player identities.
- Observations, ordinary wait timeout, and waking one waiting player when
  another submits a visible message.
- Concurrent public submissions and exact idempotent replay.
- AI-DM/private player thread creation; another player's private submission
  neither appeared in public history nor changed the observer's change token.
  Direct access to that private thread returned 404.
- Completed real AI-DM narration persisted in the player observation.
- Controlled encounter setup through the owner API, player initiative rolls,
  map reads, reachable cells, movement, and ending a turn. Retrying rolls,
  movement, and end-turn commands preserved their original results.

## Bug found and fixed

The final initiative roll initially returned HTTP 500 on PostgreSQL.
`_maybe_mark_ready` updated the readiness event's payload after inserting it,
violating the database's immutable domain-event trigger. Removed the unused
post-insert event update. The saved failed roll then succeeded with its original
operation ID and dice after restarting the backend.

The existing HTTP initiative-fulfillment test now installs an equivalent
SQLite immutability trigger. It reproduced the failure before the fix and
passed afterward.

## Automated checks

- CLI syntax checks and all 14 CLI tests passed.
- 177 backend tests passed across targeted snapshot, submissions, rolls,
  threads/privacy, readiness, encounter, turn, geometry, and deterministic
  end-to-end gameplay suites; one test was skipped.
- All 96 affected combat/roll/geometry tests passed again after the fix.
- `git diff --check` passed.

This was a bounded scripted API/CLI playtest. Browser rendering and sustained
autonomous Muse sessions were not exercised. Live provider logs also emitted
`ledger_charge_dropped` with `AmbiguousCostError`; accounting was not validated
by this CLI test.

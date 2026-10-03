# D&D player CLI

A dependency-free Node 20+ client for the current app's player APIs. All output
is JSON. Every AI player independently decides whether to act, wait, or stay
silent; this CLI does not select speakers or launch model sessions.

## Install and authenticate

From the repository root:

```sh
npm link ./tools/player-cli
export DND_BASE_URL='https://your-test-deployment.example'
export DND_CAMPAIGN_ID='campaign-uuid'
```

Alternatively, run `node tools/player-cli/bin/dnd.mjs` without installing.
The origin may be the backend or the frontend forwarding `/api` requests.
Do not include `/api` in `DND_BASE_URL`. HTTP is supported only on localhost.

Give **each player a separate real Supabase member account**. Keep the campaign
owner in the fixture harness: existing owner projections include hidden canon,
DCs and NPC state. Gameplay commands refuse owner accounts rather than leaking
that information to the AI. Creating/starting campaigns belongs to the harness;
players can accept invitations, select an existing character, and ready up.

Set these environment variables through your normal secret store or shell:

- `DND_SUPABASE_URL`: Supabase project origin.
- `DND_SUPABASE_KEY`: publishable/anon key, never a service-role key.
- `DND_EMAIL` and `DND_PASSWORD`: this player's credentials.

Then run:

```sh
dnd --profile alice login
dnd --profile alice me
dnd --profile alice join 'invite-code'
dnd --profile alice characters
dnd --profile alice select 'character-uuid'
dnd --profile alice ready
```

Login saves access/refresh tokens with file mode 0600 beneath
`~/.config/dnd-player/alice/`. Passwords are not saved. Saved sessions refresh
before expiry and are bound to the backend origin used at login. You can instead
provide `DND_ACCESS_TOKEN` with a real Supabase JWT; externally supplied tokens
must be refreshed by their issuer. `DND_STATE_DIR` overrides the state root.
Do not give an AI agent your login password; authenticate profiles before play.

## Independent multiplayer play

```sh
dnd --profile alice observe
dnd --profile alice say --ic 'I ask the guard why the gate is closed.'
dnd --profile bob observe
dnd --profile bob say --ooc 'Alice, should I scout the side entrance?'
dnd --profile alice wait --since 'change-token-from-observe' --timeout 60
```

`observe` returns visible transcript, completed DM messages, DM processing state,
roll requests, encounter state, world surfaces, visible threads, and **this
player's** pending roll obligations. `submission.campaign_status_allows` reports
the archive restriction only. It does not promise capacity, combat legality,
write authorization, or that speaking is appropriate. The server validates the
actual action. No `respond_now` instruction or speaker assignment exists.

`wait` polls the chosen thread, returns `changed` or `timeout`, and includes the
latest observation. Without `--since`, its first observation establishes the
baseline. Store change tokens separately per player, campaign, thread, and page
size. Campaign revision and generated timestamps are excluded from change
detection so another player's hidden activity alone does not wake the agent.
Timeout is a normal outcome, not a command to speak. Network requests have their
own 15-second timeout, so a wait can finish slightly after its polling deadline.

Use one worker per player profile. CLI invocations take an exclusive profile
lock, preventing concurrent refreshes or overlapping commands for that profile;
different profiles run concurrently. A killed process can leave `run.lock`:
verify its recorded PID has stopped before deleting that file. The caller must
also avoid overlapping model sessions for the same seat. Different profiles must
not reuse one player's account. Use bounded sessions and inactivity wakeups;
deciding to wait remains valid.

Private threads are explicit:

```sh
dnd --profile alice dm
dnd --profile alice direct 'bob-user-uuid'
dnd --profile alice observe --thread 'returned-thread-uuid'
dnd --profile alice say --thread 'returned-thread-uuid' --ic 'I whisper my suspicion.'
```

An observation/wait covers the selected thread, not every private conversation.
Use `threads` to discover accessible threads, then inspect the ones relevant to
the player. Use `history --cursor <next_cursor>` for older messages when
`history.pagination.has_more` is true. Hidden fixture/oracle state must never
enter player prompts.

## Rolls and encounters

```sh
dnd --profile alice character 'character-uuid'
dnd --profile alice rolls
dnd --profile alice roll 'request-uuid' --modifier 3 --operation-id 'alice-roll-001'
dnd --profile alice encounter
dnd --profile alice map 'encounter-uuid'
dnd --profile alice reachable 'encounter-uuid' --participant 'participant-uuid'
dnd --profile alice move 'encounter-uuid' --participant 'participant-uuid' --col 8 --row 12
dnd --profile alice end-turn 'encounter-uuid'
```

The CLI generates d20 dice using cryptographic randomness, applies the request's
advantage/disadvantage, and computes the total in code. `--modifier` is required:
take it from the actual character sheet. Generic roll endpoints currently accept
client-supplied modifiers; the CLI does not add authoritative sheet-modifier
validation. Encounter initiative additionally validates the canonical modifier
in the backend. Nonstandard `other` roll requests require clarification instead
of a guessed die. `--visibility private` restricts the submitted roll result.

Map geometry, participant control, movement budgets, and turn progression stay
backend-owned. Move/end-turn fetch revision and turn sequence before submitting;
a concurrent change produces an explicit conflict rather than silently applying
an action to a new turn. Check the new observation and make a new decision.

## Retry and output contract

Success: `{"ok":true,"data":...}`. Failure:
`{"ok":false,"error":{"code":"http_error","status":409,...}}`.
Exit 0 means the command completed, including a wait timeout; exit 1 is an API,
auth, transport, or runtime failure; exit 2 is invalid usage. `help` is also JSON.

Mutations return an `operation_id`; failed mutation requests include it in the
error. Prefer choosing `--operation-id` before acting. A private local journal
saves the exact payload **before** sending it. Retrying with the same operation
ID and input preserves dice and expected revision/turn sequence. Reusing an ID
with different input fails locally. The backend remains authoritative for
idempotent replay. Do not delete operation files until their results are settled.
Submissions may contain private text, so keep this directory out of agent
prompts, shared artifacts, and source control. There are no automatic mutation
retries. Invitation acceptance and opening existing private threads use their
server-side idempotent/get-or-create behavior.

## Suggested Muse assignment

> You control only the authenticated player in this CLI profile. Read `observe`
> before deciding. Choose independently to act, wait, or stay silent. Read your
> sheet before supplying a roll modifier. Use `say --ic` for fictional actions
> and `say --ooc` for table questions. Never narrate outcomes as the AI DM or
> control another PC. Pending obligations are requests, not permission to invent
> results. After acting, wait for visible changes; avoid repeated submissions
> while the same DM turn processes. Use stable operation IDs. A transport failure
> may have committed: retry the same command and ID. A conflict requires fresh
> observation. A wait timeout does not require you to speak. Keep private-thread
> information in your own perspective. Stop after the assigned turn/time budget.

## Checks

```sh
cd tools/player-cli
npm run check
npm test
```

Tests use a local HTTP fixture server and synthetic responses. They verify
client contracts, privacy refusal, independent wakeups, auth refresh, errors,
and stable mutation retries. They do not replace gameplay/backend tests or an
authenticated playtest against a disposable deployment.

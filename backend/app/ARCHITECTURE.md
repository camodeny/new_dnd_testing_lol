# Backend architecture

One FastAPI app (`app/factory.py`, served by `main.py`), one Postgres schema
(`models/`, Alembic in `alembic/`). The AI is the only DM.

## Packages, in layer order

Lower layers never import higher ones at module load. Remaining package-level
cycles are listed at the end.

**Foundation**
- `schema.py`: `StrictModel` base plus `coerce_uuid` / `coerce_optional_uuid`.
- `clock.py`: UTC clock helpers (`utcnow`, `ms_between`).
- `idempotency.py`: durable, actor-scoped idempotent command execution.
- `observability/`: request tracing middleware, structured logs, AI run ledger, `/traces/{id}` debug endpoint.
- `worker/`: job envelope plus the `WorkerExecution` idempotency/retry ledger (`execute_worker_job`) that the cron sweeps use.
- `providers/`: LLM adapters, per-area provider/model pinning, role failover policy, `runner.AiRunLedger`.
- `billing/`: campaign capacity ledger, AI spend charging, Stripe funding, resolution guarantee.
- `decisions/`: bounded decision runtime. Models choose only among candidates that code supplies.
- `visibility/`: disclosure vocabulary (`policy`) and per-record receive checks (`access.may_user_receive`).
- `auth/`: Supabase JWT/JWKS verification (`jwt`) and profile resolution (`service`).
- `deps/`: FastAPI dependencies: `current_profile`, `campaign_for(role=...)`, `run_campaign_command`, HTTP idempotency, cron secret guard.

**Domain state**
- `characters/`: characters, sheets (`service.latest_sheet`), and the character-creator chat.
- `rules/`: the deterministic 5e engine (mechanics, checks/saves, attacks, spells, rules-state). No FastAPI.
- `rules_corpus/`: SRD rules-text ingest, embeddings, hybrid search, `/api/rules/*`.
- `threads/`: shared/private campaign threads, membership, read/write authorization.
- `campaigns/`: the campaign aggregate. `events.commit_campaign_mutation` is the single revision-ordered write path. Also members, invites, lobby, lore, party, lifecycle/start, world seed, and HTTP `routes/`.
- `world/`: entities, scene, facts/relations, knowledge, clocks, NPC state, identity, retrieval, semantic index.

**Gameplay**
- `combat/`: encounter lifecycle, initiative, turn economy, maps/geometry, ending.
- `rolls/`: player-owned roll requests and fulfillment.
- `adventures/`: adventure lifecycle, summaries/recaps, epilogues, closing jobs.
- `submissions/`: player submission acceptance and DM turn coordination entry.
- `dm/`: the turn spine. Covers turns/attempts, context, contract, adjudication, evidence (+ `tools/`), validators, narration, streams, effects, recovery, and the cron sweep.
- `post_turn/`: the durable post-turn checkpoint, materialization, consistency incidents, and backpressure.

**Transport / read models**
- `realtime/`: audience-safe Supabase Realtime broadcasts and channel authorization.
- `snapshot/`: the reconnect-safe live-table read model.
- `health/`, `factory.py`: liveness and app wiring.

Routers live inside each package (`*/router.py`, `campaigns/routes/`). Services do not import FastAPI.

## Player action -> DM turn -> commit -> post-turn

The authoritative walkthrough is the module docstring of `dm/execution.py`. In short:

1. `POST /api/campaigns/{id}/submissions` (`submissions/router.py`) accepts input.
   `dm.turns.coordinate_turn` groups unresolved input into a turn with one prepared attempt.
2. The response schedules `dm.recovery.execute_committed_attempt` as a FastAPI
   `BackgroundTasks` task. Campaign start, roll fulfillment and `/retry` do the same.
3. `dm.execution.execute_dm_attempt` claims the attempt and assembles context (`dm.context`).
   It adjudicates with failover inside the bounded evidence loop, then validates.
   Next it dispatches on the contract mode: `await_roll` goes to `rolls.service`, `silent` commits.
4. For `respond`, `dm.narration.execute_validated_turn` streams durable narration chunks.
   It then calls `dm.turns.commit_turn`, which applies `dm.effects` inside
   `campaigns.events.commit_campaign_mutation` (revision bump + domain event).
5. The same mutation stages a `PostTurnRun` via `post_turn.service.maybe_trigger_post_turn`.
   `/api/cron/post-turn` materializes world state, evaluates clocks, and verifies consistency.
   Only after that does it advance the checkpoint.

## Async model

There is no queue and no outbox relay. Work runs in one of two ways:

- **FastAPI `BackgroundTasks`**: post-response DM execution (`execute_committed_attempt`).
- **Supabase pg_cron** (`scripts/supabase/schedule_*.sql`): authenticated by `deps.cron.require_cron_secret`.
  - `/api/cron/dm-execute`: `dm.execution.run_dm_execute_sweep`. It recovers stuck claims, then runs the oldest prepared attempts the background task missed.
  - `/api/cron/post-turn`: `post_turn.service.run_post_turn_sweep`. On the same tick it runs `world.semantic_index.run_semantic_index_sweep` to drain pending semantic-index rows.
  - `/api/cron/adventure-closing`: `adventures.service.run_adventure_closing_sweep`.
    It consumes `Outbox` rows of type `adventure.closing`.
    Completion stages those rows in the same transaction; this is the only use of the outbox table.

The post-turn and adventure-closing sweeps run handlers through `worker.execute_worker_job`.
That gives them idempotency, leases, and bounded retries.

## Where authority lives

- **Auth**: real Supabase JWTs everywhere (`auth/jwt.py`).
  - Routers depend on `deps.auth.current_profile`.
  - Campaign routes use `deps.campaign.campaign_for(role=...)` (parse -> load -> 404 -> 403).
- **Visibility**:
  - `visibility/policy.py` owns the vocabularies and normalizers.
  - `visibility/access.py` owns participation, DM authority and per-record receive checks.
  - `threads/service.py` owns thread read/write checks.
  - Realtime, snapshot, retrieval and the DM context all filter through these before serializing.
- **Dice and rules**: dice arithmetic is code-owned.
  - `rolls.service` recomputes `total == kept dice + modifier`.
  - `combat.service` recomputes initiative totals.
  - `rules/` computes checks, attacks and damage.
  - Decision and DM models never compute outcomes.
- **Ordering**: every authoritative mutation goes through `campaigns.events.commit_campaign_mutation`.
  That means expected-revision optimistic concurrency and a domain event per revision.

## Remaining package cycles

These are package-level only. No module-level import cycle exists, since every module imports standalone. Where a module-level cycle would form, the import is function-level.

- `campaigns` <-> `dm` / `submissions` / `world` / `combat`.
  - `campaigns` hosts both the core write path (`events`, `service`) and orchestration that sits above gameplay: start, lobby chat, world seed.
  - `campaigns.events` also calls `post_turn` to stage runs, and `combat` for encounter event visibility.
- `campaigns` <-> `characters`, `deps` <-> `campaigns`/`auth`: a router or dependency module in one package uses the other package's service.
- `dm` <-> `rolls` / `post_turn` / `adventures`: gameplay steps call each other.
  - Rolls resume DM attempts.
  - The DM requests rolls, checks backpressure, and completes adventures.
  - Adventures redact through the DM contract.
- `combat` <-> `realtime`: combat publishes encounter events, and the realtime map payload builder reads `combat.maps`.

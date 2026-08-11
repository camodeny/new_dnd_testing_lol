# Deployment

This app deploys through GitHub Actions using the same general flow as Cognito:

1. run backend tests and build the Vite client;
2. join the tailnet with Tailscale OAuth credentials;
3. ship the repository archive to the server over SSH;
4. build the Docker image on the server;
5. restart the app with Docker Compose and run a container health check.

## GitHub secrets

Set these repository or environment secrets:

- `DEPLOY_USER`
- `DEPLOY_SSH_KEY`
- `TS_SERVER_ADDRESS`
- `TS_OAUTH_CLIENT_ID`
- `TS_OAUTH_SECRET`
- `SECRET_KEY`
- `OPENAI_API_KEY` for encounter map image generation
- `CLIENT_ID` for Pendergrass SSO browser login
- `CLIENT_SECRET` for Pendergrass SSO browser login
- Provider credentials for the selected provider:
  - OpenRouter: `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`
  - OpenCode Go: `OPENCODE_GO_API_KEY`
- Embeddings: `GEMINI_API_KEY`

Optional secrets:

- `DATABASE_URL`

If `DATABASE_URL` is not set, the app uses a SQLite database stored in the remote `data/` volume.
Local `.env` files are intentionally excluded from the deploy archive. Values that need to reach the server must be configured as GitHub secrets or variables and are written to the remote `.deploy.env` during deployment.
For automation-audit shell work on the deployed host, the container now expects `LLM_CAMPAIGN_ENV_FILE` to point at a writable volume path, defaulting to `/app/data/automation/llm_campaign.env`.
The in-container `/usr/local/bin/dnd-automationctl` is a symlink to `/app/automation/automationctl.sh`; the wrapper resolves the real application root (it does not depend on the symlink destination or the current working directory), and supports an explicit `AUTOMATION_ROOT` override. When the automation control env file is missing, the wrapper prints a diagnostic naming the expected path and the `dnd-ensure-host-audit-env` command to generate it.
If the host's home directory is mounted `noexec`, host convenience wrappers copied into `~/bin` should be run through `sh` rather than executed directly.

## GitHub environment variables

Optional variables:

- `DEPLOY_PATH`: defaults to `/home/$DEPLOY_USER/new_dnd_testing_lol`.
- `APP_PORT`: defaults to `5889`.
- `FRONTEND_ORIGINS`: defaults to `http://localhost:$APP_PORT`.
- `PUBLIC_APP_BASE_URL`: defaults to `http://localhost:$APP_PORT`.
- `SSO_URL`: optional provider base URL for browser login. Set this to `https://auth.pendergrass.dev` when enabling hosted Pendergrass SSO.
- `REDIRECT_URI`: optional app callback URL for browser login. For the current Tailscale deployment, use `http://camden-server.tailea98b.ts.net:$APP_PORT/api/auth/callback`.
- `AUTH_COOKIE_SECURE`: defaults to `false`. Keep it `false` while the app itself is served over plain HTTP, even if `SSO_URL` is HTTPS.
- `DND_API_BASE`: defaults to `http://127.0.0.1:$PORT` inside the app container for host-driven automation CLI usage.
- `GUNICORN_TIMEOUT`: defaults to `420` in Docker Compose to allow slower image generation requests.
- `WORKER_REPLICAS`: defaults to `4`; the deploy workflow applies this count with Docker Compose on every deployment.
- `LLM_CAMPAIGN_ENV_FILE`: defaults to `/app/data/automation/llm_campaign.env` so host-driven audit wrappers can source a persisted owner automation key.
- `GUNICORN_WORKER_CLASS`: defaults to `gthread` for better handling of long-lived streaming requests.
- `WEB_CONCURRENCY`: defaults to `3` Gunicorn workers.
- `WEB_THREADS`: defaults to `12` threads per worker when using `gthread`.
- `REDIS_URL`: defaults to `redis://redis:6379/0` for cross-worker and cross-instance session stream fanout.
- `SESSION_STREAM_REDIS_CHANNEL`: optional pub/sub channel name; defaults to `dnd:session_stream`.
- `JWT_EXPIRATION_HOURS`: defaults to `24`.
- `LLM_PROVIDER`: `openrouter` or `opencode_go`; defaults to `openrouter`.
- `OPENCODE_GO_MODEL`: required when `LLM_PROVIDER=opencode_go`.
- `OPENCODE_GO_THINKING`: set to `enabled` to request DeepSeek V4 thinking mode through OpenCode Go; defaults to `disabled`.
- `OPENCODE_GO_REASONING_EFFORT`: `high` or `max`; defaults to `high`.
- `OPENAI_IMAGE_QUALITY`: `low`, `medium`, or `high`; defaults to `low` for cheaper map iteration.
- `OPENAI_IMAGE_TIMEOUT_SECONDS`: defaults to `240`.
- `OPENAI_IMAGE_QA_ENABLED`: `true` or `false`; defaults to `true` to review generated maps before saving.
- `OPENAI_IMAGE_QA_MODEL`: defaults to `gpt-5.4`.
- `OPENAI_IMAGE_QA_THRESHOLD`: 1-10 review score required to accept the first map; defaults to `8`.
- `OPENAI_IMAGE_QA_MAX_RETRIES`: number of review-guided regeneration attempts; defaults to `1`.
- `OPENAI_IMAGE_QA_TIMEOUT_SECONDS`: defaults to `90`.
- `OPENAI_IMAGE_GRID_VALIDATION_ENABLED`: `true` or `false`; defaults to `true` to reject generated maps whose baked-in grid cannot be detected.
- `OPENAI_IMAGE_GRID_MAX_RETRIES`: number of grid-guided regeneration attempts before saving the final candidate; defaults to `2`.
- `OPENAI_IMAGE_SETUP_MODEL`: defaults to `gpt-5.4` for VTT setup analysis.
- `OPENAI_IMAGE_SETUP_TIMEOUT_SECONDS`: defaults to `90`.
- `ENCOUNTER_MAP_STORAGE_DIR`: defaults to `/app/data/encounter_maps`.
- `GEMINI_EMBEDDINGS_ENABLED`: `true` or `false`; defaults to `true` but fails open when `GEMINI_API_KEY` is unset.
- `GEMINI_EMBEDDING_MODEL`: defaults to `gemini-embedding-2`.
- `GEMINI_EMBEDDING_DIMENSIONS`: defaults to `768`.
- `MEMORY_EMBEDDING_DEDUPE_THRESHOLD`: defaults to `0.90`.
- `MEMORY_EMBEDDING_SEARCH_WEIGHT`: defaults to `0.70`.
- `DND_DM_VISIBLE_RESPONSE_TIMEOUT`: visible-response phase timeout in seconds; defaults to `720` (12 minutes). **Temporary internal-testing configuration** -- timeout reduction and performance tuning will be handled separately after memory reliability improves.
- `DND_DM_POST_TURN_TIMEOUT`: post-turn (memory, clock) phase timeout in seconds; defaults to `720` (12 minutes). Same caveat as above.
- `DND_DM_RESPONSE_TIMEOUT`: legacy combined timeout in seconds; defaults to `720`. When used alone, sets only the visible phase timeout; the post-turn timeout defaults to 720 seconds independently.
- `DND_DM_LATE_COMPLETION_RECONCILIATION_SECONDS`: bounded pre-failure reconciliation window in seconds for DM-turn timeouts (visible or post-turn phase). After a timeout the run enters a non-terminal `reconciling` state and is only finalized as failed once this window elapses without the turn completing; defaults to `30`.

## Provider examples

Use OpenRouter:

```bash
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=...
OPENROUTER_MODEL=deepseek/deepseek-v4-flash
```

Use OpenCode Go with DeepSeek V4 Flash thinking mode:

```bash
LLM_PROVIDER=opencode_go
OPENCODE_GO_API_KEY=...
OPENCODE_GO_MODEL=deepseek-v4-flash
OPENCODE_GO_THINKING=enabled
OPENCODE_GO_REASONING_EFFORT=high
```

Enable hosted Pendergrass SSO for the current deployment:

```bash
SSO_URL=https://auth.pendergrass.dev
REDIRECT_URI=http://camden-server.tailea98b.ts.net:5889/api/auth/callback
AUTH_COOKIE_SECURE=false
CLIENT_ID=...
CLIENT_SECRET=...
```

## Automation worker scaling

The deploy workflow scales the automation worker service using the `WORKER_REPLICAS` GitHub variable, defaulting to four replicas. This makes the desired replica count persistent across deployments. For a one-off host-side adjustment, run:

```bash
docker compose --env-file .deploy.env up -d --scale worker=4
```

The next deployment restores the count configured by `WORKER_REPLICAS`.

Each replica:

- Receives a unique generated worker ID (container hostname + PID + random nonce), or one explicitly set via `--worker-id` or `DND_AUTOMATION_WORKER_ID`.
- Executes one automation run at a time.
- Four replicas can therefore process at most four independent runs concurrently.

**Important contract:** One run is never intentionally shared by multiple workers. Each run is claimed atomically through a conditional database `UPDATE` that ensures exactly one worker wins the lease. Non-expired leases cannot be displaced; expired leases are reclaimable by a single winner.

### Runtime lease semantics

| Concept | Detail |
|---|---|
| Lease duration | Configured per-run via `runner_config.lease_seconds` (default 45 s). |
| Provisioning lease | First-time materialization uses `max(lease_seconds, provisioning_lease_seconds)` (default 300 s) to protect clone creation. After materialization, the lease is replaced with the normal runtime value. |
| Heartbeat | Workers heartbeat via `/api/automation/runs/{id}/heartbeat`, extending the runtime lease. |
| Expiry | Expired leases can be claimed by any worker. |
| Token safety | Every lease operation (heartbeat, event, completion) is gated by both `worker_id` and `lease_token` to prevent cross-worker interference. |
| Bounded reclaim | When a worker aborts a run outside gameplay due to a control-plane failure, it reports a stable failure fingerprint via `/api/automation/runs/{id}/worker-error`. Identical failures increment a consecutive count; the run is released back to `queued` for retry below `runner_config.max_reclaim_failures` (default 5), and terminalized as `failed` with a diagnosable `infrastructure_failure_reclaim_loop` error once the threshold is reached. |

### Operational inspection

The workspace endpoint (`/api/automation`) surfaces:

- `active_workers` — workers that have polled or heartbeated in the last 5 minutes.
- `queue_length` — count of queued runs.
- `active_runs` — each run includes `worker_id`, `heartbeat_at`, `lease_expires_at`, and `claimable` (server-computed flag).

### Database guidance

- **PostgreSQL** is recommended for sustained multi-worker throughput. Its row-level locking and MVCC handle concurrent claim `UPDATE` statements efficiently.
- **SQLite** serializes all writes and may bottleneck under multiple worker replicas. It is appropriate only for small or local deployments even after claim correctness is fixed.

## Trigger

- Pushes to `main` deploy with `docker-compose.yml`.
- Manual workflow runs deploy only when the selected ref is `main`.
- Pushes to non-`main` branches run tests and build only.

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
- Provider credentials for the selected provider:
  - OpenRouter: `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`
  - OpenCode Go: `OPENCODE_GO_API_KEY`
- Embeddings: `GEMINI_API_KEY`

Optional secrets:

- `DATABASE_URL`

If `DATABASE_URL` is not set, the app uses a SQLite database stored in the remote `data/` volume.
Local `.env` files are intentionally excluded from the deploy archive. Values that need to reach the server must be configured as GitHub secrets or variables and are written to the remote `.deploy.env` during deployment.

## GitHub environment variables

Optional variables:

- `DEPLOY_PATH`: defaults to `/home/$DEPLOY_USER/new_dnd_testing_lol`.
- `APP_PORT`: defaults to `5889`.
- `FRONTEND_ORIGINS`: defaults to `http://localhost:$APP_PORT`.
- `PUBLIC_APP_BASE_URL`: defaults to `http://localhost:$APP_PORT`.
- `GUNICORN_TIMEOUT`: defaults to `420` in Docker Compose to allow slower image generation requests.
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
- `OPENAI_IMAGE_QA_MODEL`: defaults to `gpt-5.4-mini`.
- `OPENAI_IMAGE_QA_THRESHOLD`: 1-10 review score required to accept the first map; defaults to `8`.
- `OPENAI_IMAGE_QA_MAX_RETRIES`: number of review-guided regeneration attempts; defaults to `1`.
- `OPENAI_IMAGE_QA_TIMEOUT_SECONDS`: defaults to `90`.
- `OPENAI_IMAGE_GRID_VALIDATION_ENABLED`: `true` or `false`; defaults to `true` to reject generated maps whose baked-in grid cannot be detected.
- `OPENAI_IMAGE_GRID_MAX_RETRIES`: number of grid-guided regeneration attempts before saving the final candidate; defaults to `2`.
- `OPENAI_IMAGE_SETUP_MODEL`: defaults to `gpt-5.4-mini` for VTT setup analysis.
- `OPENAI_IMAGE_SETUP_TIMEOUT_SECONDS`: defaults to `90`.
- `ENCOUNTER_MAP_STORAGE_DIR`: defaults to `/app/data/encounter_maps`.
- `GEMINI_EMBEDDINGS_ENABLED`: `true` or `false`; defaults to `true` but fails open when `GEMINI_API_KEY` is unset.
- `GEMINI_EMBEDDING_MODEL`: defaults to `gemini-embedding-001`.
- `GEMINI_EMBEDDING_DIMENSIONS`: defaults to `768`.
- `MEMORY_EMBEDDING_DEDUPE_THRESHOLD`: defaults to `0.90`.
- `MEMORY_EMBEDDING_SEARCH_WEIGHT`: defaults to `0.70`.

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

## Trigger

- Pushes to `main` deploy with `docker-compose.yml`.
- Manual workflow runs deploy only when the selected ref is `main`.
- Pushes to non-`main` branches run tests and build only.

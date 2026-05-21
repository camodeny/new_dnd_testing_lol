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
- `JWT_EXPIRATION_HOURS`: defaults to `24`.
- `LLM_PROVIDER`: `openrouter` or `opencode_go`; defaults to `openrouter`.
- `OPENCODE_GO_MODEL`: required when `LLM_PROVIDER=opencode_go`.
- `OPENCODE_GO_THINKING`: set to `enabled` to request DeepSeek V4 thinking mode through OpenCode Go; defaults to `disabled`.
- `OPENCODE_GO_REASONING_EFFORT`: `high` or `max`; defaults to `high`.
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

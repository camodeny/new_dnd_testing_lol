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
- `OPENROUTER_API_KEY`
- `OPENROUTER_MODEL`

Optional secrets:

- `DATABASE_URL`

If `DATABASE_URL` is not set, the app uses a SQLite database stored in the remote `data/` volume.

## GitHub environment variables

Optional variables:

- `DEPLOY_PATH`: defaults to `/home/$DEPLOY_USER/new_dnd_testing_lol`.
- `APP_PORT`: defaults to `5889`.
- `FRONTEND_ORIGINS`: defaults to `http://localhost:$APP_PORT`.
- `PUBLIC_APP_BASE_URL`: defaults to `http://localhost:$APP_PORT`.
- `JWT_EXPIRATION_HOURS`: defaults to `24`.

## Trigger

- Pushes to `main` deploy with `docker-compose.yml`.
- Manual workflow runs deploy only when the selected ref is `main`.
- Pushes to non-`main` branches run tests and build only.

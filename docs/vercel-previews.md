# Vercel preview deployments (on-demand)

Automatic Vercel deployments are scoped to `main` only. Root `vercel.json`
sets `git.deploymentEnabled` to `{"**": false, "main": true}`, so pushes to
PR / feature branches do **not** trigger Preview Deployments, while
pushes/merges to `main` still deploy automatically to Production.

Existing frontend/backend routing, cron schedules, and rewrites in
`vercel.json` are unaffected.

## Creating a preview on demand

Pick one:

1. **Vercel dashboard:** Project > Deployments > "Create Deployment"
   (top-right "..." / "Deploy" menu) > select the branch or commit > Deploy.
2. **Vercel CLI** (from the repo root, authenticated via `vercel login`):
   ```bash
   vercel                      # preview of current branch/commit
   vercel --yes                # same, non-interactive
   git push origin <branch> && vercel --force  # redeploy a pushed branch
   ```

Preview builds skip production migrations by design:
`backend`'s build script only runs `python -m scripts.migrate` when
`VERCEL_ENV=production` (see `backend/README.md`).

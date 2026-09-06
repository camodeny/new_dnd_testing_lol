# Legacy Repository Instructions (not applicable to main application)

- Do not run the development server unless the user explicitly asks you to.
- The AI is the only Dungeon Master/DM in this product. Do not write docs, UX copy, comments, or code that implies there is a separate human DM, human Game Master, or non-AI moderator controlling the campaign.
- For fully autonomous AI-player runs, use an AI auto-player orchestrator that reads the current board/session state and chooses which AI player should act next; do not implement hard-coded round-robin speaker rotation as the control model.
- The deployed site for this repo is reachable over Tailscale at `http://100.99.192.92:5889`.
- On `camden-server`, the deployed app container can be found with the name pattern `new_dnd_testing_lol-app-(some number)`.
- `camden-server` is reachable via `ssh cpendergrass@camden-server`, and this login does not require an interactive password prompt.
- Pre-alpha: no users exist yet, so do not add backward-compatibility fallbacks, legacy aliases, or migration shims. Prefer a single canonical implementation (one env var name, one code path, one schema) and cut the old one outright rather than keeping both.
- Auth is real Supabase JWT everywhere, including local dev — mock auth was removed and must not be reintroduced. See `docs/local-dev-auth.md` for dev-user setup.

# Repository Instructions

## Work selection and planning

- The linked GitHub Project [DND AI — Development](https://github.com/users/camodeny/projects/1) is the source of truth for cross-issue priority, current sequencing, and work status.
- Before choosing a task, inspect the Project's current priority/queue and verify the issue is still open.
- Native GitHub issue dependencies determine whether work is actually available. Do not start an issue that is blocked by an open dependency merely because it appears high in the queue.
- Native parent/sub-issue relationships define epic ownership and progress. Epic issue bodies define subsystem intent; individual issue bodies define implementation scope and acceptance criteria.
- Do not infer priority from issue number, creation date, update date, backlog size, or the apparent importance of an epic.
- If Project data is unavailable to your tooling, do not invent a replacement priority order. Inspect the relevant issue dependencies and ask for the current team priority when selection matters.
- If Project state conflicts with actual code, issue state, or dependencies, surface the inconsistency rather than silently choosing a different task.
- Keep pull requests scoped to the selected issue. Do not absorb adjacent backlog items simply because they are nearby in the same epic.
- This repository is pre-alpha. When a temporary implementation is superseded, delete/replace it rather than preserving legacy compatibility unless an issue explicitly requires otherwise.

## Product and implementation rules

- Do not run the development server unless the user explicitly asks you to.
- The AI is the only Dungeon Master/DM in this product. Do not write docs, UX copy, comments, or code that implies there is a separate human DM, human Game Master, or non-AI moderator controlling the campaign.
- For fully autonomous AI-player runs, use an AI auto-player orchestrator that reads the current board/session state and chooses which AI player should act next; do not implement hard-coded round-robin speaker rotation as the control model.
- The deployed site for this repo is reachable over Tailscale at `http://100.99.192.92:5889`.
- On `camden-server`, the deployed app container can be found with the name pattern `new_dnd_testing_lol-app-(some number)`.
- `camden-server` is reachable via `ssh cpendergrass@camden-server`, and this login does not require an interactive password prompt.
- Pre-alpha: no users exist yet, so do not add backward-compatibility fallbacks, legacy aliases, or migration shims. Prefer a single canonical implementation (one env var name, one code path, one schema) and cut the old one outright rather than keeping both.
- Auth is real Supabase JWT everywhere, including local dev — mock auth was removed and must not be reintroduced. See `docs/local-dev-auth.md` for dev-user setup.

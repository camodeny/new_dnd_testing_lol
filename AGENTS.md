# Repository Instructions

## Work selection and planning

- The linked GitHub Project **DND AI — Development** is the source of truth for cross-issue priority, current sequencing, and work status.
- These selection rules apply when you are picking work from the backlog. Work the user directly asks for does not need a tracking issue, and its PR need not link one.
- Before choosing a task from the backlog, inspect the Project's current priority/queue and verify the issue is still open.
- Native GitHub issue dependencies determine whether work is actually available. Do not start an issue that is blocked by an open dependency merely because it appears high in the queue.
- Native parent/sub-issue relationships define epic ownership and progress. Epic issue bodies define subsystem intent; individual issue bodies define implementation scope and acceptance criteria.
- Do not infer priority from issue number, creation date, update date, backlog size, or the apparent importance of an epic.
- If Project data is unavailable to your tooling, do not invent a replacement priority order. Inspect the relevant issue dependencies and ask for the current team priority when selection matters.
- If Project state conflicts with actual code, issue state, or dependencies, surface the inconsistency rather than silently choosing a different task.
- Keep pull requests scoped to the selected issue or the user's request. Do not absorb adjacent backlog items simply because they are nearby in the same epic.
- This repository is pre-alpha. When a temporary implementation is superseded, delete/replace it rather than preserving legacy compatibility unless an issue explicitly requires otherwise.

## Product and implementation rules

- Do not run the development server unless the user explicitly asks you to.
- The AI is the only Dungeon Master/DM in this product. Do not write docs, UX copy, comments, or code that implies there is a separate human DM, human Game Master, or non-AI moderator controlling the campaign.
- For fully autonomous AI-player runs, each player independently decides whether to act, wait, or stay silent from its own visible state. Coordination may deliver events and prevent overlapping runs or duplicate submissions, but must not select speakers or use hard-coded round-robin rotation.
- For bounded semantic AI decisions, deterministic/domain code defines the authorized and legal candidate/action space. Decision models may choose or judge only among supplied candidates; when bounded coverage can be incomplete, include an explicit open-ended DM, clarify, or defer path rather than forcing the nearest candidate.
- Do not delegate deterministic authority to probabilistic models: authorization, ownership, visibility scope, rule legality, dice arithmetic, action economy, geometry, idempotency, billing/accounting, provenance/source existence, storage constraints, and provider/transport failure taxonomy remain code-owned even when a decision model helps with semantic routing or ranking.
- Pre-alpha: no users exist yet, so do not add backward-compatibility fallbacks, legacy aliases, or migration shims. Prefer a single canonical implementation (one env var name, one code path, one schema) and cut the old one outright rather than keeping both.
- Auth is real Supabase JWT everywhere, including local dev — mock auth was removed and must not be reintroduced. See `docs/local-dev-auth.md` for dev-user setup.

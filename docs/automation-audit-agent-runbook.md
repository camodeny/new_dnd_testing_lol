# Automation Audit Agent Runbook

This runbook is for an agent that needs to kick off an automation run, pause at audit checkpoints, record custom scorecard feedback, and resume until the run is complete.

## Goal

Use the automation workspace to audit the AI DM from runtime truth, not from vibes.

The recommended evidence order is:

1. Transcript and latest visible turn state
2. `campaign_audit_events`
3. persisted world state and clocks
4. provider call / retry artifacts

## Defaults

- Local app base: `http://127.0.0.1:5889`
- CLI wrapper: `automation/automationctl.sh`
- Underlying worker entrypoint: `automation/run_automation_worker.py`
- Authentication:
  - `DND_OWNER_API_KEY` for CLI and worker control
  - user JWT token only if you intentionally bypass the CLI and call browser-style API routes directly

## CLI Setup

Use the CLI wrapper by default. It loads project provider settings from `.env` first, then `automation/llm_campaign.env` for automation-specific control settings before delegating to `automation/automationctl.py`. The latter may intentionally override a provider setting when needed.

```bash
export API_BASE="${API_BASE:-http://127.0.0.1:5889}"
export DND_OWNER_API_KEY="${DND_OWNER_API_KEY:?set DND_OWNER_API_KEY first}"
AUTOMATION_CTL="./automation/automationctl.sh"
```

Notes:

- Add `--pretty` to any CLI command when you want formatted JSON.
- The CLI is the default path for scorecards, scenarios, snapshots, runs, audit submission, scorecard fetches, comparisons, and worker start.
- A worker now stays alive through an `awaiting_audit` checkpoint, heartbeating while the cycle is reviewed and resuming after `run continue`. Do not start a second worker merely to continue an audited cycle.
- The CLI can also bootstrap a fresh manifest-backed campaign for later use with `"$AUTOMATION_CTL" campaign create --manifest automation/state/my-campaign.json --no-start`.
- Raw `curl` examples are no longer the recommended path for this workflow.

### Deployed host shell usage

When you have shell access on the deployed host rather than a local checkout, run the CLI inside the app container.

First create or rotate a host-side owner automation key and write the env file that the wrappers will source:

```bash
docker exec new_dnd_testing_lol-app-1 \
  dnd-ensure-host-audit-env \
  --username phazedrl \
  --env-file /app/data/automation/llm_campaign.env
```

Then run the normal CLI from inside the same container:

```bash
docker exec new_dnd_testing_lol-app-1 \
  dnd-automationctl run status \
  --run-id "$RUN_ID" \
  --pretty
```

The container inherits provider credentials from deploy env, so the worker path only needs the generated owner automation key file plus the existing app env.

If you want shorter commands on the host, copy `automation/deployed_host/dnd-automationctl` and `automation/deployed_host/dnd-ensure-host-audit-env` into a host bin directory and run those wrappers instead of spelling out `docker exec` every time.
If the host home filesystem is mounted `noexec`, invoke them as `sh ~/bin/dnd-automationctl ...` and `sh ~/bin/dnd-ensure-host-audit-env ...`.

## Audit Workflow

1. Ask the user what they want audited.
2. Convert that into a reusable scorecard template.
3. Create or reuse an automation scenario against the source campaign.
4. Capture a snapshot.
5. Queue a run.
6. Start the worker.
7. Wait for the run to pause in `awaiting_audit`.
8. Audit the current cycle from runtime truth.
9. Submit cycle feedback with the custom scorecard.
10. Continue the run.
11. Repeat until the run finishes.
12. Fetch the final scorecard and, if useful, compare against a baseline run.

## Scorecard Templates

Create a reusable scorecard template first. The run stores a snapshot of the template so later template edits do not rewrite historical audits.

### Create a scorecard template

```bash
cat > /tmp/automation-scorecard.json <<'JSON'
{
  "name": "AI DM Continuity Audit v1",
  "description": "Track AI DM memory, continuity, pacing, and hidden-state correctness.",
  "instructions": "Use runtime truth, not vibes. Grade visible narration and hidden-state consequences separately when needed.",
  "criteria": [
    {"id": "memory_quality", "label": "Memory Quality", "description": "Did the AI DM preserve and use the right remembered facts?"},
    {"id": "story_consistency", "label": "Story Consistency", "description": "Did the AI DM stay consistent with prior events and scene logic?"},
    {"id": "scene_state", "label": "Scene State", "description": "Did transcript, world state, clocks, and locations stay aligned?"},
    {"id": "npc_portrayal", "label": "NPC Portrayal", "description": "Did NPC behavior and voice stay coherent?"},
    {"id": "pacing", "label": "Pacing", "description": "Did the turn move the scene forward at the right speed?"}
  ],
  "defaults": {
    "pause_phases": ["after_dm"]
  }
}
JSON

"$AUTOMATION_CTL" scorecard create \
  --api-base "$API_BASE" \
  --input-file /tmp/automation-scorecard.json \
  --pretty
```

`pause_phases` may be:

- `["after_dm"]`: recommended default
- `["after_player", "after_dm"]`: pause after both player and AI DM checkpoints

## Scenario Creation

Create the scenario against a source campaign and attach the scorecard template.

```bash
cat > /tmp/automation-scenario.json <<'JSON'
{
  "source_campaign_id": 11,
  "name": "Campaign 11 continuity benchmark",
  "description": "Audit memory quality, continuity, and hidden-state correctness.",
  "scorecard_template_id": 3,
  "runner_config": {
    "audit_pause_phases": ["after_dm"]
  }
}
JSON

"$AUTOMATION_CTL" scenario create \
  --api-base "$API_BASE" \
  --input-file /tmp/automation-scenario.json \
  --pretty
```

## Built-In Tool-Calling Auditors

The app can run built-in auditors at audit gates without launching external `opencode run` processes. Built-in auditors use the app provider stack and a dedicated read-only auditor tool registry. They can inspect transcript, `campaign_audit_events`, persisted world state, clocks, NPCs, characters, provider calls, run events, snapshots, and scorecard templates.

The built-in path now starts from a compact cycle evidence packet and uses targeted drill-down tools for specific audit events, provider calls, and run events. Raw truth is still reachable, but the default loop avoids replaying giant JSON blobs unless the auditor explicitly asks for them. Detail tools now also support exact field-path selection for event payloads, provider artifacts, and snapshot data when the auditor only needs a few concrete facts.

To queue a run for built-in auditors, set `runner_config.auditor_config.mode` to `built_in` and make sure the run pauses at `after_dm`.

```json
{
  "runner_config": {
    "audit_pause_phases": ["after_dm"],
    "auditor_config": {
      "mode": "built_in",
      "model": "opencode-go/deepseek-v4-flash",
      "count": 1,
      "auto_continue": false,
      "target_cycles": 3,
      "required_tools": "runtime_truth_full"
    }
  }
}
```

When the run reaches `awaiting_audit`, use the Automation Run page's **Run Built-In Auditors** button. The app records auditor jobs, tool-call traces, provider artifacts with phase `auditor_decision`, and the aggregated cycle scorecard.

External smarter auditors are still supported. Use the CLI loop below when you want another agent or model to inspect the same runtime truth and submit feedback through `automationctl run audit`.

## Snapshot and Run

### Capture a snapshot

```bash
"$AUTOMATION_CTL" snapshot create \
  --api-base "$API_BASE" \
  --scenario-id "$SCENARIO_ID" \
  --label "Pre-audit snapshot" \
  --pretty
```

### Queue a run

```bash
"$AUTOMATION_CTL" run start \
  --api-base "$API_BASE" \
  --scenario-id "$SCENARIO_ID" \
  --snapshot-id "$SNAPSHOT_ID" \
  --pretty
```

## Worker Start

The worker claims queued runs, executes them, and pauses them at configured audit checkpoints.

```bash
"$AUTOMATION_CTL" worker start \
  --api-base "$API_BASE" \
  --run-id "$RUN_ID" \
  --worker-id "audit-worker-1"
```

## Watching a Run

### One-shot run fetch

```bash
"$AUTOMATION_CTL" run status \
  --api-base "$API_BASE" \
  --run-id "$RUN_ID" \
  --pretty
```

### Wait for the next audit checkpoint

```bash
"$AUTOMATION_CTL" run wait \
  --api-base "$API_BASE" \
  --run-id "$RUN_ID" \
  --wait-for after_dm \
  --pretty
```

Important:

- `run wait` consumes the run SSE stream for you and exits when the requested condition is reached.
- `--wait-for` accepts `after_dm`, `after_player`, `either`, or `terminal`.
- If the run reaches `completed`, `failed`, or `stopped` before the requested checkpoint, the CLI exits non-zero with a JSON payload that tells you what happened.
- The worker is what actually halts at audit checkpoints. The stream and CLI wait command are for observation and coordination, not control.

## Audit Pause Loop

When the run status becomes `awaiting_audit`, inspect:

- `current_audit_cycle`
- `latest_session.messages`
- `events`
- `provider_calls`
- scorecard state so far

Then submit the cycle audit.

### Submit cycle feedback

```bash
cat > /tmp/automation-audit-scorecard.json <<'JSON'
{
  "overall_status": "warn",
  "overall_summary": "Good continuity, mild pacing issue.",
  "criteria": [
    {
      "criterion_id": "memory_quality",
      "status": "pass",
      "summary": "Remembered the sigil and prior scene context correctly.",
      "evidence": "Transcript plus world-state references stayed aligned."
    },
    {
      "criterion_id": "story_consistency",
      "status": "pass",
      "summary": "No canon contradiction.",
      "evidence": "No conflicting facts across transcript and audit trail."
    },
    {
      "criterion_id": "scene_state",
      "status": "warn",
      "summary": "Clock/state follow-through looked incomplete.",
      "evidence": "Visible narration suggested escalation, but persisted pressure did not move."
    }
  ]
}
JSON

"$AUTOMATION_CTL" run audit \
  --api-base "$API_BASE" \
  --run-id "$RUN_ID" \
  --cycle-id "$CYCLE_ID" \
  --summary "The AI DM kept continuity but introduced a small pacing stall." \
  --notes "Transcript matched the stored scene. Clock pressure was unchanged when it should probably have advanced one step." \
  --scorecard-file /tmp/automation-audit-scorecard.json \
  --pretty
```

### Continue the run

```bash
"$AUTOMATION_CTL" run continue \
  --api-base "$API_BASE" \
  --run-id "$RUN_ID" \
  --pretty
```

The continue call will fail if the current audit cycle has not been audited yet, unless forced.

## Final Scorecard and Comparisons

### Fetch the current scorecard

```bash
"$AUTOMATION_CTL" run scorecard \
  --api-base "$API_BASE" \
  --run-id "$RUN_ID" \
  --pretty
```

The final scorecard combines:

- built-in automation health checks
- aggregated custom scorecard results across audited cycles

Custom criteria are written as checks like:

- `custom:memory_quality`
- `custom:story_consistency`

### Compare two runs

```bash
"$AUTOMATION_CTL" run compare \
  --api-base "$API_BASE" \
  --left-run-id "$LEFT_RUN_ID" \
  --right-run-id "$RIGHT_RUN_ID" \
  --pretty
```

Use this after running the same scenario with the same scorecard template to track improvements over time.

## Scorecard Weighting and Applicability Policy

Both built-in and custom scorecard criteria follow the same execution and aggregation rules:

### 1. Scorecard Status and Weight Values
- **`pass`**: Counts as `1.0 * weight`.
- **`warn`**: Counts as `0.5 * weight` (half credit).
- **`fail`**: Counts as `0.0 * weight` (no credit).
- **`not_assessed`**: Excluded from the weighted total score calculation. This is the default status for custom criteria that have not yet had any cycle feedback recorded, built-in checks with missing metrics (represented as `None`), and template criteria that a built-in auditor omitted from its final scorecard.
- **`not_applicable`**: Excluded from the weighted total score calculation. A custom criterion is marked `not_applicable` if it is explicitly submitted with `status: "not_applicable"`, if its applicability is false, or if all cycle assessments for that criterion are non-applicable.

### 2. Category Breakdowns
Scorecard results (built-in and custom) are mapped to 5 canonical categories:
- `operational/runtime reliability`
- `narrative quality`
- `durable state correctness`
- `retrieval or memory use`
- `safety/private-information handling`

If a category contains no applicable criteria, its status is set to `not_applicable` with a `None` score. Otherwise, its status is the worst status among the category's *assessed* criteria (`fail` < `warn` < `pass`); consistent with their exclusion from weighting, `not_assessed` and `not_applicable` criteria do not lower a category's status, so a category with one `pass` and one still-unassessed criterion reports `pass`. A category reports `not_assessed` only when it has applicable criteria but none of them have been assessed, and `not_applicable` when all of its criteria are not applicable. Its category score is the weighted average of the assessed (pass/warn/fail) criteria in that category. Any criterion that cannot be mapped to these categories is kept uncategorized and excluded from these 5 named category breakdown cards (but still counts towards the overall score).

### 3. Score Precision and UI Safeguards
- Overall and category scores are rounded to 4 decimal places.
- To prevent false perfect aggregate scores, if any warning or failure exists in the applicable criteria, the score will never round to `1.0` or display as `100.0%` in the UI (it is capped at a maximum value of `0.9999` and renders as `99.9%` in the UI).
- Criteria weights are bounded/clamped between `1` and `100` during template creation.

### 4. Weight Defaults, Legacy Snapshots, and Category Inference
- **Default weight**: a custom criterion with no stored `weight` defaults to `2`. This applies both at template creation (missing or non-integer weights are normalized to `2`) and at scoring time for older template/run snapshots that predate the weight field.
- **Weight bounds**: template creation and update clamp weights into `1`–`100`. At scoring time, stored weights are only lower-bounded at `1`; values above `100` can only exist in legacy snapshots written before the clamp was introduced.
- **Legacy snapshots**: runs store a snapshot of the scorecard template, so later template edits do not rewrite historical audits. Snapshot criteria lacking `weight` or `category` are interpreted with the defaults above and the category inference below; no migration is required.
- **Category inference** (`get_criterion_category()`): an explicit `category` value is first normalized case-insensitively against known aliases — `operational/runtime reliability` (`operational_reliability`, `operational`), `narrative quality` (`narrative_quality`, `narrative`), `durable state correctness` (`durable_state_correctness`, `state_correctness`, `state`), `retrieval or memory use` (`retrieval_memory_use`, `retrieval`, `memory`), `safety/private-information handling` (`safety_private_info`, `safety`). If the explicit category is missing or matches no alias, the criterion's id/metric/label is keyword-matched in this order: operational (`completion`, `error`, `turn`, `retry`, `status`, `reliability`, `perf`, `run`), durable state (`state`, `durable`, `db`, `entity`, `relation`, `save`, `progress`), retrieval/memory (`memory`, `retrieval`, `context`, `history`, `anchor`, `recall`), safety (`safety`, `private`, `secure`, `moderation`, `policy`, `leak`, `spoiler`), narrative (`narrative`, `story`, `quality`, `silence`, `empty`, `dialog`, `dialogue`, `rp`, `play`). If nothing matches, the criterion stays uncategorized: it is excluded from the 5 named category breakdown cards but still counts towards the overall weighted score.


## Troubleshooting

### Worker environment

- `automation/automationctl.sh worker start` still launches a separate worker process.
- That worker needs its own provider environment, not just `DND_API_BASE` and `DND_OWNER_API_KEY`.
- In practice that means `LLM_PROVIDER` plus the active provider key and model variables must be present in the worker process environment.
- If the app server has the right provider env but the worker does not, the run can claim successfully and still fail when it tries to generate overseer, player, or DM turns.

### Fresh campaign bootstrap on older local DBs

- If fresh LLM-player bootstrap fails with a uniqueness error on `llm_players.user_id`, check for orphaned historical `llm_players` rows whose `user_id` values are above the current `users` table range.
- The local route now assigns a safe new user id above both `users.id` and `llm_players.user_id`, but older deployments or branches may still hit this.

### Session start can look hung

- A long first-session request can still succeed before the client call returns.
- If bootstrap appears stuck, confirm runtime truth with `campaign_sessions`, `campaign_audit_events`, and the source campaign snapshot metadata before assuming session start failed.

### DM timeout tuning

- The default visible-response timeout (`--dm-visible-response-timeout` / `DND_DM_VISIBLE_RESPONSE_TIMEOUT`) is **720** seconds (12 minutes).
- The default post-turn timeout (`--dm-post-turn-timeout` / `DND_DM_POST_TURN_TIMEOUT`) is **720** seconds (12 minutes).
- These values are a **temporary internal-testing configuration**, not a permanent performance target. Timeout reduction and performance tuning will be handled separately after memory reliability improves.
- Complex DM turns that hit guard, repair, memory, or clock-adjudication passes may need a larger value such as `120` or `180` seconds above the default; the current defaults should accommodate most such turns.
- If a run fails with `dm_response_timeout`, `dm_visible_response_timeout`, or `dm_post_turn_timeout`, inspect `campaign_audit_events` to see whether the DM was still progressing through repair or post-turn persistence when the worker gave up.
- The two values may be configured independently. Setting only one will leave the other at its 720-second default.
- The legacy combined `DND_DM_RESPONSE_TIMEOUT` / `--dm-response-timeout` sets only the visible timeout; the post-turn timeout defaults to 720 seconds when the legacy option is used alone.

### Audit submission flow

- Submit the audit and wait for that request to complete before calling `continue`.
- If `continue` races the audit write, the server correctly returns `Current audit cycle must be audited before continuing`.
- The easiest sequencing is: `run wait` -> inspect runtime truth -> `run audit` -> `run continue` -> `run wait` again.

### Pre-audit scorecard rows

- Before the first audited cycle is saved, custom scorecard checks will show `not_assessed` with `No custom audit feedback recorded for this criterion.`
- They are excluded from score weighting until assessments are recorded.

## Recommended Agent Behavior

When another agent is told to use this system:

1. Ask what the user wants audited.
2. Turn that request into a scorecard template if one does not already exist.
3. Prefer `after_dm` pauses unless the user explicitly wants `after_player` too.
4. Audit from transcript plus persisted state, not from visible text alone.
5. Keep the AI DM audit observe-only unless the user explicitly authorizes player-side repair.
6. Submit evidence-backed cycle feedback before continuing the run.
7. Reuse the same scorecard template for follow-up runs when the goal is tracking improvement.

# Jev / decision-first roadmap pivot handoff

> Temporary handoff file. Delete this file after the native GitHub Project and issue-relationship cleanup below is complete.

## Why this file exists

The issue bodies and repository instructions were updated through ChatGPT's GitHub connector, but the connector available in that session did not expose GitHub Projects or native parent/sub-issue/dependency mutation APIs. The roadmap pivot is therefore already represented in issue text; this file exists only for the remaining native relationship / Project metadata work.

Do not turn this file into a permanent roadmap. The linked GitHub Project **DND AI — Development** remains the cross-issue priority/sequencing source of truth, and the issue bodies remain the implementation source of truth.

## Architecture now represented in the issues

The product is moving from a generative-DM-centric semantic orchestration model toward a decision-first control plane:

1. Deterministic/domain code owns authoritative state, permissions, rules, legal candidate enumeration, mutations, idempotency, arithmetic, geometry, visibility, billing/accounting, provenance/source existence, and provider/transport failure taxonomy.
2. A provider-neutral bounded decision runtime chooses among code-supplied candidates and can score/judge semantic questions. TypeSafe Jev is the first adapter/reference model, not the architecture itself.
3. A decision model may never invent canonical IDs, legal actions, targets, permissions, or side effects outside the supplied candidate set.
4. Common bounded gameplay paths may execute directly when the decision-class policy permits. Risk, reversibility, deterministic verification, calibration, and ambiguity/margin determine policy; there is no single global confidence threshold.
5. `OPEN_ENDED_DM`, `CLARIFY`, `DEFER`, or equivalent escape candidates preserve infinite-pathway play. Novel fiction, genuinely open-ended adjudication, and prose generation continue to use generative models.
6. Full available decision distributions plus candidate/question/schema/policy/model versions, execution outcome, fallback/escalation, stale-state revalidation, and relevant validation results are observable/replayable.
7. Decision-first execution changes semantic orchestration, not authority: deterministic invariants still fail closed regardless of AI confidence.

`AGENTS.md` now contains the evergreen implementation guardrails above without embedding roadmap sequencing.

## New epic and foundational issues

- #379 `[EPIC] Decision-first AI control plane & bounded semantic execution`
- #380 `AI decisions: add provider-neutral bounded decision runtime with Jev adapter`
- #381 `AI decisions: build candidate enumeration, decision-frame, and execution-policy primitives`
- #382 `Forward DM: add decision-first semantic router and bounded fast-path executor`
- #383 `AI decisions: add shadow/active telemetry, calibration, and replay`
- #384 `AI decisions: add semantic judge layer for ambiguous validation and narration fidelity`

#379 already has the `epic` label and its body lists #380–#384 as children. What remains is the native GitHub relationship metadata.

## Existing issues already amended

The following issue bodies/titles were updated to reflect the decision-first architecture where it materially changes implementation:

- Epics / cross-cutting: #177, #178, #179, #180, #181, #183, #184, #186
- Forward/provider: #208
- World/retrieval/NPC: #212, #213, #214, #215
- Post-turn: #217, #218, #219, #220
- Rules/combat: #229, #233, #236
- Private gameplay: #248, #251, #252
- Usage/provider routing: #257, #258
- Adventure completion: #261
- E2E/eval/perf/ops: #267, #268, #269, #273, #274, #275, #374, #375

Closed foundational issues such as #192 and #373 were intentionally not reopened/redefined; #383 extends the existing tracing foundation and #380 adds the decision fake adapter needed for future deterministic E2E coverage.

## Remaining native GitHub cleanup

Use an agent/tooling session with GitHub Projects and native issue-relationship mutation support:

1. **Native parent/sub-issue relationships**
   - Make #380, #381, #382, #383, and #384 native children of #379.
   - Do not create duplicate wrapper issues just to simulate the relationship.

2. **Project membership**
   - Add #379–#384 to **DND AI — Development**.
   - Inspect the Project's current fields/queue before setting priority/status. Do **not** infer priority from issue numbers, this handoff, or the apparent importance of Jev.

3. **Native dependencies**
   - Read the issue bodies before editing dependencies; they deliberately distinguish hard dependencies from optional later integrations.
   - #380 should precede #381.
   - #381 is required for active decision policy/candidate execution paths such as #382.
   - #383 should be available early enough to observe/calibrate experimental active paths and before broad invite-alpha direct execution.
   - #384 depends on #380/#381/#383 and the existing validator/narration foundations.
   - Add native dependencies from amended issues to #380–#384 only where their bodies describe a true blocking dependency. Do **not** globally block deterministic rules, persistence, VTT geometry, UI, or unrelated infrastructure on the decision epic.
   - Preserve existing valid dependencies.

4. **Project sequencing**
   - Use the Project's actual current queue/readiness to position the new work.
   - Ensure agents inspecting the Project will see the decision-runtime foundation before they implement old assumptions such as `freeform prose -> full generative DM -> command` everywhere.
   - Do not encode a second priority order in a markdown file.

5. **Stale Project metadata**
   - Check Project descriptions/notes/custom-field text for statements that imply the generative forward DM is the universal semantic orchestrator.
   - Update only stale architecture wording; do not rewrite unrelated Project metadata.

## PR #378

PR #378 (`Add Jev integration opportunities doc`) predates this roadmap rewrite and its current exploration note should not become a competing roadmap.

Before merging it, revise the doc so it:

- points to #379–#384 as the implementation architecture;
- presents bounded decision models as a first-class provider-neutral runtime, with Jev as the first adapter/reference implementation;
- centers candidate-space safety and explicit `OPEN_ENDED_DM` / clarify / defer escape semantics;
- describes direct execution as decision-class/risk/calibration policy rather than one global confidence threshold;
- explains that common paths may be more aggressive when code constrains and revalidates the action space;
- keeps deterministic runtime facts/permissions/legality/idempotency/billing/provider-failure taxonomy deterministic;
- removes `legacy_system` as an integration target;
- covers the roadmap-native integrations now represented in #214, #217–#220, #233, #236, #251/#252, #268/#269, and retrieval #212/#213;
- treats the issues/Project—not the doc—as the source of implementation scope and priority.

## Completion

After native relationships, Project membership/fields, dependencies, and PR #378's design note are consistent with the issue bodies, delete `docs/jev-roadmap-pivot-handoff.md`.
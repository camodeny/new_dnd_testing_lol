# Jev / decision-first roadmap pivot handoff

> Temporary handoff file. Delete this file after the native GitHub Project and issue-relationship cleanup below is complete.

## Why this file exists

The issue bodies can be updated through the current GitHub connector, but this connector does not expose GitHub Projects or native parent/sub-issue/dependency mutation APIs. The roadmap pivot is therefore being implemented directly in issue text, while the remaining native relationship / Project metadata work must be done by an agent with GitHub CLI/API/Projects access.

## Architecture being adopted

The product is moving from a generative-DM-centric semantic orchestration model toward a decision-first control plane:

1. Deterministic code owns authoritative state, permissions, rules, legal candidate enumeration, mutations, idempotency, arithmetic, geometry, and visibility boundaries.
2. A provider-neutral bounded decision runtime chooses among code-supplied candidates and can score/judge semantic questions. TypeSafe Jev is the first adapter/reference model, not the architecture itself.
3. The decision model may never invent canonical IDs, legal actions, targets, permissions, or side effects outside the supplied candidate set.
4. Common, bounded gameplay paths should execute directly when policy permits. Risk/reversibility/verification determine auto-execution policy; there is no single global confidence threshold.
5. `OPEN_ENDED_DM` / equivalent escape candidates preserve infinite-pathway play. Novel fiction, genuinely open-ended adjudication, and prose generation continue to use generative models.
6. Full decision distributions, candidate schema/policy/model versions, execution outcome, fallback/escalation, and relevant validation results are observable and replayable.
7. Deterministic invariants remain deterministic. Decision models handle semantic ambiguity, ranking, classification, routing, and judgment.

## Remaining native GitHub cleanup

After the issue creation/update work in this pivot lands:

- Inspect the newly created `[EPIC] Decision-first AI control plane & bounded semantic execution` issue and its five foundational child issues.
- Set native GitHub parent/sub-issue relationships so those five issues are children of that epic.
- Add the new epic and all five children to the linked GitHub Project **DND AI — Development**.
- Inspect current Project fields/queue before setting priority or status. Do **not** invent priority from issue numbers or this file.
- Set native dependencies to reflect the issue bodies. At minimum, the decision runtime foundation should precede decision-frame/policy and active forward-DM execution; telemetry/calibration should be available before broad automatic rollout. Preserve any existing dependencies that remain valid.
- Inspect amended issues (#177, #178, #179, #180, #181, #186, #208, #212, #213, #214, #215, #217, #218, #219, #220, #229, #233, #236, #251, #258, #267, #268, #269, #273, #274, #261) and add native dependencies on the new foundational decision issues only where the amended body explicitly requires them.
- Do not make all product work globally dependent on the decision epic. Deterministic rules, persistence, geometry, UI, and unrelated infrastructure should remain independently implementable.
- Update Project sequencing so agents see the new architecture before implementing old generative-DM assumptions. Use current Project context and dependency readiness to decide exact ordering.
- Verify there are no stale Project descriptions/custom-field notes that still describe the generative forward DM as the universal semantic orchestrator.
- Delete this handoff file when complete.

## PR #378

PR #378 (`Add Jev integration opportunities doc`) predates this roadmap rewrite. Update its design note to match the issue architecture rather than treating Jev as a set of isolated plug-ins. In particular:

- present bounded decision models as a first-class provider-neutral runtime;
- keep Jev as the first adapter/reference implementation;
- describe candidate-space safety and `OPEN_ENDED_DM` escape semantics;
- treat direct execution as policy/risk-class based rather than one global confidence threshold;
- keep deterministic runtime facts deterministic;
- remove legacy-system integration targets;
- point readers to the new decision-control-plane epic and amended roadmap issues.

Once the PR accurately reflects the issue architecture, it can remain as background/design context rather than the roadmap source of truth.

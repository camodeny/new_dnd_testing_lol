# Decision-first AI control plane

Status: architecture direction for pre-alpha implementation. GitHub issues and the linked Project remain the planning source of truth.

## Core rule

Deterministic code defines what exists, what is authorized, what is mechanically legal, and what side effects are possible. A bounded decision model chooses or judges among code-supplied possibilities. Generative models remain the escape hatch for open-ended adjudication and the source of prose/new language where generation is actually required.

TypeSafe Jev is the first adapter/reference decision model, not the architecture itself.

## Runtime shape

`authoritative state -> candidate enumeration -> bounded semantic decision(s) -> deterministic validation/execution OR OPEN_ENDED_DM -> generative adjudication -> deterministic validation/commit -> narration`

Decision questions may fan out in parallel when they are independently answerable from the same state, for example route, action family, actor, target, roll need, evidence relevance, identity candidate, or semantic validator judgments. Code only consumes answers relevant to the selected path.

## Safety / trust model

- Candidate sets are authoritative and versioned. The decision model cannot invent canonical IDs, permissions, targets, actions, or mutations outside the supplied set.
- Always provide an explicit escape/defer option where the bounded set may not cover player intent (`OPEN_ENDED_DM`, `CLARIFY`, `DEFER`, or an equivalent typed choice).
- Deterministic invariants remain deterministic: authorization, ownership, visibility scopes, action economy, rule legality, dice math, geometry, idempotency, billing/accounting, provider failure taxonomy, provenance/source existence, and storage constraints are not delegated to a probabilistic model.
- Auto-execution policy is decision-class specific. Risk, reversibility, deterministic verification, calibration, and the gap between plausible alternatives matter more than a single global confidence threshold.
- Cheap/reversible/fully verified choices may execute from the top valid decision without a high fixed threshold. Irreversible/canon-sensitive choices may require stronger calibrated confidence, extra verification, or escalation.
- The generative DM remains available for genuinely novel action interpretation, new fictional content, unusual long-tail mechanics, and situations where bounded candidate enumeration is insufficient.

## Observability

Every decision run should be traceable with the decision role, model/provider/adapter, model version, question/policy/schema versions, candidate IDs, full returned probability distribution where available, selected answer, runner-up/margin, latency/cost, shadow/active mode, direct-execute/primer/escalation result, deterministic validation result, and downstream outcome/repair signal when available.

## Product intent

The goal is not to replace the DM with a classifier. The goal is to stop paying a generative model to rediscover decisions the runtime can safely bound. Routine mechanics and orchestration should become faster, cheaper, and more auditable, while the generative DM concentrates on the parts of tabletop play that are actually open-ended.

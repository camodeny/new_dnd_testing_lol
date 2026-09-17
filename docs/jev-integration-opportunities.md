# Jev / bounded decision models — integration direction

Date: 2026-09-17
Status: Background/design context only. GitHub issues and the linked **DND AI — Development** Project are the implementation and priority source of truth.

Primary roadmap: #379 with foundational issues #380–#384.

## 1. Architectural conclusion

Jev should not be treated as a collection of isolated plug-ins around an otherwise unchanged generative-DM pipeline. The stronger opportunity is a provider-neutral **bounded decision runtime** that becomes the semantic control plane for decisions the application can safely constrain.

TypeSafe Jev is the first adapter/reference implementation of that runtime, not the architecture itself.

The target shape is:

`authoritative state -> code-enumerated candidates -> bounded semantic decision(s) -> deterministic validation/execution OR OPEN_ENDED_DM -> generative adjudication -> deterministic validation/commit -> narration`

The important safety property is not a single confidence threshold. It is that **code defines the possible actions and authority boundaries before the model chooses**.

## 2. Division of responsibility

### Deterministic/domain code owns

- authorization, actor ownership, audience/visibility scope;
- canonical IDs and source/provenance existence;
- legal action/candidate enumeration;
- D&D rule legality, action economy, dice math, HP/resources, geometry;
- state revisions, stale-candidate detection, idempotency and commit;
- billing/accounting and provider/transport/config failure taxonomy;
- the actual side effects performed after a semantic choice.

A bounded decision model may not invent or override any of those.

### Bounded decision models own semantic choice/judgment

Examples:

- route / supported intent family;
- mapping freeform prose onto a bounded set of legal commands;
- choosing among real entity-identity candidates;
- relevance/reranking among already-authorized retrieval candidates;
- clock-criteria satisfaction among legal transitions;
- semantic support / contradiction / narration-agency judgments;
- bounded NPC action choice when the mechanics layer can enumerate legal actions.

Independent questions should fan out in one decision request when possible rather than becoming a serial chain of model round trips.

### Generative models remain necessary for

- genuinely open-ended or unusual D&D adjudication;
- new fictional claims/content that cannot be assembled from authoritative candidate payloads;
- narration, dialogue, clarification wording, summaries, and other new prose;
- long-tail mechanics/fiction where the application cannot enumerate a safe complete candidate set.

Every bounded surface whose candidate set may be incomplete should expose an explicit escape such as `OPEN_ENDED_DM`, `CLARIFY`, or `DEFER`.

## 3. Execution policy: more aggressive than a global 85% rule

Do not hard-code one confidence threshold for every decision class.

The runtime should evaluate at least:

- returned probability/confidence and runner-up margin where available;
- whether the selected action is reversible;
- whether deterministic code can fully verify it before mutation;
- consequence class if it is wrong;
- calibration measured for that exact decision role / schema / policy version.

This allows aggressive execution where the action space itself makes mistakes cheap or impossible to commit incorrectly. A cheap/reversible/fully revalidated choice may eventually execute from the top valid candidate with a relatively low or even no fixed probability floor. Canon-sensitive, destructive, or hard-to-reverse choices can require stronger calibrated evidence, extra verification, or escalation.

#381 owns these policies. #383/#268 measure them. #269 gates approved invite-alpha use while still permitting clearly marked pre-alpha experiments.

## 4. Forward-DM control plane

#382 changes the current assumption that every accepted player intent begins with a full generative `DmTurnContractV1` call.

A decision frame can ask bounded questions such as route, action family, actor, target, roll need, or evidence relevance, but direct execution is allowed only when **all required execution payload** is available from authoritative code/candidates.

A mode label by itself is not enough. For example, choosing `await_roll` does not magically generate a safe roll contract; a direct roll path is valid only when actor/check/ability/skill/advantage/DC policy and required public payload can be assembled from authoritative state/rules without arbitrary invention.

When the bounded path is incomplete, the original player intent reaches the existing full generative adjudicator through `OPEN_ENDED_DM`.

Middle-policy results may be supplied to that adjudicator as an advisory prior rather than as authority.

## 5. Combat is a flagship use case

#233 now defines the intended pattern for freeform combat prose.

For an input such as:

> I rush the wounded goblin and hit him with my longsword.

code should enumerate concrete commands that are actually available from authoritative encounter/rules/VTT state, for example:

- `ATTACK:<pc>:<longsword>:<goblin_a>`
- `ATTACK:<pc>:<longsword>:<goblin_b>`
- other supported concrete commands that are currently legal;
- `CLARIFY`;
- `OPEN_ENDED_DM`.

The decision model selects. It does not manufacture actor IDs, targets, weapons, positions, action costs, or command JSON.

The selected command is revalidated against the current encounter revision and then executed through the same deterministic services used by structured UI actions.

Creative intent such as swinging from a chandelier while knocking a brazier into an enemy remains open-ended DM work rather than being coerced into the nearest supported command.

#236 extends the same pattern to supported NPC/enemy action selection while explicitly never AI-controlling absent human PCs.

## 6. World, retrieval, and identity

### Entity identity — #214

Resolve exact canonical refs/aliases deterministically first. If identity remains ambiguous, code retrieves a bounded set of real candidate entity IDs and explicit outcomes such as `NEW_ENTITY`, `KEEP_DISTINCT`, or `DEFER`. The decision model chooses only among them. Destructive merges are a higher-risk policy class.

### Retrieval — #212/#213

Authorization/filtering happens before model exposure. Graph/vector search produces bounded authoritative-source references; a decision model may rerank/select from those authorized candidates or return no relevant result. Vector similarity and decision probability are ranking metadata, never evidence authority.

### NPC state — #215

NPC state should be structured enough that relevant goals/knowledge/resources and legal action candidates can feed bounded semantic decisions without creating a dedicated generative LLM agent per NPC.

## 7. Post-turn

The post-turn roadmap now separates generation from decision/judgment:

- **#217 materialization:** generation may propose novel fact/entity/relation content; bounded decisions can classify write categories or verify proposed assertions; deterministic provenance/visibility/identity/idempotency remain final gates.
- **#218 clocks:** code supplies explicit criteria, evidence, current state, and legal transitions such as `NO_CHANGE`, allowed advance, `COMPLETE`, `DEFER`; the decision model judges semantic criteria satisfaction.
- **#219 summaries:** summary prose remains generative; claim-level support/secrecy checks can use bounded judgments.
- **#220 consistency:** exact contradictions are deterministic; paraphrased/implicit conflicts can use `CONSISTENT | CONTRADICTION | UNCERTAIN` semantic judgments.

Do not build new Jev integrations into `legacy_system`; pre-alpha superseded code should be deleted/replaced rather than preserved.

## 8. Validation and secrecy

#384 owns semantic judge infrastructure. Good targets are the residue that is currently brittle under lexical heuristics:

- invented voluntary PC speech/thought/action;
- unsupported narration additions;
- paraphrased semantic contradiction;
- implicit secret/knowledge leakage.

Keep exact/computable checks deterministic: IDs, ownership, provenance/source existence, visibility authorization, hidden literal/DC leakage, typed contract invariants, mechanics legality, etc.

A positive semantic judge can never override a deterministic failure.

Private/secret state is filtered and scoped **before** candidate/model exposure; a decision model is never allowed to see unauthorized records merely so it can rank them.

## 9. Observability, evals, and approval

#383 should retain per decision run:

- decision role and execution mode (`shadow`, `primer`, `direct`);
- adapter/provider/model/version;
- question/candidate/schema/policy versions;
- candidate IDs and full available probability distribution;
- selected answer, runner-up/margin where available;
- latency/cost;
- deterministic revalidation result;
- downstream correction/repair/validator signals where available.

#268 evaluates each decision role independently: confusion/accuracy, calibration by bucket, direct-execution error rate, unnecessary escalation, failure to choose the escape path, robustness to candidate ordering/noise, and generative calls avoided.

#269 approves exact roles/policies rather than a model globally. A decision model may be approved for retrieval reranking and still be unapproved for direct canon-sensitive identity resolution.

## 10. Explicit non-fits

Do not delegate these to Jev merely because Jev is cheap/fast:

- provider HTTP/config/retryability classification;
- authorization / RLS / audience scope;
- rule legality and arithmetic;
- dice results;
- action economy and VTT geometry;
- idempotency / transactional correctness;
- billing/capacity accounting;
- arbitrary new prose/fiction;
- arbitrary unconstrained materialization patches.

## 11. Rollout philosophy

This is pre-alpha and there is one active user, so experimentation can be aggressive without pretending early thresholds are already production-calibrated.

Use a mix of:

- shadow comparison where mistakes would be difficult to observe or repair;
- experimental active direct execution where code tightly constrains/revalidates the action space;
- primer/advisory use where a generative model still needs to assemble novel content;
- explicit open-ended escalation whenever bounded coverage is incomplete.

The goal is not to make Jev the DM. The goal is to stop using a generative model to rediscover semantic choices that the runtime can safely bound, while concentrating generative intelligence on the genuinely open-ended parts of tabletop play.

## 12. Source of truth

Implementation scope and sequencing live in:

- #379–#384 for the decision-control-plane foundation;
- amended subsystem issues such as #214, #217–#220, #229, #233, #236, #248/#251/#252, #258, #268/#269, #273/#274;
- the linked GitHub Project **DND AI — Development** for cross-issue priority/readiness.

This document is design/background context and should not be used as a parallel roadmap.

## External background

Jev is a TypeSafe AI bounded decision model designed around typed choices/scores/boolean-style judgments and parallel decision questions rather than arbitrary text generation. Public TypeSafe material and third-party demos should be treated as useful architectural evidence, not as a substitute for calibration on this application's own D&D decision roles.

References:
- https://docs.typesafe.ai/
- https://typesafe.ai/blog/introducing-system-one-models-and-jev

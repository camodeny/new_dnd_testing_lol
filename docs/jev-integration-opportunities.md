# Jev (TypeSafe AI) — Integration Opportunities

Date: 2026-09-17
Status: Exploration — no code changes yet
Context: User got early access to Jev, asked where it could plug into this app.

## 1. What Jev is

- TypeSafe AI's first "System One Model" (launched Sep 15-16, 2026). Founder: Diogo Almeida (co-invented RLHF / InstructGPT).
- Not an LLM chatbot: no text generation. Input = unstructured state + typed questions. Output = typed decisions + calibrated probabilities + confidence.
- Three primitives (can be mixed in one parallel call):
  - `Choice` — pick from list (up to 255 options) → `choice, probabilities, confidence`
  - `Score` — score state on a rubric → `score, probabilities, confidence`
  - `Noul` — is this statement true? → `noul (0-1)`
- Claims: 70–500ms end-to-end (40–200x faster than frontier LLMs on same decision tasks), $0.042 / MTok input, output tokens free, no type errors / hallucinations by construction.
- Training: RLCD (Reinforcement Learning for Calibrated Decisions), new architecture + parallel sampler.
- Best for: classify / route / score / verify / guardrail / boolean checks inside code loops. Worst for: prose, contracts requiring generation, images (structured text state only per current demos).
- Docs: https://docs.typesafe.ai/introduction.md | Launch post: https://typesafe.ai/blog/introducing-system-one-models-and-jev

Key architectural rule from docs: one atomic gut-check per question. If a judgment needs extended reasoning or weighs multiple factors, decompose into separate questions and combine in code.

## 2. Why it matters here

Our per-turn DM pipeline is LLM-heavy in exactly the places Jev is built for:

`context packet → adjudicate (LLM) → validators (heuristics) → evidence loop (LLM x up to 4) → regen loop (LLM x up to 4) → narrate (LLM stream)`

Worst case today is multiplicative: evidence loop (4) × regen loop (4) = up to ~16 LLM calls per turn, each re-sending a growing packet. Validators that gate those retries are currently regex / keyword lists / token-overlap — cheap but brittle.

Jev fits as the **decision shell around the generative core**: keep LLMs for prose + full `DmTurnContractV1` assembly, use Jev for fast/cheap routing, verification, and confidence-gating.

What Jev explicitly cannot do: generate the turn packet / contract / narration. That requires string generation, which Jev gives up by design.

## 3. Plug-in opportunities (ranked)

### P1 — Turn-mode pre-router
- Where: `backend/app/dm/adjudication.py:22-70` (`FORWARD_DM_SYSTEM` modes), consumed by `execution.py:803,821`, `evidence.py:709`
- Today: single `execute_chat` with strict JSON schema infers `mode: respond | await_roll | need_evidence | clarify | table_chat | silent | unsupported` as part of full contract generation.
- Jev shape: `Choice` with 7 options. State = player input + scene summary + history tail. Questions: `mode_choice`.
- Why: cheapest way to cut a full LLM call when turn is trivially `silent` / `table_chat`, or to short-circuit to roll/clarify paths. Also gives calibrated `confidence` to decide auto-act vs full adjudication.
- Notes: keep LLM as authority initially; run Jev in shadow/log-only to measure agreement.

### P2 — Validator upgrades (Agency / Visibility / Epistemic / Canon)
- Where: `backend/app/dm/validators.py:241-859` (`Agency, Ownership, Entity, Provenance, Epistemic, Visibility, CanonValidator`), plus `narration.py:426-560,608-628` (`validate_narration_fidelity`, `secret_leakage`, `agency_violation`)
- Today: pydantic + set-membership + regex (`_SPEECH_ATTRIBUTION_RE:361`), keyword lists (`_CONSEQUENCE_VERBS:351`, `_VOLUNTARY_PC_VERBS:356`), antonym pairs, token-overlap. PC-agency branch disabled (`_PC_AGENCY_CHECK_ENABLED=False:375`).
- Jev shape:
  - `Noul`: "This beat invents voluntary PC speech/thought/action." / "This claim leaks dm_private truth / hidden DCs / internal IDs." / "This claim contradicts established canon."
  - `Score`: agency-risk 0-5, secrecy-risk 0-5, canon-conflict 0-5
  - `Choice`: `claim_kind (observation | world_fact | npc_utterance | player_declaration | roll_instruction | roll_outcome)`, `origin`, `truth_status (truthful | deceptive | mistaken | incomplete | unknown)`
- Why: these are single gut-check judgments Jev is built for, and current heuristics are the brittleness point that causes expensive regen retries (`validators.py:1177`, max 3 regens).
- Notes: run parallel — all questions in one call against same state; adding questions barely changes latency per docs.

### P3 — Evidence-loop gate (`need_evidence` stop/continue)
- Where: `backend/app/dm/evidence.py:706,709` (`run_bounded_evidence_loop`, `MAX_EVIDENCE_ROUNDS=3` → up to 4 adjudications/turn)
- Today: LLM decides `need_evidence` with 1-3 `evidence_requests` + `safe_prelude`; loop re-sends grown packet each round.
- Jev shape: `Choice: [proceed, need_evidence, clarify]` + `Score: evidence_sufficiency` + `Noul: "Required fact is missing to resolve intent."`
- Why: model can prolong loop today; a cheap gate with calibrated confidence lets code enforce stopping rules.

### P4 — Regen / failure classifier
- Where: `backend/app/dm/execution.py:147-165` (`_classify_failure` → `retriable | terminal`), `adjudication.py:356,555`, `execution.py:743,752` (evidence × regen cascade), `validators.py:1177` (`run_with_bounded_regeneration`)
- Today: exception-type / string-match heuristics.
- Jev shape: `Choice: [retriable, terminal]` + `Score: repair_likelihood` + `Noul: "Re-running with same packet is likely to succeed."`
- Why: decides whether to burn another full adjudication. High leverage on cost/latency multiplier.

### P5 — Memory / audit judges (map-reduce scoring)
- Where:
  - `legacy_system/server/services/dm_memory_repair.py:640` repair-judge loop
  - `legacy_system/server/services/memory_recovery.py:239`, `openrouter.py:6313` staged memory-writer (1×/post-turn + retries)
  - `legacy_system/server/services/automation_auditor.py:1468,1470` (up to ~15 LLM calls per audit cycle, 180s timeout)
  - `legacy_system/server/services/encounter_map_service.py:1396,1402,880,894` (image + vision-LLM QA — note: Jev does NOT do images yet, so map QA stays LLM for now)
- Today: full LLMs used as judges/scorers in loops.
- Jev shape: `Score` rubrics (patch-correctness, memory-salience, audit-severity) + `Noul` validity checks, map-reduced in code.
- Why: docs' canonical Jev workload — turn petabytes into features, verify everything. Auditor loop is the biggest call-count hotspot in repo.

### P6 — Chat / fork routing (high volume, low complexity)
- Where: `backend/app/characters/chat/service.py:156` (1 stream/message + tool loop), `legacy_system/server/services/clarification_forks.py:267,311` (1 LLM/fork turn, multiplies concurrent load)
- Today: full chat LLM per message.
- Jev shape: `Choice: [answer_directly, needs_tools, escalate_to_dm, table_chat]` as pre-router; `Score: ambiguity` to trigger clarification forks only when needed.
- Why: user-facing TTFT win; cheap filter before expensive path.

### P7 — Claim-level provenance / truth-status tagging
- Where: `backend/app/dm/adjudication.py:84-199` (per-claim `claim_kind`, `origin`, `beat.type`, `truth_status`, `visibility`, `effect_type`, `evidence.tool`, `roll.*`)
- Today: inferred as part of full contract JSON by generative LLM, no confidence attached.
- Jev shape: parallel `Choice` per claim/beat. Each claim scored independently against same state — matches Jev's "parallel, no context-rot" design.
- Why: gives calibrated probabilities code can branch/sort/route on; enables thresholding (auto-apply vs review).

## 4. Explicit non-fits

- Full `DmTurnContractV1` assembly (`adjudication.py:202-366`, `normalize_contract`) — requires constrained JSON generation with cross-field rules (≤2 new entities, ≤4 staged effects, roll handles). Jev has no string generation.
- Narration prose (`adjudication.py:369-599` `build_provider_narrator`, `stream_chat`) — generative by definition.
- Context packet assembly (`context.py`, `router.py`, `turns.py`) — deterministic; no judgment to replace.
- Image map QA — Jev demos note structured text state only, not images yet.
- Anything needing chain-of-thought reasoning over multiple independent factors — must be decomposed first per TypeSafe guidance, not sent as one mega-question.

## 5. Suggested rollout

1. Shadow mode: add Jev client + log `Choice/Score/Noul` alongside existing LLM/heuristic decisions for P1+P2. Measure agreement + calibration. No behavior change.
2. Gate mode: use Jev confidence thresholds to skip/keep expensive paths (e.g., high-confidence `silent/table_chat`, low-risk validator pass). Keep LLM fallback.
3. Replace mode: only after shadow data shows parity, replace heuristic validators (P2) and failure classifier (P4) outright. Keep bounded regen caps.
4. Budget enforcement: cap combined evidence × regen budget in `execution.py:743,752` regardless of model — currently multiplicative worst case.

## 6. Open questions for spike

- Auth / SDK: API key shape, Python adapter (`system-one-adapter-python` mentioned in launch post), Vercel AI Gateway `typesafe-ai/jev` (`evaluate()` example) vs direct API.
- Calibration: do returned `confidence` scores actually threshold cleanly on our validators? Needs shadow data.
- Cardinality limits: docs say up to 255 options; high-cardinality choices use 2-stage scoring — relevant for entity/proposal ranking if we try it.
- Latency from our infra: published 70–500ms measured from US West Coast laptops; verify from our deploy region.
- Cost baseline: capture current per-turn LLM $/latency before claiming 100x wins — launch-page 193.6× faster / 444.6× cheaper is on their 4 workflow evals vs GPT-6 Astra / Fable 5.1 average, not necessarily our workload.

## 7. Source notes

- Code refs from `backend/app/dm/{adjudication,validators,narration,execution,evidence,context,router,turns,recovery}.py` and `legacy_system/server/services/{encounter_map_service,dm_memory_repair,automation_auditor,clarification_forks}.py`, inspected 2026-09-17.
- Jev behavior/pricing from `docs.typesafe.ai` + `typesafe.ai/blog/introducing-system-one-models-and-jev` (early access, Sep 2026).

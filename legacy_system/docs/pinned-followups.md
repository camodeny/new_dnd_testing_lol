# Pinned Follow-Ups

Work queue of items intentionally deferred for a future session. When an item is
done, remove it from this file.

## 1. NPC dossier public/private content-visibility split (PINNED 2026-08-15)

**Context:** Run 56 (Memory Audit Scorebook) flagged Criterion 8 as `warn` in
both audited cycles: "Mixed-visibility party_known NPC dossiers contain private
material." Party-known NPC dossiers (e.g. `selka_moss`, `brother_cyrus`,
`ferra_holk`) carry `visibility: "party_known"` while their blob also holds
DM-private `background`, `secrets`, `recent_offscreen_activity`, `wants`,
`fears` (audit events 23548 / 23636, derived campaign 59).

**The problem:** `visibility` on an NPC dossier is overloaded. It means *identity*
visibility (does the party know this NPC exists?) but is consumed in places as if
it were *content* visibility:

- The DM prompt says "Treat public and party_known items as grounded context"
  (`openrouter.py` build_session_dm_tool_messages), and `internal_only` is
  computed as `visibility == 'dm_private'` (`dm_tools.py` build_session_retrieval_packet).
- The leak guard's NPC branch (`dm_tools.py` `_leak_guard_memory_patch`) only
  inspects `name`, `role`, `public_summary` — it structurally cannot catch
  private fields riding inside a party_known dossier.
- Embeddings (`embedding_service.py` `canonical_text_for_item` for `npc_actor`)
  embed `Secrets` + `Recent offscreen activity` regardless of visibility. They
  only stayed `dm_private` in run 56 by luck: `upsert_memory_embedding` reads
  top-level `visibility`, which `NPCActor.to_dict()` does not expose.

**No current direct party leak** (party-facing surfaces use
`world.to_public_dict()` / `NPCActor.to_dict(include_private=False)`), and the
DM retrieval packet compacts NPCs to name/role/public_summary + wants/fears
(currently empty due to a nesting bug). But the labeling is fragile and the
embedding path is a latent leak if visibility is ever used as a content gate.

**Proposed fix direction:** separate identity visibility from content
visibility for NPC dossiers — e.g. an explicit `public` slice vs `private`
slice inside the dossier, or a distinct content tier; then teach the leak guard,
embedding compaction, and `internal_only` labeling to use it.

## 2. Leak-guard demotion heuristics are brittle; rework toward provenance-first (PINNED 2026-08-15)

`_leak_guard_has_private_content` (dm_tools.py:305) demotes a party-visible item
to `dm_private` if ANY of three paths fires. All three are heuristic and all
three have demonstrated or latent failure modes; run 56 surfaced path 2, and
the others are only un-misfired because their inputs are absent or benign:

**Path 1 — provenance evidence** (dm_tools.py:313-317,
`_evidence_source_is_party_visible`): the RIGHT idea (don't promote an item if
it cites private sources) but under-wired AND the rule itself conflates two
questions. Compiled facts in run 56 had `provenance.evidence_sources: None`, so
it never ran. Conceptually, however, "public knowledge backed by private
knowledge" is the normal case — the DM translates private canon into public play
every turn. The system already models that as two parallel records (e.g. party
`fact_4` "Selka claims she saw Osric enter the boathouse" vs dm_private `fact_8`
"Private canon: the claim is invented misdirection paid for by House Ashfen";
the extraction prompt's `dm_private_context` rule at openrouter.py:280 encodes
the same split). The current `any(private source) -> demote` answers "was this
informed by private knowledge?" instead of "is this item's CONTENT party-safe?"
so a correctly-provenanced party fact would be demoted exactly because the
resolver cited its private interpretation. Correct rule: a party_known item must
have party-visible *content* backing (transcript source / disclosed facet);
private interpretation lives in a parallel dm_private record and must not gate
the public record's visibility. The guard's real job is preventing the reverse —
laundering private canon into a party_known item with no party-visible backing.

**TODO (investigate): why is `provenance.evidence_sources` always None?**
`_resolve_evidence_sources` (session_memory_agent.py:1427) reads, in order:
item `evidence` -> item `provenance.evidence_sources` -> item `evidence_basis`.
In run 56 all three were absent on the compiled facts, so the provenance-first
path never ran. The resolver's `submit_resolved_memory` DOES emit a top-level
`evidence_basis` (transcript_message_673, world_state_dm_private,
npc_dossier_selka_moss, get_scene_candidates, ...), but that global basis is
never propagated to per-item provenance, and the extraction/resolver prompts
(openrouter.py:283-308) list `evidence` fields but the model doesn't populate
them per item. Question to answer: should the resolver schema require per-item
`evidence`/`source_surface` provenance, or should compile attach the relevant
subset of the top-level `evidence_basis` to each item so Path 1 can actually
decide? Also note the risk if fixed naively: wiring the global basis (which
contains dm_private sources) into per-item sources would demote everything.

**Path 2 — hidden identifier backstop** (`_protected_identifier_terms` +
`_memory_text_has_hidden_identifier`): the most dangerous; caused run 56's
demotions. It treats "stored as dm_private" as "is a secret" — conflating
storage tier with secrecy. Any dm_private entity whose name is the public
subject of the scene becomes a forbidden word:
- `basilica_boathouse` (dm_private, name "the boathouse") is a duplicate of the
  party_known `boathouse` entity.
- `harbormaster_osric` (dm_private, name "Harbormaster Osric") is a
  publicly-known NPC whose graph identity is kept dm_private.
- The only escape hatch is "did the *current turn's* two messages mention the
  name?" — cycle 2's lockplate close-up (msg 674/675) didn't, so the exclusion
  check failed and all four party-observable facts were demoted, then
  `_upsert_by_id_strict`'s last-wins merge overwrote cycle 1's correct
  party_known versions.
- Context-blind beyond the latest exchange; depends on graph modeling
  (duplicate entities) and on the disclosure registry being populated (run 56
  had no disclosure rows, so every dm_private entity was "protected").
- **Code/doc contradiction:** the docstring (dm_tools.py:237-240) says facts are
  NOT guarded lexically and are governed by provenance validation, but the fact
  branch in `_leak_guard_memory_patch` calls `_leak_guard_has_private_content`,
  which applies the backstop to fact text anyway.

**Path 3 — private-vocabulary signal** (`_contains_hidden_policy_signal`,
word list: private/secret/hidden/unrevealed/spoiler/offscreen/off-screen):
flakiest lexically — over-matches innocent narration ("the hidden staircase",
"the secret passage") and under-matches real secrets phrased without those words
("the lock was jammed from the inside"). The `_is_supported_by_visible_exchange`
overlap mitigation only checks the latest exchange, and the whole path silently
switches off when `visible_text` is empty.

**Direction:** these heuristics exist as substitutes for provenance the resolver
isn't reliably emitting. Prefer provenance-first: require the resolver to attach
per-item evidence sources (transcript/visible = party-safe; dm_private/internal_only
= demote), narrow or drop paths 2-3, and clean up the duplicate-entity noise
(`basilica_boathouse` vs `boathouse`) that feeds the backstop. The running-summary
redaction that shared this mechanism was already dropped (see item 3).

**EXPERIMENT DONE 2026-08-16 — model adjudicator now the only behavior:** implemented
in `dm_tools.py`. The deterministic flags are collected and every flagged item is
sent to a real LLM (`_adjudicate_leak_guard_flags`) which decides `keep` vs
`demote`, using the recent transcript + current exchange. (Originally behind
`DND_LEAK_GUARD_ADJUDICATOR`; made always-on in the PR.) Re-ran run 56's
cycle-2 failure point (the compiled patch from audit event 23627) 30 times
against a local copy of the deployed DB:

- Baseline (deterministic): `facts_demoted=4` on every run — fact_1..fact_4 all
  dm_private (reproduces the run-56 bug exactly).
- Adjudicated: fact_1/fact_2/fact_3 kept party_known **30/30**; fact_4 (Selka's
  public claim, mentions "Harbormaster Osric") **24/30**; private canon facts
  fact_6..fact_8 stayed dm_private **30/30**. All four kept: **24/30 (80%)**,
  up from 0%.
- Failure-mode analysis: initial version lost ~30% of runs to JSON truncation
  (`max_tokens=1200` cut off mid-JSON) and empty responses — NOT wrong judgment.
  When the model answered, it said "keep" ~100% of the time. Hardened with
  max_tokens=3000, short-reason instruction, robust JSON extraction, and one
  retry → 0 parse failures. The only remaining misses are genuine (and debatable)
  model conservatism on fact_4 (~5-20%).
- The adjudicator's reasoning was grounded: it cited Mira's investigation and
  Selka's in-world statement, i.e. it read the transcript rather than guessing.

Follow-ups from the experiment: decide whether fact_4-style claims (party heard a
lie; DM holds the truth) should be adjudicated differently (e.g. always keep the
surface claim + ensure the companion private record exists); consider gating the
adjudicator batch to one call per turn (already the shape); and validate on more
failure points than this one before enabling by default.

## 3. Running-summary redaction removed (DONE 2026-08-15)

The DM running summary is no longer pre-redacted at the write boundary. The DM
is expected to know private context; leak prevention is the job of output-side
defenses (canon discipline check, private output guard, spoiler checker).
Changes: removed the running-summary block from `_leak_guard_memory_patch`,
removed `redact_session_summary_private_terms` and its call sites in
`dm_tools.py`, `memory_recovery.py`, and `routes/sessions.py`. Memory anchors
are still guarded (they are served to campaign members).

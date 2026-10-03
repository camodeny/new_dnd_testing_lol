# SRD retrieval and Jev latency experiment — 2026-10-03

Recommendation: use local BM25 candidate retrieval followed by one batched Jev
relevance/ranking call, retaining at most three canonical passages. This was the
best measured balance of quality, latency, and excluding rules from ordinary
conversation. Keep legality in deterministic code and treat the later Jev
proposal check as advisory. These experiments do not change production routing.

## Measured results

Thirty synthetic cases (24 mechanics, six conversation), each repeated twice.
The table's accuracy is agreement with the expected advisory outcome across all
60 trials per method. Repeats are not independent new examples.

| Approach | Advisory accuracy | Median added time | p95 added time |
|---|---:|---:|---:|
| Configured Postgres lexical fallback + Jev | 23.3% | 97 ms | 171 ms |
| BM25, then advisory check | 86.7% | 268 ms | 394 ms |
| **BM25 + Jev rerank, then advisory check** | **93.3%** | **533 ms** | **848 ms** |
| Hierarchical title selection, then advisory check | 46.7% | 828 ms | 1,354 ms |
| Title selection + BM25 + Jev rerank, then advisory check | 86.7% | 1,144 ms | 1,968 ms |
| All core rules + BM25 extras, one advisory call | 91.7% | 625 ms | 878 ms |

The first five totals include retrieval before the AI DM turn and the advisory
check after the proposed resolution. They exclude DM generation, dice/domain
execution, startup and database connection establishment. The full-core variant
judges the proposed resolution directly; it does **not** produce a bounded set of
references for the AI DM's pre-turn context, so it is not a drop-in replacement.

For the recommended path, retrieval/reranking took 305 ms median (486 ms p95).
The advisory stage took 241 ms median (411 ms p95), including packet assembly;
conversation trials usually skipped that stage. Stage medians do not add to the
median total. A perfect-evidence diagnostic control reached 98.3% advisory
accuracy at 251 ms median / 372 ms p95. That control used local gold source IDs
and is not a deployable retrieval strategy or a gold-free comparison.

## Retrieval quality

These are exact coverage of curated canonical source groups, not proof of
semantic correctness. An alternative passage can sometimes support a ruling
without matching the curated source ID; advisory classification is a separate
metric. Multiple sufficient sources are alternatives within a group.

| Approach | Primary source recall | All required groups covered | Empty context on conversation |
|---|---:|---:|---:|
| Configured lexical + Jev | 0.0% | 0.0% | 100% |
| BM25 | 66.7% | 54.2% | 0% |
| BM25 + Jev | 77.1% | 68.8% | 100% |
| Titles | 22.9% | 22.9% | 100% |
| Titles + BM25 + Jev | 72.9% | 62.5% | 100% |

The configured fallback returned no retained references in 46 of 48 mechanical
trials. Its low latency mostly measures missing evidence, not successful rules
checking. Without retained references, a plain `respond` proposal with
`dm_adjudication` claims can be marked NOT_MECHANICAL by the production advisory
heuristic, even if its prose discusses mechanics. That is an observed coverage
and detection limitation, not a successful rule verification.

BM25 plus Jev missed the Prone governing passage and the full Paralyzed →
Incapacitated reaction interaction in both repeats. Its four incorrect advisory
outcomes were NOT_MECHANICAL or INSUFFICIENT_EVIDENCE. Full-core judging instead
incorrectly returned SUPPORTED for the teleportation/opportunity-attack and
incapacitation/concentration contradictions in both repeats, and the invalid
Hide proposal once. Even the perfect-evidence control incorrectly supported the
concentration contradiction once. Jev must not own rule legality.

## All-rules feasibility and HTTP transport

The canonical corpus contains 2,667 sections and 1,156,463 body characters.
A compact title/body JSON payload for the entire SRD is approximately 313,126
`cl100k_base` tokens; this is a sizing proxy, not Jev's tokenizer. It exceeds
Jev's documented 64k total-request / 32k state-plus-longest-question limits.
Choice questions also have a 255-option limit. See the official
[model limits](https://docs.typesafe.ai/models) and
[API contract](https://docs.typesafe.ai/api).

Playing the Game plus Rules Glossary contains 261 sections and 109,012 body
characters. The full-core experiment sends all their bodies, without truncation,
plus up to eight BM25 passages from other documents to one four-outcome question.
Actual provider-reported input usage was about 31,104 tokens median, up to 32,473.
This fits the tested provider request but leaves little room for more state.
It excludes most spell, class, equipment and creature text; it is **not all SRD**.

The stock adapter uses fresh `requests.post` connections. Ten paired calls using
identical synthetic action/evidence, alternating order, measured:

| Transport | Median | p95 |
|---|---:|---:|
| Fresh connection | 226 ms | 327 ms |
| Reused `requests.Session` | 153 ms | 266 ms |

This suggests connection reuse is useful, but the full algorithm benchmarks used
the stock transport. No pooled end-to-end estimate is claimed. The first pooled
call is included. The pooled adapter is confined to the experiment; production
transport was not changed.

## Method and limitations

- The configured database has only `stub-hash-v1` embeddings, and no Gemini key
  is configured. Stub vectors are ignored. **This does not evaluate real vector
  retrieval and cannot establish that BM25 beats embeddings.**
- The snapshot and BM25 index are warmed in memory. Index construction took
  315 ms, recorded exactly in the raw report. Canonical references are
  rehydrated from the same immutable snapshot for every experimental path.
  Production database freshness checks would add work not measured here.
- The configured baseline executes the real `hybrid_search` lexical fallback
  against a read-only transaction, restricted to the same `dnd-srd` corpus.
- BM25 searches titles/headings and body text, with generic English normalization.
  No case-specific routing or hand-authored gameplay keyword table is used.
- Title routing enumerates document/heading groups and alphabetical ranges where
  needed, with NONE available and bounded Choice candidates. Branch questions
  request primary and interacting sources; leaf probabilities nominate up to
  three passages. The standalone path retains the first three nominated passages;
  the union path interleaves up to four title and four BM25 candidates and reranks.
  These findings apply to this prototype, not every possible title router.
- Reranking uses the actual production batched Choice + Score questions. The
  advisory experiment invokes the actual production `check_rules_advisory`.
  The full-core experiment reuses its question but bypasses the three-passage cap.
- The actual model identity returned by all successful calls was `jev-1.13.0`.
  Requests used the configured alias. Gold labels remain local except in the
  explicitly labelled perfect-evidence diagnostic control. No campaign/private
  data was sent, no database writes were made, and no dev server was started.
- There were 716 recorded live provider calls across the final comparisons and
  transport probe, with no provider failures. Preliminary smoke calls are excluded.
  This is a small exploratory benchmark, not a broad D&D compliance evaluation;
  network latency and classification can vary between runs.

Corpus content SHA-256 (sorted-key compact UTF-8 JSON):
`b2c10081e56628b53a5de3d568aab934bb955a4765625b8d914d8072353c8d9e`.
Byte hashes, fixture hashes, per-case IDs, verdicts, provider input usage and call
latencies are retained in the [retrieval report](runs/2026-10-03-rules-retrieval.json),
[full-context report](runs/2026-10-03-rules-full-context.json), and
[transport report](runs/2026-10-03-jev-transport.json).

## Reproduce

From `backend`, using configured database credentials and `TYPESAFE_API_KEY`:

```sh
PYTHONPATH=. python3 scripts/export_rules_eval_corpus.py --output /tmp/srd-eval.json
PYTHONPATH=. python3 scripts/evaluate_rules_retrieval.py --corpus /tmp/srd-eval.json --output /tmp/retrieval.json --repeats 2
PYTHONPATH=.:scripts python3 scripts/evaluate_rules_full_context.py --corpus /tmp/srd-eval.json --output /tmp/full-context.json --repeats 2
PYTHONPATH=.:scripts python3 scripts/evaluate_jev_transport.py --corpus /tmp/srd-eval.json --output /tmp/transport.json --pairs 10
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_rules_selection_prototype.py -q
```

Verification: all seven new prototype tests passed, and Ruff/diff checks passed.
The broader relevant run had 217 passing tests, one skip and one failing existing
wall-clock deadline test (`test_tool_timeout_retry_with_deadline`). It reproduced
alone: two nominal 100 ms joins took about 200 ms each, and a nominal 50 ms sleep
took about 150 ms on this workstation, exceeding the test's 400 ms bound. The
evidence timeout implementation and that test were unchanged by this prototype.

## Production adoption

The winning BM25 + Jev path is now wired into automatic `enrich_rules_context`.
The experiment and production code share `Bm25Index`; there is no title-router
or embedding step in automatic guidance. Production uses hourly per-engine
postings caches, independent public-source reads, and one batched fresh canonical
lookup before reranking. Explicit hybrid-search tools remain available. A live
production-path smoke check retained Fireball references, removed dialogue
references, and reused the enriched packet without additional calls. First-load
latency was 2.8 seconds; a warm dialogue check took 0.5 seconds on this workstation.
These timings include live database work that the original warm snapshot benchmark
excluded. The selected relevant suite passed 224 tests with one skip and the
previously diagnosed wall-clock deadline test explicitly deselected.

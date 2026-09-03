# Rules Corpus — SRD 5.2.1 (Issue #223)

**Authority:** Wizards of the Coast SRD 5.2.1 / 2024 revision (CC BY 4.0). Store exact corpus version, source URL, license, attribution, import/build timestamp, and hash of source artifact.

- Official: https://www.dndbeyond.com/srd (also https://media.dndbeyond.com/compendium-images/srd/SRD_CC_v5.2.1.pdf)
- Structured bootstrap (pinned): `Cantilux/dnd-srd-json` (treat as derivative, validate before promotion)
- Cross-check: Open5e `open5e/open5e-api`
- Markdown fallback: `downfallx/dnd-5e-srd-markdown`
- Refs only: `azemoning/omni-5e`, `igorsobralcc/dnd-compendium-service`

**Content rights:** Launch corpus contains only SRD 5.2.1 content legally permitted to redistribute/use. All citations preserve CC BY attribution (`app/rules/metadata.py:ATTRIBUTION`). See `models.RulesCorpus` / `RulesSection.citation_metadata`.

**Identity:** `rule_id` (app-owned, e.g. `srd521.playing-the-game.combat.melee-attacks.opportunity-attacks`), `source_section_id` (kebab of document+full heading path), `corpus_id`/`corpus_version`, document+heading path, body+tables, source/content hashes. Deterministic derivation in `app/rules/ids.py`; collisions fail import and require `app/rules/aliases.json` override.

**Storage:** `rules_corpora`, `rules_sections` (canonical, + FTS), `rules_section_aliases`, `rules_embeddings` (derived pgvector, rebuildable), `rules_corpus_imports` (provenance). Independent of campaign/world-memory tables.

**Ingest:** `scripts/ingest_rules.py` + `app/rules/ingest.py` — deterministic, versioned, fail-closed. Validates pinned JSON against the official artifact hash, canary `weapon mastery` (5.2.1 addition), collision/alias checks. Promote only on success; log `build_id`, checksum, validation errors. The trusted official artifact SHA-256 is versioned solely in `app/rules/official_manifest.json` (single source of truth); derivative dataset hashes are stored separately as `derivative_checksum` in `pinned_inputs`.

**Retrieval:** `app/rules/store.py` — exact `lookup_by_rule_id` + bounded hybrid lexical (`tsvector`/`LIKE`) + vector (`pgvector HNSW`, fallback to lexical if unavailable) with RRF fusion; scores are metadata only, citations from canonical records. Duplicate concepts remain separately addressable.

**Evidence tools:** `app/rules/evidence_tools.py` → `lookup_rule` / `search_rules` via `app/dm/evidence.py#203` (`ALLOWED_TOOLS`). Bounded output, `missing`/`tool_failure` never hallucinated.

**Embeddings:** `app/rules/embeddings.py` + `scripts/build_rule_embeddings.py` — section_with_heading_context, versioned `model/version/build_id`; rebuild does not mutate `rule_id`.
- Default: `stub-hash-v1` (1536 dims, deterministic, offline/test).
- **Gemini Embeddings 2:** `gemini-embedding-2` (default, 3072 dims, multimodal, 8192 tokens, falls back to `gemini-embedding-001` if 2 not yet GA) or `gemini-embedding-001` (2048 tokens) / `text-embedding-004` (768 dims). Set `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) and run:
  `GEMINI_API_KEY=... python -m scripts.build_rule_embeddings --model gemini-embedding-2` (aliases: `2`, `gemini-2`). Per [Gemini docs](https://ai.google.dev/gemini-api/docs/embeddings), 001 uses `taskType` field (`RETRIEVAL_DOCUMENT`/`RETRIEVAL_QUERY`), while 2 requires prefixed prompt `task: retrieval | document: ...` / `task: retrieval | query: ...` (handled automatically), and spaces are incompatible. `GEMINI_EMBEDDING_DIM` clamps MRL output (e.g. `768`). Provider is `app/rules/gemini.py:gemini_embed_texts` (`batchEmbedContents`, batch 100, retries + 404 fallback 2→001). `vector` column is generic (`vector`) so 768/1536/3072 all work. Without API key or on failure, search gracefully degrades to lexical-only.

**API:** `GET /api/rules/lookup?rule_id=`, `GET /api/rules/search?q=&limit=`, `GET /api/rules/corpora`.

**Observability:** logs: corpus/version/build_id, import failures, checksum, lookup/search latency, exact/lexical/vector path, miss rate, citation IDs, embedding model/version failures, result counts/scores (see `store.py` / `evidence.py` structured logs).

**Migrations:** explicit via `alembic upgrade head` (`2a04bc8c83ba_initial_schema.py`), no cold-start DDL per #187.

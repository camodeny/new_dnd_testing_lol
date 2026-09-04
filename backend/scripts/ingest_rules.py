"""CLI for deterministic rules corpus import — issue #223.

Usage:
  python -m scripts.ingest_rules --fixture backend/tests/fixtures/srd_sample.json
  python -m scripts.ingest_rules --markdown path/to/srd.md
  CANTILUX_COMMIT=abc123 python -m scripts.ingest_rules --cantilux-json path/to/cantilux_dump.json

Validates against official SRD before promotion (fail-closed).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from database import SessionLocal
from app.rules.ingest import import_fixture_sections, import_markdown_text, parse_cantilux_json, compute_sha256
from app.rules.metadata import CORPUS_ID, CORPUS_VERSION, OFFICIAL_SRD_URL
from app.rules.embeddings import build_embeddings

logger = logging.getLogger(__name__)

GEMINI_ALIASES = {
    "gemini",
    "gemini-2",
    "gemini-embedding-2",
    "2",
    "gemini-embedding-002",
}


def resolve_embedding_model(explicit_model: str | None, gemini_model: str | None) -> tuple[str, str]:
    """Resolve embedding model for --build-embeddings (issue #333).

    Precedence: explicit --model > --gemini-model > GEMINI_EMBEDDING_MODEL env >
    gemini-embedding-2 when a Gemini key is configured > stub-hash-v1.
    Returns (model, reason) where reason describes the selection for logging.
    """
    raw = explicit_model or gemini_model or os.getenv("GEMINI_EMBEDDING_MODEL")
    if raw:
        m = raw.strip()
        if m in GEMINI_ALIASES:
            return "gemini-embedding-2", "explicit-flag-alias"
        return m, "explicit"
    if os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or os.getenv("GOOGLE_GENAI_API_KEY"):
        return "gemini-embedding-2", "gemini-key-configured"
    return "stub-hash-v1", "stub_fallback_no_gemini_key"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--fixture", help="Path to JSON fixture (list of raw sections)")
    p.add_argument("--cantilux-json", help="Path to Cantilux dump JSON")
    p.add_argument("--markdown", help="Path to markdown fallback file")
    p.add_argument("--corpus-id", default=CORPUS_ID)
    p.add_argument("--corpus-version", default=CORPUS_VERSION)
    p.add_argument("--source-artifact-hash", default=None, help="SHA256 of official WotC SRD 5.2.1 artifact (required)")
    p.add_argument("--source-artifact-file", default=None, help="Path to official SRD artifact file to hash (PDF/HTML)")
    p.add_argument("--build-embeddings", action="store_true", help="Rebuild embeddings after import (selects gemini-embedding-2 when GEMINI_API_KEY is set, else stub-hash-v1 with explicit degradation log)")
    p.add_argument("--model", default=None, help="embedding model: gemini-embedding-2 (default when GEMINI_API_KEY set), gemini-embedding-001, text-embedding-004, stub-hash-v1 (offline/test)")
    p.add_argument("--gemini-model", default=None, help="alias for --model when using Gemini")
    p.add_argument("--embedding-version", default="1", help="embedding version tag")
    p.add_argument("--dim", type=int, default=None, help="output dimensionality for Gemini (e.g. 768, 3072) — also via GEMINI_EMBEDDING_DIM")
    p.add_argument("--validate-canaries", action="store_true", default=True)
    args = p.parse_args()

    embedding_model, embedding_reason = resolve_embedding_model(args.model, args.gemini_model)
    if args.dim is not None:
        os.environ["GEMINI_EMBEDDING_DIM"] = str(args.dim)

    if not SessionLocal:
        print("DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)

    raw = None
    if args.fixture:
        with open(args.fixture) as f:
            raw = json.load(f)
            if isinstance(raw, dict) and "sections" in raw:
                raw = raw["sections"]
    elif args.cantilux_json:
        with open(args.cantilux_json) as f:
            data = json.load(f)
        raw = parse_cantilux_json(data)
    elif args.markdown:
        with open(args.markdown) as f:
            md = f.read()
        # Resolve official hash
        official = args.source_artifact_hash
        if args.source_artifact_file:
            import hashlib
            official = hashlib.sha256(open(args.source_artifact_file, "rb").read()).hexdigest()
        if not official:
            print("ERROR: --source-artifact-hash (or --source-artifact-file) is required for promotion — official WotC artifact hash must be distinct from derivative", file=sys.stderr)
            sys.exit(2)
        derivative = compute_sha256(md)
        pinned = {"cantilux_commit": os.getenv("CANTILUX_COMMIT", "unknown"), "source": args.markdown, "derivative_checksum": derivative}
        db = SessionLocal()
        try:
            bid, recs = import_markdown_text(db, md, corpus_id=args.corpus_id, corpus_version=args.corpus_version, source_artifact_hash=official, pinned_inputs=pinned, validate_canaries=args.validate_canaries)
            print(f"Imported {len(recs)} sections build={bid} official={official[:12]} derivative={derivative[:12]}")
            if args.build_embeddings:
                if embedding_model == "stub-hash-v1":
                    msg = f"WARNING: --build-embeddings falling back to stub model={embedding_model} reason={embedding_reason} — retrieval will be lexical-only"
                    print(msg, file=sys.stderr)
                    logger.warning(msg)
                ebid = build_embeddings(db, corpus_id=args.corpus_id, corpus_version=args.corpus_version, embedding_model=embedding_model, embedding_version=args.embedding_version)
                print(f"Embeddings build={ebid} model={embedding_model} reason={embedding_reason}")
        finally:
            db.close()
        return
    else:
        p.error("one of --fixture, --cantilux-json, --markdown required")

    # Resolve official artifact hash — must be hash of official WotC artifact, not derivative
    official = args.source_artifact_hash
    if args.source_artifact_file:
        import hashlib
        official = hashlib.sha256(open(args.source_artifact_file, "rb").read()).hexdigest()
    if not official:
        print("ERROR: --source-artifact-hash (or --source-artifact-file) is required for promotion — official WotC artifact hash must be distinct from derivative", file=sys.stderr)
        sys.exit(2)
    derivative = compute_sha256(raw)
    pinned = {"cantilux_commit": os.getenv("CANTILUX_COMMIT", "unknown"), "source": args.cantilux_json or args.fixture, "derivative_checksum": derivative}
    # Keep official and derivative distinct for observability
    if official == derivative:
        print("WARNING: official hash equals derivative hash — ensure official is hash of authoritative WotC artifact, not normalized derivative", file=sys.stderr)
    db = SessionLocal()
    try:
        bid, recs = import_fixture_sections(
            db,
            raw,
            corpus_id=args.corpus_id,
            corpus_version=args.corpus_version,
            source_url=OFFICIAL_SRD_URL,
            source_artifact_hash=official,
            pinned_inputs=pinned,
            validate_canaries=args.validate_canaries,
        )
        print(f"Imported {len(recs)} sections build={bid} official={official[:12]} derivative={derivative[:12]}")
        if args.build_embeddings:
            if embedding_model == "stub-hash-v1":
                msg = f"WARNING: --build-embeddings falling back to stub model={embedding_model} reason={embedding_reason} — retrieval will be lexical-only"
                print(msg, file=sys.stderr)
                logger.warning(msg)
            ebid = build_embeddings(db, corpus_id=args.corpus_id, corpus_version=args.corpus_version, embedding_model=embedding_model, embedding_version=args.embedding_version)
            print(f"Embeddings build={ebid} model={embedding_model} reason={embedding_reason}")
    finally:
        db.close()


if __name__ == "__main__":
    main()

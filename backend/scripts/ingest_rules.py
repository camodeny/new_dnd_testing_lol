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
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from database import SessionLocal
from app.rules.ingest import import_fixture_sections, import_markdown_text, parse_cantilux_json, compute_source_checksum
from app.rules.metadata import CORPUS_ID, CORPUS_VERSION, OFFICIAL_SRD_URL
from app.rules.embeddings import build_embeddings


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--fixture", help="Path to JSON fixture (list of raw sections)")
    p.add_argument("--cantilux-json", help="Path to Cantilux dump JSON")
    p.add_argument("--markdown", help="Path to markdown fallback file")
    p.add_argument("--corpus-id", default=CORPUS_ID)
    p.add_argument("--corpus-version", default=CORPUS_VERSION)
    p.add_argument("--source-checksum", default=None)
    p.add_argument("--build-embeddings", action="store_true", help="Rebuild stub embeddings after import")
    p.add_argument("--validate-canaries", action="store_true", default=True)
    args = p.parse_args()

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
        db = SessionLocal()
        try:
            bid, recs = import_markdown_text(db, md, corpus_id=args.corpus_id, corpus_version=args.corpus_version)
            print(f"Imported {len(recs)} sections build={bid}")
            if args.build_embeddings:
                ebid = build_embeddings(db, corpus_id=args.corpus_id, corpus_version=args.corpus_version)
                print(f"Embeddings build={ebid}")
        finally:
            db.close()
        return
    else:
        p.error("one of --fixture, --cantilux-json, --markdown required")

    pinned = {"cantilux_commit": os.getenv("CANTILUX_COMMIT", "unknown"), "source": args.cantilux_json or args.fixture}
    db = SessionLocal()
    try:
        checksum = args.source_checksum or compute_source_checksum(raw)
        bid, recs = import_fixture_sections(
            db,
            raw,
            corpus_id=args.corpus_id,
            corpus_version=args.corpus_version,
            source_url=OFFICIAL_SRD_URL,
            source_checksum=checksum,
            pinned_inputs=pinned,
            validate_canaries=args.validate_canaries,
        )
        print(f"Imported {len(recs)} sections build={bid} checksum={checksum[:12]}")
        if args.build_embeddings:
            ebid = build_embeddings(db, corpus_id=args.corpus_id, corpus_version=args.corpus_version)
            print(f"Embeddings build={ebid}")
    finally:
        db.close()


if __name__ == "__main__":
    main()

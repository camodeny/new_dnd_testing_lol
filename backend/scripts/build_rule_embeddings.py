"""Rebuild rule embeddings as derived index — issue #223."""

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import argparse
from database import SessionLocal
from app.rules.embeddings import build_embeddings

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--corpus-id", default=None)
    p.add_argument("--corpus-version", default=None)
    p.add_argument("--model", default=None, help="embedding model: stub-hash-v1 (default), gemini-embedding-2 (Gemini Embeddings 2), gemini-embedding-001, text-embedding-004")
    p.add_argument("--version", default="1")
    p.add_argument("--gemini-model", default=None, help="alias for --model when using Gemini")
    p.add_argument("--dim", type=int, default=None, help="output dimensionality for Gemini (e.g. 768, 3072) — also via GEMINI_EMBEDDING_DIM")
    args = p.parse_args()
    # Resolve model: explicit --model > --gemini-model > env > default stub
    import os as _os

    if args.gemini_model and not args.model:
        args.model = args.gemini_model
    if not args.model:
        # If GEMINI_API_KEY is set, default to Gemini Embeddings 2
        if _os.getenv("GEMINI_API_KEY") or _os.getenv("GOOGLE_API_KEY"):
            args.model = "gemini-embedding-2"
        else:
            args.model = "stub-hash-v1"
    # Allow dim override via env for Gemini
    if args.dim is not None:
        _os.environ["GEMINI_EMBEDDING_DIM"] = str(args.dim)
    # Support gemini/text-embedding alias without models/ prefix
    if args.model:
        m = args.model.strip()
        if m in ("gemini", "gemini-2", "gemini-embedding-2", "2", "gemini-embedding-002"):
            args.model = "gemini-embedding-2"
        elif m in ("gemini-embedding-001", "001"):
            pass  # keep as requested
    if not SessionLocal:
        print("DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)
    db = SessionLocal()
    try:
        bid = build_embeddings(db, corpus_id=args.corpus_id, corpus_version=args.corpus_version, embedding_model=args.model, embedding_version=args.version)
        print(f"build_id={bid} model={args.model}")
    finally:
        db.close()

if __name__ == "__main__":
    main()

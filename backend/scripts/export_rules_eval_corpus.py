"""Export the canonical public SRD to a local evaluation snapshot (read-only)."""

from __future__ import annotations
import argparse
import json
from pathlib import Path
from sqlalchemy import text
from database import SessionLocal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with SessionLocal() as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
        db.execute(text("SET LOCAL statement_timeout='15s'"))
        rows = (
            db.execute(
                text(
                    "SELECT rule_id, corpus_id, corpus_version, document, heading_path, title, body, source_locator, content_hash FROM rules_sections WHERE corpus_id = :corpus ORDER BY rule_id"
                ),
                {"corpus": "dnd-srd"},
            )
            .mappings()
            .all()
        )
        snapshot = [dict(row) for row in rows]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n")
    print(f"Exported {len(snapshot)} public SRD sections")


if __name__ == "__main__":
    main()

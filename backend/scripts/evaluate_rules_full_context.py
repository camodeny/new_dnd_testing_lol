"""Compare full core SRD judging with a perfect-evidence diagnostic control.

Core means Playing the Game + Rules Glossary, not all spells/stat blocks.
BM25 supplies up to eight additional passages from other documents. This is
an isolated experiment: it bypasses production's three-passage context cap.
Gold evidence is used only by the explicitly labelled oracle control.
"""

from __future__ import annotations
import argparse
import hashlib
import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from evaluate_rules_retrieval import MeasuredJev, packet, percentile, snapshot_hash
from app.dm.rules_guidance import check_rules_advisory
from app.rules_corpus.selection_prototype import RuleIndex, passage


class Capture:
    def decide(self, request):
        self.request = request
        return SimpleNamespace(
            results={
                "rules-advisory": SimpleNamespace(selected_id="INSUFFICIENT_EVIDENCE")
            }
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--cases", type=Path, default=Path("tests/fixtures/rules_retrieval_eval.json")
    )
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    rows = json.loads(args.corpus.read_text())
    cases = json.loads(args.cases.read_text())["cases"]
    if args.limit:
        cases = cases[: args.limit]
    index = RuleIndex(rows)
    service = MeasuredJev()
    core = [
        {"title": r["title"], "body": r["body"]}
        for r in rows
        if r["document"] in {"playing-the-game", "rules-glossary"}
    ]
    samples = []
    report = {
        "core_sections": len(core),
        "corpus_content_sha256": snapshot_hash(rows),
        "core_chars": sum(len(r["body"]) for r in core),
        "samples": samples,
        "repeats": args.repeats,
        "corpus_sha256": hashlib.sha256(args.corpus.read_bytes()).hexdigest(),
        "fixture_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
    }
    for repeat in range(args.repeats):
        for c in cases:
            for method in ("full_core_bm25", "oracle_evidence"):
                start = time.perf_counter()
                service.calls = []
                error_kind = None
                outcome = None
                capture = Capture()
                check_rules_advisory(
                    packet(c["query"], [passage(rows[0])]),
                    c["proposal"],
                    decision_service=capture,
                )
                request = capture.request
                if method == "full_core_bm25":
                    extra = [
                        r
                        for r in index.lexical(c["query"])
                        if index.by_id[r["rule_id"]]["document"]
                        not in {"playing-the-game", "rules-glossary"}
                    ]
                    evidence = core + extra
                else:
                    evidence = [
                        passage(index.by_id[g[0]]) for g in c["expected_rule_groups"]
                    ]
                request = replace(
                    request,
                    state={**request.state, "rules": evidence},
                    timeout_seconds=10,
                )
                try:
                    outcome = (
                        service.decide(request).results["rules-advisory"].selected_id
                    )
                except Exception as e:
                    error_kind = getattr(e, "kind", type(e).__name__)
                samples.append(
                    {
                        "method": method,
                        "case": c["id"],
                        "repeat": repeat,
                        "outcome": outcome,
                        "correct": outcome == c["expected_advisory"],
                        "ms": (time.perf_counter() - start) * 1000,
                        "calls": list(service.calls),
                        "error": error_kind,
                        "state_chars": len(json.dumps(request.state)),
                    }
                )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2) + "\n")
            print(
                f"repeat={repeat + 1} case={c['id']} samples={len(samples)}", flush=True
            )
    report["summary"] = {
        m: {
            "accuracy": sum(s["correct"] for s in samples if s["method"] == m)
            / sum(s["method"] == m for s in samples),
            "errors": sum(bool(s["error"]) for s in samples if s["method"] == m),
            "p50_ms": percentile([s["ms"] for s in samples if s["method"] == m], 0.5),
            "p95_ms": percentile([s["ms"] for s in samples if s["method"] == m], 0.95),
        }
        for m in ("full_core_bm25", "oracle_evidence")
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["summary"], indent=2))


if __name__ == "__main__":
    main()

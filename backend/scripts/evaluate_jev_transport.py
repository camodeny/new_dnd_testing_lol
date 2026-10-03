"""Opt-in paired fresh/pooled Jev HTTP timing using synthetic public actions."""

from __future__ import annotations
import argparse
import json
from pathlib import Path
import requests
from evaluate_rules_retrieval import MeasuredJev, packet, percentile, snapshot_hash
from evaluate_rules_full_context import Capture
from app.decisions.adapters.jev import JevAdapter
from app.decisions.errors import DecisionError
from app.dm.rules_guidance import check_rules_advisory
from app.rules_corpus.selection_prototype import RuleIndex


class PooledJev(JevAdapter):
    def __init__(self, session):
        self.session = session

    def execute(self, request, *, model, timeout):
        self.require_config(model)
        try:
            response = self.session.post(
                self.base_url(),
                headers=self.build_headers(),
                json=self.build_payload(request, model=model),
                timeout=timeout,
            )
            response.raise_for_status()
        except Exception as e:
            raise self.classify_error(e) from e
        try:
            return response.json()
        except ValueError as e:
            raise DecisionError(
                "invalid JSON", provider=self.name, kind="malformed"
            ) from e


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pairs", type=int, default=10)
    args = parser.parse_args()
    index = RuleIndex(json.loads(args.corpus.read_text()))
    case = json.loads(Path("tests/fixtures/rules_retrieval_eval.json").read_text())[
        "cases"
    ][0]
    capture = Capture()
    check_rules_advisory(
        packet(case["query"], index.lexical(case["query"], 3)),
        case["proposal"],
        decision_service=capture,
    )
    fresh = MeasuredJev()
    pooled = MeasuredJev()
    samples = []
    with requests.Session() as session:
        pooled.adapter = PooledJev(session)
        for pair in range(args.pairs):
            order = [("fresh", fresh), ("pooled", pooled)]
            if pair % 2:
                order.reverse()
            for name, service in order:
                service.calls = []
                answer = service.decide(capture.request)
                samples.append(
                    {
                        "pair": pair,
                        "transport": name,
                        "outcome": answer.results["rules-advisory"].selected_id,
                        **service.calls[0],
                    }
                )
    report = {
        "samples": samples,
        "corpus_content_sha256": snapshot_hash(index.rows),
        "summary": {
            name: {
                "p50_ms": percentile(
                    [s["ms"] for s in samples if s["transport"] == name], 0.5
                ),
                "p95_ms": percentile(
                    [s["ms"] for s in samples if s["transport"] == name], 0.95
                ),
            }
            for name in ("fresh", "pooled")
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["summary"], indent=2))


if __name__ == "__main__":
    main()

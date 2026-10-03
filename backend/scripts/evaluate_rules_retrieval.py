"""Opt-in live public-SRD evaluation; no DB writes or production routing.

Run from backend with PYTHONPATH=.; provide a JSON canonical corpus snapshot.
Only synthetic actions/public SRD go to Jev. Gold labels remain local.
"""

from __future__ import annotations
import argparse
import hashlib
import json
import time
import math
import statistics
from pathlib import Path
from sqlalchemy import text
from database import SessionLocal
from app.decisions.adapters.jev import JevAdapter
from app.decisions.contracts import DecisionResponse
from app.decisions.runtime import validate_request
from app.dm.context import (
    AuthorizationScope,
    ContextAudience,
    ContextRecord,
    LaneName,
    SourceRef,
    assemble_context_packet,
)
from app.dm.rules_guidance import check_rules_advisory
from app.rules_corpus.selection_prototype import RuleIndex, passage
from app.rules_corpus.store import hybrid_search


class MeasuredJev:
    """Real adapter/parser, omitting game accounting for synthetic eval calls."""

    def __init__(self):
        self.adapter = JevAdapter()
        self.calls = []

    def decide(self, request):
        validate_request(request)
        start = time.perf_counter()
        model = request.model or self.adapter.default_model()
        try:
            data = self.adapter.execute(
                request, model=model, timeout=request.timeout_seconds or 10
            )
            results, actual_model, usage = self.adapter.parse_response(data, request)
        except Exception as error:
            self.calls.append(
                {
                    "ms": (time.perf_counter() - start) * 1000,
                    "error": getattr(error, "kind", type(error).__name__),
                }
            )
            raise
        elapsed = (time.perf_counter() - start) * 1000
        self.calls.append(
            {
                "ms": elapsed,
                "model": actual_model,
                "usage": usage,
                "questions": len(request.questions),
            }
        )
        return DecisionResponse(
            results=results,
            provider="jev",
            model=actual_model,
            latency_ms=round(elapsed),
            trace_id=None,
            usage=usage,
        )


def packet(query, rows):
    campaign = "00000000-0000-0000-0000-000000000001"
    audience = ContextAudience(
        campaign_id=campaign,
        thread_id="00000000-0000-0000-0000-000000000002",
        audience="campaign",
        user_ids=[],
    )
    auth = AuthorizationScope(campaign_id=campaign)
    records = {
        LaneName.PLAYER_INPUTS: [
            ContextRecord(
                record_id="submission:eval",
                value={"segments": [{"segment_type": "ic", "text": query}]},
                sources=[
                    SourceRef(
                        source_type="fixture", source_id="eval", source_version="1"
                    )
                ],
                authorization=auth,
                visibility="campaign",
                use="narration_eligible",
            )
        ],
        LaneName.EVIDENCE_RESULTS: [
            ContextRecord(
                record_id="rules-guidance:" + r["rule_id"],
                value=r,
                sources=[
                    SourceRef(
                        source_type="dnd_srd_rule",
                        source_id=r["rule_id"],
                        source_version="5.2.1",
                    )
                ],
                authorization=auth,
                visibility="public",
                use="adjudication_only",
            )
            for r in rows
        ],
    }
    return assemble_context_packet(audience=audience, records=records)


def snapshot_hash(rows):
    return hashlib.sha256(
        json.dumps(
            rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


def percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    if fraction == 0.5:
        return statistics.median(values)
    return values[max(0, math.ceil(len(values) * fraction) - 1)]


def summarize(samples):
    result = {}
    for method in sorted({s["method"] for s in samples}):
        subset = [s for s in samples if s["method"] == method]
        mech = [s for s in subset if not s["no_rules"]]
        convo = [s for s in subset if s["no_rules"]]
        result[method] = {
            "samples": len(subset),
            "errors": sum(bool(s.get("error")) for s in subset),
            "primary_recall": sum(s["groups_hit"][0] for s in mech) / len(mech),
            "all_groups_coverage": sum(all(s["groups_hit"]) for s in mech) / len(mech),
            "group_recall": sum(sum(s["groups_hit"]) for s in mech)
            / sum(len(s["groups_hit"]) for s in mech),
            "conversation_empty": sum(not s["ids"] for s in convo) / len(convo)
            if convo
            else None,
            "advisory_accuracy": sum(s["advisory_correct"] for s in subset)
            / len(subset),
            **{
                f"{stage}_{p}": percentile([s[stage + "_ms"] for s in subset], f)
                for stage in ("retrieval", "advisory", "total")
                for p, f in (("p50", 0.5), ("p95", 0.95))
            },
        }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument(
        "--cases", type=Path, default=Path("tests/fixtures/rules_retrieval_eval.json")
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    rows = json.loads(args.corpus.read_text())
    cases = json.loads(args.cases.read_text())["cases"]
    if args.limit:
        cases = cases[: args.limit]
    start = time.perf_counter()
    index = RuleIndex(rows)
    build_ms = (time.perf_counter() - start) * 1000
    for c in cases:
        assert all(
            rid in index.by_id for group in c["expected_rule_groups"] for rid in group
        )
    service = MeasuredJev()
    service.adapter.require_config()
    methods = [
        "configured_lexical_jev",
        "bm25",
        "bm25_jev",
        "titles",
        "titles_bm25_jev",
    ]
    samples = []
    report = {
        "corpus_sha256": hashlib.sha256(args.corpus.read_bytes()).hexdigest(),
        "corpus_content_sha256": snapshot_hash(rows),
        "fixture_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
        "index_build_ms": build_ms,
        "sections": len(rows),
        "repeats": args.repeats,
        "transport": "stock requests.post; fresh connection per request",
        "embedding_benchmark": False,
        "samples": samples,
    }
    with SessionLocal() as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
        db.execute(text("SET LOCAL statement_timeout='15s'"))
        db.execute(text("SELECT 1"))
        for repeat in range(args.repeats):
            for ci, c in enumerate(cases):
                offset = (repeat + ci) % len(methods)
                for method in methods[offset:] + methods[:offset]:
                    service.calls = []
                    start = time.perf_counter()
                    error_kind = None
                    selected = []
                    try:
                        if method == "configured_lexical_jev":
                            hits = hybrid_search(
                                db, c["query"], corpus_id="dnd-srd", limit=8
                            )
                            selected = index.rerank(
                                [passage(index.by_id[h["rule_id"]]) for h in hits],
                                c["query"],
                                service,
                            )
                        elif method == "bm25":
                            selected = index.lexical(c["query"], 3)
                        elif method == "bm25_jev":
                            selected = index.rerank(
                                index.lexical(c["query"]), c["query"], service
                            )
                        elif method == "titles":
                            selected = index.titles(c["query"], service)[:3]
                        else:
                            title_hits = index.titles(c["query"], service)
                            selected = index.rerank(
                                index.combine(index.lexical(c["query"]), title_hits),
                                c["query"],
                                service,
                            )
                        selected = [
                            passage(index.by_id[r["rule_id"]]) for r in selected
                        ]
                    except Exception as error:
                        error_kind = getattr(error, "kind", type(error).__name__)
                    retrieval_ms = (time.perf_counter() - start) * 1000
                    retrieval_calls = list(service.calls)
                    service.calls = []
                    start = time.perf_counter()
                    verdict = check_rules_advisory(
                        packet(c["query"], selected),
                        c["proposal"],
                        decision_service=service,
                    )
                    advisory_ms = (time.perf_counter() - start) * 1000
                    ids = [r["rule_id"] for r in selected]
                    samples.append(
                        {
                            "case": c["id"],
                            "repeat": repeat,
                            "method": method,
                            "no_rules": c["no_rules"],
                            "ids": ids,
                            "groups_hit": [
                                bool(set(g) & set(ids))
                                for g in c["expected_rule_groups"]
                            ],
                            "advisory": verdict,
                            "advisory_correct": verdict["outcome"]
                            == c["expected_advisory"],
                            "retrieval_ms": retrieval_ms,
                            "advisory_ms": advisory_ms,
                            "total_ms": retrieval_ms + advisory_ms,
                            "error": error_kind,
                            "retrieval_calls": retrieval_calls,
                            "advisory_calls": list(service.calls),
                        }
                    )
                report["summary"] = summarize(samples)
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(report, indent=2) + "\n")
                print(
                    f"repeat={repeat + 1} case={c['id']} samples={len(samples)}",
                    flush=True,
                )
    print(json.dumps(report["summary"], indent=2))


if __name__ == "__main__":
    main()

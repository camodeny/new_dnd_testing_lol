"""Offline unit tests for the rules-corpus selection prototype.

Uses tiny synthetic source rows and a scripted fake decision service only.
No providers, secrets, or dev servers.
"""

from __future__ import annotations

import pytest

from app.decisions.contracts import ChoiceResult, DecisionResponse
from app.decisions.errors import DecisionError
from app.decisions.runtime import DecisionService
from app.rules_corpus.selection_prototype import NONE, RuleIndex
from tests.support.fake_decisions import FakeDecisionAdapter


def _row(rule_id, title, heading, body, document="srd-doc"):
    return {
        "rule_id": rule_id,
        "title": title,
        "heading_path": [heading],
        "document": document,
        "corpus_version": "5.2.1",
        "body": body,
    }


def _tiny_index():
    return RuleIndex(
        [
            _row(
                "rule-grapple",
                "Grapple",
                "Combat",
                "grapple shove contest athletic grapple grapple grapple",
            ),
            _row(
                "rule-spell",
                "Spellcasting",
                "Magic",
                "spell slots cantrip ritual components",
            ),
            _row(
                "rule-rest",
                "Resting",
                "Recovery",
                "short rest long rest hit dice recover",
            ),
        ]
    )


class _AutoFake(FakeDecisionAdapter):
    """Scripted fake: selects the known target when offered, else first legal.

    Probabilities are deterministic (selected 1.0, others 0.0 via the base
    fake parser). Records every question's candidate count for bound asserts.
    """

    def __init__(self, target=None):
        super().__init__(answers={})
        self.target = target
        self.seen_counts: list[int] = []

    def execute(self, request, *, model, timeout):
        self.calls.append(
            {
                "questions": sorted(q.question_id for q in request.questions),
                "state": request.state,
            }
        )
        answers = {}
        for question in request.questions:
            ids = [c.id for c in question.candidates]
            self.seen_counts.append(len(ids))
            if self.target is not None and self.target in ids:
                answers[question.question_id] = self.target
            else:
                answers[question.question_id] = ids[0]
        return {"answers": answers}


def _assert_bounded(fake):
    assert fake.seen_counts, "fake service was never called"
    assert all(count <= 255 for count in fake.seen_counts)
    assert max(fake.seen_counts) <= 255


def test_lexical_ranking_prefers_distinctive_terms():
    index = _tiny_index()
    hits = index.lexical("how does a grapple contest work?", limit=8)
    assert hits, "expected BM25 hits for a matching query"
    assert hits[0]["rule_id"] == "rule-grapple"
    assert all("rule_id" in hit for hit in hits)


def test_lexical_limit_and_empty_query():
    index = _tiny_index()
    assert len(index.lexical("grapple", limit=2)) <= 2
    assert index.lexical("zzzqqq nonexistent term", limit=8) == []


def test_duplicate_canonical_ids_rejected():
    rows = [
        _row("rule-dup", "First", "Combat", "first body"),
        _row("rule-dup", "Second", "Magic", "second body"),
    ]
    with pytest.raises(ValueError, match="duplicate canonical rule IDs"):
        RuleIndex(rows)
    index = _tiny_index()
    assert set(index.by_id) == {"rule-grapple", "rule-spell", "rule-rest"}


def test_titles_all_none_returns_empty():
    index = _tiny_index()
    # Root has one document child -> one branch, two role questions at level 0.
    answers = {
        "level-0-branch-0-primary": NONE,
        "level-0-branch-0-exception": NONE,
    }
    adapter = FakeDecisionAdapter(answers=answers)
    service = DecisionService(adapter)
    assert index.titles("ordinary hello there dialogue", service) == []
    assert len(adapter.calls) == 1


def test_titles_large_document_split_into_bounded_questions():
    target = "rule-big-042"
    rows = [
        _row(
            f"rule-big-{i:03d}",
            f"Rule {i:03d}",
            f"Section {i:03d}",
            f"unique body text alpha number {i} zebrafon",
            document="big-doc",
        )
        for i in range(260)
    ]
    index = RuleIndex(rows)
    fake = _AutoFake(target=target)
    service = DecisionService(fake)

    first = index.titles("zebrafon section 042", service)
    _assert_bounded(fake)
    calls = len(fake.calls)
    assert 1 <= calls <= 8
    assert first, "expected the known target section to be retrieved"
    assert first[0]["rule_id"] == target

    repeat_fake = _AutoFake(target=target)
    second = index.titles("zebrafon section 042", DecisionService(repeat_fake))
    assert [r["rule_id"] for r in second] == [r["rule_id"] for r in first]
    assert repeat_fake.seen_counts == fake.seen_counts
    assert len(repeat_fake.calls) == calls


def test_titles_unknown_selection_fails():
    class _BogusService:
        def decide(self, request):
            return DecisionResponse(
                results={
                    q.question_id: ChoiceResult(
                        question_id=q.question_id,
                        selected_id="no-such-candidate",
                        probabilities={},
                    )
                    for q in request.questions
                },
                provider="stub",
                model="stub",
                latency_ms=0,
                trace_id=None,
            )

    with pytest.raises(DecisionError, match="unknown title selection"):
        _tiny_index().titles("grapple", _BogusService())


def test_combine_union_preserves_sources_dedups_and_caps():
    lexical = [{"rule_id": f"rule-l{i}", "title": f"L{i}"} for i in range(10)]
    titles = [{"rule_id": f"rule-t{i}", "title": f"T{i}"} for i in range(10)]

    # Full distinct inputs hit the cap of 8.
    combined = RuleIndex.combine(lexical, titles)
    assert len(combined) == 8

    # Overlap within the window dedups to a single entry, sources preserved.
    titles[1] = dict(lexical[0])  # overlap: dedup must keep a single entry
    combined = RuleIndex.combine(lexical, titles)
    assert len(combined) == 7
    ids = [r["rule_id"] for r in combined]
    assert len(set(ids)) == len(ids)
    # Interleaved union reserving space per source: titles[0], lexical[0], ...
    assert ids[0] == titles[0]["rule_id"]
    assert ids[1] == lexical[0]["rule_id"]
    assert combined[0]["title"] == "T0"
    assert combined[1]["title"] == "L0"

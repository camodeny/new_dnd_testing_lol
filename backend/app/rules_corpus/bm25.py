"""Shared BM25 ranking for public SRD retrieval; scores never grant authority."""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict

STOPWORDS = set(
    "a an the i me my we our you your he she it they their is are was were be been to of in on at for with and or but if then this that as do does can could would should how what when while from into about have has had not no after before just want please".split()
)


def terms(text: str) -> list[str]:
    """Small English lexical normalizer, with no game-specific routing table."""
    result = []
    for token in re.findall(r"[a-z]+", text.lower()):
        if token in STOPWORDS or len(token) < 3:
            continue
        if len(token) > 5 and token.endswith("ing"):
            token = token[:-3]
        elif len(token) > 4 and token.endswith("ed"):
            token = token[:-2]
        elif len(token) > 4 and token.endswith("s") and not token.endswith("ss"):
            token = token[:-1]
        result.append(token)
    return result


class Bm25Index:
    """Derived postings only: canonical bodies must be reloaded before use."""

    def __init__(self, rows: list[dict]):
        rows = sorted(rows, key=lambda row: row["rule_id"])
        self.rule_ids = tuple(row["rule_id"] for row in rows)
        if len(set(self.rule_ids)) != len(rows):
            raise ValueError("duplicate canonical rule IDs")
        self.lengths = []
        self.postings = defaultdict(list)
        for index, row in enumerate(rows):
            heading = " ".join([row["title"], *row["heading_path"]])
            counts = Counter(terms(heading + " " + heading + " " + row["body"]))
            self.lengths.append(sum(counts.values()))
            for term, count in counts.items():
                self.postings[term].append((index, count))
        self.avg_length = sum(self.lengths) / max(1, len(rows))

    @classmethod
    def from_texts(cls, docs: dict[str, str]) -> "Bm25Index":
        """Index arbitrary keyed documents (keys play the rule-ID role)."""
        return cls([
            {"rule_id": key, "title": "", "heading_path": [], "body": text}
            for key, text in docs.items()
        ])

    def rank_ids(self, query: str, limit: int = 8) -> list[str]:
        return [doc_id for doc_id, _ in self.rank_scored(query, limit)]

    def rank_scored(
        self, query: str, limit: int = 8, *, max_limit: int = 20,
    ) -> list[tuple[str, float]]:
        scores = defaultdict(float)
        n = len(self.rule_ids)
        for term in set(terms(query)):
            postings = self.postings.get(term, [])
            idf = math.log(1 + (n - len(postings) + 0.5) / (len(postings) + 0.5))
            for index, frequency in postings:
                normalization = 1.2 * (
                    0.25 + 0.75 * self.lengths[index] / self.avg_length
                )
                scores[index] += idf * frequency * 2.2 / (frequency + normalization)
        ranked = sorted(
            scores, key=lambda index: (-scores[index], self.rule_ids[index])
        )
        return [
            (self.rule_ids[index], scores[index])
            for index in ranked[: max(0, min(limit, max_limit))]
        ]

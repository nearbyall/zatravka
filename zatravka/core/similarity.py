"""Lexical similarity: TF-IDF over character trigrams of words.

Trigrams tolerate Russian inflection, and the inverted index keeps lookups fast
over thousands of ideas without numpy.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict

_TOKEN = re.compile(r"[^\W_]+", re.U)
STOPWORDS = {
    "для", "и", "в", "на", "с", "по", "из", "от", "к", "о", "а", "или", "что", "как", "это", "без",
    "the", "a", "an", "for", "to", "of", "and", "in", "on", "with", "by", "or", "that", "is",
    "сервис", "приложение", "платформа", "инструмент", "app", "tool", "service", "platform",
}


def grams(text: str, n: int = 3) -> Counter:
    out: Counter = Counter()
    for word in _TOKEN.findall((text or "").lower()):
        if word in STOPWORDS or len(word) < 2:
            continue
        padded = f" {word} "
        if len(padded) <= n:
            out[padded] += 1
            continue
        for i in range(len(padded) - n + 1):
            out[padded[i:i + n]] += 1
    return out


class SimIndex:
    def __init__(self, corpus: list[str] | None = None):
        self.df: Counter = Counter()
        self.n_docs = 0
        self.postings: dict[str, dict[int, float]] = defaultdict(dict)
        self.docs: dict[int, dict[str, float]] = {}
        for text in corpus or []:
            self.observe(text)

    def observe(self, text: str) -> None:
        """Counts the text into IDF statistics; call before indexing."""
        self.n_docs += 1
        for g in grams(text):
            self.df[g] += 1

    def _vector(self, text: str) -> dict[str, float]:
        n = max(self.n_docs, 1)
        v = {g: (1 + math.log(c)) * math.log((n + 1) / (self.df.get(g, 0) + 1) + 1) for g, c in grams(text).items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {g: x / norm for g, x in v.items()}

    def add(self, doc_id: int, text: str) -> None:
        v = self._vector(text)
        self.docs[doc_id] = v
        for g, w in v.items():
            self.postings[g][doc_id] = w

    def remove(self, doc_id: int) -> None:
        for g in self.docs.pop(doc_id, {}):
            self.postings[g].pop(doc_id, None)

    def query(self, text: str, top: int = 5, exclude: int | None = None) -> list[tuple[int, float]]:
        scores: dict[int, float] = defaultdict(float)
        for g, qw in self._vector(text).items():
            for doc_id, w in self.postings.get(g, {}).items():
                scores[doc_id] += qw * w
        if exclude is not None:
            scores.pop(exclude, None)
        return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top]


def similarity(a: str, b: str) -> float:
    index = SimIndex([a, b])
    index.add(1, b)
    found = index.query(a, top=1)
    return found[0][1] if found else 0.0

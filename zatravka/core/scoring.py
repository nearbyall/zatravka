"""Idea score: weighted LLM sub-scores plus a bonus for repeated mentions.

The final number is computed in code rather than by the model, so it stays stable
and can be recomputed when weights change.
"""
from __future__ import annotations

import math

SCORE_KEYS = ("demand", "monetization", "novelty", "feasibility", "whitespace", "urgency")
NEUTRAL_SUBSCORE = 5


def base_score(scores: dict[str, float], weights: dict[str, float]) -> float:
    total_weight = sum(weights.get(k, 0) for k in SCORE_KEYS) or 1.0
    return round(sum(scores.get(k, NEUTRAL_SUBSCORE) * weights.get(k, 0) for k in SCORE_KEYS) / total_weight, 2)


def final_score(base: float | None, mentions: int, bonus: float) -> float | None:
    if base is None:
        return None
    return round(base + bonus * math.log2(max(1, mentions)), 2)

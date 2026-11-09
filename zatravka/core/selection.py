from __future__ import annotations

import math
import random
from typing import Any, TypeVar

T = TypeVar("T")


def chunks(items: list[T], size: int) -> list[list[T]]:
    size = max(1, size)
    return [items[i:i + size] for i in range(0, len(items), size)]


def sample(items: list[T], k: int, rng: random.Random) -> list[T]:
    return rng.sample(items, min(k, len(items))) if items and k > 0 else []


def weighted_choice(weights: dict[str, float], rng: random.Random) -> str:
    items = [(k, w) for k, w in weights.items() if w > 0]
    r = rng.random() * sum(w for _, w in items)
    for k, w in items:
        r -= w
        if r <= 0:
            return k
    return items[-1][0]


def select_diverse(rows: list[dict[str, Any]], limit: int, max_share: float,
                   key: str = "direction") -> list[dict[str, Any]]:
    """Best rows first, capping any single value of `key` at max_share of the limit
    so one hot topic cannot take the whole run. Unused quota goes to the best leftovers."""
    cap = max(1, math.ceil(limit * max_share)) if max_share < 1 else limit
    picked, rest, taken = [], [], {}
    for r in rows:
        value = r.get(key) or "?"
        if len(picked) < limit and taken.get(value, 0) < cap:
            picked.append(r)
            taken[value] = taken.get(value, 0) + 1
        else:
            rest.append(r)
    return picked + rest[: limit - len(picked)]

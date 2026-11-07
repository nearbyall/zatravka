"""Схемы ответов ИИ. Снисходительные: приводят типы, обрезают диапазоны,
подставляют значения по умолчанию — чтобы мелкая неаккуратность модели
не стоила повторного платного вызова.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from ..core.scoring import SCORE_KEYS


def _clamp(v: Any, lo: float, hi: float, default: float) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, x))


def _text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, list):
        return "; ".join(str(x) for x in v)
    return str(v).strip()


def _str_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [s.strip() for s in v.split(",") if s.strip()]
    return [str(x).strip() for x in v if str(x).strip()]


# ── triage ──────────────────────────────────────────────────────────────
class TriageItem(BaseModel):
    id: int
    score: float = 0
    signals: list[str] = Field(default_factory=list)
    note: str = ""

    _score = field_validator("score", mode="before")(lambda v: _clamp(v, 0, 10, 0))
    _signals = field_validator("signals", mode="before")(_str_list)
    _note = field_validator("note", mode="before")(_text)


class TriageOut(BaseModel):
    items: list[TriageItem] = Field(default_factory=list)


# ── extract ─────────────────────────────────────────────────────────────
INSIGHT_TYPES = ("explicit", "implicit", "lateral")


class IdeaOut(BaseModel):
    title: str
    one_liner: str = ""
    problem: str = ""
    solution: str = ""
    audience: str = ""
    insight_type: str = "implicit"
    reasoning: str = ""
    evidence: str = ""
    why_now: str = ""
    monetization: str = ""
    risks: str = ""
    confidence: float = 0.5

    _texts = field_validator("title", "one_liner", "problem", "solution", "audience", "reasoning",
                             "evidence", "why_now", "monetization", "risks", mode="before")(_text)
    _conf = field_validator("confidence", mode="before")(lambda v: _clamp(v, 0, 1, 0.5))

    @field_validator("insight_type", mode="before")
    @classmethod
    def _insight(cls, v: Any) -> str:
        s = _text(v).lower()
        for t in INSIGHT_TYPES:
            if t in s:
                return t
        return {"явн": "explicit", "неяв": "implicit", "латер": "lateral", "неочев": "lateral"}.get(s[:5], "implicit")


class ExtractItem(BaseModel):
    post_id: int
    ideas: list[IdeaOut] = Field(default_factory=list)
    skip_reason: str | None = None


class ExtractOut(BaseModel):
    ideas: list[IdeaOut] = Field(default_factory=list)
    skip_reason: str | None = None


# ── classify ────────────────────────────────────────────────────────────
class ClassItem(BaseModel):
    id: int
    market: str = ""
    sphere: str = "other"
    secondary_spheres: list[str] = Field(default_factory=list)
    nature: str = ""
    business_model: str = "other"
    tags: list[str] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)
    origin_region: str = ""
    scores: dict[str, float] = Field(default_factory=dict)
    score_note: str = ""

    _lists = field_validator("secondary_spheres", "tags", "regions", mode="before")(_str_list)
    _texts = field_validator("market", "sphere", "nature", "business_model", "score_note", "origin_region",
                             mode="before")(_text)

    @field_validator("scores", mode="before")
    @classmethod
    def _scores(cls, v: Any) -> dict[str, float]:
        v = v if isinstance(v, dict) else {}
        return {k: _clamp(v.get(k), 1, 10, 5) for k in SCORE_KEYS}


class ClassifyOut(BaseModel):
    items: list[ClassItem] = Field(default_factory=list)


# ── merge ───────────────────────────────────────────────────────────────
class MergeDecision(BaseModel):
    id: int
    same_as: int | None = None

    @field_validator("same_as", mode="before")
    @classmethod
    def _same(cls, v: Any) -> int | None:
        if v in (None, "", "null", "none", 0, "0"):
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None


class MergeOut(BaseModel):
    decisions: list[MergeDecision] = Field(default_factory=list)


# ── explore ─────────────────────────────────────────────────────────────
class ExploreDirection(BaseModel):
    key: str
    name: str = ""
    rationale: str = ""
    subreddits: list[str] = Field(default_factory=list)
    reddit_search: list[str] = Field(default_factory=list)
    x_queries: list[str] = Field(default_factory=list)
    threads_queries: list[str] = Field(default_factory=list)

    _lists = field_validator("subreddits", "reddit_search", "x_queries", "threads_queries", mode="before")(_str_list)
    _texts = field_validator("key", "name", "rationale", mode="before")(_text)


class ExploreOut(BaseModel):
    directions: list[ExploreDirection] = Field(default_factory=list)


# ── discover accounts ───────────────────────────────────────────────────
class AccountIdea(BaseModel):
    username: str
    why: str = ""

    _texts = field_validator("username", "why", mode="before")(_text)


class AccountsOut(BaseModel):
    accounts: list[AccountIdea] = Field(default_factory=list)

"""Cheap pre-LLM filter: drops junk and ranks the queue, leaving the idea call to the LLM."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from ..config import Prefilter

STOPWORDS = {
    "en": {"the", "and", "is", "to", "of", "it", "that", "for", "you", "with", "this", "have", "was", "are", "my",
           "on", "but", "not", "what", "can", "be", "they", "just", "how", "there", "would", "any", "i'm", "don't"},
    "de": {"und", "der", "die", "das", "ist", "nicht", "ich", "mit", "ein", "eine", "auf", "für", "auch", "sie",
           "wie", "aber"},
    "es": {"el", "la", "que", "de", "y", "en", "los", "las", "por", "una", "para", "con", "pero", "como", "muy",
           "más"},
    "fr": {"le", "la", "les", "et", "est", "pas", "je", "une", "des", "que", "pour", "dans", "avec", "mais", "sur",
           "qui"},
    "pt": {"o", "que", "não", "uma", "para", "com", "os", "as", "mas", "como", "mais", "por", "você", "isso",
           "muito"},
    "ru": {"и", "в", "не", "на", "что", "я", "с", "как", "это", "по", "но", "а", "для", "у", "так", "все", "если",
           "или", "есть", "уже"},
    "uk": {"і", "та", "що", "не", "на", "це", "як", "але", "для", "є", "від", "або", "вже", "мені", "щоб", "дуже"},
}
CYRILLIC_LANGS = ("ru", "uk")
UNDETERMINED_LANGS = ("und", "qme", "zxx")
MAX_SIGNAL_SCORE = 8.0

_WORD = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?", re.U)
_UK_LETTERS = set("іїєґ")


@dataclass
class Verdict:
    keep: bool
    score: float
    lang: str
    reason: str


def detect_lang(text: str) -> str:
    """Rough guess by alphabet and stopwords; good enough for filtering."""
    words = [w.lower() for w in _WORD.findall(text[:3000])]
    if not words:
        return "unknown"
    letters = "".join(words)
    cyrillic = sum(1 for ch in letters if "Ѐ" <= ch <= "ӿ")
    if cyrillic / max(1, len(letters)) > 0.4:
        return _cyrillic_lang(letters, words)
    if any("一" <= ch <= "鿿" for ch in letters[:200]):
        return "zh"
    if any("぀" <= ch <= "ヿ" for ch in letters[:200]):
        return "ja"
    scores = {lang: sum(1 for w in words if w in sw) for lang, sw in STOPWORDS.items() if lang not in CYRILLIC_LANGS}
    best = max(scores, key=scores.get)
    if scores[best] == 0:
        return "en" if all(ord(c) < 128 for c in letters) else "unknown"
    return best


def _cyrillic_lang(letters: str, words: list[str]) -> str:
    uk_letters = sum(1 for ch in letters if ch in _UK_LETTERS)
    uk_words = sum(1 for w in words if w in STOPWORDS["uk"])
    ru_words = sum(1 for w in words if w in STOPWORDS["ru"])
    return "uk" if uk_letters > 2 or uk_words > ru_words + 1 else "ru"


@lru_cache(maxsize=8)
def _compiled(patterns: tuple[str, ...]) -> list[re.Pattern]:
    return [re.compile(p) for p in patterns]


def evaluate(cfg: Prefilter, title: str | None, body: str | None, created_at: datetime | None,
             score: int | None = None, num_comments: int | None = None, nsfw: bool = False,
             lang_hint: str | None = None, now: datetime | None = None) -> Verdict:
    text = "\n".join(p for p in (title, body) if p)
    lang = lang_hint if lang_hint and lang_hint not in UNDETERMINED_LANGS else detect_lang(text)
    reject = _rejection(cfg, text, lang, created_at, score, nsfw, now or datetime.now(timezone.utc))
    if reject:
        return Verdict(False, 0, lang, reject)
    patterns = _compiled(tuple(s.pattern for s in cfg.signals))
    hits = [s.weight for rx, s in zip(patterns, cfg.signals) if rx.search(text)]
    signal = min(sum(hits), MAX_SIGNAL_SCORE)
    total = round(signal + _engagement(score, num_comments) + _shape(text), 2)
    return Verdict(True, total, lang, f"signals={len(hits)}" if hits else "no_signals")


def _rejection(cfg: Prefilter, text: str, lang: str, created_at: datetime | None, score: int | None,
               nsfw: bool, now: datetime) -> str | None:
    if nsfw:
        return "nsfw"
    if len(text) < cfg.min_chars:
        return "short"
    if created_at and now - created_at > timedelta(days=cfg.max_age_days):
        return "old"
    if cfg.languages and lang not in cfg.languages:
        return f"lang:{lang}"
    if score is not None and score < cfg.min_reddit_score:
        return "low_score"
    if any(rx.search(text) for rx in _compiled(tuple(cfg.spam_patterns))):
        return "spam"
    return None


def _engagement(score: int | None, num_comments: int | None) -> float:
    """Logarithmic, so viral posts do not crowd out everything else."""
    value = 0.0
    if score:
        value += math.log10(max(1, score)) * 0.8
    if num_comments:
        value += math.log10(max(1, num_comments))
    return value


def _shape(text: str) -> float:
    """Questions and long posts more often carry the context of a pain."""
    return 0.3 * min(3, text.count("?")) + (0.5 if len(text) > 600 else 0)

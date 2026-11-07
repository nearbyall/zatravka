"""Дешёвый фильтр до ИИ: язык, длина, возраст, спам, сигнальные фразы.

Задача — не решать, есть ли идея (это делает ИИ), а убрать мусор и
упорядочить очередь: посты с явными сигналами боли/спроса идут первыми.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from .config import Prefilter

STOPWORDS = {
    "en": {"the", "and", "is", "to", "of", "it", "that", "for", "you", "with", "this", "have", "was", "are", "my", "on", "but", "not", "what", "can", "be", "they", "just", "how", "there", "would", "any", "i'm", "don't"},
    "de": {"und", "der", "die", "das", "ist", "nicht", "ich", "mit", "ein", "eine", "auf", "für", "auch", "sie", "wie", "aber"},
    "es": {"el", "la", "que", "de", "y", "en", "los", "las", "por", "una", "para", "con", "pero", "como", "muy", "más"},
    "fr": {"le", "la", "les", "et", "est", "pas", "je", "une", "des", "que", "pour", "dans", "avec", "mais", "sur", "qui"},
    "pt": {"o", "que", "não", "uma", "para", "com", "os", "as", "mas", "como", "mais", "por", "você", "isso", "muito"},
    "ru": {"и", "в", "не", "на", "что", "я", "с", "как", "это", "по", "но", "а", "для", "у", "так", "все", "если", "или", "есть", "уже"},
    "uk": {"і", "та", "що", "не", "на", "це", "як", "але", "для", "є", "від", "або", "вже", "мені", "щоб", "дуже"},
}
_WORD = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?", re.U)
_UK_LETTERS = set("іїєґ")


def detect_lang(text: str) -> str:
    """Грубое определение языка по алфавиту и стоп-словам. Достаточно для фильтра."""
    words = [w.lower() for w in _WORD.findall(text[:3000])]
    if not words:
        return "unknown"
    letters = "".join(words)
    cyr = sum(1 for ch in letters if "Ѐ" <= ch <= "ӿ")
    if cyr / max(1, len(letters)) > 0.4:
        uk_hits = sum(1 for ch in letters if ch in _UK_LETTERS)
        uk_words = sum(1 for w in words if w in STOPWORDS["uk"])
        ru_words = sum(1 for w in words if w in STOPWORDS["ru"])
        if uk_hits > 2 or uk_words > ru_words + 1:
            return "uk"
        return "ru"
    if any("一" <= ch <= "鿿" for ch in letters[:200]):
        return "zh"
    if any("぀" <= ch <= "ヿ" for ch in letters[:200]):
        return "ja"
    scores = {lang: sum(1 for w in words if w in sw) for lang, sw in STOPWORDS.items() if lang not in ("ru", "uk")}
    best = max(scores, key=scores.get)
    if scores[best] == 0:
        return "en" if all(ord(c) < 128 for c in letters) else "unknown"
    return best


@lru_cache(maxsize=8)
def _compiled(patterns: tuple[str, ...]) -> list[re.Pattern]:
    return [re.compile(p) for p in patterns]


@dataclass
class Verdict:
    keep: bool
    score: float
    lang: str
    reason: str


def evaluate(cfg: Prefilter, title: str | None, body: str | None, created_at: datetime | None,
             score: int | None = None, num_comments: int | None = None, nsfw: bool = False,
             lang_hint: str | None = None, now: datetime | None = None) -> Verdict:
    text = "\n".join(p for p in (title, body) if p)
    lang = lang_hint if lang_hint and lang_hint not in ("und", "qme", "zxx") else detect_lang(text)
    now = now or datetime.now(timezone.utc)

    if nsfw:
        return Verdict(False, 0, lang, "nsfw")
    if len(text) < cfg.min_chars:
        return Verdict(False, 0, lang, "short")
    if created_at and now - created_at > timedelta(days=cfg.max_age_days):
        return Verdict(False, 0, lang, "old")
    if cfg.languages and lang not in cfg.languages:
        return Verdict(False, 0, lang, f"lang:{lang}")
    if score is not None and score < cfg.min_reddit_score:
        return Verdict(False, 0, lang, "low_score")
    for rx in _compiled(tuple(cfg.spam_patterns)):
        if rx.search(text):
            return Verdict(False, 0, lang, "spam")

    hits: list[str] = []
    sig = 0.0
    sig_patterns = tuple(s.pattern for s in cfg.signals)
    for rx, s in zip(_compiled(sig_patterns), cfg.signals):
        if rx.search(text):
            sig += s.weight
            hits.append(rx.pattern[:24])
    sig = min(sig, 8.0)
    # Вовлечённость: логарифм, чтобы вирусные посты не забивали всё остальное
    eng = 0.0
    if score:
        eng += math.log10(max(1, score)) * 0.8
    if num_comments:
        eng += math.log10(max(1, num_comments)) * 1.0
    # Вопросы и длинные развёрнутые посты чаще содержат контекст боли
    shape = 0.3 * min(3, text.count("?")) + (0.5 if len(text) > 600 else 0)
    total = round(sig + eng + shape, 2)
    return Verdict(True, total, lang, f"signals={len(hits)}" if hits else "no_signals")

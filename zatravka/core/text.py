from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import Any

_BLOCK_TAGS = {"p", "br", "div", "li", "ul", "ol", "blockquote", "pre", "h1", "h2", "h3", "h4", "tr"}
_HIDDEN_TAGS = {"script", "style"}
_INLINE_SPACE = re.compile("[ \t ]+")
_BLANK_LINES = re.compile(r"\n\s*\n+")
_NON_WORD = re.compile(r"[^\w]+", re.U)
_SHINGLE = 5
_SHINGLE_MATCH_SHARE = 0.7


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in _HIDDEN_TAGS:
            self.hidden_depth += 1
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("• ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _HIDDEN_TAGS and self.hidden_depth:
            self.hidden_depth -= 1
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth:
            self.parts.append(data)


def html_to_text(fragment: str) -> str:
    parser = _TextExtractor()
    parser.feed(fragment or "")
    parser.close()
    text = _INLINE_SPACE.sub(" ", html.unescape("".join(parser.parts)))
    return _BLANK_LINES.sub("\n\n", text).strip()


def truncate(text: str | None, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit * 0.8 else cut) + " …"


def normalize(text: str | None) -> str:
    return _NON_WORD.sub(" ", (text or "").lower()).strip()


def verify_quote(quote: str, source: str) -> bool:
    """True when the quote occurs in the source ignoring case and punctuation,
    or when most of its five-word shingles do."""
    q, s = normalize(quote), normalize(source)
    if len(q) < 8:
        return False
    if q in s:
        return True
    words = q.split()
    if len(words) < _SHINGLE:
        return False
    shingles = [" ".join(words[i:i + _SHINGLE]) for i in range(len(words) - _SHINGLE + 1)]
    return sum(sh in s for sh in shingles) / len(shingles) >= _SHINGLE_MATCH_SHARE

from __future__ import annotations

import csv
import io
import json

from .records import EXPORT_FIELDS, Record

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value: object) -> object:
    """Texts from social networks and the LLM must not run as spreadsheet formulas."""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def to_csv(items: list[Record]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=EXPORT_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for it in items:
        row = {k: safe_cell(v) for k, v in it.items()}
        row["tags"] = ", ".join(it.get("tags") or [])
        row["regions"] = ", ".join(it.get("regions") or [])
        row["sources"] = " ".join(s["url"] or "" for s in it.get("sources") or [])
        writer.writerow(row)
    return buf.getvalue()


def to_json(items: list[Record]) -> str:
    return json.dumps(items, ensure_ascii=False, indent=2)


def to_markdown(items: list[Record]) -> str:
    lines = ["# Идеи", ""]
    for it in items:
        labels = f"`{it['market']}` `{it['sphere']}` `{it['nature']}` `{it['business_model']}` `{it['insight_type']}`"
        if it.get("regions"):
            labels += f" регионы: {', '.join(it['regions'])}"
        if (it.get("mentions") or 1) > 1:
            labels += f" · упоминаний: {it['mentions']}"
        lines += [
            f"## {it['title']} — {it['score']}", labels, "", it.get("one_liner") or "", "",
            f"**Проблема.** {it.get('problem') or '—'}",
            f"**Решение.** {it.get('solution') or '—'}",
            f"**Аудитория.** {it.get('audience') or '—'}",
            f"**Монетизация.** {it.get('monetization') or '—'}",
            f"**Риски.** {it.get('risks') or '—'}",
            "",
        ]
        lines += [f"> {s['quote']}  \n> — {s['url']}" for s in it.get("sources") or []]
        lines.append("")
    return "\n".join(lines)

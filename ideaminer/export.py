"""Выборка и экспорт идей (CSV, Excel, JSON, Markdown) — общая логика для CLI и веба."""
from __future__ import annotations

import csv
import io
import json
from typing import Any

from .db import DB, loads

SORTS = {
    "score": "i.score DESC NULLS LAST, i.id DESC",
    "new": "i.id DESC",
    "mentions": "i.mentions DESC, i.score DESC",
    "novelty": "json_extract(i.scores_json, '$.novelty') DESC, i.score DESC",
    "demand": "json_extract(i.scores_json, '$.demand') DESC, i.score DESC",
    "feasibility": "json_extract(i.scores_json, '$.feasibility') DESC, i.score DESC",
}


def ideas_where(*, market: str | None = None, sphere: str | None = None, nature: str | None = None,
                insight: str | None = None, model: str | None = None, status: str | None = None,
                platform: str | None = None, q: str | None = None, min_score: float | None = None,
                days: float | None = None, region: str | None = None, with_global: bool = True,
                origin: str | None = None, clusters_only: bool = True, **_: Any) -> tuple[str, list[Any]]:
    """WHERE для таблицы ideas (алиас i) по фильтрам интерфейса и CLI.
    region — где идею можно запустить; with_global — считать ли подходящими «глобальные» идеи;
    origin — откуда пришёл сигнал."""
    where, params = ["i.classified_at IS NOT NULL"], []
    if region:
        values = [region] + (["global"] if with_global and region != "global" else [])
        where.append(f"EXISTS (SELECT 1 FROM json_each(COALESCE(i.regions_json, '[]')) j"
                     f" WHERE j.value IN ({','.join('?' * len(values))}))")
        params += values
    if origin:
        where.append("i.origin_region = ?")
        params.append(origin)
    if clusters_only:
        where.append("i.cluster_id = i.id")
    for col, val in (("market", market), ("sphere", sphere), ("nature", nature),
                     ("insight_type", insight), ("business_model", model)):
        if val:
            where.append(f"i.{col} = ?")
            params.append(val)
    if status == "active" or not status:
        where.append("i.status NOT IN ('rejected', 'archived')")
    elif status != "all":
        where.append("i.status = ?")
        params.append(status)
    if min_score is not None:
        where.append("i.score >= ?")
        params.append(min_score)
    if days:
        where.append("i.created_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)")
        params.append(f"-{float(days)} days")
    if q:
        where.append("(i.title LIKE ? OR i.one_liner LIKE ? OR i.problem LIKE ? OR i.solution LIKE ?"
                     " OR i.tags_json LIKE ? OR i.audience LIKE ?)")
        params += [f"%{q}%"] * 6
    if platform:
        where.append("EXISTS (SELECT 1 FROM ideas m JOIN idea_evidence e ON e.idea_id=m.id"
                     " JOIN posts p ON p.id=e.post_id WHERE m.cluster_id=i.id AND p.platform=?)")
        params.append(platform)
    return " AND ".join(where), params


def query_ideas(db: DB, *, sort: str = "score", limit: int = 100, offset: int = 0,
                **filters: Any) -> tuple[list[dict[str, Any]], int]:
    w, params = ideas_where(**filters)
    total = db.scalar(f"SELECT COUNT(*) FROM ideas i WHERE {w}", params, 0)
    rows = db.query(
        f"SELECT i.* FROM ideas i WHERE {w} ORDER BY {SORTS.get(sort, SORTS['score'])} LIMIT ? OFFSET ?",
        [*params, limit, offset],
    )
    for r in rows:
        decode(r)
    return rows, total


def region_counts(db: DB, by_sphere: bool = False, **filters: Any) -> list[dict[str, Any]]:
    """Число идей по регионам (идея с несколькими регионами учитывается в каждом)."""
    w, params = ideas_where(**filters)
    sphere = "i.sphere AS sphere, " if by_sphere else ""
    group = "i.sphere, j.value" if by_sphere else "j.value"
    return db.query(f"SELECT {sphere}j.value AS region, COUNT(*) AS n, ROUND(MAX(i.score), 1) AS best"
                    f" FROM ideas i, json_each(COALESCE(i.regions_json, '[]')) j WHERE {w} GROUP BY {group}", params)


def facet_counts(db: DB, cols: tuple[str, ...], **filters: Any) -> list[dict[str, Any]]:
    """Число идей в разрезе колонок (для карты «сфера × рынок» и счётчиков фильтров)."""
    w, params = ideas_where(**filters)
    group = ", ".join(f"i.{c}" for c in cols)
    return db.query(f"SELECT {', '.join(f'i.{c} AS {c}' for c in cols)}, COUNT(*) AS n,"
                    f" ROUND(MAX(i.score), 1) AS best FROM ideas i WHERE {w} GROUP BY {group}", params)


def decode(r: dict[str, Any]) -> dict[str, Any]:
    r["tags"] = loads(r.get("tags_json"), [])
    r["scores"] = loads(r.get("scores_json"), {})
    r["spheres_extra"] = loads(r.get("spheres_json"), [])
    r["regions"] = loads(r.get("regions_json"), [])
    return r


def evidence_for(db: DB, cluster_id: int) -> list[dict[str, Any]]:
    return db.query(
        "SELECT e.quote, e.quote_verified, m.id AS idea_id, m.title AS idea_title, p.id AS post_id, p.platform,"
        " p.url, p.title, p.body, p.community, p.author, p.score AS post_score, p.created_at, p.triage_score,"
        " p.triage_note, p.direction FROM ideas m JOIN idea_evidence e ON e.idea_id=m.id"
        " JOIN posts p ON p.id=e.post_id WHERE m.cluster_id=? OR m.id=? ORDER BY p.score DESC",
        (cluster_id, cluster_id),
    )


EXPORT_FIELDS = ["id", "score", "title", "one_liner", "market", "sphere", "nature", "business_model",
                 "regions", "origin_region", "insight_type", "mentions", "problem", "solution", "audience", "reasoning", "why_now",
                 "monetization", "risks", "tags", "status", "note", "created_at", "sources"]


def export_rows(db: DB, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        ev = evidence_for(db, r["id"])
        item = {k: r.get(k) for k in EXPORT_FIELDS if k not in ("tags", "sources", "regions")}
        item["tags"] = r.get("tags") or loads(r.get("tags_json"), [])
        item["regions"] = r.get("regions") or loads(r.get("regions_json"), [])
        item["scores"] = r.get("scores") or loads(r.get("scores_json"), {})
        item["sources"] = [{"url": e["url"], "platform": e["platform"], "quote": e["quote"],
                            "verified": bool(e["quote_verified"]), "post_title": e["title"]} for e in ev]
        out.append(item)
    return out


def safe_cell(v: Any) -> Any:
    """Тексты из соцсетей и от ИИ не должны исполняться в таблице как формулы."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def to_csv(items: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=EXPORT_FIELDS, extrasaction="ignore")
    w.writeheader()
    for it in items:
        row = {k: safe_cell(v) for k, v in it.items()}
        row["tags"] = ", ".join(it.get("tags") or [])
        row["regions"] = ", ".join(it.get("regions") or [])
        row["sources"] = " ".join(s["url"] or "" for s in it.get("sources") or [])
        w.writerow(row)
    return buf.getvalue()


def to_xlsx(items: list[dict[str, Any]], tax: dict[str, dict[str, str]]) -> bytes:
    """Книга Excel: «Идеи» (фильтры, закреплённая шапка, ссылки), «Источники» с цитатами
    и сводка «Сфера × рынок»."""
    from openpyxl import Workbook
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    from .core.labels import INSIGHT, NATURE, PLATFORM, SCORE_LABELS, STATUS, region_short

    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="2747C7")
    wrap = Alignment(wrap_text=True, vertical="top")
    top = Alignment(vertical="top")
    link_font = Font(color="2747C7", underline="single")

    def put(ws: Any, row: int, col: int, value: Any) -> Any:
        if isinstance(value, str):
            value = ILLEGAL_CHARACTERS_RE.sub("", value)
        cell = ws.cell(row=row, column=col, value=value)
        if isinstance(value, str) and value.startswith("="):
            cell.data_type = "s"  # строка, а не формула
        return cell

    def header(ws: Any, cols: list[tuple[str, int]]) -> None:
        for c, (title, width) in enumerate(cols, 1):
            cell = put(ws, 1, c, title)
            cell.font, cell.fill, cell.alignment = head_font, head_fill, Alignment(vertical="center", wrap_text=True)
            ws.column_dimensions[get_column_letter(c)].width = width
        ws.row_dimensions[1].height = 32

    wb = Workbook()
    ws = wb.active
    ws.title = "Идеи"
    score_cols = [(label, 10) for label in SCORE_LABELS.values()]
    regions_label = lambda keys: ", ".join(region_short(k, tax.get("regions")) for k in keys or [])  # noqa: E731
    cols = [("ID", 7), ("Балл", 7), ("Название", 42), ("Суть", 60), ("Рынок", 9), ("Сфера", 28),
            ("Тип продукта", 13), ("Модель монетизации", 22), ("Регионы запуска", 20), ("Откуда сигнал", 15),
            ("Как найдена", 13), ("Упоминаний", 12),
            *score_cols, ("Проблема", 55), ("Решение", 55), ("Аудитория", 40), ("Как выведена", 50),
            ("Почему сейчас", 40), ("Монетизация", 40), ("Риски", 40), ("Теги", 28), ("Статус", 12),
            ("Заметка", 30), ("Создана", 19), ("Источник", 45)]
    header(ws, cols)
    long_cols = {3, 4, 9} | set(range(13 + len(score_cols), len(cols)))
    for r, it in enumerate(items, 2):
        src = (it.get("sources") or [{}])[0]
        values = [
            it["id"], it.get("score"), it.get("title"), it.get("one_liner"), it.get("market"),
            tax["spheres"].get(it.get("sphere") or "", it.get("sphere")),
            NATURE.get(it.get("nature") or "", it.get("nature")),
            tax["business_models"].get(it.get("business_model") or "", it.get("business_model")),
            regions_label(it.get("regions")), region_short(it.get("origin_region"), tax.get("regions")),
            INSIGHT.get(it.get("insight_type") or "", it.get("insight_type")), it.get("mentions"),
            *[(it.get("scores") or {}).get(k) for k in SCORE_LABELS],
            it.get("problem"), it.get("solution"), it.get("audience"), it.get("reasoning"), it.get("why_now"),
            it.get("monetization"), it.get("risks"), ", ".join(it.get("tags") or []),
            STATUS.get(it.get("status") or "", it.get("status")), it.get("note"),
            (it.get("created_at") or "").replace("T", " ")[:19], src.get("url"),
        ]
        for c, v in enumerate(values, 1):
            cell = put(ws, r, c, v)
            cell.alignment = wrap if c in long_cols else top
        ws.cell(row=r, column=2).number_format = "0.0"
        if src.get("url"):
            link = ws.cell(row=r, column=len(cols))
            link.hyperlink, link.font = src["url"], link_font
    ws.freeze_panes = "D2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(cols))}{max(1, len(items) + 1)}"

    ws2 = wb.create_sheet("Источники")
    header(ws2, [("ID идеи", 9), ("Идея", 42), ("Площадка", 11), ("Пост", 45), ("Цитата", 80),
                 ("Цитата найдена в посте", 14), ("Ссылка", 50)])
    r = 2
    for it in items:
        for s in it.get("sources") or []:
            values = [it["id"], it.get("title"), PLATFORM.get(s.get("platform") or "", s.get("platform")),
                      s.get("post_title"), s.get("quote"), "да" if s.get("verified") else "нет", s.get("url")]
            for c, v in enumerate(values, 1):
                put(ws2, r, c, v).alignment = wrap if c in (2, 4, 5) else top
            if s.get("url"):
                ws2.cell(row=r, column=7).hyperlink = s["url"]
                ws2.cell(row=r, column=7).font = link_font
            r += 1
    ws2.freeze_panes = "C2"
    ws2.auto_filter.ref = f"A1:G{max(1, r - 1)}"

    def pivot(title: str, columns: list[tuple[str, str]], keys_of: Any) -> None:
        """Сводная «сфера × колонка»; keys_of(идея) → ключи колонок, в которые она попадает."""
        sheet = wb.create_sheet(title)
        counts: dict[str, dict[str, int]] = {}
        for it in items:
            row = counts.setdefault(it.get("sphere") or "other", {})
            for k in keys_of(it):
                row[k] = row.get(k, 0) + 1
        used = [(k, label) for k, label in columns if any(k in row for row in counts.values())] or columns
        header(sheet, [("Сфера", 36), *[(label, 12) for _, label in used], ("Идей", 9)])
        spheres = sorted(counts, key=lambda k: -max(counts[k].values(), default=0))
        for r, sp in enumerate(spheres, 2):
            put(sheet, r, 1, tax["spheres"].get(sp, sp))
            for c, (k, _) in enumerate(used, 2):
                if counts[sp].get(k):
                    put(sheet, r, c, counts[sp][k])
            n = sum(1 for it in items if (it.get("sphere") or "other") == sp)
            put(sheet, r, len(used) + 2, n).font = Font(bold=True)
        sheet.freeze_panes = "B2"

    pivot("Сфера × рынок", [(m, m) for m in tax["markets"]], lambda it: [it.get("market") or "?"])
    pivot("Сфера × регион", [(k, region_short(k, tax.get("regions"))) for k in tax.get("regions", {})],
          lambda it: it.get("regions") or [])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def to_json(items: list[dict[str, Any]]) -> str:
    return json.dumps(items, ensure_ascii=False, indent=2)


def to_markdown(items: list[dict[str, Any]]) -> str:
    lines = ["# Идеи", ""]
    for it in items:
        lines += [
            f"## {it['title']} — {it['score']}",
            f"`{it['market']}` `{it['sphere']}` `{it['nature']}` `{it['business_model']}` `{it['insight_type']}`"
            + (f" регионы: {', '.join(it.get('regions') or [])}" if it.get("regions") else "")
            + (f" · упоминаний: {it['mentions']}" if (it.get("mentions") or 1) > 1 else ""),
            "",
            it.get("one_liner") or "",
            "",
            f"**Проблема.** {it.get('problem') or '—'}",
            f"**Решение.** {it.get('solution') or '—'}",
            f"**Аудитория.** {it.get('audience') or '—'}",
            f"**Монетизация.** {it.get('monetization') or '—'}",
            f"**Риски.** {it.get('risks') or '—'}",
            "",
        ]
        for s in it.get("sources") or []:
            lines.append(f"> {s['quote']}  \n> — {s['url']}")
        lines.append("")
    return "\n".join(lines)

"""Excel workbook: "Идеи" with filters, a frozen header and links, "Источники" with quotes,
and "sphere × market" and "sphere × region" pivots."""
from __future__ import annotations

import io
from typing import Any, Callable

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE, Cell
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from ..core.labels import INSIGHT, NATURE, PLATFORM, SCORE_LABELS, STATUS, region_short
from ..core.taxonomy import Taxonomy
from .records import Record

ACCENT = "2747C7"
HEADER_FONT = Font(bold=True, color="FFFFFF")
HEADER_FILL = PatternFill("solid", fgColor=ACCENT)
LINK_FONT = Font(color=ACCENT, underline="single")
WRAP = Alignment(wrap_text=True, vertical="top")
TOP = Alignment(vertical="top")

IDEA_COLUMNS = [("ID", 7), ("Балл", 7), ("Название", 42), ("Суть", 60), ("Рынок", 9), ("Сфера", 28),
                ("Тип продукта", 13), ("Модель монетизации", 22), ("Регионы запуска", 20), ("Откуда сигнал", 15),
                ("Как найдена", 13), ("Упоминаний", 12), *[(label, 10) for label in SCORE_LABELS.values()],
                ("Проблема", 55), ("Решение", 55), ("Аудитория", 40), ("Как выведена", 50), ("Почему сейчас", 40),
                ("Монетизация", 40), ("Риски", 40), ("Теги", 28), ("Статус", 12), ("Заметка", 30), ("Создана", 19),
                ("Источник", 45)]
FIRST_LONG_TEXT_COLUMN = 13 + len(SCORE_LABELS)
IDEA_WRAPPED = {3, 4, 9} | set(range(FIRST_LONG_TEXT_COLUMN, len(IDEA_COLUMNS)))
SOURCE_COLUMNS = [("ID идеи", 9), ("Идея", 42), ("Площадка", 11), ("Пост", 45), ("Цитата", 80),
                  ("Цитата найдена в посте", 14), ("Ссылка", 50)]
SOURCE_WRAPPED = {2, 4, 5}


def to_xlsx(items: list[Record], tax: Taxonomy) -> bytes:
    wb = Workbook()
    _ideas_sheet(wb.active, items, tax)
    _sources_sheet(wb.create_sheet("Источники"), items)
    _pivot(wb.create_sheet("Сфера × рынок"), items, tax, [(m, m) for m in tax["markets"]],
           lambda it: [it.get("market") or "?"])
    _pivot(wb.create_sheet("Сфера × регион"), items, tax,
           [(k, region_short(k, tax.get("regions"))) for k in tax.get("regions", {})],
           lambda it: it.get("regions") or [])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _put(ws: Worksheet, row: int, col: int, value: Any) -> Cell:
    if isinstance(value, str):
        value = ILLEGAL_CHARACTERS_RE.sub("", value)
    cell = ws.cell(row=row, column=col, value=value)
    if isinstance(value, str) and value.startswith("="):
        cell.data_type = "s"
    return cell


def _header(ws: Worksheet, columns: list[tuple[str, int]]) -> None:
    for c, (title, width) in enumerate(columns, 1):
        cell = _put(ws, 1, c, title)
        cell.font, cell.fill, cell.alignment = HEADER_FONT, HEADER_FILL, Alignment(vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(c)].width = width
    ws.row_dimensions[1].height = 32


def _link(cell: Cell, url: str) -> None:
    cell.hyperlink, cell.font = url, LINK_FONT


def _ideas_sheet(ws: Worksheet, items: list[Record], tax: Taxonomy) -> None:
    ws.title = "Идеи"
    _header(ws, IDEA_COLUMNS)
    for r, it in enumerate(items, 2):
        for c, value in enumerate(_idea_values(it, tax), 1):
            _put(ws, r, c, value).alignment = WRAP if c in IDEA_WRAPPED else TOP
        ws.cell(row=r, column=2).number_format = "0.0"
        url = (it.get("sources") or [{}])[0].get("url")
        if url:
            _link(ws.cell(row=r, column=len(IDEA_COLUMNS)), url)
    ws.freeze_panes = "D2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(IDEA_COLUMNS))}{max(1, len(items) + 1)}"


def _idea_values(it: Record, tax: Taxonomy) -> list[Any]:
    def label(options: dict[str, str], key: str) -> Any:
        return options.get(it.get(key) or "", it.get(key))

    regions = tax.get("regions")
    return [
        it["id"], it.get("score"), it.get("title"), it.get("one_liner"), it.get("market"),
        label(tax["spheres"], "sphere"), label(NATURE, "nature"), label(tax["business_models"], "business_model"),
        ", ".join(region_short(k, regions) for k in it.get("regions") or []),
        region_short(it.get("origin_region"), regions), label(INSIGHT, "insight_type"), it.get("mentions"),
        *[(it.get("scores") or {}).get(k) for k in SCORE_LABELS],
        it.get("problem"), it.get("solution"), it.get("audience"), it.get("reasoning"), it.get("why_now"),
        it.get("monetization"), it.get("risks"), ", ".join(it.get("tags") or []), label(STATUS, "status"),
        it.get("note"), (it.get("created_at") or "").replace("T", " ")[:19],
        (it.get("sources") or [{}])[0].get("url"),
    ]


def _sources_sheet(ws: Worksheet, items: list[Record]) -> None:
    _header(ws, SOURCE_COLUMNS)
    r = 2
    for it in items:
        for s in it.get("sources") or []:
            values = [it["id"], it.get("title"), PLATFORM.get(s.get("platform") or "", s.get("platform")),
                      s.get("post_title"), s.get("quote"), "да" if s.get("verified") else "нет", s.get("url")]
            for c, value in enumerate(values, 1):
                _put(ws, r, c, value).alignment = WRAP if c in SOURCE_WRAPPED else TOP
            if s.get("url"):
                _link(ws.cell(row=r, column=len(SOURCE_COLUMNS)), s["url"])
            r += 1
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(SOURCE_COLUMNS))}{max(1, r - 1)}"


def _pivot(ws: Worksheet, items: list[Record], tax: Taxonomy, columns: list[tuple[str, str]],
           keys_of: Callable[[Record], list[str]]) -> None:
    """Sphere × column counts; keys_of(idea) gives the columns an idea falls into."""
    counts: dict[str, dict[str, int]] = {}
    totals: dict[str, int] = {}
    for it in items:
        sphere = it.get("sphere") or "other"
        totals[sphere] = totals.get(sphere, 0) + 1
        row = counts.setdefault(sphere, {})
        for k in keys_of(it):
            row[k] = row.get(k, 0) + 1
    used = [(k, label) for k, label in columns if any(k in row for row in counts.values())] or columns
    _header(ws, [("Сфера", 36), *[(label, 12) for _, label in used], ("Идей", 9)])
    for r, sphere in enumerate(sorted(counts, key=lambda k: -max(counts[k].values(), default=0)), 2):
        _put(ws, r, 1, tax["spheres"].get(sphere, sphere))
        for c, (k, _) in enumerate(used, 2):
            if counts[sphere].get(k):
                _put(ws, r, c, counts[sphere][k])
        _put(ws, r, len(used) + 2, totals[sphere]).font = Font(bold=True)
    ws.freeze_panes = "B2"

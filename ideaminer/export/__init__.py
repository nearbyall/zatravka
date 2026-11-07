"""Exports of ideas shared by the CLI and the web UI."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from ..core.taxonomy import Taxonomy
from .records import EXPORT_FIELDS, Record, export_rows
from .text import to_csv, to_json, to_markdown
from .xlsx import to_xlsx


@dataclass(frozen=True)
class Format:
    media_type: str
    render: Callable[[list[Record], Taxonomy], str | bytes]


FORMATS = {
    "xlsx": Format("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", to_xlsx),
    "csv": Format("text/csv; charset=utf-8", lambda items, _: to_csv(items)),
    "json": Format("application/json; charset=utf-8", lambda items, _: to_json(items)),
    "md": Format("text/markdown; charset=utf-8", lambda items, _: to_markdown(items)),
}


def export_file_name(fmt: str) -> str:
    return f"ideas-{datetime.now():%Y%m%d-%H%M}.{fmt}"


__all__ = ["EXPORT_FIELDS", "FORMATS", "Format", "Record", "export_file_name", "export_rows", "to_csv", "to_json",
           "to_markdown", "to_xlsx"]

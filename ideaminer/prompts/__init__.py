"""Промпты лежат в .md-файлах рядом — правятся без изменения кода.
Подстановки — $имя (string.Template), чтобы фигурные скобки JSON не мешали."""
from __future__ import annotations

from pathlib import Path
from string import Template

LANG_NAMES = {"ru": "русский", "en": "английский", "uk": "украинский", "de": "немецкий", "es": "испанский"}


def render(prompts_dir: Path, name: str, **values: object) -> str:
    path = prompts_dir / f"{name}.md"
    if not path.exists():  # своя папка может переопределять только часть промптов
        path = Path(__file__).parent / f"{name}.md"
    text = path.read_text(encoding="utf-8")
    if "language" in values:
        values["language"] = LANG_NAMES.get(str(values["language"]), str(values["language"]))
    return Template(text).safe_substitute({k: str(v) for k, v in values.items()})

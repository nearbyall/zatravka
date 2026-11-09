"""Prompts live in the .md files next to this module and are edited without touching code.
Placeholders use $name (string.Template) so that JSON braces need no escaping."""
from __future__ import annotations

from pathlib import Path
from string import Template

LANG_NAMES = {"ru": "русский", "en": "английский", "uk": "украинский", "de": "немецкий", "es": "испанский"}


def render(prompts_dir: Path, name: str, **values: object) -> str:
    path = prompts_dir / f"{name}.md"
    if not path.exists():  # a custom prompts_dir may override only some prompts
        path = Path(__file__).parent / f"{name}.md"
    text = path.read_text(encoding="utf-8")
    if "language" in values:
        values["language"] = LANG_NAMES.get(str(values["language"]), str(values["language"]))
    return Template(text).safe_substitute({k: str(v) for k, v in values.items()})

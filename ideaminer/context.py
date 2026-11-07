from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import Any

from .config import Config
from .db import DB
from .http import PoliteHttp
from .llm import LLM
from .prompts import render
from .sources import Source, build_sources


@dataclass(frozen=True)
class Scope:
    """Какие посты разбирают стадии ИИ. Пустая — вся очередь. Прогон с --sources или
    --directions обрабатывает только свои посты (и оставшиеся от прошлых таких же
    прогонов), а не накопленный хвост остальных площадок и направлений."""
    sources: tuple[str, ...] = ()
    directions: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.sources or self.directions)

    def sql(self) -> tuple[str, list[str]]:
        """Условие для WHERE по таблице posts (« AND …» или пусто) и его параметры."""
        parts, params = [], []
        if self.sources:
            # Источник поста — начало ключа его ленты: «x:search:…», «threads:profile:…»
            parts.append(f"substr(feed, 1, instr(feed, ':') - 1) IN ({','.join('?' * len(self.sources))})")
            params += self.sources
        if self.directions:
            parts.append(f"direction IN ({','.join('?' * len(self.directions))})")
            params += self.directions
        return "".join(f" AND {p}" for p in parts), params

    def describe(self) -> str:
        return "; ".join(f"{k} {', '.join(v)}" for k, v in
                         (("источники", self.sources), ("направления", self.directions)) if v)


@dataclass
class Ctx:
    """Всё, что нужно стадиям конвейера."""
    cfg: Config
    db: DB
    http: PoliteHttp
    sources: dict[str, Source]
    llm: LLM
    rng: random.Random
    run_id: int | None = None
    scope: Scope = field(default_factory=Scope)
    stats: dict[str, Any] = field(default_factory=dict)
    log: logging.Logger = field(default_factory=lambda: logging.getLogger("ideaminer"))

    def prompt(self, name: str, **values: Any) -> str:
        values.setdefault("language", self.cfg.output_language)
        return render(self.cfg.prompts_dir, name, **values)

    def stat(self, stage: str, key: str, inc: int | float = 1) -> None:
        s = self.stats.setdefault(stage, {})
        s[key] = s.get(key, 0) + inc

    def set_run(self, run_id: int | None) -> None:
        self.run_id = run_id
        self.llm.run_id = run_id

    def close(self) -> None:
        self.http.close()


def make_ctx(cfg: Config, db: DB | None = None, seed: int | None = None) -> Ctx:
    db = db or DB(cfg.db_path)
    http = PoliteHttp(cfg.http)
    rng = random.Random(seed if seed is not None else cfg.planner.seed)
    return Ctx(cfg=cfg, db=db, http=http, sources=build_sources(cfg, http, db), llm=LLM(cfg, db), rng=rng)

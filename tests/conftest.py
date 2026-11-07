from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable

import pytest

from ideaminer.config import load_config
from ideaminer.context import make_ctx
from ideaminer.llm.client import Usage

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures"


class FakeProvider:
    """ИИ-провайдер для тестов: отвечает функцией от (system, user)."""
    name = "fake"
    key = "test"

    def __init__(self, fn: Callable[[str, str], str]):
        self.fn = fn
        self.calls: list[tuple[str, str]] = []

    def model_for(self, stage: str) -> str:
        return "deepseek-flash"

    def complete(self, model, system, messages, max_tokens, temperature):
        user = messages[-1]["content"]
        self.calls.append((system, user))
        return self.fn(system, user), Usage(1000, 200, 0)


def smart_llm(system: str, user: str) -> str:
    """Правдоподобные ответы на каждую стадию по тексту промпта."""
    if "скаут" in system:
        ids = [int(x) for x in re.findall(r"### id=(\d+)", user)]
        return json.dumps({"items": [{"id": i, "score": 8 if "wish" in user.lower() else 3,
                                      "signals": ["wish"], "note": "есть запрос"} for i in ids]})
    if "латеральным" in system:
        quote = "I wish there was an app that tracks my plant watering"
        return json.dumps({"ideas": [
            {"title": "Напоминалка полива комнатных растений", "one_liner": "Приложение для полива",
             "problem": "Люди забывают поливать", "solution": "Напоминания по виду растения",
             "audience": "Владельцы растений", "insight_type": "explicit", "reasoning": "Прямой запрос",
             "evidence": quote, "monetization": "подписка", "risks": "конкуренция", "confidence": 0.7},
            {"title": "Датчики влажности для цветочных магазинов", "one_liner": "B2B-датчики",
             "insight_type": "lateral", "evidence": "выдуманная цитата которой нет в тексте поста вообще",
             "confidence": 0.4},
        ]})
    if "таксономии" in system:
        items = json.loads(user)
        return json.dumps({"items": [{
            "id": it["id"], "market": "b2c" if "Напоминалка" in it["title"] else "B2B",
            "sphere": "home", "secondary_spheres": ["hobbies", "nonexistent"], "nature": "digital",
            "business_model": "subscription", "tags": ["растения", "ИИ"],
            "regions": ["СНГ", "eu", "global"] if "Напоминалка" in it["title"] else ["global"],
            "origin_region": it.get("source", {}).get("region_hint") or "north_america",
            "scores": {"demand": 7, "monetization": 6, "novelty": 5, "feasibility": 8, "whitespace": 4, "urgency": 5},
            "score_note": "за и против"} for it in items]})
    if "дубликаты" in system:
        items = json.loads(user)
        return json.dumps({"decisions": [{"id": it["id"], "same_as": it["candidates"][0]["id"]} for it in items]})
    if "исследователь рынков" in system:
        return json.dumps({"directions": [{"key": "Beekeeping Pros", "name": "Пасечники", "rationale": "ниша",
                                           "subreddits": ["Beekeeping", "r/apiculture", "bad name!"],
                                           "reddit_search": ['"hive" tracking'], "x_queries": ["beekeeping app"],
                                           "threads_queries": ["пасека"]}]})
    raise AssertionError(f"неожиданный промпт: {system[:80]}")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Тесты не ходят в сеть: настоящий X стоит денег, а Reddit режет частые запросы.
    TestClient FastAPI работает через свой транспорт и не затрагивается."""
    import httpx

    def blocked(self, request):
        raise RuntimeError(f"сеть в тестах запрещена: {request.url.host}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)


@pytest.fixture
def cfg(tmp_path):
    from ideaminer.config import Secrets
    c = load_config(ROOT / "config.yaml", {
        "storage": {"db_path": str(tmp_path / "test.db"), "inbox_dir": str(tmp_path / "inbox"),
                    "exports_dir": str(tmp_path / "exports"), "digests_dir": str(tmp_path / "digests")},
        "llm": {"concurrency": 2},
        "budget": {"daily_usd": 10, "per_run_usd": 10},
        "http": {"min_interval_s": {"default": 0}},
        "prefilter": {"max_age_days": 100000},
    })
    c.secrets = Secrets()  # настоящие ключи из .env тестам не нужны и опасны
    c.sources.threads.web.enabled = False  # настоящий Chromium в тестах не запускаем
    return c


@pytest.fixture
def ctx(cfg):
    c = make_ctx(cfg, seed=42)
    c.fake = FakeProvider(smart_llm)
    c.llm.providers = [c.fake]
    yield c
    c.close()
    c.db.close()

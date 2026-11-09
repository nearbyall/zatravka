"""LLM client in "return JSON" mode.

Answers are parsed and validated against a pydantic schema; a schema error is
shown to the model once for a fix. Every call is recorded in llm_calls with its
cost, and the daily and per-run budgets are checked before each call.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from ..config import Config
from ..storage.db import DB
from ..storage.usage import record_llm_call, spent_in_run, spent_today
from .providers import REGISTRY, LLMError, Messages, Provider, ProviderDown, Usage

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
RETRY_ECHO_CHARS = 6000
ERROR_ECHO_CHARS = 800


class BudgetExceeded(RuntimeError):
    pass


def extract_json(text: str) -> Any:
    """JSON from a model answer, without ``` fences and surrounding chatter."""
    s = (text or "").strip()
    fence = _FENCE.search(s)
    if fence:
        s = fence.group(1).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    start = min((i for i in (s.find("{"), s.find("[")) if i >= 0), default=-1)
    end = max(s.rfind("}"), s.rfind("]"))
    if start >= 0 and end > start:
        return json.loads(s[start:end + 1])
    raise json.JSONDecodeError("JSON не найден", s, 0)


class LLM:
    def __init__(self, cfg: Config, db: DB, run_id: int | None = None):
        self.cfg = cfg
        self.db = db
        self.run_id = run_id
        self.providers: list[Provider] = [REGISTRY[p](cfg) for p in cfg.llm.providers if p in REGISTRY]
        self._down: set[str] = set()
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return any(self._usable(p) for p in self.providers)

    def _usable(self, provider: Provider) -> bool:
        return bool(provider.key) and provider.name not in self._down

    def check_budget(self) -> None:
        b = self.cfg.budget
        today = spent_today(self.db)
        if b.daily_usd and today >= b.daily_usd:
            raise BudgetExceeded(f"дневной бюджет ${b.daily_usd:.2f} исчерпан (потрачено ${today:.4f})")
        if self.run_id and b.per_run_usd:
            run = spent_in_run(self.db, self.run_id)
            if run >= b.per_run_usd:
                raise BudgetExceeded(f"бюджет прогона ${b.per_run_usd:.2f} исчерпан (потрачено ${run:.4f})")

    def json(self, stage: str, system: str, user: str, schema: type[T],
             max_tokens: int = 4000, temperature: float = 0.3) -> T:
        self.check_budget()
        errors: list[str] = []
        for provider in filter(self._usable, self.providers):
            parsed = self._ask(provider, stage, system, user, schema, max_tokens, temperature, errors)
            if parsed is not None:
                return parsed
        raise LLMError("; ".join(errors)
                       or "нет доступных ИИ-провайдеров (задайте DEEPSEEK_API_KEY или REPLICATE_API_TOKEN)")

    def _ask(self, provider: Provider, stage: str, system: str, user: str, schema: type[T], max_tokens: int,
             temperature: float, errors: list[str]) -> T | None:
        model = provider.model_for(stage)
        messages: Messages = [{"role": "user", "content": user}]
        for attempt in range(2):
            started = time.monotonic()
            try:
                text, usage = provider.complete(model, system, messages, max_tokens, temperature)
            except (ProviderDown, LLMError) as e:
                if isinstance(e, ProviderDown):
                    with self._lock:
                        self._down.add(provider.name)
                    log.error("Провайдер %s отключён до конца процесса: %s", provider.name, e)
                self._record(stage, provider.name, model, Usage(), started, str(e))
                errors.append(str(e))
                return None
            try:
                parsed = schema.model_validate(extract_json(text))
            except (json.JSONDecodeError, ValidationError, ValueError) as e:
                self._record(stage, provider.name, model, usage, started, f"bad json: {e}")
                errors.append(f"{provider.name}: bad json")
                messages += [
                    {"role": "assistant", "content": text[:RETRY_ECHO_CHARS]},
                    {"role": "user", "content": f"Ответ не прошёл проверку схемы: {str(e)[:ERROR_ECHO_CHARS]}\n"
                                                "Верни исправленный JSON целиком, строго по схеме."},
                ]
                continue
            self._record(stage, provider.name, model, usage, started, None)
            return parsed
        return None

    def _cost(self, model: str, u: Usage) -> float:
        price = self.cfg.llm.prices_usd_per_mtok.get(model)
        if not price:
            return 0.0
        fresh = max(0, u.prompt - u.cached)
        return (fresh * price.in_ + u.cached * price.cached_in + u.completion * price.out) / 1_000_000

    def _record(self, stage: str, provider: str, model: str, u: Usage, started: float, error: str | None) -> None:
        record_llm_call(self.db, run_id=self.run_id, stage=stage, provider=provider, model=model,
                        prompt_tokens=u.prompt, completion_tokens=u.completion, cached_tokens=u.cached,
                        cost_usd=self._cost(model, u), duration_ms=int((time.monotonic() - started) * 1000),
                        ok=error is None, error=error)

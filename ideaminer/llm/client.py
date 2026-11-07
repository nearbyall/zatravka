"""ИИ-клиент: DeepSeek (OpenAI-совместимый API) с запасным Replicate.

Режим «верни JSON»: ответ разбирается и проверяется pydantic-схемой. При ошибке
модели один раз показывается её же ошибка. Каждый вызов пишется в llm_calls
вместе со стоимостью; перед вызовом проверяется бюджет на день и на прогон.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from ..config import Config
from ..db import DB, utcnow

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class BudgetExceeded(RuntimeError):
    pass


class LLMError(RuntimeError):
    pass


class ProviderDown(RuntimeError):
    """Провайдер недоступен надолго (нет ключа, нет денег) — переключаемся на следующий."""


@dataclass
class Usage:
    prompt: int = 0
    completion: int = 0
    cached: int = 0


def extract_json(text: str) -> Any:
    """JSON из ответа модели: снимает ```-ограждения и мусор вокруг объекта."""
    s = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
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


class DeepSeekProvider:
    name = "deepseek"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.key = cfg.secrets.deepseek_api_key
        self.client = httpx.Client(timeout=cfg.llm.timeout_s)

    def model_for(self, stage: str) -> str:
        return self.cfg.llm.deepseek.model_for(stage)

    def complete(self, model: str, system: str, messages: list[dict[str, str]],
                 max_tokens: int, temperature: float) -> tuple[str, Usage]:
        if not self.key:
            raise ProviderDown("нет DEEPSEEK_API_KEY")
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system}, *messages],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        last: Exception | None = None
        for attempt in range(4):
            try:
                r = self.client.post(
                    f"{self.cfg.llm.deepseek.base_url}/chat/completions",
                    json=body, headers={"Authorization": f"Bearer {self.key}"},
                )
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last = e
                time.sleep(2 ** attempt * 2)
                continue
            if r.status_code in (401, 402, 403):
                raise ProviderDown(f"DeepSeek {r.status_code}: {r.text[:200]}")
            if r.status_code in (429, 500, 502, 503, 504):
                last = LLMError(f"DeepSeek {r.status_code}")
                time.sleep(2 ** attempt * 3)
                continue
            if r.status_code >= 400:
                raise LLMError(f"DeepSeek {r.status_code}: {r.text[:300]}")
            data = r.json()
            u = data.get("usage") or {}
            cached = u.get("prompt_cache_hit_tokens")
            if cached is None:
                cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
            choice = data["choices"][0]
            if choice.get("finish_reason") == "length":
                log.warning("DeepSeek упёрся в max_tokens=%s", max_tokens)
            return choice["message"].get("content") or "", Usage(
                u.get("prompt_tokens", 0), u.get("completion_tokens", 0), cached or 0)
        raise LLMError(f"DeepSeek не ответил: {last}")


class ReplicateProvider:
    name = "replicate"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.key = cfg.secrets.replicate_api_token
        self.client = httpx.Client(timeout=cfg.llm.timeout_s)

    def model_for(self, stage: str) -> str:
        return self.cfg.llm.replicate.model

    def complete(self, model: str, system: str, messages: list[dict[str, str]],
                 max_tokens: int, temperature: float) -> tuple[str, Usage]:
        if not self.key:
            raise ProviderDown("нет REPLICATE_API_TOKEN")
        if len(messages) == 1:
            prompt = messages[0]["content"]
        else:
            prompt = "\n\n".join(
                f"[{'Пользователь' if m['role'] == 'user' else 'Твой предыдущий ответ'}]\n{m['content']}"
                for m in messages)
        inp = {
            "system_prompt": system + "\n\nОтвечай ТОЛЬКО валидным JSON-объектом, без пояснений и без ```.",
            "prompt": prompt,
            "max_completion_tokens": max_tokens * 2,  # у reasoning-моделей часть уходит на размышления
            "reasoning_effort": self.cfg.llm.replicate.reasoning_effort,
        }
        headers = {"Authorization": f"Bearer {self.key}", "Prefer": "wait=60"}
        r = self.client.post(f"https://api.replicate.com/v1/models/{model}/predictions",
                             json={"input": inp}, headers=headers)
        if r.status_code in (401, 402, 403):
            raise ProviderDown(f"Replicate {r.status_code}: {r.text[:200]}")
        if r.status_code >= 400:
            raise LLMError(f"Replicate {r.status_code}: {r.text[:300]}")
        pred = r.json()
        deadline = time.monotonic() + self.cfg.llm.timeout_s * 2
        while pred.get("status") not in ("succeeded", "failed", "canceled"):
            if time.monotonic() > deadline:
                raise LLMError("Replicate: истекло время ожидания")
            time.sleep(1.5)
            pred = self.client.get(pred["urls"]["get"], headers={"Authorization": f"Bearer {self.key}"}).json()
        if pred["status"] != "succeeded":
            raise LLMError(f"Replicate: {pred.get('status')} {str(pred.get('error'))[:300]}")
        out = pred.get("output")
        text = "".join(out) if isinstance(out, list) else str(out or "")
        m = pred.get("metrics") or {}
        pt = m.get("input_token_count") or (len(system) + len(prompt)) // 4
        ct = m.get("output_token_count") or len(text) // 4
        return text, Usage(int(pt), int(ct), 0)


class LLM:
    def __init__(self, cfg: Config, db: DB, run_id: int | None = None):
        self.cfg = cfg
        self.db = db
        self.run_id = run_id
        registry = {"deepseek": DeepSeekProvider, "replicate": ReplicateProvider}
        self.providers = [registry[p](cfg) for p in cfg.llm.providers if p in registry]
        self._down: set[str] = set()
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return any(p.key and p.name not in self._down for p in self.providers)

    # ── бюджет ──────────────────────────────────────────────────────────
    def spent_today(self) -> float:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00")
        return self.db.spent_since(day)

    def check_budget(self) -> None:
        b = self.cfg.budget
        today = self.spent_today()
        if b.daily_usd and today >= b.daily_usd:
            raise BudgetExceeded(f"дневной бюджет ${b.daily_usd:.2f} исчерпан (потрачено ${today:.4f})")
        if self.run_id and b.per_run_usd:
            run = self.db.spent_in_run(self.run_id)
            if run >= b.per_run_usd:
                raise BudgetExceeded(f"бюджет прогона ${b.per_run_usd:.2f} исчерпан (потрачено ${run:.4f})")

    def _cost(self, model: str, u: Usage) -> float:
        p = self.cfg.llm.prices_usd_per_mtok.get(model)
        if not p:
            return 0.0
        fresh = max(0, u.prompt - u.cached)
        return (fresh * p.in_ + u.cached * p.cached_in + u.completion * p.out) / 1_000_000

    def _record(self, stage: str, provider: str, model: str, u: Usage, ms: int, ok: bool, error: str | None) -> float:
        cost = self._cost(model, u)
        self.db.execute(
            "INSERT INTO llm_calls(ts, run_id, stage, provider, model, prompt_tokens, completion_tokens,"
            " cached_tokens, cost_usd, duration_ms, ok, error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (utcnow(), self.run_id, stage, provider, model, u.prompt, u.completion, u.cached,
             cost, ms, int(ok), (error or "")[:500] or None),
        )
        return cost

    # ── основной вызов ──────────────────────────────────────────────────
    def json(self, stage: str, system: str, user: str, schema: type[T],
             max_tokens: int = 4000, temperature: float = 0.3) -> T:
        self.check_budget()
        errors: list[str] = []
        for provider in self.providers:
            if provider.name in self._down or not provider.key:
                continue
            model = provider.model_for(stage)
            messages = [{"role": "user", "content": user}]
            for attempt in range(2):
                started = time.monotonic()
                try:
                    text, usage = provider.complete(model, system, messages, max_tokens, temperature)
                except ProviderDown as e:
                    with self._lock:
                        self._down.add(provider.name)
                    log.error("Провайдер %s отключён до конца процесса: %s", provider.name, e)
                    self._record(stage, provider.name, model, Usage(), int((time.monotonic() - started) * 1000), False, str(e))
                    errors.append(str(e))
                    break
                except LLMError as e:
                    self._record(stage, provider.name, model, Usage(), int((time.monotonic() - started) * 1000), False, str(e))
                    errors.append(str(e))
                    break
                ms = int((time.monotonic() - started) * 1000)
                try:
                    parsed = schema.model_validate(extract_json(text))
                except (json.JSONDecodeError, ValidationError, ValueError) as e:
                    self._record(stage, provider.name, model, usage, ms, False, f"bad json: {e}")
                    errors.append(f"{provider.name}: bad json")
                    if attempt == 0:
                        messages += [
                            {"role": "assistant", "content": text[:6000]},
                            {"role": "user", "content": f"Ответ не прошёл проверку схемы: {str(e)[:800]}\n"
                                                        "Верни исправленный JSON целиком, строго по схеме."},
                        ]
                        continue
                    break
                self._record(stage, provider.name, model, usage, ms, True, None)
                return parsed
        raise LLMError("; ".join(errors) or "нет доступных ИИ-провайдеров (задайте DEEPSEEK_API_KEY или REPLICATE_API_TOKEN)")

"""LLM providers: DeepSeek (OpenAI-compatible API) and Replicate as a fallback."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

from ..config import Config

log = logging.getLogger(__name__)

RETRY_STATUSES = (429, 500, 502, 503, 504)
FATAL_STATUSES = (401, 402, 403)
DEEPSEEK_ATTEMPTS = 4
REPLICATE_POLL_S = 1.5
JSON_ONLY = "\n\nОтвечай ТОЛЬКО валидным JSON-объектом, без пояснений и без ```."


class LLMError(RuntimeError):
    pass


class ProviderDown(RuntimeError):
    """The provider is out for good (no key, no money): switch to the next one."""


@dataclass
class Usage:
    prompt: int = 0
    completion: int = 0
    cached: int = 0


Messages = list[dict[str, str]]


class Provider(Protocol):
    name: str
    key: str | None

    def model_for(self, stage: str) -> str: ...

    def complete(self, model: str, system: str, messages: Messages, max_tokens: int,
                 temperature: float) -> tuple[str, Usage]: ...


class DeepSeekProvider:
    name = "deepseek"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.key = cfg.secrets.deepseek_api_key
        self.client = httpx.Client(timeout=cfg.llm.timeout_s)

    def model_for(self, stage: str) -> str:
        return self.cfg.llm.deepseek.model_for(stage)

    def complete(self, model: str, system: str, messages: Messages, max_tokens: int,
                 temperature: float) -> tuple[str, Usage]:
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
        for attempt in range(DEEPSEEK_ATTEMPTS):
            try:
                r = self.client.post(f"{self.cfg.llm.deepseek.base_url}/chat/completions", json=body,
                                     headers={"Authorization": f"Bearer {self.key}"})
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last = e
                time.sleep(2 ** attempt * 2)
                continue
            if r.status_code in FATAL_STATUSES:
                raise ProviderDown(f"DeepSeek {r.status_code}: {r.text[:200]}")
            if r.status_code in RETRY_STATUSES:
                last = LLMError(f"DeepSeek {r.status_code}")
                time.sleep(2 ** attempt * 3)
                continue
            if r.status_code >= 400:
                raise LLMError(f"DeepSeek {r.status_code}: {r.text[:300]}")
            return self._parse(r.json(), max_tokens)
        raise LLMError(f"DeepSeek не ответил: {last}")

    @staticmethod
    def _parse(data: dict, max_tokens: int) -> tuple[str, Usage]:
        u = data.get("usage") or {}
        cached = u.get("prompt_cache_hit_tokens")
        if cached is None:
            cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
        choice = data["choices"][0]
        if choice.get("finish_reason") == "length":
            log.warning("DeepSeek упёрся в max_tokens=%s", max_tokens)
        usage = Usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0), cached or 0)
        return choice["message"].get("content") or "", usage


class ReplicateProvider:
    name = "replicate"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.key = cfg.secrets.replicate_api_token
        self.client = httpx.Client(timeout=cfg.llm.timeout_s)

    def model_for(self, stage: str) -> str:
        return self.cfg.llm.replicate.model

    def complete(self, model: str, system: str, messages: Messages, max_tokens: int,
                 temperature: float) -> tuple[str, Usage]:
        if not self.key:
            raise ProviderDown("нет REPLICATE_API_TOKEN")
        prompt = _flatten(messages)
        payload = {"input": {
            "system_prompt": system + JSON_ONLY,
            "prompt": prompt,
            # Reasoning models spend part of the budget on thinking.
            "max_completion_tokens": max_tokens * 2,
            "reasoning_effort": self.cfg.llm.replicate.reasoning_effort,
        }}
        r = self.client.post(f"https://api.replicate.com/v1/models/{model}/predictions", json=payload,
                             headers={**self._auth(), "Prefer": "wait=60"})
        if r.status_code in FATAL_STATUSES:
            raise ProviderDown(f"Replicate {r.status_code}: {r.text[:200]}")
        if r.status_code >= 400:
            raise LLMError(f"Replicate {r.status_code}: {r.text[:300]}")
        prediction = self._wait(r.json())
        out = prediction.get("output")
        text = "".join(out) if isinstance(out, list) else str(out or "")
        metrics = prediction.get("metrics") or {}
        prompt_tokens = metrics.get("input_token_count") or (len(system) + len(prompt)) // 4
        completion_tokens = metrics.get("output_token_count") or len(text) // 4
        return text, Usage(int(prompt_tokens), int(completion_tokens), 0)

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.key}"}

    def _wait(self, prediction: dict) -> dict:
        deadline = time.monotonic() + self.cfg.llm.timeout_s * 2
        while prediction.get("status") not in ("succeeded", "failed", "canceled"):
            if time.monotonic() > deadline:
                raise LLMError("Replicate: истекло время ожидания")
            time.sleep(REPLICATE_POLL_S)
            prediction = self.client.get(prediction["urls"]["get"], headers=self._auth()).json()
        if prediction["status"] != "succeeded":
            raise LLMError(f"Replicate: {prediction.get('status')} {str(prediction.get('error'))[:300]}")
        return prediction


def _flatten(messages: Messages) -> str:
    """Replicate takes a single prompt, so a retry dialogue is folded into labelled turns."""
    if len(messages) == 1:
        return messages[0]["content"]
    return "\n\n".join(f"[{'Пользователь' if m['role'] == 'user' else 'Твой предыдущий ответ'}]\n{m['content']}"
                       for m in messages)


REGISTRY: dict[str, type[DeepSeekProvider] | type[ReplicateProvider]] = {
    "deepseek": DeepSeekProvider,
    "replicate": ReplicateProvider,
}

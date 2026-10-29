"""LLM-шлюз (D21): основной провайдер → запасной, ответ проверяется pydantic-моделью.

Порядок для одного провайдера: вызов → JSON → проверка моделью → при ошибке схемы один повтор
с текстом ошибки → иначе `LLMInvalidOutput`. Отказ, недоступность или невалидный ответ
основного → запасной провайдер, если он настроен. Все провайдеры не справились → последняя
ошибка; вызывающий код уходит в офлайн-режим.

В журнал пишем только задачу, провайдер, задержку, токены и исход — без промптов и ответов (v4b).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from pydantic import BaseModel, ValidationError

from agents.llm.base import LLMClient, LLMError, LLMInvalidOutput, LLMResult, LLMUnavailable
from agents.llm.config import LLMSettings, ProviderSettings

log = logging.getLogger("agents.llm")

REPAIR_NOTE = (
    "\n\nПредыдущий ответ не прошёл проверку схемы:\n{errors}\n"
    "Верни исправленный JSON по той же схеме."
)


@dataclass
class Completion[T: BaseModel]:
    value: T
    result: LLMResult
    attempts: int


class Gateway:
    def __init__(self, clients: list[LLMClient]) -> None:
        self.clients = clients

    @property
    def available(self) -> bool:
        return bool(self.clients)

    def complete[T: BaseModel](
        self,
        *,
        task: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        model: type[T],
        max_tokens: int = 4096,
        effort: str | None = None,
    ) -> Completion[T]:
        if not self.clients:
            raise LLMUnavailable("ключ LLM не задан")
        last: LLMError | None = None
        for client in self.clients:
            try:
                return self._complete_one(
                    client, task, system, user, schema, model, max_tokens, effort
                )
            except LLMError as exc:
                log.warning(
                    "llm task=%s provider=%s outcome=%s detail=%s",
                    task,
                    client.provider,
                    exc.reason,
                    exc,
                )
                last = exc
        assert last is not None
        raise last

    def _complete_one[T: BaseModel](
        self,
        client: LLMClient,
        task: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        model: type[T],
        max_tokens: int,
        effort: str | None,
    ) -> Completion[T]:
        prompt = user
        for attempt in (1, 2):
            result = client.complete_json(
                task=task,
                system=system,
                user=prompt,
                schema=schema,
                max_tokens=max_tokens,
                effort=effort,
            )
            try:
                value = model.model_validate(result.data)
            except ValidationError as exc:
                if attempt == 2:
                    raise LLMInvalidOutput(f"ответ не прошёл схему: {_short(exc)}") from exc
                prompt = user + REPAIR_NOTE.format(errors=_short(exc))
                continue
            log.info(
                "llm task=%s provider=%s model=%s latency_ms=%s attempts=%s usage=%s",
                task,
                result.provider,
                result.model,
                result.latency_ms,
                attempt,
                result.usage,
            )
            return Completion(value=value, result=result, attempts=attempt)
        raise AssertionError("unreachable")


def _short(exc: ValidationError, limit: int = 8) -> str:
    lines = [
        f"- {'.'.join(str(p) for p in e['loc']) or '(корень)'}: {e['msg']}"
        for e in exc.errors()[:limit]
    ]
    return "\n".join(lines)


def build_client(p: ProviderSettings, s: LLMSettings) -> LLMClient:
    if p.provider == "anthropic":
        from agents.llm.anthropic_adapter import AnthropicClient

        return AnthropicClient(
            p,
            effort=s.effort,
            timeout_s=s.timeout_s,
            max_retries=s.max_retries,
            server_fallbacks=s.anthropic_fallbacks,
        )
    if p.provider == "openai_compat":
        from agents.llm.openai_compat import OpenAICompatClient

        return OpenAICompatClient(p, timeout_s=s.timeout_s, max_retries=s.max_retries)
    if p.provider == "gigachat":
        from agents.llm.gigachat import GigaChatClient

        return GigaChatClient(p, timeout_s=s.timeout_s, max_retries=s.max_retries)
    raise ValueError(f"провайдер {p.provider} не строит клиента")


@lru_cache(maxsize=4)
def _gateway_for(settings: LLMSettings) -> Gateway:
    providers = [p for p in (settings.primary, settings.fallback) if p and p.usable]
    return Gateway([build_client(p, settings) for p in providers])


def get_gateway(settings: LLMSettings | None = None) -> Gateway:
    """Шлюз по текущему окружению. Без ключа — пустой шлюз (`available = False`)."""
    return _gateway_for(settings or LLMSettings.from_env())

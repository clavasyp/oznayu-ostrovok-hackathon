"""Общий интерфейс адаптеров LLM и ошибки шлюза.

Адаптер возвращает только JSON по переданной схеме. Что из него становится данными — решает
вызывающий код и валидатор: LLM не источник фактов (D8).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class LLMError(Exception):
    """Любая неудача вызова модели. Вызывающий код уходит на запасной путь, а не падает."""

    reason = "error"


class LLMUnavailable(LLMError):
    """Нет ключа, сети, лимита или провайдер отвечает ошибкой."""

    reason = "unavailable"


class LLMRefusal(LLMError):
    """Модель отказалась отвечать (`stop_reason = refusal`)."""

    reason = "refusal"


class LLMInvalidOutput(LLMError):
    """Ответ не JSON, обрезан или не прошёл схему."""

    reason = "invalid_output"


@dataclass
class LLMResult:
    data: dict[str, Any]
    provider: str
    model: str
    latency_ms: int
    usage: dict[str, int] = field(default_factory=dict)


class LLMClient(Protocol):
    provider: str
    model: str

    def complete_json(
        self,
        *,
        task: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        max_tokens: int,
        effort: str | None = None,
    ) -> LLMResult: ...

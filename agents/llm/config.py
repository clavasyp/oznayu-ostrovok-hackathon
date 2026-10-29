"""Настройки LLM-шлюза из окружения (D21).

Основной провайдер — `LLM_*`, запасной — `LLM_FALLBACK_*` (необязательно). Без ключа шлюз
честно недоступен: вызывающий код уходит в офлайн-режим (разбор правилами), а не падает.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

Provider = Literal["anthropic", "openai_compat", "gigachat", "off"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]

PROVIDERS: tuple[str, ...] = ("anthropic", "openai_compat", "gigachat", "off")
EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
DEFAULT_MODELS = {"anthropic": "claude-opus-5-5", "gigachat": "GigaChat-3-Ultra"}


@dataclass(frozen=True)
class ProviderSettings:
    provider: Provider
    model: str
    api_key: str
    base_url: str | None = None

    @property
    def usable(self) -> bool:
        return self.provider != "off" and bool(self.api_key) and bool(self.model)


@dataclass(frozen=True)
class LLMSettings:
    primary: ProviderSettings
    fallback: ProviderSettings | None = None
    # Разбор запроса — короткая структурная задача: начинаем с low, замер против medium — в eval.
    effort: Effort = "low"
    timeout_s: float = 20.0
    # Одна повторная попытка: медиана ответа ~5 с, худший случай — таймаут × (1 + повторы).
    max_retries: int = 1
    # Серверный запасной путь Anthropic при отказе модели (beta server-side-fallback).
    anthropic_fallbacks: bool = True

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> LLMSettings:
        env = os.environ if env is None else env
        return cls(
            primary=_provider(env, "LLM_"),
            fallback=_provider(env, "LLM_FALLBACK_") if env.get("LLM_FALLBACK_PROVIDER") else None,
            effort=_choice(env.get("LLM_EFFORT") or "low", EFFORTS, "LLM_EFFORT"),
            timeout_s=float(env.get("LLM_TIMEOUT_S") or 20),
            max_retries=int(env.get("LLM_MAX_RETRIES") or 1),
            anthropic_fallbacks=env.get("LLM_ANTHROPIC_FALLBACKS", "default") != "off",
        )


def _provider(env: Mapping[str, str], prefix: str) -> ProviderSettings:
    provider = _choice(env.get(f"{prefix}PROVIDER") or "anthropic", PROVIDERS, f"{prefix}PROVIDER")
    return ProviderSettings(
        provider=provider,
        model=env.get(f"{prefix}MODEL") or DEFAULT_MODELS.get(provider, ""),
        api_key=env.get(f"{prefix}API_KEY") or "",
        base_url=env.get(f"{prefix}BASE_URL") or None,
    )


def _choice(value: str, allowed: tuple[str, ...], name: str):
    if value not in allowed:
        raise ValueError(f"{name}={value!r}: допустимо {', '.join(allowed)}")
    return value

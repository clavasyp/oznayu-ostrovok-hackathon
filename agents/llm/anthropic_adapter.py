"""Адаптер Anthropic SDK — основной провайдер (D21, Claude Opus 5.5).

- Ответ — structured outputs: `output_config.format` с JSON-схемой (через `transform_schema` SDK:
  `additionalProperties: false`, неподдержанные ограничения уходят в описание), effort — явно.
- `thinking` не передаём: у Opus 5.5 он всегда включён, `disabled` даёт 400. Без prefill и
  `temperature` (тоже 400).
- Статичный системный промпт (каталог характеристик) кэшируется: `cache_control` на нём.
- `refusal` → `LLMRefusal`; по умолчанию сначала серверный запасной путь Anthropic
  (`fallbacks="default"`), затем — запасной провайдер шлюза.
"""

from __future__ import annotations

import json
import time
from typing import Any

import anthropic

from agents.llm.base import LLMInvalidOutput, LLMRefusal, LLMResult, LLMUnavailable
from agents.llm.config import ProviderSettings

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicClient:
    provider = "anthropic"

    def __init__(
        self,
        settings: ProviderSettings,
        *,
        effort: str,
        timeout_s: float,
        max_retries: int,
        server_fallbacks: bool = True,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        self.model = settings.model
        self.effort = effort
        self.server_fallbacks = server_fallbacks
        self._client = client or anthropic.Anthropic(
            api_key=settings.api_key,
            base_url=settings.base_url,
            timeout=timeout_s,
            max_retries=max_retries,
        )

    def complete_json(
        self,
        *,
        task: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        max_tokens: int,
        effort: str | None = None,
    ) -> LLMResult:
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user}],
            "output_config": {
                "effort": effort or self.effort,
                "format": {"type": "json_schema", "schema": anthropic.transform_schema(schema)},
            },
        }
        started = time.monotonic()
        try:
            if self.server_fallbacks:
                resp = self._client.beta.messages.create(
                    **params, betas=[FALLBACK_BETA], fallbacks="default"
                )
            else:
                resp = self._client.messages.create(**params)
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise LLMUnavailable(f"доступ: {type(exc).__name__}") from exc
        except anthropic.RateLimitError as exc:
            raise LLMUnavailable("лимит запросов") from exc
        except anthropic.BadRequestError as exc:
            # Схема или параметры не приняты — это наша ошибка, но поиск должен работать дальше.
            raise LLMUnavailable(f"запрос отклонён: {exc.message}") from exc
        except anthropic.APIStatusError as exc:
            raise LLMUnavailable(f"ошибка провайдера {exc.status_code}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMUnavailable(f"сеть: {type(exc).__name__}") from exc

        if resp.stop_reason == "refusal":
            category = getattr(resp.stop_details, "category", None)
            raise LLMRefusal(f"отказ модели: {category or 'без категории'}")
        if resp.stop_reason == "max_tokens":
            raise LLMInvalidOutput("ответ обрезан по max_tokens")
        text = next((b.text for b in resp.content if b.type == "text"), None)
        if text is None:
            raise LLMInvalidOutput("в ответе нет текста")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMInvalidOutput("ответ — не JSON") from exc
        if not isinstance(data, dict):
            raise LLMInvalidOutput("ответ — не объект JSON")
        return LLMResult(
            data=data,
            provider=self.provider,
            model=getattr(resp, "model", None) or self.model,
            latency_ms=round((time.monotonic() - started) * 1000),
            usage=_usage(resp.usage),
        )


def _usage(usage: Any) -> dict[str, int]:
    keys = (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    )
    return {k: v for k in keys if isinstance(v := getattr(usage, k, None), int)}

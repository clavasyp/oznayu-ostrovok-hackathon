"""OpenAI-совместимый адаптер — запасные провайдеры YandexGPT и GigaChat (D21).

`response_format = json_schema` (strict). Особенности провайдеров (v4b): у YandexGPT схема
не совмещается с tools, у GigaChat свой формат схемы — TODO при подключении конкретного
провайдера. На показе основной путь — Claude.
"""

from __future__ import annotations

import json
import time
from typing import Any

import openai

from agents.llm.base import LLMInvalidOutput, LLMRefusal, LLMResult, LLMUnavailable
from agents.llm.config import ProviderSettings


class OpenAICompatClient:
    provider = "openai_compat"

    def __init__(
        self,
        settings: ProviderSettings,
        *,
        timeout_s: float,
        max_retries: int,
        client: openai.OpenAI | None = None,
    ) -> None:
        self.model = settings.model
        self._client = client or openai.OpenAI(
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
        effort: str | None = None,  # у OpenAI-совместимых провайдеров нет общего аналога
    ) -> LLMResult:
        started = time.monotonic()
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": task, "schema": schema, "strict": True},
                },
            )
        except (openai.AuthenticationError, openai.PermissionDeniedError) as exc:
            raise LLMUnavailable(f"доступ: {type(exc).__name__}") from exc
        except openai.RateLimitError as exc:
            raise LLMUnavailable("лимит запросов") from exc
        except openai.BadRequestError as exc:
            raise LLMUnavailable(f"запрос отклонён: {exc.message}") from exc
        except openai.APIStatusError as exc:
            raise LLMUnavailable(f"ошибка провайдера {exc.status_code}") from exc
        except openai.APIConnectionError as exc:
            raise LLMUnavailable(f"сеть: {type(exc).__name__}") from exc

        if not resp.choices:
            raise LLMInvalidOutput("пустой ответ")
        choice = resp.choices[0]
        if getattr(choice.message, "refusal", None):
            raise LLMRefusal("отказ модели")
        if choice.finish_reason == "length":
            raise LLMInvalidOutput("ответ обрезан по max_tokens")
        try:
            data = json.loads(choice.message.content or "")
        except json.JSONDecodeError as exc:
            raise LLMInvalidOutput("ответ — не JSON") from exc
        if not isinstance(data, dict):
            raise LLMInvalidOutput("ответ — не объект JSON")
        usage = resp.usage
        return LLMResult(
            data=data,
            provider=self.provider,
            model=resp.model or self.model,
            latency_ms=round((time.monotonic() - started) * 1000),
            usage={
                "input_tokens": usage.prompt_tokens,
                "output_tokens": usage.completion_tokens,
            }
            if usage
            else {},
        )

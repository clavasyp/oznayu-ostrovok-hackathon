"""Поддельный клиент для тестов: заготовленные ответы, сеть не трогается."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from typing import Any

from agents.llm.base import LLMInvalidOutput, LLMResult

Reply = dict[str, Any] | str | Exception
Responder = Callable[[dict[str, Any]], Reply]


class FakeLLM:
    provider = "fake"
    model = "fake"

    def __init__(self, replies: Iterable[Reply] | Responder) -> None:
        self._responder = replies if callable(replies) else None
        self._replies = [] if callable(replies) else list(replies)
        self.calls: list[dict[str, Any]] = []

    def complete_json(self, **call: Any) -> LLMResult:
        self.calls.append(call)
        reply = self._responder(call) if self._responder else self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, str):
            try:
                reply = json.loads(reply)
            except json.JSONDecodeError as exc:
                raise LLMInvalidOutput("ответ — не JSON") from exc
        return LLMResult(data=reply, provider=self.provider, model=self.model, latency_ms=0)

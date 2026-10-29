"""GigaChat (Сбер) — провайдер для запуска в России (D21): Claude недоступен с российских адресов.

API — OpenAI-подобный `POST /v1/chat/completions` на `https://api.giga.chat`. Отличия от OpenAI:
- вход: ключ авторизации (`LLM_API_KEY` — Basic `Client ID:Client Secret` в base64) меняется на
  токен доступа на 30 минут (`/api/v2/oauth`, scope `GIGACHAT_SCOPE`, по умолчанию физлицо);
  токен кешируется и обновляется сам;
- ответ строго по схеме — вызовом функции (`functions` + `function_call`): `response_format`
  в виде OpenAI GigaChat не принимает;
- TLS — российский корневой сертификат Минцифры (`certs/russian_trusted_root_ca.pem`, SHA-256
  D2:6D:2D:02:31:B7:C3:9F:92:CC:73:85:12:BA:54:10:35:19:E4:40:5D:68:B5:BD:70:3E:97:88:CA:8E:CF:31),
  только для запросов к GigaChat; системные сертификаты не меняются.

Стандартная библиотека: в образе приложения нет httpx и certifi.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from agents.llm.base import LLMInvalidOutput, LLMResult, LLMUnavailable
from agents.llm.config import ProviderSettings

BASE_URL = "https://api.giga.chat/v1"
OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
ROOT_CA = Path(__file__).with_name("certs") / "russian_trusted_root_ca.pem"
TOKEN_MARGIN_S = 60  # обновляем токен за минуту до конца срока


def _tls() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    try:
        import certifi  # на Mac у Python.org нет системных сертификатов

        ctx.load_verify_locations(certifi.where())
    except ImportError:
        pass
    ctx.load_verify_locations(str(ROOT_CA))
    return ctx


class GigaChatClient:
    provider = "gigachat"

    def __init__(self, settings: ProviderSettings, *, timeout_s: float, max_retries: int) -> None:
        self.model = settings.model
        self._auth_key = settings.api_key
        self._base = (settings.base_url or BASE_URL).rstrip("/")
        self._scope = os.environ.get("GIGACHAT_SCOPE") or "GIGACHAT_API_PERS"
        self._timeout = timeout_s
        self._retries = max_retries
        self._ctx = _tls()
        self._token: tuple[str, float] | None = None
        self._lock = threading.Lock()

    def _http(self, url: str, data: bytes, headers: dict[str, str]) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                req = urllib.request.Request(url, data=data, headers=headers)
                with urllib.request.urlopen(req, timeout=self._timeout, context=self._ctx) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    raise LLMUnavailable(f"доступ: HTTP {exc.code}") from exc
                if exc.code == 400:
                    body = exc.read()[:300].decode("utf-8", "replace")
                    raise LLMUnavailable(f"запрос отклонён: {body}") from exc
                last = exc  # 429 / 5xx — повтор
            except (OSError, ValueError) as exc:  # сеть, TLS, таймаут, битый JSON
                last = exc
            if attempt < self._retries:
                time.sleep(1 + attempt)
        raise LLMUnavailable(f"сеть: {type(last).__name__}") from last

    def _access_token(self) -> str:
        with self._lock:
            if self._token and self._token[1] - TOKEN_MARGIN_S > time.time():
                return self._token[0]
            data = self._http(
                OAUTH_URL,
                urllib.parse.urlencode({"scope": self._scope}).encode(),
                {
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                    "RqUID": str(uuid.uuid4()),
                    "Authorization": f"Basic {self._auth_key}",
                },
            )
            token = data.get("access_token")
            if not token:
                raise LLMUnavailable("GigaChat не выдал токен")
            expires = float(data.get("expires_at") or 0) / 1000 or time.time() + 1800
            self._token = (token, expires)
            return token

    def complete_json(
        self,
        *,
        task: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        max_tokens: int,
        effort: str | None = None,  # у GigaChat нет аналога
    ) -> LLMResult:
        started = time.monotonic()
        name = re.sub(r"[^A-Za-z0-9_]", "_", task) or "answer"
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "functions": [{"name": name, "description": task, "parameters": schema}],
            "function_call": {"name": name},
        }
        resp = self._http(
            f"{self._base}/chat/completions",
            json.dumps(body, ensure_ascii=False).encode(),
            {
                "Authorization": f"Bearer {self._access_token()}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        choices = resp.get("choices") or []
        if not choices:
            raise LLMInvalidOutput("пустой ответ")
        choice = choices[0]
        if choice.get("finish_reason") == "length":
            raise LLMInvalidOutput("ответ обрезан по max_tokens")
        call = (choice.get("message") or {}).get("function_call") or {}
        args = call.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError as exc:
                raise LLMInvalidOutput("ответ — не JSON") from exc
        if not isinstance(args, dict):
            raise LLMInvalidOutput("модель не вызвала функцию с ответом")
        usage = resp.get("usage") or {}
        return LLMResult(
            data=args,
            provider=self.provider,
            model=resp.get("model") or self.model,
            latency_ms=round((time.monotonic() - started) * 1000),
            usage={
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
            },
        )

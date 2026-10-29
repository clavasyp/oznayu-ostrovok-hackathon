"""GigaChat: токен по ключу авторизации кешируется, ответ — аргументы вызова функции по схеме."""

import pytest

from agents.llm.base import LLMInvalidOutput, LLMUnavailable
from agents.llm.config import LLMSettings, ProviderSettings
from agents.llm.gateway import build_client
from agents.llm.gigachat import GigaChatClient


def client(responses):
    c = GigaChatClient(ProviderSettings("gigachat", "GigaChat-3-Ultra", "a2V5"), timeout_s=5,
                       max_retries=0)  # fmt: skip
    calls = []

    def http(url, data, headers):
        calls.append((url, headers))
        return responses.pop(0)

    c._http = http
    return c, calls


def test_function_call_arguments_are_the_answer_and_token_is_cached():
    call = {"function_call": {"name": "parse_query", "arguments": {"city": "Сочи"}}}
    answer = {
        "choices": [{"finish_reason": "function_call", "message": call}],
        "model": "GigaChat-3-Ultra:1",
        "usage": {"prompt_tokens": 10, "completion_tokens": 3},
    }
    token = {"access_token": "t1", "expires_at": 9_999_999_999_000}
    c, calls = client([token, answer, dict(answer)])
    out = c.complete_json(task="parse query", system="s", user="u", schema={}, max_tokens=50)
    assert out.data == {"city": "Сочи"} and out.provider == "gigachat"
    assert out.usage == {"input_tokens": 10, "output_tokens": 3}
    c.complete_json(task="parse query", system="s", user="u", schema={}, max_tokens=50)
    assert [u.rsplit("/", 1)[-1] for u, _ in calls] == ["oauth", "completions", "completions"]
    assert calls[0][1]["Authorization"] == "Basic a2V5"
    assert calls[1][1]["Authorization"] == "Bearer t1"


def test_no_function_call_is_invalid_output():
    text = {"choices": [{"finish_reason": "stop", "message": {"content": "привет"}}]}
    c, _ = client([{"access_token": "t", "expires_at": 9_999_999_999_000}, text])
    with pytest.raises(LLMInvalidOutput):
        c.complete_json(task="t", system="s", user="u", schema={}, max_tokens=10)


def test_no_token_means_unavailable_and_gateway_builds_client():
    c, _ = client([{}])
    with pytest.raises(LLMUnavailable):
        c.complete_json(task="t", system="s", user="u", schema={}, max_tokens=10)
    s = LLMSettings.from_env({"LLM_PROVIDER": "gigachat", "LLM_API_KEY": "k"})
    assert s.primary.model == "GigaChat-3-Ultra"
    assert isinstance(build_client(s.primary, s), GigaChatClient)

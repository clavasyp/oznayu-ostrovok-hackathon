from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from agents.llm import (
    Gateway,
    LLMInvalidOutput,
    LLMRefusal,
    LLMSettings,
    LLMUnavailable,
    get_gateway,
)
from agents.llm.anthropic_adapter import FALLBACK_BETA, AnthropicClient
from agents.llm.config import ProviderSettings
from agents.llm.fake import FakeLLM


class Answer(BaseModel):
    city: str
    n: int


CALL = {"task": "t", "system": "s", "user": "u", "schema": {"type": "object"}, "model": Answer}


def test_settings_from_env_defaults_and_fallback():
    s = LLMSettings.from_env({"LLM_API_KEY": "k"})
    assert s.primary.provider == "anthropic" and s.primary.model == "claude-opus-5-5"
    assert s.primary.usable and s.fallback is None and s.effort == "low"
    s = LLMSettings.from_env(
        {
            "LLM_FALLBACK_PROVIDER": "openai_compat",
            "LLM_FALLBACK_MODEL": "yandexgpt",
            "LLM_FALLBACK_API_KEY": "y",
            "LLM_EFFORT": "medium",
            "LLM_ANTHROPIC_FALLBACKS": "off",
        }
    )
    assert not s.primary.usable  # ключа основного нет
    assert s.fallback and s.fallback.usable and s.effort == "medium"
    assert not s.anthropic_fallbacks
    with pytest.raises(ValueError):
        LLMSettings.from_env({"LLM_PROVIDER": "gpt"})


def test_no_key_means_unavailable_not_crash():
    gw = get_gateway(LLMSettings.from_env({}))
    assert not gw.available
    with pytest.raises(LLMUnavailable):
        gw.complete(**CALL)


def test_valid_answer_passes_model():
    gw = Gateway([FakeLLM([{"city": "sochi", "n": 2}])])
    done = gw.complete(**CALL)
    assert done.value == Answer(city="sochi", n=2) and done.attempts == 1


def test_invalid_schema_is_repaired_once_with_errors_in_prompt():
    fake = FakeLLM([{"city": "sochi"}, {"city": "sochi", "n": 3}])
    done = Gateway([fake]).complete(**CALL)
    assert done.value.n == 3 and done.attempts == 2
    assert "не прошёл проверку схемы" in fake.calls[1]["user"] and "n" in fake.calls[1]["user"]


def test_invalid_twice_raises_invalid_output():
    with pytest.raises(LLMInvalidOutput):
        Gateway([FakeLLM([{"x": 1}, "не json"])]).complete(**CALL)


def test_refusal_goes_to_fallback_provider():
    primary = FakeLLM([LLMRefusal("отказ")])
    backup = FakeLLM([{"city": "moscow", "n": 1}])
    done = Gateway([primary, backup]).complete(**CALL)
    assert done.value.city == "moscow" and len(primary.calls) == 1


def test_all_providers_fail_raise_last_error():
    gw = Gateway([FakeLLM([LLMRefusal("a")]), FakeLLM([LLMUnavailable("b")])])
    with pytest.raises(LLMUnavailable):
        gw.complete(**CALL)


class _Recorder:
    """Подмена anthropic.Anthropic: запоминает параметры и отдаёт заготовленный ответ."""

    def __init__(self, response):
        self.params = None
        self._response = response
        create = self._create
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=create))
        self.messages = SimpleNamespace(create=create)

    def _create(self, **params):
        self.params = params
        return self._response


def _message(stop_reason="end_turn", text='{"city": "sochi", "n": 1}', **extra):
    content = [
        SimpleNamespace(type="thinking", thinking=""),
        SimpleNamespace(type="text", text=text),
    ]
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=content,
        model="claude-opus-5-5",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0),
        **extra,
    )


def _anthropic(response, **kw):
    rec = _Recorder(response)
    p = ProviderSettings(provider="anthropic", model="claude-opus-5-5", api_key="k")
    client = AnthropicClient(p, effort="low", timeout_s=5, max_retries=0, client=rec, **kw)
    return client, rec


def test_anthropic_request_shape():
    client, rec = _anthropic(_message())
    res = client.complete_json(
        task="t", system="sys", user="u", schema={"type": "object"}, max_tokens=100
    )
    assert res.data == {"city": "sochi", "n": 1} and res.usage["input_tokens"] == 10
    p = rec.params
    assert "thinking" not in p and "temperature" not in p  # у Opus 5.5 — 400
    assert p["output_config"] == {
        "effort": "low",
        "format": {
            "type": "json_schema",
            "schema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    }
    assert p["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert p["betas"] == [FALLBACK_BETA] and p["fallbacks"] == "default"


def test_anthropic_without_server_fallbacks_uses_plain_endpoint():
    client, rec = _anthropic(_message(), server_fallbacks=False)
    client.complete_json(
        task="t", system="s", user="u", schema={"type": "object"}, max_tokens=10, effort="high"
    )
    assert "betas" not in rec.params and rec.params["output_config"]["effort"] == "high"


def test_anthropic_refusal_and_truncation():
    refusal = _message("refusal", stop_details=SimpleNamespace(category="cyber"))
    with pytest.raises(LLMRefusal):
        _anthropic(refusal)[0].complete_json(
            task="t", system="s", user="u", schema={"type": "object"}, max_tokens=10
        )
    with pytest.raises(LLMInvalidOutput):
        _anthropic(_message("max_tokens"))[0].complete_json(
            task="t", system="s", user="u", schema={"type": "object"}, max_tokens=10
        )
    with pytest.raises(LLMInvalidOutput):
        _anthropic(_message(text="[1, 2]"))[0].complete_json(
            task="t", system="s", user="u", schema={"type": "object"}, max_tokens=10
        )


def test_one_retry_by_default():
    assert LLMSettings.from_env({}).max_retries == 1
    assert LLMSettings.from_env({"LLM_MAX_RETRIES": "0"}).max_retries == 0

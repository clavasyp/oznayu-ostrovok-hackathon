import pytest


@pytest.fixture(autouse=True)
def _offline_llm(monkeypatch):
    """Тесты не ходят к провайдерам LLM, даже если ключ задан в окружении разработчика."""
    for name in ("LLM_API_KEY", "LLM_FALLBACK_PROVIDER", "LLM_FALLBACK_API_KEY"):
        monkeypatch.delenv(name, raising=False)

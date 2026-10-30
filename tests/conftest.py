import pytest

from travel_agent import observability
from travel_agent.config import get_settings


def _clear_caches():
    get_settings.cache_clear()
    observability.get_langsmith.cache_clear()


@pytest.fixture(autouse=True)
def instant_retries(monkeypatch):
    """Model-call retries wait for real in production; tests record the waits instead."""
    from travel_agent.agent import retry

    waits: list[float] = []

    async def record(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(retry, "_sleep", record)
    return waits


@pytest.fixture(autouse=True)
def no_tracing(monkeypatch):
    """Keep tests off the network even when .env enables tracing; env vars beat .env."""
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    _clear_caches()
    yield
    _clear_caches()

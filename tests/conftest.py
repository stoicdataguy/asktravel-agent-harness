import pytest

from travel_agent import observability
from travel_agent.config import get_settings


def _clear_caches():
    get_settings.cache_clear()
    observability.get_langfuse.cache_clear()


@pytest.fixture(autouse=True)
def no_tracing(monkeypatch):
    """Keep tests off the network even when .env enables tracing; env vars beat .env."""
    monkeypatch.setenv("LANGFUSE_TRACING", "false")
    _clear_caches()
    yield
    _clear_caches()

import pytest

from travel_agent import observability
from travel_agent.config import get_settings


@pytest.fixture(autouse=True)
def no_tracing(monkeypatch):
    """Keep tests off the network even when .env enables LangSmith; env vars beat .env."""
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    get_settings.cache_clear()
    observability.get_tracer.cache_clear()
    yield
    get_settings.cache_clear()
    observability.get_tracer.cache_clear()

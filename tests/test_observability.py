import pytest
from langchain_core.tracers.langchain import LangChainTracer

from travel_agent import observability

# Settings/tracer caches are reset around every test by the autouse fixture in conftest.py


def test_tracing_off_by_default():
    config = observability.run_config("ana@example.com", "ana@example.com:c1")
    assert "callbacks" not in config
    assert config["configurable"]["thread_id"] == "ana@example.com:c1"
    assert config["metadata"] == {"thread_id": "ana@example.com:c1", "user_id": "ana@example.com"}


def test_tracing_on_attaches_tracer(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2-test")
    monkeypatch.setenv("LANGSMITH_PROJECT", "test-project")
    [tracer] = observability.run_config("u", "u:c")["callbacks"]
    assert isinstance(tracer, LangChainTracer)
    assert tracer.project_name == "test-project"


def test_tracing_on_without_key_fails_loudly(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "")
    with pytest.raises(RuntimeError, match="LANGSMITH_API_KEY"):
        observability.run_config("u", "u:c")

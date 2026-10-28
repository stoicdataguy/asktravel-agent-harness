import pytest
from langchain_core.tracers.langchain import LangChainTracer
from langfuse.langchain import CallbackHandler as LangfuseHandler

from travel_agent import observability

# Settings/tracer caches are reset around every test by the autouse fixture in conftest.py


def test_tracing_off_by_default():
    config = observability.run_config("ana@example.com", "ana@example.com:c1")
    assert "callbacks" not in config
    assert config["configurable"]["thread_id"] == "ana@example.com:c1"
    assert config["metadata"]["langfuse_session_id"] == "ana@example.com:c1"
    assert config["metadata"]["langfuse_user_id"] == "ana@example.com"
    assert config["metadata"]["thread_id"] == "ana@example.com:c1"


def test_langfuse_attaches_handler(monkeypatch):
    monkeypatch.setenv("LANGFUSE_TRACING", "true")
    [handler] = observability.run_config("u", "u:c")["callbacks"]
    assert isinstance(handler, LangfuseHandler)


def test_langsmith_attaches_tracer(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2-test")
    monkeypatch.setenv("LANGSMITH_PROJECT", "test-project")
    [tracer] = observability.run_config("u", "u:c")["callbacks"]
    assert isinstance(tracer, LangChainTracer)
    assert tracer.project_name == "test-project"


def test_both_backends(monkeypatch):
    monkeypatch.setenv("LANGFUSE_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2-test")
    assert len(observability.run_config("u", "u:c")["callbacks"]) == 2


def test_langsmith_without_key_fails_loudly(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "")
    with pytest.raises(RuntimeError, match="LANGSMITH_API_KEY"):
        observability.run_config("u", "u:c")

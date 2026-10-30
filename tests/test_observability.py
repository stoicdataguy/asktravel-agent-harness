import logging
from types import SimpleNamespace

import pytest
from langchain_core.tracers import LangChainTracer

from travel_agent import observability

# Settings/client caches are reset around every test by the autouse fixture in conftest.py


def test_tracing_off_by_default():
    config = observability.run_config("ana@example.com", "ana@example.com:c1")
    assert "callbacks" not in config
    assert config["configurable"]["thread_id"] == "ana@example.com:c1"
    assert config["metadata"] == {"thread_id": "ana@example.com:c1", "user_id": "ana@example.com"}
    assert observability.get_langsmith() is None


def test_langsmith_attaches_tracer(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_test")
    monkeypatch.setenv("LANGSMITH_PROJECT", "travel-agent-test")
    [tracer] = observability.run_config("u", "u:c")["callbacks"]
    assert isinstance(tracer, LangChainTracer)
    assert tracer.project_name == "travel-agent-test"
    assert tracer.client is observability.get_langsmith()


def test_trace_false_skips_tracer(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_test")
    config = observability.run_config("u", "u:c", trace=False)
    assert "callbacks" not in config
    assert config["metadata"]["thread_id"] == "u:c"


def test_evals_untraced_unless_asked(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_test")
    assert "callbacks" not in observability.eval_run_config("u", "u:c")
    monkeypatch.setenv("LANGSMITH_TRACE_EVALS", "true")
    observability.get_settings.cache_clear()
    assert "callbacks" in observability.eval_run_config("u", "u:c")


@pytest.mark.parametrize(
    ("status", "warns"), [(422, False), (200, False), (401, True), (403, True)]
)
def test_key_check_probes_write_path(monkeypatch, caplog, status, warns):
    """Tracing keys can't read projects, so the check accepts any answer but 401/403."""
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_test")
    client = observability.get_langsmith()
    monkeypatch.setattr(
        client.session, "post", lambda *a, **kw: SimpleNamespace(status_code=status)
    )
    with caplog.at_level(logging.WARNING):
        observability.check_tracing_config()
    assert ("rejected LANGSMITH_API_KEY" in caplog.text) is warns


def test_unreachable_langsmith_warns_instead_of_failing(monkeypatch, caplog):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_test")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "http://127.0.0.1:9")  # nothing listens on port 9
    with caplog.at_level(logging.WARNING):
        observability.check_tracing_config()
    assert "unreachable or rejected LANGSMITH_API_KEY" in caplog.text

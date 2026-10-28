import logging

from langfuse.langchain import CallbackHandler as LangfuseHandler

from travel_agent import observability

# Settings/tracer caches are reset around every test by the autouse fixture in conftest.py


def test_tracing_off_by_default():
    config = observability.run_config("ana@example.com", "ana@example.com:c1")
    assert "callbacks" not in config
    assert config["configurable"]["thread_id"] == "ana@example.com:c1"
    assert config["metadata"] == {
        "langfuse_session_id": "ana@example.com:c1",
        "langfuse_user_id": "ana@example.com",
    }


def test_langfuse_attaches_handler(monkeypatch):
    monkeypatch.setenv("LANGFUSE_TRACING", "true")
    [handler] = observability.run_config("u", "u:c")["callbacks"]
    assert isinstance(handler, LangfuseHandler)


def test_unreachable_langfuse_warns_instead_of_failing(monkeypatch, caplog):
    monkeypatch.setenv("LANGFUSE_TRACING", "true")
    monkeypatch.setenv("LANGFUSE_HOST", "http://127.0.0.1:9")  # nothing listens on the discard port
    # The SDK keeps one client per public key per process; a fresh key avoids reusing one that an
    # earlier test pointed at a real host
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-unreachable-test")
    with caplog.at_level(logging.WARNING):
        observability.check_tracing_config()
    assert "unreachable or rejected the keys" in caplog.text

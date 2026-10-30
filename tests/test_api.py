import json

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from tests.fakes import FakeToolModel
from travel_agent.agent.graph import build_agent
from travel_agent.api.app import app
from travel_agent.config import get_settings
from travel_agent.tools import guidebooks

# No `with` block: skips the lifespan, so tests never touch Postgres
client = TestClient(app)


@pytest.fixture
def checkpointer():
    saver = InMemorySaver()
    app.state.agent = build_agent(
        saver,
        InMemoryStore(),
        model=FakeToolModel(messages=iter([AIMessage("Hello traveller"), AIMessage("Hi")])),
    )
    return saver


def chat(stream: bool = False, content: str = "hi", chat_id: str = "c1", history=()):
    """`history` is earlier (role, text) turns: OpenAI-style clients resend the whole chat."""
    messages = [{"role": r, "content": c} for r, c in history] + [
        {"role": "user", "content": content}
    ]
    return client.post(
        "/v1/chat/completions",
        json={"model": "travel-agent", "stream": stream, "messages": messages},
        headers={"X-OpenWebUI-User-Email": "Ana@Example.com", "X-OpenWebUI-Chat-Id": chat_id},
    )


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_models_lists_agent():
    data = client.get("/v1/models").json()["data"]
    assert data[0]["id"] == "travel-agent"
    assert data[0]["name"] == "AskTravel"


def test_chat_completion(checkpointer):
    body = chat().json()
    assert body["choices"][0]["message"]["content"] == "Hello traveller"


def test_chat_completion_stream(checkpointer):
    lines = [line for line in chat(stream=True).text.splitlines() if line.startswith("data: ")]
    assert lines[-1] == "data: [DONE]"
    deltas = [json.loads(line[6:])["choices"][0]["delta"] for line in lines[:-1]]
    assert "".join(d.get("content", "") for d in deltas) == "Hello traveller"


def test_thread_keyed_by_user_and_chat(checkpointer):
    chat()
    chat(content="and again", history=[("user", "hi"), ("assistant", "Hello traveller")])
    state = checkpointer.get({"configurable": {"thread_id": "ana@example.com:c1"}})
    # Two human turns + two AI replies accumulated in the checkpointed thread
    assert len(state["channel_values"]["messages"]) == 4


def test_stream_shows_tool_status(tmp_path, monkeypatch):
    (tmp_path / "hawaii-test.md").write_text("# hawaii-test\n# Kona\n[p. 1]\nManta rays.\n")
    monkeypatch.setenv("GUIDEBOOKS_DIR", str(tmp_path))
    guidebooks.load_books.cache_clear()
    call = {"name": "search_guidebooks", "args": {"query": "manta"}, "id": "call_1"}
    app.state.agent = build_agent(
        InMemorySaver(),
        InMemoryStore(),
        # Non-streaming: the fake can't stream a content-less tool-call message
        model=FakeToolModel(
            messages=iter([AIMessage("", tool_calls=[call]), AIMessage("Done")]),
            disable_streaming=True,
        ),
    )
    lines = [line for line in chat(stream=True).text.splitlines() if line.startswith("data: {")]
    deltas = [json.loads(line[6:])["choices"][0]["delta"] for line in lines]
    progress = "".join(d.get("reasoning_content", "") for d in deltas)
    # Progress goes to Open WebUI's reasoning block: the call, its outcome, then the next step
    assert progress.startswith("Starting…\n")
    assert '🔧 `search_guidebooks(query="manta")` → ✓ 1 passage · ' in progress
    assert progress.endswith("Reading the results…\n")
    # The answer stays clean
    assert "".join(d.get("content", "") for d in deltas) == "Done"
    guidebooks.load_books.cache_clear()


@pytest.mark.parametrize("style", ["inline", "off"])
def test_progress_style_inline_or_off(style, monkeypatch, checkpointer):
    monkeypatch.setenv("PROGRESS_STYLE", style)
    get_settings.cache_clear()
    try:
        lines = [line for line in chat(stream=True).text.splitlines() if line.startswith("data: {")]
        deltas = [json.loads(line[6:])["choices"][0]["delta"] for line in lines]
        assert not any("reasoning_content" in d for d in deltas)
        text = "".join(d.get("content", "") for d in deltas)
        if style == "inline":
            assert text.startswith("Starting…") and text.endswith("Hello traveller")
        else:
            assert text == "Hello traveller"
    finally:
        get_settings.cache_clear()

import json

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from travel_agent.agent.graph import build_agent
from travel_agent.api.app import app

# No `with` block: skips the lifespan, so tests never touch Postgres
client = TestClient(app)


@pytest.fixture
def checkpointer():
    saver = InMemorySaver()
    app.state.agent = build_agent(
        saver,
        InMemoryStore(),
        model=GenericFakeChatModel(messages=iter([AIMessage("Hello traveller"), AIMessage("Hi")])),
    )
    return saver


def chat(stream: bool = False, content: str = "hi", chat_id: str = "c1"):
    return client.post(
        "/v1/chat/completions",
        json={
            "model": "travel-agent",
            "stream": stream,
            "messages": [{"role": "user", "content": content}],
        },
        headers={"X-OpenWebUI-User-Email": "Ana@Example.com", "X-OpenWebUI-Chat-Id": chat_id},
    )


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_models_lists_agent():
    data = client.get("/v1/models").json()["data"]
    assert data[0]["id"] == "travel-agent"


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
    chat(content="and again")
    state = checkpointer.get({"configurable": {"thread_id": "ana@example.com:c1"}})
    # Two human turns + two AI replies accumulated in the checkpointed thread
    assert len(state["channel_values"]["messages"]) == 4

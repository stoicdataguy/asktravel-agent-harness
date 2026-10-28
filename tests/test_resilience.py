import json

import httpx
import openai
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from tests.fakes import FakeToolModel
from travel_agent.agent.graph import build_agent
from travel_agent.api.app import app
from travel_agent.api.openai_compat import RATE_LIMITED
from travel_agent.llm import get_chat_model


def rate_limit_error() -> openai.RateLimitError:
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    response = httpx.Response(429, request=request)
    return openai.RateLimitError("Rate limit exceeded", response=response, body=None)


class FailingModel(FakeToolModel):
    error: Exception

    def _generate(self, *args, **kwargs):
        raise self.error


@pytest.fixture
def client_with(request):
    def make(error: Exception) -> TestClient:
        model = FailingModel(messages=iter([]), error=error, disable_streaming=True)
        app.state.agent = build_agent(InMemorySaver(), InMemoryStore(), model=model)
        return TestClient(app)

    return make


def chat(client: TestClient, stream: bool):
    return client.post(
        "/v1/chat/completions",
        json={
            "model": "travel-agent",
            "stream": stream,
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers={"X-OpenWebUI-User-Email": "ana@example.com", "X-OpenWebUI-Chat-Id": "c1"},
    )


def test_retry_and_pacing_settings_reach_the_model(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_MAX_RETRIES", "7")
    monkeypatch.setenv("LLM_REQUESTS_PER_MINUTE", "18")
    model = get_chat_model()
    assert model.max_retries == 7
    assert model.rate_limiter.requests_per_second == pytest.approx(0.3)


def test_pacing_is_off_by_default(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.delenv("LLM_REQUESTS_PER_MINUTE", raising=False)
    monkeypatch.setenv("LLM_MAX_RETRIES", "10")
    model = get_chat_model()
    assert model.max_retries == 10 and model.rate_limiter is None


def test_rate_limit_mid_stream_ends_with_message_and_done(client_with):
    response = chat(client_with(rate_limit_error()), stream=True)
    assert response.status_code == 200
    events = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
    assert events[-1] == "[DONE]"
    text = "".join(json.loads(e)["choices"][0]["delta"].get("content", "") for e in events[:-1])
    assert text.strip() == RATE_LIMITED
    assert json.loads(events[-2])["choices"][0]["finish_reason"] == "stop"


def test_other_errors_get_a_generic_message(client_with):
    response = chat(client_with(ValueError("boom")), stream=True)
    assert "Something went wrong while answering (ValueError)" in response.text
    assert response.text.rstrip().endswith("data: [DONE]")


def test_non_streaming_returns_the_message(client_with):
    content = chat(client_with(rate_limit_error()), stream=False).json()
    assert content["choices"][0]["message"]["content"] == RATE_LIMITED

import json
import logging
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import openai
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from tests.fakes import FakeToolModel
from travel_agent.agent.graph import build_agent
from travel_agent.agent.retry import is_retryable, retry_after
from travel_agent.api.app import app

URL = "https://openrouter.ai/api/v1/chat/completions"


def error(status: int, headers: dict | None = None, body=None) -> openai.APIStatusError:
    response = httpx.Response(status, request=httpx.Request("POST", URL), headers=headers or {})
    kind = openai.RateLimitError if status == 429 else openai.APIStatusError
    return kind(f"error {status}", response=response, body=body)


class FlakyModel(FakeToolModel):
    """Raises the queued errors first, then answers from its scripted messages."""

    errors: list = []

    def _generate(self, *args, **kwargs):
        if self.errors:
            raise self.errors.pop(0)
        return super()._generate(*args, **kwargs)


def stream_with(model) -> tuple[str, str]:
    app.state.agent = build_agent(InMemorySaver(), InMemoryStore(), model=model)
    response = TestClient(app).post(
        "/v1/chat/completions",
        json={
            "model": "travel-agent",
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers={"X-OpenWebUI-User-Email": "ana@example.com", "X-OpenWebUI-Chat-Id": "c1"},
    )
    events = [json.loads(e[6:]) for e in response.text.splitlines() if e.startswith("data: {")]
    deltas = [e["choices"][0]["delta"] for e in events]
    progress = "".join(d.get("reasoning_content", "") for d in deltas)
    return progress, "".join(d.get("content", "") for d in deltas)


def test_rate_limits_are_retried_and_the_wait_is_shown(instant_retries):
    limited = {"retry-after": "3"}
    model = FlakyModel(
        messages=iter([AIMessage("Hello traveller")]),
        errors=[error(429, limited), error(429, limited)],
        disable_streaming=True,
    )
    progress, answer = stream_with(model)
    assert answer == "Hello traveller"
    assert instant_retries == [3.0, 3.0]  # the provider's Retry-After, not our backoff
    assert (
        "⏳ The model provider is busy (rate limit); retrying in 3 s (attempt 1 of 10)\n"
        in progress
    )
    assert (
        "⏳ The model provider is busy (rate limit); retrying in 3 s (attempt 2 of 10)\n"
        in progress
    )


def test_errors_that_wont_go_away_are_not_retried(instant_retries):
    model = FlakyModel(messages=iter([]), errors=[error(400)], disable_streaming=True)
    progress, answer = stream_with(model)
    assert instant_retries == []
    assert "⏳" not in progress
    assert answer.strip().startswith("⚠️ Something went wrong")


def test_which_errors_are_retried():
    assert is_retryable(error(429)) and is_retryable(error(503)) and is_retryable(error(408))
    assert not is_retryable(error(400)) and not is_retryable(error(401))
    assert is_retryable(openai.APITimeoutError(request=httpx.Request("POST", URL)))
    assert not is_retryable(ValueError("boom"))


def test_retry_after_header_forms():
    assert retry_after(error(429, {"retry-after-ms": "1500"})) == 1.5
    assert retry_after(error(429, {"retry-after": "20"})) == 20.0
    soon = format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)
    assert 25 < retry_after(error(429, {"retry-after": soon})) <= 30
    assert retry_after(error(429, {"retry-after": "600"})) is None  # too long: back off instead
    assert retry_after(error(429)) is None


def test_rate_limits_are_logged_with_who_limited_us(caplog):
    body = {"error": {"message": "Rate limit exceeded", "metadata": {"provider_name": "OpenAI"}}}
    model = FlakyModel(
        messages=iter([AIMessage("ok")]),
        errors=[error(429, {"x-ratelimit-remaining-requests": "0", "retry-after": "2"}, body)],
        disable_streaming=True,
    )
    with caplog.at_level(logging.WARNING, logger="travel_agent.agent.retry"):
        stream_with(model)
    [record] = [r for r in caplog.records if r.name == "travel_agent.agent.retry"]
    assert "429" in record.getMessage()
    assert "x-ratelimit-remaining-requests" in record.getMessage()
    assert "provider_name" in record.getMessage()


def test_the_agents_own_client_leaves_retries_to_the_middleware(monkeypatch):
    from travel_agent.agent import graph

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")

    captured = {}
    real = graph.create_agent

    def spy(model, **kwargs):
        captured["model"] = model
        return real(model, **kwargs)

    monkeypatch.setattr(graph, "create_agent", spy)
    build_agent(InMemorySaver(), InMemoryStore())
    assert captured["model"].max_retries == 0

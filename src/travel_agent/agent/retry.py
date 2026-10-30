"""Retry failed model calls where the user can see it, and log why they failed.

The OpenAI client can retry rate-limited calls itself, but silently: a 30-50 s wait looks like
a hung agent. This middleware owns the agent's retries instead (its model client is built with
`max_retries=0`). It retries what the client would (429, 408, 409, 5xx, timeouts, dropped
connections), honours the provider's Retry-After (capped like the client, at 60 s), otherwise
backs off exponentially, and before each wait:

- tells the user, as a `⏳` progress line (LangGraph's custom stream; a no-op outside one);
- logs the response once (status, body, rate-limit headers): the evidence #51 needs to tell
  OpenRouter's limits from the upstream provider's.

The last error is re-raised unchanged, so the API's readable error messages still apply.
"""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import openai
from langchain.agents.middleware import ModelRequest, ModelResponse, wrap_model_call
from langgraph.config import get_stream_writer

from travel_agent.config import get_settings

logger = logging.getLogger(__name__)

INITIAL_DELAY = 0.5
MAX_BACKOFF = 8.0
MAX_RETRY_AFTER = 60.0
_sleep = asyncio.sleep  # replaced in tests


def _status(exc: Exception) -> int | None:
    return getattr(exc, "status_code", None)


def is_retryable(exc: Exception) -> bool:
    if isinstance(exc, openai.APIConnectionError | openai.APITimeoutError):
        return True
    status = _status(exc)
    return status is not None and (status in (408, 409, 429) or status >= 500)


def retry_after(exc: Exception) -> float | None:
    """The wait the provider asked for, in seconds, if any (and if sensible)."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    try:
        if ms := headers.get("retry-after-ms"):
            wait = float(ms) / 1000
        elif value := headers.get("retry-after"):
            try:
                wait = float(value)
            except ValueError:
                wait = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
        else:
            return None
    except (TypeError, ValueError):
        return None
    return wait if 0 < wait <= MAX_RETRY_AFTER else None


def backoff(attempt: int) -> float:
    """Exponential backoff with jitter, like the OpenAI client: 0.5 s, 1 s, 2 s … up to 8 s."""
    return min(INITIAL_DELAY * 2**attempt, MAX_BACKOFF) * (1 - 0.25 * random.random())


def describe(exc: Exception) -> str:
    status = _status(exc)
    if status == 429:
        return "The model provider is busy (rate limit)"
    if status is not None:
        return f"The model provider returned an error ({status})"
    return "The connection to the model provider failed"


def log_failure(exc: Exception) -> None:
    """Everything that says who limited us and why (#51)."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    limits = {
        k: v for k, v in headers.items() if k.lower().startswith(("x-ratelimit", "retry-after"))
    }
    body = getattr(exc, "body", None)
    logger.warning(
        "model call failed: %s %s; headers %s; body %s",
        _status(exc) or type(exc).__name__,
        getattr(exc, "message", str(exc))[:200],
        limits,
        str(body)[:500] if body is not None else None,
    )


def tell_user(text: str) -> None:
    try:
        writer = get_stream_writer()
    except RuntimeError:  # not inside a graph run
        return
    try:
        writer({"progress": {"kind": "waiting", "text": text}})
    except Exception:  # progress must never break the retry
        logger.debug("waiting progress dropped", exc_info=True)


@wrap_model_call
async def retry_model_calls(
    request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
) -> ModelResponse:
    retries = get_settings().llm_max_retries
    attempt = 0
    while True:
        try:
            return await handler(request)
        except Exception as exc:
            if not is_retryable(exc) or attempt >= retries:
                raise
            log_failure(exc)
            wait = retry_after(exc) or backoff(attempt)
            attempt += 1
            tell_user(f"{describe(exc)}; retrying in {wait:.0f} s (attempt {attempt} of {retries})")
            await _sleep(wait)

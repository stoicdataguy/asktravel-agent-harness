from langchain_core.rate_limiters import InMemoryRateLimiter
from langchain_openai import ChatOpenAI

from travel_agent.config import get_settings


def get_chat_model() -> ChatOpenAI:
    """Chat model routed through OpenRouter (OpenAI-compatible API).

    Retries rate-limited and failed calls with backoff, and optionally paces calls so a
    rate-limited account stays under its limit (shared by every model call in this process).
    """
    settings = get_settings()
    if not settings.openrouter_api_key.get_secret_value():
        raise RuntimeError("OPENROUTER_API_KEY is not set: copy .env.example to .env and add it")
    rate_limiter = None
    if settings.llm_requests_per_minute:
        rate_limiter = InMemoryRateLimiter(
            requests_per_second=settings.llm_requests_per_minute / 60,
            check_every_n_seconds=0.1,
            max_bucket_size=1,
        )
    return ChatOpenAI(
        model=settings.openrouter_model,
        base_url=settings.openrouter_base_url,
        api_key=settings.openrouter_api_key,
        streaming=True,
        max_retries=settings.llm_max_retries,
        rate_limiter=rate_limiter,
    )

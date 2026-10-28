from langchain_openai import ChatOpenAI

from travel_agent.config import get_settings


def get_chat_model() -> ChatOpenAI:
    """Chat model routed through OpenRouter (OpenAI-compatible API)."""
    settings = get_settings()
    if not settings.openrouter_api_key.get_secret_value():
        raise RuntimeError("OPENROUTER_API_KEY is not set: copy .env.example to .env and add it")
    return ChatOpenAI(
        model=settings.openrouter_model,
        base_url=settings.openrouter_base_url,
        api_key=settings.openrouter_api_key,
        streaming=True,
    )

"""LangSmith tracing, configured from our settings rather than ambient environment variables."""

from functools import lru_cache

from langchain_core.runnables import RunnableConfig
from langchain_core.tracers.langchain import LangChainTracer, wait_for_all_tracers
from langsmith import Client

from travel_agent.config import get_settings


@lru_cache
def get_tracer() -> LangChainTracer | None:
    settings = get_settings()
    if not settings.langsmith_tracing:
        return None
    if not settings.langsmith_api_key.get_secret_value():
        raise RuntimeError("LANGSMITH_TRACING is on but LANGSMITH_API_KEY is not set in .env")
    client = Client(
        api_url=settings.langsmith_endpoint,
        api_key=settings.langsmith_api_key.get_secret_value(),
    )
    return LangChainTracer(project_name=settings.langsmith_project, client=client)


def run_config(user_id: str, thread_id: str) -> RunnableConfig:
    """Config for one agent run: checkpointer thread plus trace metadata.

    LangSmith groups traces sharing a `thread_id` metadata key into one conversation in its
    Threads view, and `user_id` makes traces filterable per (dummy) user.
    """
    config: RunnableConfig = {
        "configurable": {"thread_id": thread_id},
        "metadata": {"thread_id": thread_id, "user_id": user_id},
    }
    if tracer := get_tracer():
        config["callbacks"] = [tracer]
    return config


def flush_traces() -> None:
    """Block until queued traces are sent; call on shutdown so the last runs aren't lost."""
    if tracer := get_tracer():
        wait_for_all_tracers()
        # The client batches uploads on a background thread; drain it too
        tracer.client.flush()

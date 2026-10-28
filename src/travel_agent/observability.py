"""Tracing callbacks (Langfuse and/or LangSmith), configured from our settings rather than ambient
environment variables. Each backend is toggled independently in .env.
"""

from functools import lru_cache

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.runnables import RunnableConfig
from langchain_core.tracers.langchain import LangChainTracer, wait_for_all_tracers
from langfuse import Langfuse
from langfuse.langchain import CallbackHandler as LangfuseHandler
from langsmith import Client

from travel_agent.config import get_settings


@lru_cache
def get_langfuse() -> Langfuse | None:
    settings = get_settings()
    if not settings.langfuse_tracing:
        return None
    # Registers the client; LangfuseHandler instances find it by public key
    return Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key.get_secret_value(),
        base_url=settings.langfuse_host,
    )


@lru_cache
def get_langsmith_tracer() -> LangChainTracer | None:
    settings = get_settings()
    if not settings.langsmith_tracing:
        return None
    key = settings.langsmith_api_key.get_secret_value()
    # ".env.example" ships "lsv2_..." as a placeholder; treat it as unset
    if not key or key.endswith("..."):
        raise RuntimeError("LANGSMITH_TRACING is on but LANGSMITH_API_KEY is not set in .env")
    client = Client(
        api_url=settings.langsmith_endpoint,
        api_key=settings.langsmith_api_key.get_secret_value(),
    )
    return LangChainTracer(project_name=settings.langsmith_project, client=client)


def check_tracing_config() -> None:
    """Build the configured tracers once so misconfiguration fails at startup, not mid-request."""
    get_langfuse()
    get_langsmith_tracer()


def run_config(user_id: str, thread_id: str) -> RunnableConfig:
    """Config for one agent run: checkpointer thread plus tracing callbacks and metadata.

    `langfuse_session_id` / `langfuse_user_id` group traces into Sessions and Users in Langfuse;
    LangSmith groups by the plain `thread_id` key in its Threads view.
    """
    config: RunnableConfig = {
        "configurable": {"thread_id": thread_id},
        "metadata": {
            "thread_id": thread_id,
            "user_id": user_id,
            "langfuse_session_id": thread_id,
            "langfuse_user_id": user_id,
        },
    }
    callbacks: list[BaseCallbackHandler] = []
    if get_langfuse():
        callbacks.append(LangfuseHandler(public_key=get_settings().langfuse_public_key))
    if tracer := get_langsmith_tracer():
        callbacks.append(tracer)
    if callbacks:
        config["callbacks"] = callbacks
    return config


def flush_traces() -> None:
    """Block until queued traces are sent; call on shutdown so the last runs aren't lost."""
    if langfuse := get_langfuse():
        langfuse.flush()
    if tracer := get_langsmith_tracer():
        wait_for_all_tracers()
        # The client batches uploads on a background thread; drain it too
        tracer.client.flush()

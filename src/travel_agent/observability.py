"""LangSmith tracing, configured from our settings rather than ambient environment variables."""

import logging
from functools import lru_cache

from langchain_core.runnables import RunnableConfig
from langchain_core.tracers import LangChainTracer
from langsmith import Client

from travel_agent.config import get_settings

logger = logging.getLogger(__name__)


@lru_cache
def get_langsmith() -> Client | None:
    settings = get_settings()
    if not settings.langsmith_tracing:
        return None
    return Client(
        api_url=settings.langsmith_endpoint,
        api_key=settings.langsmith_api_key.get_secret_value() or None,
    )


def check_tracing_config() -> None:
    """At startup, warn (don't fail) if LangSmith is unreachable or rejects the key."""
    if not (client := get_langsmith()):
        return
    settings = get_settings()
    try:
        # Any authenticated read checks the key; listing one project is the cheapest
        next(iter(client.list_projects(limit=1)), None)
    except Exception as exc:  # network and auth errors surface as various exception types
        logger.warning(
            "LANGSMITH_TRACING is on but %s is unreachable or rejected LANGSMITH_API_KEY; traces "
            "will be dropped (%s)",
            settings.langsmith_endpoint,
            type(exc).__name__,
        )


def run_config(user_id: str, thread_id: str, *, trace: bool = True) -> RunnableConfig:
    """Config for one agent run: checkpointer thread plus tracing callback and metadata.

    `thread_id` metadata groups a chat's traces into one LangSmith thread; `user_id` is filterable.
    `trace=False` skips tracing for this run even when it's on (evals, to save the free quota).
    """
    config: RunnableConfig = {
        "configurable": {"thread_id": thread_id},
        "metadata": {"thread_id": thread_id, "user_id": user_id},
    }
    if trace and (client := get_langsmith()):
        config["callbacks"] = [
            LangChainTracer(project_name=get_settings().langsmith_project, client=client)
        ]
    return config


def eval_run_config(user_id: str, thread_id: str) -> RunnableConfig:
    """`run_config` for eval scripts: traced only with LANGSMITH_TRACE_EVALS=true."""
    return run_config(user_id, thread_id, trace=get_settings().langsmith_trace_evals)


def flush_traces() -> None:
    """Block until queued traces are sent; call on shutdown so the last runs aren't lost."""
    if client := get_langsmith():
        client.flush()

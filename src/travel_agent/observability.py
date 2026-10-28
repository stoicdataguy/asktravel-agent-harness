"""Langfuse tracing, configured from our settings rather than ambient environment variables."""

import logging
from functools import lru_cache

from langchain_core.runnables import RunnableConfig
from langfuse import Langfuse
from langfuse.langchain import CallbackHandler as LangfuseHandler

from travel_agent.config import get_settings

logger = logging.getLogger(__name__)


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


def check_tracing_config() -> None:
    """At startup, warn (don't fail) if Langfuse is unreachable or rejects the keys."""
    if not (langfuse := get_langfuse()):
        return
    try:
        ok = langfuse.auth_check()
    except Exception as exc:  # network errors surface as various exception types
        ok = False
        logger.debug("Langfuse auth check failed: %s", exc)
    if not ok:
        logger.warning(
            "LANGFUSE_TRACING is on but %s is unreachable or rejected the keys; traces will be "
            "dropped. Is the Langfuse stack running (docker compose up -d)?",
            get_settings().langfuse_host,
        )


def run_config(user_id: str, thread_id: str) -> RunnableConfig:
    """Config for one agent run: checkpointer thread plus tracing callback and metadata.

    `langfuse_session_id` / `langfuse_user_id` group traces in Langfuse's Sessions and Users views.
    """
    config: RunnableConfig = {
        "configurable": {"thread_id": thread_id},
        "metadata": {"langfuse_session_id": thread_id, "langfuse_user_id": user_id},
    }
    if get_langfuse():
        config["callbacks"] = [LangfuseHandler(public_key=get_settings().langfuse_public_key)]
    return config


def flush_traces() -> None:
    """Block until queued traces are sent; call on shutdown so the last runs aren't lost."""
    if langfuse := get_langfuse():
        langfuse.flush()

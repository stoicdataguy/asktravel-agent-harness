"""Long-term memory: per-user facts kept in the LangGraph store across conversations.

Facts are keyed by a short snake_case topic (`home_airport`, `diet`, ...) so updating a preference
overwrites it instead of piling up duplicates. All of a user's facts are injected into the system
prompt each turn (see `agent.graph`), so there is no separate recall tool.
"""

import re
from datetime import UTC, datetime

from langchain.tools import ToolRuntime, tool
from langgraph.store.base import BaseStore

from travel_agent.context import UserContext

MAX_MEMORIES_IN_PROMPT = 50
MAX_VALUE_CHARS = 300


def namespace(user_id: str) -> tuple[str, ...]:
    # Store namespace labels can't contain "."; user ids are emails, so percent-encode the dots
    return (user_id.replace(".", "%2E"), "memories")


def normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")


async def load_memories(store: BaseStore | None, user_id: str) -> dict[str, str]:
    """The user's saved facts as {key: value}, oldest first, capped for the prompt."""
    if store is None:
        return {}
    items = await store.asearch(namespace(user_id), limit=MAX_MEMORIES_IN_PROMPT)
    items.sort(key=lambda item: item.value.get("updated_at", ""))
    return {item.key: item.value["value"] for item in items}


def format_memories(memories: dict[str, str]) -> str:
    if not memories:
        return "(nothing saved yet)"
    return "\n".join(f"- {key}: {value}" for key, value in memories.items())


@tool
async def save_memory(key: str, value: str, runtime: ToolRuntime[UserContext]) -> str:
    """Remember a lasting fact or preference about the user for future conversations.

    Args:
        key: Short snake_case topic, e.g. home_airport, diet, budget_level, travel_companions,
            loyalty_programs, nationality, mobility, likes, dislikes. Reuse an existing key to
            update it.
        value: Concise fact in plain words, e.g. "SFO (San Francisco)" or "vegetarian, no seafood".
    """
    key = normalize_key(key)
    if not key or not value.strip():
        return "Not saved: key and value are required."
    await runtime.store.aput(
        namespace(runtime.context.user_id),
        key,
        {"value": value.strip()[:MAX_VALUE_CHARS], "updated_at": datetime.now(UTC).isoformat()},
    )
    return f"Saved {key}: {value.strip()[:MAX_VALUE_CHARS]}"


@tool
async def delete_memory(key: str, runtime: ToolRuntime[UserContext]) -> str:
    """Forget a saved fact about the user (use when they ask you to forget it or it's wrong).

    Args:
        key: The key of the saved fact, as listed in your instructions.
    """
    key = normalize_key(key)
    ns = namespace(runtime.context.user_id)
    if await runtime.store.aget(ns, key) is None:
        return f"Nothing saved under {key!r}."
    await runtime.store.adelete(ns, key)
    return f"Forgot {key}."


MEMORY_TOOLS = [save_memory, delete_memory]

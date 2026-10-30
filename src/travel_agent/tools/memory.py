"""Long-term memory: per-user facts kept in the LangGraph store across conversations.

Facts are keyed by a short snake_case topic (`home_airport`, `diet`, ...) so updating a preference
overwrites it instead of piling up duplicates. All of a user's facts are injected into the system
prompt each turn (see `agent.graph`), so there is no separate recall tool.

Most keys hold one value that a save overwrites. The keys in `LIST_KEYS` hold a list instead:
a save adds items to it, so one topic can't grow into a run-on string. Stored values look like
`{"value": str, "updated_at", "confirmed_at", "source"}`, plus `"items": [...]` for list keys
(`value` is then the items joined with "; ", so readers of `value` keep working). `source` is
the thread id of the chat that last saved the fact.
"""

import re
from datetime import UTC, datetime

from langchain.tools import ToolRuntime, tool
from langgraph.store.base import BaseStore

from travel_agent.context import UserContext
from travel_agent.memory import user_namespace

MAX_MEMORIES_IN_PROMPT = 50
MAX_VALUE_CHARS = 300
LIST_KEYS = frozenset({"likes", "dislikes", "interests"})
MAX_ITEM_CHARS = 120
MAX_LIST_ITEMS = 20
SEPARATOR = "; "


def namespace(user_id: str) -> tuple[str, ...]:
    return user_namespace(user_id, "memories")


def normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")


def split_items(text: str) -> list[str]:
    """Split a ';'-separated value into trimmed, case-insensitively unique items."""
    return dedupe(part.strip() for part in text.split(";"))


def dedupe(items) -> list[str]:
    seen: set[str] = set()
    unique = []
    for item in items:
        if item and item.casefold() not in seen:
            seen.add(item.casefold())
            unique.append(item)
    return unique


def stored_items(value: dict | None) -> list[str]:
    """Items of a list-key fact, also for facts saved as one string before lists existed."""
    if not value:
        return []
    if isinstance(value.get("items"), list):
        return list(value["items"])
    return split_items(value.get("value", ""))


def fact(value: str, runtime: ToolRuntime, previous: dict | None, changed: bool, **extra) -> dict:
    """A JSON-safe store value with provenance: the saving chat and when it was confirmed."""
    now = datetime.now(UTC).isoformat()
    updated_at = now if changed or not previous else previous.get("updated_at", now)
    return {
        **extra,
        "value": value,
        "updated_at": updated_at,
        "confirmed_at": now,
        "source": runtime.config.get("configurable", {}).get("thread_id"),
    }


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


async def add_items(key: str, value: str, runtime: ToolRuntime[UserContext]) -> str:
    ns = namespace(runtime.context.user_id)
    previous = await runtime.store.aget(ns, key)
    items = stored_items(previous.value if previous else None)
    known = {item.casefold() for item in items}
    incoming = [item[:MAX_ITEM_CHARS] for item in split_items(value)]
    new = [item for item in incoming if item.casefold() not in known]
    room = MAX_LIST_ITEMS - len(items)
    added, dropped = new[: max(room, 0)], new[max(room, 0) :]
    items += added
    await runtime.store.aput(
        ns,
        key,
        fact(SEPARATOR.join(items), runtime, previous and previous.value, bool(added), items=items),
    )
    parts = [f"Added to {key}: {SEPARATOR.join(added)}." if added else f"No change to {key}."]
    if repeats := [item for item in incoming if item.casefold() in known]:
        parts.append(f"Already saved: {SEPARATOR.join(repeats)}.")
    if dropped:
        parts.append(
            f"Not added (list full at {MAX_LIST_ITEMS} items; delete one first): "
            f"{SEPARATOR.join(dropped)}."
        )
    parts.append(f"{key} now: {SEPARATOR.join(items)}")
    return " ".join(parts)


@tool
async def save_memory(key: str, value: str, runtime: ToolRuntime[UserContext]) -> str:
    """Remember a lasting fact or preference about the user for future conversations.

    Most keys hold one value, and saving an existing key replaces it (the result shows what it
    replaced). The keys likes, dislikes and interests are lists: each save adds one item to the
    list without repeating or rewriting what's there, so pass only the new item.

    Args:
        key: Short snake_case topic, e.g. home_airport, diet, budget_level, travel_companions,
            loyalty_programs, nationality, mobility, likes, dislikes, interests. Reuse an
            existing key to update it.
        value: Concise fact in plain words, e.g. "SFO (San Francisco)" or "vegetarian, no seafood".
            For likes, dislikes and interests: just the new item, e.g. "astronomy".
    """
    key = normalize_key(key)
    value = value.strip()
    if not key or not value:
        return "Not saved: key and value are required."
    if key in LIST_KEYS:
        return await add_items(key, value, runtime)

    value = value[:MAX_VALUE_CHARS]
    ns = namespace(runtime.context.user_id)
    previous = await runtime.store.aget(ns, key)
    old = previous.value.get("value") if previous else None
    if old is not None and old.casefold() == value.casefold():
        await runtime.store.aput(ns, key, fact(old, runtime, previous.value, changed=False))
        return f"No change: {key} is already {old}"
    await runtime.store.aput(ns, key, fact(value, runtime, previous and previous.value, True))
    if old is None:
        return f"Saved {key}: {value}"
    return f"Updated {key} (was: {old}): {value}"


@tool
async def delete_memory(key: str, runtime: ToolRuntime[UserContext], item: str = "") -> str:
    """Forget a saved fact about the user (use when they ask you to forget it or it's wrong).

    Args:
        key: The key of the saved fact, as listed in your instructions.
        item: For likes, dislikes or interests: the one item to remove, e.g. "museums".
            Leave empty to forget the whole key.
    """
    key = normalize_key(key)
    ns = namespace(runtime.context.user_id)
    previous = await runtime.store.aget(ns, key)
    if previous is None:
        return f"Nothing saved under {key!r}."
    item = item.strip()
    if not item:
        await runtime.store.adelete(ns, key)
        return f"Forgot {key}."
    if key not in LIST_KEYS:
        return f"{key} isn't a list; call delete_memory with just the key to forget it."
    items = stored_items(previous.value)
    kept = [existing for existing in items if existing.casefold() != item.casefold()]
    if len(kept) == len(items):
        return f"{item!r} isn't in {key}. {key} holds: {SEPARATOR.join(items)}"
    if not kept:
        await runtime.store.adelete(ns, key)
        return f"Removed {item} from {key}; {key} is now empty, so it was forgotten."
    await runtime.store.aput(
        ns, key, fact(SEPARATOR.join(kept), runtime, previous.value, True, items=kept)
    )
    return f"Removed {item} from {key}. {key} now: {SEPARATOR.join(kept)}"


async def migrate_memories(store: BaseStore) -> int:
    """Convert list-key facts saved as one string into items. Idempotent; returns the count."""
    migrated = 0
    for ns in await store.alist_namespaces(suffix=("memories",), limit=10_000):
        for entry in await store.asearch(ns, limit=1000):
            if entry.key not in LIST_KEYS or isinstance(entry.value.get("items"), list):
                continue
            # Keep every item whole: trimming here would lose what the user said
            items = split_items(entry.value.get("value", ""))
            await store.aput(
                ns,
                entry.key,
                {
                    "source": None,
                    "confirmed_at": entry.value.get("updated_at"),
                    **entry.value,
                    "items": items,
                    "value": SEPARATOR.join(items),
                },
            )
            migrated += 1
    return migrated


MEMORY_TOOLS = [save_memory, delete_memory]

"""Everything AskTravel keeps about one user: view, export and delete it.

A user's data lives in two places: store namespaces that start with their (dot-encoded) email
(memories, trips, itineraries, chats, and whatever kinds are added later), and checkpointed
threads keyed `<email>:<chat id>`. Both are found generically here, so a new kind of memory is
covered without changes.
"""

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.store.base import BaseStore

from travel_agent.memory import user_namespace

MAX_ITEMS_PER_KIND = 1000


def _user_label(user_id: str) -> str:
    return user_namespace(user_id, "")[0]


async def store_items(store: BaseStore, user_id: str) -> dict[str, dict[str, dict]]:
    """{kind: {key: value}} for every store namespace of the user."""
    found: dict[str, dict[str, dict]] = {}
    for ns in await store.alist_namespaces(prefix=(_user_label(user_id),), limit=1000):
        if items := await store.asearch(ns, limit=MAX_ITEMS_PER_KIND):
            found[".".join(ns[1:])] = {item.key: item.value for item in items}
    return found


async def thread_ids(checkpointer: BaseCheckpointSaver, user_id: str) -> list[str]:
    """Ids of the user's checkpointed threads (one per chat)."""
    prefix = f"{user_id}:"
    ids = set()
    async for saved in checkpointer.alist(None):
        thread_id = saved.config["configurable"]["thread_id"]
        if thread_id.startswith(prefix):
            ids.add(thread_id)
    return sorted(ids)


async def thread_messages(checkpointer: BaseCheckpointSaver, thread_id: str) -> list[dict]:
    saved = await checkpointer.aget_tuple({"configurable": {"thread_id": thread_id}})
    messages = saved.checkpoint["channel_values"].get("messages", []) if saved else []
    return [{"type": m.type, "content": m.text, **_tool_calls(m)} for m in messages]


def _tool_calls(message) -> dict:
    calls = getattr(message, "tool_calls", None)
    return {"tool_calls": [{"name": c["name"], "args": c["args"]} for c in calls]} if calls else {}


async def remembered(store: BaseStore, checkpointer: BaseCheckpointSaver, user_id: str) -> dict:
    """A readable view: facts, trips, saved plans by title, and chats by their first message."""
    items = await store_items(store, user_id)
    plans = items.get("itineraries", {})
    chats = []
    for thread_id in await thread_ids(checkpointer, user_id):
        messages = await thread_messages(checkpointer, thread_id)
        first = next((m["content"] for m in messages if m["type"] == "human"), "")
        chats.append(
            {"thread_id": thread_id, "first_message": first[:120], "messages": len(messages)}
        )
    return {
        "user_id": user_id,
        "facts": items.get("memories", {}),
        "trips": list(items.get("trips", {}).values()),
        "itineraries": [
            {
                "thread_id": key,
                "title": value["itinerary"]["title"],
                "trip_id": value.get("trip_id"),
            }
            for key, value in plans.items()
        ],
        "chats": chats,
        "other": {k: v for k, v in items.items() if k not in ("memories", "trips", "itineraries")},
    }


async def export(store: BaseStore, checkpointer: BaseCheckpointSaver, user_id: str) -> dict:
    """Everything stored for the user: every store item and every chat's messages."""
    return {
        "user_id": user_id,
        "store": await store_items(store, user_id),
        "chats": {
            thread_id: await thread_messages(checkpointer, thread_id)
            for thread_id in await thread_ids(checkpointer, user_id)
        },
    }


async def delete_all(store: BaseStore, checkpointer: BaseCheckpointSaver, user_id: str) -> dict:
    """Delete every store item and every thread of the user. Returns what was deleted."""
    deleted_items = 0
    for ns in await store.alist_namespaces(prefix=(_user_label(user_id),), limit=1000):
        for item in await store.asearch(ns, limit=MAX_ITEMS_PER_KIND):
            await store.adelete(ns, item.key)
            deleted_items += 1
    threads = await thread_ids(checkpointer, user_id)
    for thread_id in threads:
        await checkpointer.adelete_thread(thread_id)
    return {"store_items": deleted_items, "chats": len(threads)}

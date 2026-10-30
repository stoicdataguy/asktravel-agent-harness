"""Episodic memory: search the user's earlier chats.

Facts and trips hold what the user told us; they can't answer "which gelato shops did you
recommend?". The checkpointed threads can, so `recall_conversations` keyword-searches the user's
other chats (user message + final answer of each turn) and returns the best-matching turns.
No summaries and no extra model calls: raw logs, searched on demand.

Chats are found through a small index in the store, `(email, "chats") / thread_id`, written after
every agent run by `index_chat` and backfilled at startup for chats from before it existed.
"""

import re
from datetime import UTC, datetime

from langchain.agents.middleware import after_agent
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.config import get_config
from langgraph.store.base import BaseStore

from travel_agent.agent.compaction import split_turns
from travel_agent.context import UserContext
from travel_agent.memory import user_namespace
from travel_agent.tools.guidebooks import normalize

MAX_CHATS_SEARCHED = 30
MAX_CHATS_RETURNED = 3
MAX_TURNS_PER_CHAT = 2
MAX_TEXT_CHARS = 600
_STOPWORDS = {
    "the", "and", "for", "you", "your", "our", "did", "what", "which", "were", "was", "that",
    "this", "with", "from", "about", "earlier", "chat", "last", "time", "recommend",
    "recommended", "remind", "me",
}  # fmt: skip


def chats_namespace(user_id: str) -> tuple[str, ...]:
    return user_namespace(user_id, "chats")


def _title(messages: list[BaseMessage]) -> str:
    first = next((m.text for m in messages if isinstance(m, HumanMessage)), "")
    return " ".join(first.split())[:80]


@after_agent
async def index_chat(state, runtime) -> None:
    """Record this chat in the user's chat index so later chats can search it."""
    thread_id = get_config().get("configurable", {}).get("thread_id")
    if not thread_id or runtime.store is None:
        return
    messages = state["messages"]
    ns = chats_namespace(runtime.context.user_id)
    previous = await runtime.store.aget(ns, thread_id)
    # Merge: keep fields other jobs keep here (e.g. what memory consolidation has processed)
    await runtime.store.aput(
        ns,
        thread_id,
        {
            **(previous.value if previous else {}),
            "title": _title(messages),
            "updated_at": datetime.now(UTC).isoformat(),
            "turns": sum(isinstance(m, HumanMessage) for m in messages),
        },
    )


async def backfill_chat_index(checkpointer: BaseCheckpointSaver, store: BaseStore) -> int:
    """Index chats saved before the index existed. Idempotent; returns how many were added."""
    latest: dict[str, tuple] = {}
    async for saved in checkpointer.alist(None):
        thread_id = saved.config["configurable"]["thread_id"]
        if ":" in thread_id and thread_id not in latest:
            latest[thread_id] = saved
    added = 0
    for thread_id, saved in latest.items():
        user_id = thread_id.split(":", 1)[0]
        if await store.aget(chats_namespace(user_id), thread_id):
            continue
        messages = saved.checkpoint["channel_values"].get("messages", [])
        if not messages:
            continue
        value = {"title": _title(messages), "updated_at": saved.checkpoint["ts"]}
        await store.aput(chats_namespace(user_id), thread_id, value)
        added += 1
    return added


def query_words(text: str) -> set[str]:
    words = re.split(r"[^a-z0-9]+", normalize(text))
    return {w for w in words if len(w) > 2 and w not in _STOPWORDS}


def _trim(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_TEXT_CHARS else text[: MAX_TEXT_CHARS - 1] + "…"


def matching_turns(messages: list[BaseMessage], words: set[str]) -> list[tuple[int, str, str]]:
    """(score, user text, answer) for each turn that mentions any of `words`, best first."""
    scored = []
    for turn in split_turns(messages):
        question = next((m.text for m in turn if isinstance(m, HumanMessage)), "")
        answer = next(
            (m.text for m in reversed(turn) if isinstance(m, AIMessage) and not m.tool_calls), ""
        )
        score = len(words & query_words(f"{question} {answer}"))
        if score:
            scored.append((score, question, answer))
    return sorted(scored, key=lambda t: -t[0])


def make_recall_tool(checkpointer: BaseCheckpointSaver):
    """`recall_conversations`, bound to the checkpointer that holds the chats' messages."""

    @tool
    async def recall_conversations(query: str, runtime: ToolRuntime[UserContext]) -> str:
        """Search the user's earlier chats with you, for what was said or recommended there.

        Use when the user refers to a previous conversation ("you recommended", "last time",
        "what did we decide"). Returns the best-matching exchanges from up to 3 earlier chats.

        Args:
            query: A few specific keywords, e.g. "gelato Rome" or "Porto Lisbon decision".
        """
        words = query_words(query)
        if not words:
            return "Give a few specific keywords to search earlier chats for."
        current = runtime.config.get("configurable", {}).get("thread_id")
        index = await runtime.store.asearch(chats_namespace(runtime.context.user_id), limit=200)
        index.sort(key=lambda item: item.value.get("updated_at", ""), reverse=True)
        found = []
        for item in [i for i in index if i.key != current][:MAX_CHATS_SEARCHED]:
            saved = await checkpointer.aget_tuple({"configurable": {"thread_id": item.key}})
            messages = saved.checkpoint["channel_values"].get("messages", []) if saved else []
            if turns := matching_turns(messages, words):
                found.append((turns[0][0], item, turns[:MAX_TURNS_PER_CHAT]))
        if not found:
            return f"No earlier chats mention {query!r}."
        found.sort(key=lambda f: -f[0])
        blocks = []
        for _, item, turns in found[:MAX_CHATS_RETURNED]:
            lines = [f'Chat "{item.value["title"]}" ({item.value["updated_at"][:10]}):']
            for _, question, answer in turns:
                lines += [f"  User: {_trim(question)}", f"  You: {_trim(answer)}"]
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    return recall_conversations

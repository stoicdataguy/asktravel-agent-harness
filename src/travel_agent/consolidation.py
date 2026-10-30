"""Memory consolidation (M5): review a user's saved facts once their chats go quiet.

The agent saves facts while it talks (`save_memory`), and rules on write keep them tidy (M1b).
What rules can't do is understand content: a dislike filed as an interest, a fact the user has
since contradicted. Once a user's chats have been idle for a while, one model call per user
reviews their facts against what they said in those chats and proposes Mem0-style operations
(add / update / delete / noop). Code then decides what is applied:

- **Evidence:** every change must quote the user's own words or an already-saved fact, and the
  quote must really be there. Only the user's messages are given to the model, never tool
  results, so text from guidebooks or web pages can't write memory.
- **Scope:** sensitive facts are never changed automatically; at most `MAX_OPS` changes a pass.
- **Audit:** every applied change goes to `(email, "memory_log")` with the old value, visible in
  `/api/memory` and deleted with the rest of the user's data.

Runs in the API's background loop (`run_consolidation_loop`), batched per user and capped per
cycle, because the model provider allows about 20 calls a minute.
"""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.store.base import BaseStore
from pydantic import BaseModel, Field

from travel_agent.memory import user_namespace
from travel_agent.tools.conversations import chats_namespace
from travel_agent.tools.memory import (
    LIST_KEYS,
    MAX_ITEM_CHARS,
    MAX_LIST_ITEMS,
    MAX_VALUE_CHARS,
    SEPARATOR,
    dedupe,
    namespace,
    normalize_key,
    stored_items,
)

logger = logging.getLogger(__name__)

MAX_OPS = 10
MAX_MESSAGE_CHARS = 8000  # newest user messages first, up to this much text per pass


class MemoryOp(BaseModel):
    """One proposed change to a saved fact."""

    action: Literal["add", "update", "delete", "noop"]
    key: str = Field(description="snake_case key, e.g. dislikes, diet, home_airport.")
    old: str | None = Field(
        default=None,
        description="update/delete: the saved value, or for likes/dislikes/interests the one "
        "item, exactly as currently saved.",
    )
    new: str | None = Field(
        default=None,
        description="add/update: the new value, or for likes/dislikes/interests one new item.",
    )
    evidence: str = Field(
        description="A short verbatim quote from the user's messages or the saved facts that "
        "justifies the change."
    )


class ConsolidationPlan(BaseModel):
    ops: list[MemoryOp] = Field(default_factory=list)


Propose = Callable[[dict[str, dict], list[str]], Awaitable[ConsolidationPlan]]

PROMPT = """\
You maintain the long-term memory of a travel assistant about one user: short facts saved under
snake_case keys. likes, dislikes and interests are lists of short items; every other key holds
one value. Trip-specific details (dates, hotels, bookings) do not belong here.

Review the saved facts against the user's recent messages and propose the fewest changes that
make the facts correct and tidy:
- file each item under the right key: split an item that mixes a like and a dislike, and move
  the dislike to dislikes;
- update a fact the user has changed; delete one the user retracted or that is clearly wrong;
- add a lasting preference the user stated that isn't saved yet.
Only use what the user said or what is already saved, and quote it as evidence. Do not invent,
infer or generalise. If nothing needs changing, return no ops.

Saved facts:
{facts}

The user's recent messages (newest last):
{messages}
"""


_QUOTES = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"})


def _norm(text: str) -> str:
    """Compare quotes loosely: whitespace, case and curly vs straight quotes don't matter."""
    return re.sub(r"\s+", " ", text.translate(_QUOTES)).strip().casefold()


def _quote(evidence: str) -> str:
    # Models sometimes wrap the quote itself in quotation marks
    return _norm(evidence).strip("\"' ")


def llm_proposer(model: BaseChatModel) -> Propose:
    structured = model.with_structured_output(ConsolidationPlan)

    async def propose(facts: dict[str, dict], messages: list[str]) -> ConsolidationPlan:
        lines = [
            f"- {k}: {SEPARATOR.join(stored_items(v)) if k in LIST_KEYS else v['value']}"
            for k, v in facts.items()
        ]
        prompt = PROMPT.format(
            facts="\n".join(lines) or "(none)", messages="\n".join(f"- {m}" for m in messages)
        )
        return await structured.ainvoke([SystemMessage(prompt), HumanMessage("Propose changes.")])

    return propose


async def apply_plan(
    store: BaseStore, user_id: str, plan: ConsolidationPlan, messages: list[str], source: str
) -> list[dict]:
    """Apply the ops that pass the guards; returns the log entries of what changed."""
    ns = namespace(user_id)
    facts = {i.key: i.value for i in await store.asearch(ns, limit=100)}
    evidence_pool = _norm(
        " ".join(messages)
        + " "
        + " ".join(
            SEPARATOR.join(stored_items(v)) if k in LIST_KEYS else v.get("value", "")
            for k, v in facts.items()
        )
    )
    now = datetime.now(UTC).isoformat()
    applied: list[dict] = []
    for op in plan.ops[:MAX_OPS]:
        key = normalize_key(op.key)
        if op.action == "noop" or not key:
            continue
        if not _quote(op.evidence) or _quote(op.evidence) not in evidence_pool:
            logger.info("consolidation: dropped %s %s (evidence not found)", op.action, key)
            continue
        current = facts.get(key)
        if current and current.get("sensitive"):
            logger.info("consolidation: skipped sensitive fact %s", key)
            continue
        before = (
            (SEPARATOR.join(stored_items(current)) if key in LIST_KEYS else current["value"])
            if current
            else None
        )
        if key in LIST_KEYS:
            items = stored_items(current)
            if op.action in ("update", "delete"):
                match = next(
                    (i for i in items if op.old and i.casefold() == op.old.casefold()), None
                )
                if match is None:
                    continue
                items = [i for i in items if i is not match]
            if op.action in ("add", "update") and op.new and op.new.strip():
                items = dedupe([*items, op.new.strip()[:MAX_ITEM_CHARS]])[:MAX_LIST_ITEMS]
            value = {"items": items, "value": SEPARATOR.join(items)} if items else None
        else:
            if op.action in ("update", "delete") and (
                not current or not op.old or _norm(op.old) != _norm(current["value"])
            ):
                continue
            new = (op.new or "").strip()[:MAX_VALUE_CHARS]
            if op.action in ("add", "update") and not new:
                continue
            value = None if op.action == "delete" else {"value": new}
        if value is None:
            if current:
                await store.adelete(ns, key)
        else:
            await store.aput(
                ns,
                key,
                {
                    **(current or {}),
                    **value,
                    "updated_at": now,
                    "confirmed_at": now,
                    "source": source,
                },
            )
        after = None if value is None else value["value"]
        if after == before:
            continue
        entry = {
            "at": now,
            "action": op.action,
            "key": key,
            "before": before,
            "after": after,
            "evidence": op.evidence,
            "source": source,
        }
        await store.aput(user_namespace(user_id, "memory_log"), f"{now}-{len(applied)}", entry)
        if value is None:
            facts.pop(key, None)
        else:
            facts[key] = {**(current or {}), **value}
        applied.append(entry)
    return applied


async def consolidate_user(
    store: BaseStore,
    checkpointer: BaseCheckpointSaver,
    propose: Propose,
    user_id: str,
    idle: timedelta,
    now: datetime | None = None,
) -> list[dict] | None:
    """Consolidate one user's facts against the new messages of their idle chats.

    Returns the applied changes, or None when there was nothing new to review.
    """
    now = now or datetime.now(UTC)
    pending = [
        item
        for item in await store.asearch(chats_namespace(user_id), limit=200)
        if item.value.get("turns", 0) > item.value.get("consolidated_turns", 0)
        and datetime.fromisoformat(item.value["updated_at"]) <= now - idle
    ]
    if not pending:
        return None
    messages: list[str] = []
    for item in sorted(pending, key=lambda i: i.value["updated_at"]):
        saved = await checkpointer.aget_tuple({"configurable": {"thread_id": item.key}})
        thread = saved.checkpoint["channel_values"].get("messages", []) if saved else []
        humans = [m.text for m in thread if isinstance(m, HumanMessage)]
        messages += humans[item.value.get("consolidated_turns", 0) :]
    # Newest messages win when there's more than one pass can take
    kept, size = [], 0
    for text in reversed(messages):
        size += len(text)
        if size > MAX_MESSAGE_CHARS:
            break
        kept.insert(0, text)
    facts = {i.key: i.value for i in await store.asearch(namespace(user_id), limit=100)}
    plan = await propose(facts, kept)
    applied = await apply_plan(store, user_id, plan, kept, "consolidation")
    for item in pending:
        await store.aput(
            chats_namespace(user_id),
            item.key,
            {
                **item.value,
                "consolidated_turns": item.value.get("turns", 0),
                "consolidated_at": now.isoformat(),
            },
        )
    if applied:
        logger.info("consolidation: %d change(s) for %s", len(applied), user_id)
    return applied


async def run_consolidation_loop(
    store: BaseStore,
    checkpointer: BaseCheckpointSaver,
    propose: Propose,
    idle_minutes: float,
    max_users_per_cycle: int,
    interval: float = 60,
) -> None:
    """Background loop: consolidate users whose chats have gone idle, a few per cycle.

    Errors are logged and retried next cycle, so a model or database hiccup never stops it.
    """
    while True:
        try:
            done = 0
            for ns in await store.alist_namespaces(suffix=("chats",), limit=10_000):
                if done >= max_users_per_cycle:
                    break
                user_id = ns[0].replace("%2E", ".")
                result = await consolidate_user(
                    store, checkpointer, propose, user_id, timedelta(minutes=idle_minutes)
                )
                done += result is not None
        except Exception:
            logger.exception("memory consolidation failed; retrying")
        await asyncio.sleep(interval)

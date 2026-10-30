"""Turn compaction: what the model sees of earlier turns, without touching the saved thread.

Tool results are most of a long chat (72-88% of measured threads: guidebook pages, searches),
and every model call resends them. Before each call this middleware sends earlier turns as the
user's message and the agent's final answer, with a one-line note of the tools that answer used;
the current turn (from the newest user message on) goes verbatim. Kept from earlier turns:

- `load_skill` results: small playbooks the prompt tells the agent not to reload.
- The latest `present_itinerary` call: the plan exists only in its arguments, and follow-ups
  ("swap day 3 and 4") need it.

Only the request changes, never the checkpointed thread, so `thread_sync` rewind and branching
keep working. A turn is compacted once, when the next one starts, so the prompt prefix stays
stable for caching within a turn and across later ones.
"""

from collections.abc import Awaitable, Callable

from langchain.agents.middleware import ModelRequest, ModelResponse, wrap_model_call
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

KEEP_ALWAYS = {"load_skill"}
KEEP_LATEST = {"present_itinerary"}
NOTE_PREFIX = "[context note: "
MAX_ARG_CHARS = 40


def _short(value) -> str:
    text = str(value)
    return text if len(text) <= MAX_ARG_CHARS else text[: MAX_ARG_CHARS - 1] + "…"


def describe_call(call: dict) -> str:
    args = ", ".join(f"{k}={_short(v)}" for k, v in call["args"].items() if k != "itinerary")
    return f"{call['name']}({args})"


def split_turns(messages: list[BaseMessage]) -> list[list[BaseMessage]]:
    """Split into turns, each starting at a human message (a leading non-human run is its own)."""
    turns: list[list[BaseMessage]] = []
    for message in messages:
        if isinstance(message, HumanMessage) or not turns:
            turns.append([])
        turns[-1].append(message)
    return turns


def compact_messages(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Earlier turns reduced to user message, kept tool pairs and final answer (+ tool note)."""
    turns = split_turns(messages)
    if len(turns) < 2:
        return list(messages)
    *earlier, current = turns

    # The newest present_itinerary call before the current turn
    latest = None
    for turn in earlier:
        for m in turn:
            for call in m.tool_calls if isinstance(m, AIMessage) else []:
                if call["name"] in KEEP_LATEST:
                    latest = call["id"]

    compacted: list[BaseMessage] = []
    for turn in earlier:
        kept_ids: set[str] = set()
        dropped: list[str] = []
        for m in turn:
            if isinstance(m, AIMessage):
                for call in m.tool_calls:
                    if call["name"] in KEEP_ALWAYS or call["id"] == latest:
                        kept_ids.add(call["id"])
                    else:
                        dropped.append(describe_call(call))
        final = next(
            (m for m in reversed(turn) if isinstance(m, AIMessage) and not m.tool_calls), None
        )
        for m in turn:
            if isinstance(m, HumanMessage):
                compacted.append(m)
            elif isinstance(m, AIMessage) and m.tool_calls:
                calls = [c for c in m.tool_calls if c["id"] in kept_ids]
                if calls:
                    # Keep only the kept calls; their text was thinking-aloud before the answer
                    compacted.append(AIMessage(content="", tool_calls=calls, id=m.id))
            elif isinstance(m, ToolMessage):
                if m.tool_call_id in kept_ids:
                    compacted.append(m)
            elif m is final:
                if dropped:
                    note = f"{NOTE_PREFIX}tools used for this answer, results removed: "
                    note += "; ".join(dropped) + "]"
                    m = AIMessage(content=f"{m.text}\n\n{note}", id=m.id)
                compacted.append(m)
    return [*compacted, *current]


@wrap_model_call
async def compact_earlier_turns(
    request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
) -> ModelResponse:
    return await handler(request.override(messages=compact_messages(list(request.messages))))

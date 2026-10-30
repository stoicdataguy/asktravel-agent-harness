"""Replay a saved chat call by call: what each model call's context window holds, with today's code.

For every model call in the thread (one per AI message), rebuilds the messages the model is sent
now (with turn compaction) and without compaction, splits them into the window's parts, and
simulates the provider's prompt cache: a request reuses the cache only when it extends an earlier
request exactly (measured on OpenRouter/OpenAI, see the prompt-caching note in
docs/memory-explainer.html). Prints JSON for the "Context window" view in docs/architecture.html.

Token counts: the fixed prompt parts are the provider's own counts (measured 2026-10-30); message
sizes are o200k tokenizer estimates. Only counts and tool names are printed, never message text.

Usage: uv run python scripts/context_window_trace.py [--thread <thread id>]  (default: largest)
"""

import argparse
import asyncio
import json
import sys

import tiktoken
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from travel_agent.agent.compaction import compact_messages
from travel_agent.config import get_settings
from travel_agent.memory import open_memory

# Provider-counted sizes of the fixed parts of the prompt (see docs/memory-explainer.html)
FIXED = {"rules_skills_tools": 1121 + 298 + 2894, "user_memory": 676}
ENC = tiktoken.get_encoding("o200k_base")


def tokens(message: BaseMessage) -> int:
    n = len(ENC.encode(message.text))
    if isinstance(message, AIMessage) and message.tool_calls:
        n += len(ENC.encode(json.dumps([c["args"] for c in message.tool_calls])))
    return n + 4  # per-message framing


def key(message: BaseMessage) -> str:
    """Identity of a message as sent, for the cache simulation."""
    calls = getattr(message, "tool_calls", None) or []
    return json.dumps([message.type, message.text, [c.get("id") for c in calls]])


def split_window(sent: list[BaseMessage]) -> dict:
    last_human = max(i for i, m in enumerate(sent) if isinstance(m, HumanMessage))
    earlier, current = sent[:last_human], sent[last_human:]
    return {
        "earlier_turns": sum(tokens(m) for m in earlier),
        "turn_messages": sum(tokens(m) for m in current if not isinstance(m, ToolMessage)),
        "turn_tool_results": sum(tokens(m) for m in current if isinstance(m, ToolMessage)),
    }


async def main(args) -> int:
    async with open_memory(get_settings().database_url) as (checkpointer, _):
        latest = {}
        async for saved in checkpointer.alist(None):
            latest.setdefault(saved.config["configurable"]["thread_id"], saved)
        threads = {t: s.checkpoint["channel_values"].get("messages", []) for t, s in latest.items()}
    thread = args.thread or max(threads, key=lambda t: sum(tokens(m) for m in threads[t]))
    messages = threads[thread]

    calls, cached_requests = [], []
    turn = step = 0
    fixed = sum(FIXED.values())
    for i, message in enumerate(messages):
        if isinstance(message, HumanMessage):
            turn, step = turn + 1, 0
            continue
        if not isinstance(message, AIMessage) or turn == 0:
            continue
        step += 1
        sent = compact_messages(messages[:i])
        request = [key(m) for m in sent]
        sizes = [tokens(m) for m in sent]
        # Longest earlier request this one extends exactly; the fixed prompt comes with it
        reuse = max((len(r) for r in cached_requests if request[: len(r)] == r), default=-1)
        read = fixed + sum(sizes[:reuse]) if reuse >= 0 else 0
        total = fixed + sum(sizes)
        cached_requests.append(request)
        calls.append(
            {
                "turn": turn,
                "step": step,
                **FIXED,
                **split_window(sent),
                "without_compaction": fixed + sum(tokens(m) for m in messages[:i]),
                "cache_read": read,
                "cache_written": total - read,
                "tools": [c["name"] for c in message.tool_calls],
            }
        )
    json.dump({"thread_turns": turn, "calls": calls}, sys.stdout)
    print()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--thread", help="thread id to replay (default: the largest)")
    sys.exit(asyncio.run(main(parser.parse_args())))

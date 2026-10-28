"""Check that the real agent triggers the right skills for typical requests.

Runs each prompt through the agent (real LLM via OpenRouter, in-memory checkpointer) and records
which skills it loads via `load_skill`. A case passes when every expected skill is loaded (extra
skills are reported but allowed); a case with no expected skills passes when none are loaded.
Runs stop after a few model turns, since skills are loaded up front; this keeps the eval cheap.

Usage: uv run python scripts/eval_skills.py [--only budget] [--turns 3]
"""

import argparse
import asyncio
import sys

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from travel_agent.agent.graph import UserContext, build_agent
from travel_agent.observability import flush_traces, run_config

CASES: list[tuple[str, set[str]]] = [
    ("I want to go somewhere warm next year, can you help me plan a trip?", {"trip-intake"}),
    (
        "Make me a 5-day itinerary for Rome and Florence in May, 2 adults, mid-range budget, "
        "we love food and art.",
        {"itinerary-builder"},
    ),
    (
        "Roughly how much would 10 days in Thailand cost for two backpackers, excluding flights?",
        {"budget-planner"},
    ),
    ("What should I pack for hiking in the Swiss Alps in October?", {"packing-list"}),
    ("I'm a US citizen. Do I need a visa for Peru, and any vaccinations?", {"visa-and-entry"}),
    (
        "What's the best way to get from Florence to Cinque Terre without a car?",
        {"local-transport"},
    ),
    ("What dishes should I try in Bangkok, and is tipping expected?", {"food-and-culture"}),
    (
        "Plan 4 days on the Big Island for 2 people on a $3000 budget, flights already booked.",
        {"itinerary-builder", "budget-planner"},
    ),
    ("Thanks, that's all for now!", set()),
]


async def run_case(agent, index: int, prompt: str, max_turns: int) -> set[str]:
    loaded: set[str] = set()
    turns = 0
    thread_id = f"skill-eval:{index}"
    stream = agent.astream(
        {"messages": [HumanMessage(prompt)]},
        run_config("skill-eval", thread_id),
        context=UserContext("skill-eval"),
        stream_mode="updates",
    )
    async for update in stream:
        if not isinstance(update.get("model"), dict):
            continue
        turns += 1
        for message in update["model"].get("messages", []):
            for call in getattr(message, "tool_calls", None) or []:
                if call["name"] == "load_skill":
                    loaded.add(call["args"].get("name", ""))
        if turns >= max_turns:
            break
    await stream.aclose()
    return loaded


async def main(args) -> int:
    agent = build_agent(InMemorySaver(), InMemoryStore())
    cases = [(i, p, e) for i, (p, e) in enumerate(CASES) if not args.only or args.only in p.lower()]
    semaphore = asyncio.Semaphore(args.concurrency)

    async def guarded(i, prompt, expected):
        async with semaphore:
            return await run_case(agent, i, prompt, args.turns)

    results = await asyncio.gather(*(guarded(i, p, e) for i, p, e in cases))
    failures = 0
    for (_, prompt, expected), loaded in zip(cases, results, strict=True):
        ok = expected <= loaded if expected else not loaded
        failures += not ok
        extra = sorted(loaded - expected) if expected else []
        note = f"  (extra: {', '.join(extra)})" if extra else ""
        status = "PASS" if ok else "FAIL"
        print(f"{status}  expected={sorted(expected) or '-'}  loaded={sorted(loaded) or '-'}{note}")
        print(f"      {prompt[:90]}")
    print(f"\n{len(cases) - failures}/{len(cases)} passed")
    flush_traces()
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", help="run only prompts containing this text")
    parser.add_argument("--turns", type=int, default=3, help="max model turns per case")
    parser.add_argument("--concurrency", type=int, default=4)
    sys.exit(asyncio.run(main(parser.parse_args())))

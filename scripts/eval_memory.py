"""Memory eval: how well the real agent remembers, forgets and budgets its context.

Runs the agent in-process (real LLM via OpenRouter, real Postgres checkpointer and store), each
case as its own throwaway user, and reports a pass rate per case plus input tokens per model
call. Cases follow docs/plans/memory-architecture.md §8; some are expected to fail until the
phase named in their description lands, which is the point: this is the baseline.

The model isn't deterministic, so each case runs --runs times. Trips are seeded with dates
relative to today, so the expected answers hold whenever the eval runs.

Usage: uv run python scripts/eval_memory.py [--quick] [--only time] [--runs 3] [--json out.json]
       [--keep]
"""

import argparse
import asyncio
import json
import re
import statistics
import sys
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from langchain_core.messages import AIMessage, HumanMessage

from travel_agent.agent.graph import build_agent
from travel_agent.config import get_settings
from travel_agent.context import UserContext
from travel_agent.memory import open_memory, user_namespace
from travel_agent.observability import flush_traces, run_config
from travel_agent.tools.trips import upsert_trip

KINDS = ("memories", "trips", "itineraries", "chats")
# The §6 target for input tokens per model call
CALL_BUDGET = 25_000
# Same pattern as e2e_memory.py: the agent asking for something it should already know
ASKS_WHERE = re.compile(
    r"where (are|will) you (be )?stay|are you staying|which (town|city|island|hotel)|"
    r"what (city|town|hotel|island)|don.t have your (city|hotel|lodging)|name of your hotel",
    re.I,
)


@dataclass
class Call:
    input_tokens: int
    cache_read: int


@dataclass
class Session:
    """One case run: a user, their chats, and every model call made on their behalf."""

    agent: object
    store: object
    checkpointer: object
    user: str
    calls: list[Call] = field(default_factory=list)
    chats: list[str] = field(default_factory=list)
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def new_chat(self) -> str:
        self.chats.append(f"eval-{uuid.uuid4().hex[:8]}")
        return self.chats[-1]

    async def say(self, chat_id: str, text: str) -> str:
        """Send one user message in a chat and return the agent's final reply."""
        thread_id = f"{self.user}:{chat_id}"
        result = await self.agent.ainvoke(
            {"messages": [HumanMessage(text)]},
            run_config(self.user, thread_id),
            context=UserContext(self.user),
        )
        messages = result["messages"]
        last_human = max(i for i, m in enumerate(messages) if isinstance(m, HumanMessage))
        for message in messages[last_human + 1 :]:
            if isinstance(message, AIMessage) and message.usage_metadata:
                usage = message.usage_metadata
                details = usage.get("input_token_details") or {}
                self.calls.append(Call(usage["input_tokens"], details.get("cache_read") or 0))
        return messages[-1].text

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append((name, bool(ok), detail))

    async def items(self, kind: str) -> list:
        return await self.store.asearch(user_namespace(self.user, kind), limit=100)

    async def remember(self, key: str, value: str) -> None:
        await self.store.aput(
            user_namespace(self.user, "memories"),
            key,
            {"value": value, "updated_at": datetime.now(UTC).isoformat()},
        )

    async def cleanup(self) -> None:
        for chat_id in self.chats:
            await self.checkpointer.adelete_thread(f"{self.user}:{chat_id}")
        for kind in KINDS:
            for item in await self.items(kind):
                await self.store.adelete(user_namespace(self.user, kind), item.key)


def short(text: str) -> str:
    return " ".join(text.split())[:160]


def first_int(text: str) -> int | None:
    match = re.search(r"-?\d+", text.replace(",", ""))
    return int(match.group()) if match else None


# --- Cases -------------------------------------------------------------------------------------
# Each takes a fresh Session and records checks; a run passes when every check passes.


async def cross_chat_recall(s: Session) -> None:
    """A stay mentioned in one chat is used in the next without asking (exists: e2e_memory A)."""
    start = date.today() + timedelta(days=40)
    await s.say(
        s.new_chat(),
        f"Heads up: I'm going to the Big Island of Hawaii from {start} to "
        f"{start + timedelta(days=5)}, staying at the Royal Kona Resort in Kailua-Kona. "
        "No need to plan anything yet.",
    )
    reply = await s.say(
        s.new_chat(),
        "For my Hawaii trip, name 3 sunset spots within 30 minutes of where I'm staying, "
        "and say which town I'm based in. Keep it short.",
    )
    asks = ASKS_WHERE.search(reply)
    s.check("knows the stay", not asks and "kona" in reply.lower(), short(reply))


async def time_relative(s: Session) -> None:
    """Questions about when trips are, against trips seeded at +25, +60 and +120 days.

    Mirrors the 2026-10-29 bug report, asked about "the next 2 months". (The seeded ids are the
    stale ones from that report; since M1 unknown ids get a fresh opaque id instead.)
    Expected to improve with M1 (relative dates computed for the prompt).
    """
    today = date.today()
    seeds = [
        ("international-destination-tbd-undated", "Rome, Italy", 25, 6),
        ("hawaii-the-big-island-undated", "Big Island, Hawaii", 60, 5),
        (None, "Lisbon, Portugal", 120, 4),
    ]
    for trip_id, destination, offset, length in seeds:
        start = today + timedelta(days=offset)
        await upsert_trip(
            s.store,
            s.user,
            trip_id=trip_id,
            destination=destination,
            start_date=start,
            end_date=start + timedelta(days=length),
        )
    chat = s.new_chat()
    reply = await s.say(
        chat, "How many of my trips start in the next 2 months? Answer with just the number."
    )
    s.check("count in next 2 months = 2", first_int(reply) == 2, short(reply))
    reply = await s.say(
        chat, "How many days from today until my Hawaii trip starts? Answer with just the number."
    )
    days = first_int(reply)
    s.check("days until Hawaii = 60 (±1)", days is not None and abs(days - 60) <= 1, short(reply))
    reply = await s.say(s.new_chat(), "Which is my next trip? Answer with just the destination.")
    s.check("next trip = Rome", "rome" in reply.lower(), short(reply))
    # The open-ended wording from the bug report, where the model got the window wrong
    reply = await s.say(
        s.new_chat(),
        "hey where all am i going in the next 2 months? list only the trips that start in that "
        "window, one destination per line, nothing else",
    )
    low = reply.lower()
    ok = "rome" in low and re.search(r"hawai|big island", low) and "lisbon" not in low
    s.check("open question lists Rome + Hawaii only", ok, short(reply))


async def trip_identity(s: Session) -> None:
    """One real trip described across chats stays one trip, with an id that isn't stale.

    Failed on the stale id until M1 made ids opaque (baseline 0/3).
    """
    await s.say(
        s.new_chat(),
        "I'm thinking about a trip to Europe next spring with my sister, nothing decided yet. "
        "Just remember it for now.",
    )
    await s.say(
        s.new_chat(),
        "Update on that Europe trip with my sister: it's Rome, April 10 to 16, 2027. "
        "Just remember it.",
    )
    await s.say(
        s.new_chat(),
        "For our Rome trip in April we'll stay at Hotel Artemide near Termini. Just save that.",
    )
    trips = [item.value for item in await s.items("trips")]
    summary = "; ".join(
        f"{t['trip_id']} ({t['destination']}, {t.get('start_date')})" for t in trips
    )
    s.check("one trip", len(trips) == 1, summary)
    if len(trips) == 1:
        trip = trips[0]
        s.check("destination is Rome", "rome" in trip["destination"].lower(), summary)
        s.check("id not stale", not re.search(r"undated|europe|tbd", trip["trip_id"]), summary)
        s.check("stay saved", "artemide" in json.dumps(trip).lower(), summary)


async def preference_evolution(s: Session) -> None:
    """A changed preference updates the saved fact instead of adding a second one (PersonaMem)."""
    await s.say(s.new_chat(), "Good to know for any trip: I'm vegetarian.")
    await s.say(
        s.new_chat(), "Small update about my diet: I eat fish now, but still no meat or poultry."
    )
    facts = {item.key: item.value["value"] for item in await s.items("memories")}
    diet = {k: v for k, v in facts.items() if re.search(r"vegetarian|fish|meat|diet", v, re.I)}
    detail = "; ".join(f"{k}: {v}" for k, v in diet.items()) or "(none)"
    s.check("one diet fact", len(diet) == 1, detail)
    s.check("mentions fish", any("fish" in v.lower() for v in diet.values()), detail)


async def conversation_recall(s: Session) -> None:
    """What the agent said in an older chat is recalled in a new one. Expected to fail until M3.

    Uses the agent's own recommendations: facts and trips hold what the *user* said, so only
    episodic memory can answer. (A user decision gets saved as a trip note and passes today.)
    """
    first = await s.say(
        s.new_chat(),
        "Recommend exactly 3 gelato shops in Rome. Names only, one per line, nothing else.",
    )
    names = [n.strip(" -*•0123456789.").lower() for n in first.splitlines() if n.strip()][:3]
    reply = await s.say(
        s.new_chat(),
        "In an earlier chat you recommended 3 gelato shops in Rome. Which were they? Names only.",
    )
    low = reply.lower()
    hits = [n for n in names if n and n in low]
    s.check("recalls 2+ of 3 names", len(hits) >= 2, f"earlier {names} | now {short(reply)!r}")


async def over_personalization(s: Session) -> None:
    """An unrelated question gets no unprompted mention of saved facts or trips."""
    await s.remember("diet", "vegetarian")
    await s.remember("home_airport", "SFO (San Francisco)")
    start = date.today() + timedelta(days=30)
    await upsert_trip(s.store, s.user, destination="Kyoto, Japan", start_date=start)
    reply = await s.say(s.new_chat(), "What's the capital of Peru?")
    leaked = re.findall(r"vegetarian|sfo|san francisco|kyoto|japan", reply, re.I)
    s.check("answers", "lima" in reply.lower(), short(reply))
    s.check("no unprompted memory", not leaked, f"mentioned: {leaked}" if leaked else "")


async def forgetting(s: Session) -> None:
    """Asking to forget a fact deletes it from the store."""
    await s.remember("diet", "vegetarian, no seafood")
    await s.remember("home_airport", "SFO (San Francisco)")
    await s.say(s.new_chat(), "Please forget my dietary preference.")
    facts = {item.key: item.value["value"] for item in await s.items("memories")}
    s.check("diet forgotten", not any("vegetarian" in v for v in facts.values()), str(facts))
    s.check("other facts kept", "home_airport" in facts, str(facts))


# Rome, because it's in the guidebooks: guidebook page reads are what make real threads large
LONG_CHAT = [
    "We're 2 adults planning 4 days in Rome in late May. One of us uses a wheelchair, and we "
    "don't eat pork. What neighbourhoods should we base ourselves in?",
    "What are the must-see sights that are wheelchair accessible?",
    "How do we get around? Are buses and the metro accessible?",
    "Where should we eat? Traditional Roman food but no pork.",
    "Is a day trip to Ostia Antica realistic for us?",
    "What about the Vatican Museums? How long do we need there?",
    "Any good viewpoints that don't involve steep hills or stairs?",
    "Plan the 4 days as an itinerary now.",
    "Swap day 3 and day 4.",
    "Recap the constraints I gave you in my very first message, as a short list.",
]


async def long_thread(s: Session) -> None:
    """A 10-turn planning chat: the per-call token budget, and early context still used.

    Expected to exceed the budget until M2 (turn compaction).
    """
    chat = s.new_chat()
    for text in LONG_CHAT[:-1]:
        await s.say(chat, text)
    reply = await s.say(chat, LONG_CHAT[-1])
    low = reply.lower()
    s.check("recalls constraints", "wheelchair" in low and "pork" in low, short(reply))
    peak = max((c.input_tokens for c in s.calls), default=0)
    s.check(f"max input ≤ {CALL_BUDGET:,}", peak <= CALL_BUDGET, f"max {peak:,} tokens")


CASES: dict[str, Callable[[Session], Awaitable[None]]] = {
    "cross_chat_recall": cross_chat_recall,
    "time_relative": time_relative,
    "trip_identity": trip_identity,
    "preference_evolution": preference_evolution,
    "conversation_recall": conversation_recall,
    "over_personalization": over_personalization,
    "forgetting": forgetting,
    "long_thread": long_thread,
}
# Expensive cases (about 10 turns, many model calls) run once unless --runs-long says otherwise
LONG_CASES = {"long_thread"}


# --- Runner ------------------------------------------------------------------------------------


def token_stats(calls: list[Call]) -> dict:
    if not calls:
        return {"calls": 0}
    inputs = sorted(c.input_tokens for c in calls)
    total = sum(inputs)
    return {
        "calls": len(calls),
        "mean_input": round(statistics.mean(inputs)),
        "p95_input": inputs[min(len(inputs) - 1, int(len(inputs) * 0.95))],
        "max_input": inputs[-1],
        "total_input": total,
        "cache_read_share": round(sum(c.cache_read for c in calls) / total, 3) if total else 0,
    }


async def run_case(agent, checkpointer, store, name: str, run: int, keep: bool) -> Session:
    user = f"eval-memory-{name.replace('_', '-')}-{run}@travel.local"
    s = Session(agent, store, checkpointer, user)
    await s.cleanup()  # leftovers from a --keep run
    try:
        await CASES[name](s)
    except Exception as exc:  # report and keep going: one broken case shouldn't sink the suite
        s.check("ran without error", False, f"{type(exc).__name__}: {exc}")
    finally:
        if not keep:
            await s.cleanup()
    return s


async def main(args) -> int:
    wanted = [w.strip() for w in (args.only or "").split(",") if w.strip()]
    names = [n for n in CASES if not wanted or any(w in n for w in wanted)]
    async with open_memory(get_settings().database_url) as (checkpointer, store):
        agent = build_agent(checkpointer, store)
        semaphore = asyncio.Semaphore(args.concurrency)

        async def guarded(name: str, run: int) -> tuple[str, Session]:
            async with semaphore:
                s = await run_case(agent, checkpointer, store, name, run, args.keep)
            status = "pass" if all(ok for _, ok, _ in s.checks) else "FAIL"
            print(f"  … {name} run {run + 1}: {status} ({len(s.calls)} calls)", flush=True)
            return name, s

        if args.quick:
            names = [n for n in names if n not in LONG_CASES]
            args.runs = 1
        jobs = [
            guarded(name, run)
            for name in names
            for run in range(args.runs_long if name in LONG_CASES else args.runs)
        ]
        results = await asyncio.gather(*jobs)
        print()

    report = {"date": date.today().isoformat(), "model": get_settings().openrouter_model}
    report["cases"] = {}
    all_calls: list[Call] = []
    for name in names:
        sessions = [s for n, s in results if n == name]
        passed = sum(all(ok for _, ok, _ in s.checks) for s in sessions)
        calls = [c for s in sessions for c in s.calls]
        all_calls += calls
        report["cases"][name] = {"passed": passed, "runs": len(sessions), **token_stats(calls)}
        print(f"{name:22} {passed}/{len(sessions)} passed")
        for s in sessions:
            for check, ok, detail in s.checks:
                if not ok or args.verbose:
                    print(f"    {'PASS' if ok else 'FAIL'}  {check}: {detail}")
    report["tokens"] = token_stats(all_calls)

    print("\nInput tokens per model call:")
    print(f"  {'case':22} {'calls':>5} {'mean':>7} {'p95':>7} {'max':>7} {'cached':>7}")
    for name, stats in report["cases"].items():
        if stats["calls"]:
            print(
                f"  {name:22} {stats['calls']:>5} {stats['mean_input']:>7,} "
                f"{stats['p95_input']:>7,} {stats['max_input']:>7,} "
                f"{stats['cache_read_share']:>7.0%}"
            )
    t = report["tokens"]
    if t["calls"]:
        print(
            f"  {'all':22} {t['calls']:>5} {t['mean_input']:>7,} {t['p95_input']:>7,} "
            f"{t['max_input']:>7,} {t['cache_read_share']:>7.0%}"
        )
    if args.json:
        with open(args.json, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\nWrote {args.json}")
    flush_traces()
    passed = sum(c["passed"] for c in report["cases"].values())
    runs = sum(c["runs"] for c in report["cases"].values())
    print(f"\n{passed}/{runs} case runs passed")
    return 0 if passed == runs else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--only", help="run only cases whose name contains this text (comma-separated for several)"
    )
    parser.add_argument("--runs", type=int, default=3, help="runs per case")
    parser.add_argument("--runs-long", type=int, default=1, help="runs of the long_thread case")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument(
        "--quick", action="store_true", help="one run per case, skip long_thread (a few minutes)"
    )
    parser.add_argument("--json", help="also write the report to this file")
    parser.add_argument("--verbose", action="store_true", help="show passing checks too")
    parser.add_argument("--keep", action="store_true", help="keep the eval users' data")
    sys.exit(asyncio.run(main(parser.parse_args())))

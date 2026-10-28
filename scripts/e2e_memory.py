"""End-to-end test: trip details shared in one chat are known in a new chat.

Talks to the running agent API the way Open WebUI does (same headers, full transcripts), with the
real LLM and Postgres, as a dedicated test user:

A. itinerary: plan a trip with dates and a hotel in chat 1; in a new chat 2, ask for sunset spots
   near where I'm staying. The reply must use the hotel's area instead of asking for it.
B. passing mention: mention a hotel for another trip in chat 3 (no itinerary); in a new chat 4,
   ask for day trips from that hotel.

Usage: uv run python scripts/e2e_memory.py [--keep]
"""

import argparse
import asyncio
import json
import re
import sys
import urllib.request
import uuid

from travel_agent.config import get_settings
from travel_agent.memory import open_memory, user_namespace

API = "http://localhost:8000/v1/chat/completions"
USER = "e2e-memory@travel.local"
# What the agent said in the bug report when it had lost the trip context
ASKS_WHERE = re.compile(
    r"where (are|will) you (be )?stay|are you staying|which (town|city|island|hotel)|"
    r"what (city|town|hotel|island)|"
    r"don.t have your (city|hotel|lodging)|name of your hotel",
    re.I,
)


def chat(chat_id: str, history: list[dict], text: str) -> str:
    messages = [*history, {"role": "user", "content": text}]
    body = json.dumps({"model": "travel-agent", "stream": False, "messages": messages}).encode()
    request = urllib.request.Request(
        API,
        data=body,
        headers={
            "content-type": "application/json",
            "X-OpenWebUI-User-Email": USER,
            "X-OpenWebUI-Chat-Id": chat_id,
        },
    )
    with urllib.request.urlopen(request, timeout=600) as response:
        reply = json.load(response)["choices"][0]["message"]["content"]
    history[:] = [*messages, {"role": "assistant", "content": reply}]
    return reply


def check(results: list, name: str, ok: bool, detail: str) -> None:
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")


def short(text: str) -> str:
    return " ".join(line for line in text.splitlines() if not line.startswith("`🔧"))[:200]


async def cleanup(chats: list[str]) -> None:
    async with open_memory(get_settings().database_url) as (checkpointer, store):
        for chat_id in chats:
            await checkpointer.adelete_thread(f"{USER}:{chat_id}")
        for kind in ("itineraries", "memories", "trips"):
            for item in await store.asearch(user_namespace(USER, kind), limit=100):
                await store.adelete(user_namespace(USER, kind), item.key)


async def main(args) -> int:
    await cleanup([])  # start from a clean user
    results: list[bool] = []
    chats = [f"e2e-{uuid.uuid4().hex[:8]}" for _ in range(4)]
    try:
        # A. Itinerary in one chat, location-dependent question in another
        chat(
            chats[0],
            [],
            "I'm going to the Big Island of Hawaii from Dec 28, 2026 to Jan 2, 2027 with my wife, "
            "staying at the Royal Kona Resort in Kailua-Kona. Plan 3 days, keep it very short.",
        )
        reply = chat(
            chats[1],
            [],
            "For my Hawaii trip, what are 3 good sunset spots within 30 minutes of where I'm "
            "staying? Keep it short.",
        )
        asks = ASKS_WHERE.search(reply)
        check(
            results,
            "A: new chat knows the stay",
            not asks and "kona" in reply.lower(),
            f"{'asked: ' + repr(asks.group(0)) if asks else 'ok'} | {short(reply)!r}",
        )

        # B. Trip detail mentioned in passing, no itinerary
        chat(
            chats[2],
            [],
            "Heads up for later: for my Peru trip in May 2027 I'll be staying at Casa Andina in "
            "Cusco. No need to plan anything yet.",
        )
        reply = chat(
            chats[3],
            [],
            "Which day trips from my hotel in Peru take under 2 hours each way? Just list 3.",
        )
        asks = ASKS_WHERE.search(reply)
        check(
            results,
            "B: new chat knows the hotel",
            not asks and "cusco" in reply.lower(),
            f"{'asked: ' + repr(asks.group(0)) if asks else 'ok'} | {short(reply)!r}",
        )
    finally:
        if not args.keep:
            await cleanup(chats)
    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", action="store_true", help="keep the test user's data")
    sys.exit(asyncio.run(main(parser.parse_args())))

"""End-to-end test: hotel search through the real stack.

Drives Open WebUI in a browser against the agent API, the configured LLM, Google Hotels and
Postgres:

1. Ask for hotels in Kailua-Kona for given dates and constraints. The agent must call
   search_hotels with the right dates, and the hotels and nightly prices in its reply must match a
   separate live Google Hotels lookup made by this script (so they're real, not invented).
2. Ask it to save the first one as where the user is staying. The trip must record that hotel.

Needs the full stack running (docker compose up -d, uv run travel-agent) and a Playwright
browser. Exits non-zero on failure.

Usage: uv run python scripts/e2e_hotels.py [--keep]
"""

import argparse
import asyncio
import json
import re
import sys
from datetime import date

from owui import agent_tool_calls, answer_text, chat_id, delete_chat, get_token, send
from playwright.async_api import async_playwright

from travel_agent.config import get_settings
from travel_agent.memory import open_memory, user_namespace
from travel_agent.tools.hotels import fetch_hotel_cards, parse_card
from travel_agent.tools.trips import load_trips

BASE_URL = "http://localhost:3000"
EMAIL, PASSWORD = "e2e-hotels@travel.local", "e2e-pass-123"
CHECK_IN, CHECK_OUT = date(2026, 12, 28), date(2027, 1, 2)
ASK = (
    "Find 3 hotels in Kailua-Kona, Hawaii for Dec 28, 2026 to Jan 2, 2027, under $400 a night "
    "and rated 4 or higher. Show the nightly price for each."
)
SAVE = "Save the first hotel you listed to my Hawaii trip as where I'm staying."
PRICE_TOLERANCE = 0.05  # Google's prices can move a little between two lookups


async def live_hotels() -> dict[str, int]:
    cards, _ = await fetch_hotel_cards("Kailua-Kona, Hawaii", CHECK_IN, CHECK_OUT)
    hotels = [parse_card(c["label"], c["name"], c["text"]) for c in cards]
    return {h.name: h.price_per_night for h in hotels if h and h.price_per_night}


def dollars(text: str) -> list[int]:
    return [int(m.replace(",", "")) for m in re.findall(r"\$(\d[\d,]*)", text)]


async def cleanup(chats: list[str], token: str) -> None:
    async with open_memory(get_settings().database_url) as (checkpointer, store):
        for chat in chats:
            await checkpointer.adelete_thread(f"{EMAIL}:{chat}")
        for kind in ("itineraries", "memories", "trips"):
            for item in await store.asearch(user_namespace(EMAIL, kind), limit=100):
                await store.adelete(user_namespace(EMAIL, kind), item.key)
    for chat in chats:
        delete_chat(BASE_URL, token, chat)


def check(results: list, name: str, ok: bool, detail: str) -> None:
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")


async def main(args) -> int:
    token = get_token(BASE_URL, "E2E Hotels", EMAIL, PASSWORD)
    results: list[bool] = []
    chats: list[str] = []
    await cleanup([], token)
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            await page.add_init_script(f"localStorage.setItem('token', {json.dumps(token)})")
            await page.goto(BASE_URL)
            await page.locator("#chat-input").wait_for()

            # 1. Search
            await send(page, ASK)
            chats.append(chat_id(page))
            reply = await page.locator(".chat-assistant").last.inner_text()
            calls = await agent_tool_calls(EMAIL, chats[-1], "search_hotels")
            check(
                results,
                "calls search_hotels with the trip dates",
                any(
                    str(c["args"].get("check_in")) == "2026-12-28"
                    and str(c["args"].get("check_out")) == "2027-01-02"
                    for c in calls
                ),
                str([c["args"] for c in calls])[:150] or "no search_hotels call",
            )
            answer = answer_text(reply)
            live = await live_hotels()
            quoted = dollars(answer)
            named = {
                name: price
                for name, price in live.items()
                if name.lower() in answer.lower()
                and any(abs(q - price) <= price * PRICE_TOLERANCE for q in quoted)
            }
            check(
                results,
                "hotels and nightly prices match live Google Hotels",
                len(named) >= 2,
                f"matched {named} | reply prices {quoted[:8]}",
            )

            # 2. Save the first one to the trip
            await send(page, SAVE)
            async with open_memory(get_settings().database_url) as (_, store):
                trips = await load_trips(store, EMAIL)
            lodging = [stay for trip in trips for stay in trip.lodging]
            check(
                results,
                "chosen hotel saved as the trip's lodging",
                any(name.lower() in " ".join(lodging).lower() for name in live),
                f"trips={[t.trip_id for t in trips]} lodging={lodging}",
            )
            await browser.close()
    finally:
        if not args.keep:
            await cleanup(chats, token)
    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", action="store_true", help="keep the chats and stored data")
    sys.exit(asyncio.run(main(parser.parse_args())))

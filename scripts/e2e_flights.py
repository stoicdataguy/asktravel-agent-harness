"""End-to-end test: flight search through the real stack.

Drives Open WebUI in a browser against the agent API, the configured LLM, Google Flights and
Postgres:

1. Ask for the cheapest nonstop SFO → Kona flights. The agent must call search_flights with the
   right airports, and the prices in its reply must match a live Google Flights lookup made by
   this script (so the fares are real, not invented).
2. Ask it to save the cheapest one to the trip. The trip in the store must record the flight.

Needs the full stack running (docker compose up -d, uv run travel-agent) and a Playwright
browser. Exits non-zero on failure.

Usage: uv run python scripts/e2e_flights.py [--keep]
"""

import argparse
import asyncio
import json
import re
import sys

from fast_flights import FlightQuery, Passengers, create_query, get_flights
from owui import chat_id, delete_chat, get_token, send
from playwright.async_api import async_playwright

from travel_agent.config import get_settings
from travel_agent.memory import open_memory, user_namespace
from travel_agent.tools.trips import load_trips

BASE_URL = "http://localhost:3000"
EMAIL, PASSWORD = "e2e-flights@travel.local", "e2e-pass-123"
DATE = "2026-12-28"
ASK = (
    "Find nonstop flights from San Francisco to Kona on Dec 28, 2026 for 2 adults. "
    "List the 3 cheapest with their prices."
)
SAVE = "Great, save the cheapest one to my Hawaii trip."


def live_prices() -> tuple[set[int], int, str]:
    """Totals and per-person prices from Google Flights right now, the cheapest total, airline."""
    query = create_query(
        flights=[FlightQuery(date=DATE, from_airport="SFO", to_airport="KOA", max_stops=0)],
        passengers=Passengers(adults=2),
        currency="USD",
        language="en-US",
        max_stops=0,
    )
    results = [r for r in get_flights(query) if r.price]
    prices = set()
    for r in results:
        prices |= {r.price, r.price // 2, round(r.price / 2), -(-r.price // 2)}
    cheapest = min(results, key=lambda r: r.price)
    return prices, cheapest.price, ", ".join(cheapest.airlines)


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
    token = get_token(BASE_URL, "E2E Flights", EMAIL, PASSWORD)
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
            call = re.search(r"🔧 search_flights\((.*?)\)", reply)
            check(
                results,
                "calls search_flights SFO → KOA",
                bool(call) and '"SFO"' in call.group(1) and '"KOA"' in call.group(1),
                call.group(0)[:120] if call else "no search_flights call",
            )
            answer = "\n".join(line for line in reply.splitlines() if "🔧" not in line)
            quoted = dollars(answer)
            live, cheapest, airline = live_prices()
            matched = [p for p in quoted if p in live]
            check(
                results,
                "quoted fares match live Google Flights",
                len(matched) >= 2 and (cheapest in quoted or cheapest // 2 in quoted),
                f"reply ${quoted[:8]} | live cheapest ${cheapest} ({airline})"
                f" | matched {matched[:6]}",
            )

            # 2. Save the cheapest to the trip
            await send(page, SAVE)
            async with open_memory(get_settings().database_url) as (_, store):
                trips = await load_trips(store, EMAIL)
            transport = [t for trip in trips for t in trip.transport]
            saved = " ".join(transport)
            check(
                results,
                "cheapest flight saved to the trip",
                bool(transport) and "SFO" in saved and "KOA" in saved,
                f"trips={[t.trip_id for t in trips]} transport={transport}",
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

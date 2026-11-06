"""End-to-end test: Indian train search through the real stack.

Drives Open WebUI in a browser against the agent API, the configured LLM, ConfirmTkt and Postgres:
1. Ask for trains from New Delhi to Agra on a date. The agent must call search_trains with the
   right stations and date, and the trains and fares in its reply must match a separate live
   lookup made by this script (so they're real, not invented).
2. Ask it to save one to the trip. The trip must record that train.

Needs the full stack running (docker compose up -d, uv run travel-agent) and a Playwright
browser. Exits non-zero on failure.

Usage: uv run python scripts/e2e_trains.py [--keep]
"""

import argparse
import asyncio
import json
import re
import sys
from datetime import date, timedelta

from owui import agent_tool_calls, answer_text, chat_id, delete_chat, get_token, send
from playwright.async_api import async_playwright

from travel_agent.config import get_settings
from travel_agent.memory import open_memory, user_namespace
from travel_agent.tools.trains import fetch_trains, to_option
from travel_agent.tools.trips import load_trips

BASE_URL = "http://localhost:3000"
EMAIL, PASSWORD = "e2e-trains@travel.local", "e2e-pass-123"
TRAVEL_DATE = date.today() + timedelta(days=30)
ASK = (
    f"What trains go from New Delhi to Agra on {TRAVEL_DATE:%B %-d, %Y}? List 3 with the train "
    "number, times, and the fare and availability for each class."
)
SAVE = "Save the first train you listed to my Agra trip."


def rupees(text: str) -> set[int]:
    return {int(m.replace(",", "")) for m in re.findall(r"(?:₹|Rs\.?\s?|INR\s?)(\d[\d,]*)", text)}


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
    token = get_token(BASE_URL, "E2E Trains", EMAIL, PASSWORD)
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
            calls = await agent_tool_calls(EMAIL, chats[-1], "search_trains")
            check(
                results,
                "calls search_trains with the stations and date",
                any(
                    str(c["args"].get("travel_date")) == TRAVEL_DATE.isoformat()
                    and str(c["args"].get("destination", "")).upper() in ("AGC", "AF", "IDH")
                    for c in calls
                ),
                str([c["args"] for c in calls])[:150] or "no search_trains call",
            )
            answer = answer_text(reply)
            first = calls[0]["args"] if calls else {"origin": "NDLS", "destination": "AGC"}
            data = await fetch_trains(first["origin"], first["destination"], TRAVEL_DATE)
            live = {t["trainNumber"]: to_option(t) for t in data.get("trainList") or []}
            quoted = rupees(answer)
            matched = {
                number: [c.fare for c in option.classes if c.fare in quoted]
                for number, option in live.items()
                if number in answer
            }
            matched = {number: fares for number, fares in matched.items() if fares}
            check(
                results,
                "train numbers and fares match a live lookup",
                len(matched) >= 2,
                f"matched {matched} | reply fares {sorted(quoted)[:10]}",
            )

            # 2. Save the first one to the trip
            await send(page, SAVE)
            async with open_memory(get_settings().database_url) as (_, store):
                saved = await load_trips(store, EMAIL)
            transport = " ".join(item for trip in saved for item in trip.transport)
            check(
                results,
                "chosen train saved to the trip",
                any(number in transport for number in live),
                f"trips={[t.trip_id for t in saved]} transport={transport[:120]}",
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

"""End-to-end test: the agent's progress streams into Open WebUI's reasoning block, not the answer.

Drives the real stack in a browser (Open WebUI, the agent API on :8000, Postgres, the model) as
a test user with "Always Expand Details" on. Sends a question that needs guidebook research and
checks, for a guidebook question and a hotel search:

1. progress is visible soon after sending (the "Starting…" line);
2. the stored answer has no progress lines in it;
3. every tool call in the agent's thread is in the progress block, with a ✓/✗ outcome;
4. the progress finished before the answer started (it ends with "Reading the results…");
5. for the hotel search, the tool's own progress lines (⋯) appear while it runs.

Needs the full stack (docker compose up -d, uv run travel-agent) and a Playwright browser.

Usage: uv run python scripts/e2e_progress.py [--keep]
"""

import argparse
import asyncio
import json
import re
import sys
import time
import urllib.request

from langchain_core.messages import AIMessage
from owui import chat_id, delete_chat, expand_details, get_token
from playwright.async_api import async_playwright

from travel_agent import user_data
from travel_agent.config import get_settings
from travel_agent.memory import open_memory

BASE_URL = "http://localhost:3000"
EMAIL, PASSWORD = "e2e-progress@travel.local", "e2e-pass-123"
# (name, question, a tool whose in-tool progress lines must show up)
CASES = [
    (
        "guidebook research",
        "Using the Italy guidebook, name two sights within walking distance of the Colosseum. "
        "Two short lines, with page citations.",
        None,
    ),
    (
        "hotel search",
        "Find hotels in Prati, Rome from 2026-12-09 to 2026-12-12 under $250 a night. "
        "Just three names with their nightly price.",
        "search_hotels",
    ),
]
FIRST_PROGRESS_LIMIT = 3.0  # seconds; Open WebUI adds its own request handling on top of ours


def check(results: list, name: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}{': ' + detail if detail else ''}")


def strings(value) -> list[str]:
    """Every string inside a nested JSON value (Open WebUI's stored output items)."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in strings(v)]
    return []


async def run_case(args, token: str, results: list, question: str, note_tool: str | None) -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1200, "height": 900})
        await page.add_init_script(f"localStorage.setItem('token', {json.dumps(token)})")
        await page.goto(f"{BASE_URL}/?models={get_settings().agent_model_name}")
        await page.locator("#chat-input").wait_for(timeout=30_000)
        await page.locator("#chat-input").click()
        await page.keyboard.type(question)
        sent = time.monotonic()
        await page.keyboard.press("Enter")
        await page.get_by_text("Starting…").first.wait_for(timeout=30_000)
        first = time.monotonic() - sent
        check(
            results,
            f"progress visible within {FIRST_PROGRESS_LIMIT:.0f} s",
            first < FIRST_PROGRESS_LIMIT,
            f"{first:.1f} s",
        )
        await page.locator('button[aria-label="Stop"]').wait_for(state="detached", timeout=300_000)
        await page.wait_for_timeout(1500)
        cid = chat_id(page)
        await browser.close()

    request = urllib.request.Request(
        f"{BASE_URL}/api/v1/chats/{cid}", headers={"authorization": f"Bearer {token}"}
    )
    messages = json.load(urllib.request.urlopen(request))["chat"]["history"]["messages"]
    answer = next(m for m in messages.values() if m["role"] == "assistant")
    content = answer.get("content") or ""
    reasoning = [
        "".join(strings(item))
        for item in answer.get("output", [])
        if item.get("type") == "reasoning"
    ]
    progress = "\n".join(reasoning)

    check(
        results,
        "answer has no progress lines",
        not re.search(r"🔧|Starting…|Reading the results…", content),
        f"{len(content)} chars",
    )
    check(
        results,
        "progress is in one reasoning block",
        len(reasoning) == 1,
        f"{len(reasoning)} blocks",
    )
    check(
        results,
        "progress ends before the answer",
        progress.rstrip().endswith("Reading the results…"),
        repr(progress[-60:]),
    )

    async with open_memory(get_settings().database_url) as (checkpointer, store):
        saved = await checkpointer.aget_tuple({"configurable": {"thread_id": f"{EMAIL}:{cid}"}})
        thread = saved.checkpoint["channel_values"]["messages"] if saved else []
        calls = [c for m in thread if isinstance(m, AIMessage) for c in m.tool_calls]
        lines = re.findall(r"^🔧 `([a-z_]+)\(", progress, re.M)
        # Outcomes land on the call's own line (`… → ✓ …`) or, after a parallel batch or the
        # tool's own ⋯ lines, on a line of their own (`   ✓ name → …`)
        outcomes = re.findall(r"` → [✓✗] |^\s+[✓✗] [a-z_]+ → ", progress, re.M)
        names = sorted(c["name"] for c in calls)
        check(
            results,
            "every tool call is shown",
            sorted(lines) == names and bool(names),
            f"{len(lines)} lines for {len(calls)} calls: {names}",
        )
        check(
            results,
            "every tool call has an outcome",
            len(outcomes) == len(calls),
            f"{len(outcomes)} outcomes",
        )
        if note_tool:
            notes = re.findall(rf"^\s+⋯ {note_tool}: (.+)$", progress, re.M)
            check(
                results,
                f"{note_tool} reports progress while it runs",
                len(notes) >= 2,
                " | ".join(notes) or "no ⋯ lines",
            )
        if not args.keep:
            delete_chat(BASE_URL, token, cid)
            await user_data.delete_all(store, checkpointer, EMAIL)
    if not all(results):
        print("\nprogress block:\n" + progress)


async def main(args) -> int:
    results: list[bool] = []
    token = get_token(BASE_URL, "e2e progress", EMAIL, PASSWORD)
    expand_details(BASE_URL, token)
    for name, question, note_tool in CASES:
        print(f"-- {name}")
        await run_case(args, token, results, question, note_tool)

    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", action="store_true", help="keep the test chat")
    sys.exit(asyncio.run(main(parser.parse_args())))

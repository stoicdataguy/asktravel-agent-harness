"""End-to-end test: branching a chat in Open WebUI keeps the agent's short-term memory.

Drives the real stack (Open WebUI in a browser, the agent API, Postgres, the configured LLM):

1. fork: ask for a plan, fork the chat at the answer, send a follow-up in the fork. The fork's
   agent thread must contain the original turn, and the reply must build on the plan.
2. regenerate: regenerate the fork's last answer. The thread must not gain a duplicate turn.

Needs the full stack running (docker compose up -d, uv run travel-agent) and a Playwright
browser (uv run playwright install chromium). Exits non-zero on failure.

Usage: uv run python scripts/e2e_branching.py [--keep]
"""

import argparse
import asyncio
import json
import re
import sys

from langchain_core.messages import HumanMessage
from owui import chat_id, delete_chat, get_token, send, wait_for_answer
from playwright.async_api import async_playwright

from travel_agent.config import get_settings
from travel_agent.memory import open_memory, user_namespace

BASE_URL = "http://localhost:3000"
EMAIL, PASSWORD = "e2e-branching@travel.local", "e2e-pass-123"
PLAN = "Plan 2 days in Kona on Hawaii's Big Island for 2 adults. Keep it to a short list."
FOLLOW_UP = "I'm really into snorkelling. Update day 2 of that plan for it, briefly."
# Phrases the agent used in the bug report when it had lost the plan
LOST_CONTEXT = re.compile(
    r"don.t see|paste it|which (hawaiian )?island|no (previous|original) plan", re.I
)


def plain(text: str) -> str:
    # Open WebUI's editor turns typed straight quotes into curly ones
    return text.replace("\u2019", "'").replace("\u2018", "'").strip()


async def thread_humans(user: str, chat: str) -> list[str]:
    async with open_memory(get_settings().database_url) as (checkpointer, _):
        state = await checkpointer.aget({"configurable": {"thread_id": f"{user}:{chat}"}})
    if not state:
        return []
    messages = state["channel_values"]["messages"]
    return [plain(m.text) for m in messages if isinstance(m, HumanMessage)]


async def cleanup(user: str, chats: list[str], token: str) -> None:
    async with open_memory(get_settings().database_url) as (checkpointer, store):
        for chat in chats:
            await checkpointer.adelete_thread(f"{user}:{chat}")
        for kind in ("itineraries", "memories", "trips"):
            for item in await store.asearch(user_namespace(user, kind), limit=100):
                await store.adelete(user_namespace(user, kind), item.key)
    for chat in chats:
        delete_chat(BASE_URL, token, chat)


def check(results: list, name: str, ok: bool, detail: str) -> None:
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")


async def main(args) -> int:
    token = get_token(BASE_URL, "E2E Branching", EMAIL, PASSWORD)
    results: list[bool] = []
    chats: list[str] = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            await page.add_init_script(f"localStorage.setItem('token', {json.dumps(token)})")
            await page.goto(BASE_URL)
            await page.locator("#chat-input").wait_for()

            # 1. Original chat: get a plan
            await send(page, PLAN)
            original = chat_id(page)
            chats.append(original)
            print(f"original chat {original}")

            # 2. Fork at the plan, then ask for a change that only makes sense with the plan
            await page.locator('button[aria-label="Fork chat"]').last.click()
            await page.wait_for_url(lambda url: original not in url, timeout=15_000)
            fork = chat_id(page)
            chats.append(fork)
            print(f"forked chat   {fork}")
            await send(page, FOLLOW_UP)
            reply = await page.locator(".chat-assistant").last.inner_text()

            humans = await thread_humans(EMAIL, fork)
            check(
                results,
                "fork keeps history",
                humans == [PLAN, FOLLOW_UP],
                f"thread humans={humans}",
            )
            lost = LOST_CONTEXT.search(reply)
            check(
                results,
                "fork reply builds on the plan",
                not lost and "kona" in reply.lower(),
                f"{'lost-context phrase ' + repr(lost.group(0)) if lost else 'ok'} | "
                f"reply: {reply.strip()[:160]!r}",
            )

            # 3. Regenerate the last answer: the thread must not record the follow-up twice
            await page.locator('button[aria-label="Regenerate"]').last.click()
            await page.get_by_text("Try Again", exact=True).click()  # Regenerate opens a menu
            await wait_for_answer(page, 300)
            humans = await thread_humans(EMAIL, fork)
            check(
                results,
                "regenerate doesn't duplicate",
                humans == [PLAN, FOLLOW_UP],
                f"thread humans={humans}",
            )
            await browser.close()

    finally:
        if not args.keep:
            await cleanup(EMAIL, chats, token)
    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", action="store_true", help="keep the chats and threads")
    sys.exit(asyncio.run(main(parser.parse_args())))

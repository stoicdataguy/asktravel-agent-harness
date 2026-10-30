"""End-to-end test: deleting a chat in Open WebUI deletes the agent's thread and index entry.

Creates a chat in Open WebUI as a test user, has the agent answer in it (so a thread and a chat
index entry exist), deletes the chat through Open WebUI's API, and waits for the agent API's
sync loop to delete its copies. Needs the full stack, with Open WebUI on Postgres
(docker-compose.yml) and an agent API from this code at --api.

Usage: uv run python scripts/e2e_chat_deletion.py [--api http://localhost:8000] [--timeout 60]
"""

import argparse
import asyncio
import json
import sys
import time
import urllib.request

from owui import delete_chat, get_token

from travel_agent import user_data
from travel_agent.config import get_settings
from travel_agent.memory import open_memory
from travel_agent.tools.conversations import chats_namespace

OWUI = "http://localhost:3000"
EMAIL, PASSWORD = "e2e-deletion@travel.local", "e2e-pass-123"


def post(url: str, payload: dict, headers: dict) -> dict:
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        url, data=body, headers={"content-type": "application/json", **headers}
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.load(response)


async def exists(checkpointer, store, thread_id: str) -> tuple[bool, bool]:
    thread = await checkpointer.aget_tuple({"configurable": {"thread_id": thread_id}})
    indexed = await store.aget(chats_namespace(EMAIL), thread_id)
    return thread is not None, indexed is not None


def check(results: list, name: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}{': ' + detail if detail else ''}")


async def main(args) -> int:
    results: list[bool] = []
    token = get_token(OWUI, "e2e deletion", EMAIL, PASSWORD)
    auth = {"authorization": f"Bearer {token}"}
    chat = post(f"{OWUI}/api/v1/chats/new", {"chat": {"title": "e2e deletion"}}, auth)
    thread_id = f"{EMAIL}:{chat['id']}"
    post(
        f"{args.api}/v1/chat/completions",
        {"model": "travel-agent", "messages": [{"role": "user", "content": "Say hi in 3 words."}]},
        {"X-OpenWebUI-User-Email": EMAIL, "X-OpenWebUI-Chat-Id": chat["id"]},
    )
    async with open_memory(get_settings().database_url) as (checkpointer, store):
        try:
            before = await exists(checkpointer, store, thread_id)
            check(results, "thread and index entry exist", before == (True, True), str(before))

            delete_chat(OWUI, token, chat["id"])
            start, after = time.monotonic(), before
            while time.monotonic() - start < args.timeout and any(after):
                await asyncio.sleep(2)
                after = await exists(checkpointer, store, thread_id)
            waited = f"{time.monotonic() - start:.0f}s"
            check(results, "deleted chat's thread is gone", not after[0], waited)
            check(results, "deleted chat's index entry is gone", not after[1], waited)
        finally:
            await user_data.delete_all(store, checkpointer, EMAIL)
    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api", default="http://localhost:8000", help="agent API base URL")
    parser.add_argument("--timeout", type=float, default=60, help="seconds to wait for the sync")
    sys.exit(asyncio.run(main(parser.parse_args())))

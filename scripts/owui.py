"""Shared helpers for scripts that drive Open WebUI in a browser (Playwright)."""

import json
import urllib.error
import urllib.request

from playwright.async_api import Page


def _post(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"content-type": "application/json"}
    )
    return json.load(urllib.request.urlopen(request))


def get_token(base_url: str, name: str, email: str, password: str) -> str:
    """Sign in a user, signing them up first if they don't exist yet."""
    try:
        return _post(f"{base_url}/api/v1/auths/signin", {"email": email, "password": password})[
            "token"
        ]
    except urllib.error.HTTPError:
        payload = {"name": name, "email": email, "password": password}
        return _post(f"{base_url}/api/v1/auths/signup", payload)["token"]


def expand_details(base_url: str, token: str) -> None:
    """Turn on Open WebUI's "Always Expand Details" for this user.

    The agent streams its progress (tool calls and outcomes) as a collapsible reasoning block;
    with this per-user setting the block stays open, so the lines show live while it works.
    """
    headers = {"authorization": f"Bearer {token}", "content-type": "application/json"}
    request = urllib.request.Request(f"{base_url}/api/v1/users/user/settings", headers=headers)
    settings = json.load(urllib.request.urlopen(request)) or {}
    settings.setdefault("ui", {})["expandDetails"] = True
    request = urllib.request.Request(
        f"{base_url}/api/v1/users/user/settings/update",
        data=json.dumps(settings).encode(),
        headers=headers,
        method="POST",
    )
    urllib.request.urlopen(request).read()


def delete_chat(base_url: str, token: str, chat_id: str) -> None:
    request = urllib.request.Request(
        f"{base_url}/api/v1/chats/{chat_id}",
        method="DELETE",
        headers={"authorization": f"Bearer {token}"},
    )
    urllib.request.urlopen(request).read()


async def wait_for_answer(page: Page, timeout: float) -> None:
    """Wait for Open WebUI's Stop button to appear (generation started) and then go away.

    Watching the button rather than the text matters: the transcript can sit still for many
    seconds while the agent is running tools.
    """
    stop_button = page.locator('button[aria-label="Stop"]')
    await stop_button.wait_for(state="visible", timeout=30_000)
    await stop_button.wait_for(state="detached", timeout=timeout * 1000)


async def send(page: Page, text: str, timeout: float = 300) -> None:
    await page.locator("#chat-input").click()
    await page.keyboard.type(text)
    await page.keyboard.press("Enter")
    await wait_for_answer(page, timeout)
    await page.wait_for_timeout(1000)


def chat_id(page: Page) -> str:
    return page.url.rstrip("/").rsplit("/c/", 1)[1]

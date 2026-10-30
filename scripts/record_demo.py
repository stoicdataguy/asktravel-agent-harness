"""Record a frame-by-frame GIF of the agent answering a question in Open WebUI.

Needs the full stack running (docker compose up -d, uv run travel-agent) and a Playwright browser
(uv run playwright install chromium). Signs up / signs in a demo user, types the prompt, screenshots
the page at a fixed interval while the answer streams, then assembles the frames into a GIF.

Usage: uv run python scripts/record_demo.py [--prompt "..."] [--out docs/demo.gif]
"""

import argparse
import asyncio
import io
import json
import time
from pathlib import Path

from owui import expand_details, get_token, wait_for_answer
from PIL import Image
from playwright.async_api import Page, async_playwright

DEFAULT_PROMPT = (
    "I have 3 days on the Big Island of Hawaii. What does Lonely Planet recommend? "
    "Keep it to a short day-by-day plan."
)
VIEWPORT = {"width": 1280, "height": 800}


async def capture(
    page: Page, frames: list[tuple[float, bytes]], interval: float, stop: asyncio.Event
):
    while not stop.is_set():
        frames.append((time.monotonic(), await page.screenshot(type="png")))
        await asyncio.sleep(interval)


# Finds the transcript's scrollable ancestor and scrolls it; returns True once at the bottom
_SCROLL_JS = """([mode, step]) => {
    let el = document.querySelector('#messages-container');
    while (el && !(el.scrollHeight > el.clientHeight + 10
                   && /auto|scroll/.test(getComputedStyle(el).overflowY))) el = el.parentElement;
    if (!el) return true;
    if (mode === 'top') el.scrollTop = 0; else el.scrollTop += step;
    return el.scrollTop + el.clientHeight >= el.scrollHeight - 2;
}"""


async def scroll_review(page: Page, step_px: int, pause_ms: int) -> None:
    """Pan from the top of the chat to the bottom so the GIF shows tool calls and the answer."""
    await page.evaluate(_SCROLL_JS, ["top", 0])
    await page.wait_for_timeout(2500)
    while not await page.evaluate(_SCROLL_JS, ["down", step_px]):
        await page.wait_for_timeout(pause_ms)
    await page.wait_for_timeout(1000)


async def record(args) -> tuple[list[tuple[float, bytes]], float, float]:
    token = get_token(args.base_url, "Demo Traveller", args.email, args.password)
    expand_details(args.base_url, token)  # show the agent's progress live
    frames: list[tuple[float, bytes]] = []
    stop = asyncio.Event()
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport=VIEWPORT, color_scheme="dark")
        await page.add_init_script(f"localStorage.setItem('token', {json.dumps(token)})")
        await page.goto(args.base_url)
        chat_input = page.locator("#chat-input")
        await chat_input.wait_for()
        await page.wait_for_timeout(1000)

        capturer = asyncio.create_task(capture(page, frames, args.interval, stop))
        await page.wait_for_timeout(1500)
        await chat_input.click()
        await page.keyboard.type(args.prompt, delay=30)
        await page.wait_for_timeout(800)
        await page.keyboard.press("Enter")
        sent_at = time.monotonic()
        await wait_for_answer(page, timeout=args.timeout)
        await page.wait_for_timeout(1500)
        review_at = time.monotonic()
        await scroll_review(page, step_px=args.scroll_step, pause_ms=args.scroll_pause)
        stop.set()
        await capturer
        await browser.close()
    return frames, sent_at, review_at


def build_gif(frames: list[tuple[float, bytes]], sent_at: float, review_at: float, args) -> None:
    """Downscale, drop unchanged frames, and time frames from real capture timestamps.

    The wait for the answer (between sending and the final scroll-through) plays `speedup` times
    faster; no frame lasts longer than `max_frame_ms` except the last, which holds for `hold_ms`.
    """
    width = args.width
    images, durations, previous = [], [], None
    for i, (ts, png) in enumerate(frames):
        next_ts = frames[i + 1][0] if i + 1 < len(frames) else ts
        ms = (next_ts - ts) * 1000 / (args.speedup if sent_at <= ts < review_at else 1)
        image = Image.open(io.BytesIO(png)).convert("RGB")
        image = image.resize((width, round(image.height * width / image.width)), Image.LANCZOS)
        if previous is not None and image.tobytes() == previous:
            durations[-1] += ms
            continue
        previous = image.tobytes()
        images.append(image.quantize(colors=128, method=Image.Quantize.MEDIANCUT))
        durations.append(ms)
    durations = [int(min(max(d, 40), args.max_frame_ms)) for d in durations]
    durations[-1] = args.hold_ms

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    images[0].save(
        out, save_all=True, append_images=images[1:], duration=durations, loop=0, optimize=True
    )
    total = sum(durations) / 1000
    print(f"wrote {out}: {len(images)} frames, {total:.1f}s, {out.stat().st_size / 1e6:.1f} MB")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--out", default="docs/demo.gif")
    parser.add_argument("--base-url", default="http://localhost:3000")
    parser.add_argument("--email", default="demo@travel.local")
    parser.add_argument("--password", default="demo-pass-123")
    parser.add_argument("--interval", type=float, default=0.4, help="seconds between frames")
    parser.add_argument("--speedup", type=float, default=2.0, help="playback speed after sending")
    parser.add_argument("--width", type=int, default=960, help="GIF width in pixels")
    parser.add_argument("--max-frame-ms", type=int, default=1200)
    parser.add_argument("--hold-ms", type=int, default=5000, help="how long the last frame shows")
    parser.add_argument("--scroll-step", type=int, default=120, help="px per scroll step at end")
    parser.add_argument("--scroll-pause", type=int, default=450, help="ms between scroll steps")
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args()

    frames, sent_at, review_at = asyncio.run(record(args))
    build_gif(frames, sent_at, review_at, args)


if __name__ == "__main__":
    main()

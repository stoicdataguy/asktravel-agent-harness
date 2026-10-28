"""Generate AskTravel logo assets for Open WebUI from one SVG mark.

Writes branding/*.png|ico|svg, which docker-compose.yml mounts over Open WebUI's own assets
(see the open-webui service). Re-run after changing the colours or the mark:

    uv run python scripts/make_branding.py
"""

import io
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

OUT = Path("branding")
BLUE = "#1d5fc4"  # same accent as docs/architecture.html
LIGHT_BLUE = "#7aaeff"

# Material Design "flight" icon (Apache 2.0), 24x24 viewBox; rotated to fly up and to the right
PLANE = (
    "M21 16v-2l-8-5V3.5c0-.83-.67-1.5-1.5-1.5S10 2.67 10 3.5V9l-8 5v2l8-2.5V19l-2 1.5V22"
    "l3.5-1 3.5 1v-1.5L13 19v-5.5l8 2.5z"
)


def plane(color: str, scale: float) -> str:
    return (
        f'<g transform="translate(256 256) rotate(45) scale({scale}) translate(-11.5 -12)">'
        f'<path fill="{color}" d="{PLANE}"/></g>'
    )


def svg(body: str) -> str:
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">{body}</svg>'


TILE = svg(f'<rect width="512" height="512" rx="112" fill="{BLUE}"/>' + plane("#fff", 13))
SQUARE = svg(f'<rect width="512" height="512" fill="{BLUE}"/>' + plane("#fff", 13))
SPLASH_LIGHT = svg(plane(BLUE, 16))
SPLASH_DARK = svg(plane(LIGHT_BLUE, 16))


def render(page, markup: str, size: int) -> Image.Image:
    page.set_viewport_size({"width": size, "height": size})
    sized = markup.replace("<svg ", f"<svg width={size} height={size} ")
    page.set_content(f'<body style="margin:0;background:transparent">{sized}</body>')
    clip = {"x": 0, "y": 0, "width": size, "height": size}
    png = page.screenshot(omit_background=True, clip=clip)
    return Image.open(io.BytesIO(png)).convert("RGBA")


def main() -> None:
    OUT.mkdir(exist_ok=True)
    (OUT / "favicon.svg").write_text(TILE)
    targets = {
        "favicon.png": (TILE, 512),
        "favicon-96x96.png": (TILE, 96),
        "apple-touch-icon.png": (SQUARE, 180),  # iOS rounds the corners itself
        "web-app-manifest-192x192.png": (TILE, 192),
        "web-app-manifest-512x512.png": (TILE, 512),
        "logo.png": (SQUARE, 500),
        "splash.png": (SPLASH_LIGHT, 500),
        "splash-dark.png": (SPLASH_DARK, 500),
    }
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        for name, (markup, size) in targets.items():
            render(page, markup, size).save(OUT / name)
        render(page, TILE, 256).save(OUT / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
        browser.close()
    print("wrote", ", ".join(sorted(p.name for p in OUT.iterdir())))


if __name__ == "__main__":
    main()

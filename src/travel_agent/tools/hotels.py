"""Hotel search by scraping Google Hotels with Playwright (no API key).

The page is driven like a person would: open the hotel search for a place, pick the dates in the
calendar (URL date parameters are ignored), then read each result card. Card parsing is kept
separate from the browser (`parse_card`) so it can be unit-tested against captured card text.
Google's markup changes over time; failures come back as a message with a search link.
"""

import logging
import re
from datetime import date
from urllib.parse import quote

from langchain.tools import tool
from playwright.async_api import async_playwright

from travel_agent.agent.schemas import HotelOption

logger = logging.getLogger(__name__)

MAX_RESULTS = 10
MAX_DAYS_AHEAD = 330  # Google's calendar covers roughly the next year
PAGE_TIMEOUT_MS = 45_000

# One card per hotel: from each "Prices starting from $X, <name>" link, climb to the largest
# ancestor that still contains exactly one hotel name (h2)
_CARDS_JS = r"""() => {
  const out = []; const seen = new Set();
  for (const a of document.querySelectorAll('a[aria-label^="Prices starting from"]')) {
    let el = a;
    while (el.parentElement && el.parentElement.querySelectorAll('h2').length <= 1) {
      el = el.parentElement;
    }
    const h2 = el.querySelector('h2');
    if (!h2) continue;
    const name = h2.innerText.trim();
    if (seen.has(name)) continue;
    seen.add(name);
    out.push({label: a.getAttribute('aria-label'), name, text: el.innerText});
  }
  return out;
}"""

_PRICE = re.compile(r"Prices starting from \$([\d,]+)")
_CLASS = re.compile(r"(\d)-star hotel")
_RATING = re.compile(r"^(\d(?:\.\d)?)(?:/5)?$")
_REVIEWS = re.compile(r"^\(([\d.,]+K?)\)$")
_AMENITIES = re.compile(r"Amenities for [^:]*:\s*(.+)")
_DEAL_TAGS = ("DEAL", "GREAT PRICE")


def search_url(location: str) -> str:
    return (
        f"https://www.google.com/travel/hotels/{quote(location)}"
        f"?q={quote('hotels in ' + location)}&hl=en-US&gl=us&curr=USD"
    )


def parse_card(label: str, name: str, text: str) -> HotelOption | None:
    """Turn one result card's link label, name and visible text into a HotelOption."""
    price = _PRICE.search(label or "")
    lines = [line.strip() for line in text.splitlines() if line.strip() and line.strip() != "·"]
    rating = reviews = deal = None
    amenities: list[str] = []
    for i, line in enumerate(lines):
        if rating is None and (m := _RATING.match(line)) and 1 <= float(m.group(1)) <= 5:
            rating = float(m.group(1))
            if i + 1 < len(lines) and (r := _REVIEWS.match(lines[i + 1])):
                reviews = r.group(1)
        if deal is None and line in _DEAL_TAGS:
            hint = lines[i + 2] if i + 2 < len(lines) and not lines[i + 2].startswith("$") else ""
            deal = f"{line} {hint}".strip() if "%" in hint or "price" in hint else line
        if not amenities and (m := _AMENITIES.search(line)):
            amenities = [a.strip() for a in m.group(1).split(",") if a.strip()][:6]
    hotel_class = _CLASS.search(text)
    return HotelOption(
        name=name,
        price_per_night=int(price.group(1).replace(",", "")) if price else None,
        rating=rating,
        reviews=reviews,
        hotel_class=int(hotel_class.group(1)) if hotel_class else None,
        amenities=amenities,
        deal=deal,
        vacation_rental="VACATION RENTAL" in text.upper() or "Sleeps " in text,
    )


async def fetch_hotel_cards(
    location: str, check_in: date, check_out: date
) -> tuple[list[dict], str]:
    """Open Google Hotels, set the dates, and return raw cards plus the page URL."""
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page(locale="en-US", viewport={"width": 1280, "height": 1000})
            page.set_default_timeout(PAGE_TIMEOUT_MS)
            await page.goto(search_url(location), wait_until="domcontentloaded")
            await page.locator('a[aria-label^="Prices starting from"]').first.wait_for()
            await page.locator('input[aria-label="Check-in"]').first.click()
            for day in (check_in, check_out):
                await page.locator(f'[data-iso="{day.isoformat()}"]:visible').first.click()
                await page.wait_for_timeout(300)
            await page.locator('text="Done" >> visible=true').first.click()
            # The check-in box shows e.g. "Mon, Dec 28" once the new dates are applied
            expected = f"{check_in:%a, %b} {check_in.day}"
            await page.wait_for_function(
                """expected => [...document.querySelectorAll('input[aria-label="Check-in"]')]
                     .some(e => e.value === expected)""",
                arg=expected,
            )
            await page.wait_for_timeout(3500)  # results re-render after the date change
            cards = await page.evaluate(_CARDS_JS)
            return cards, page.url
        finally:
            await browser.close()


def format_hotel(n: int, hotel: HotelOption, nights: int) -> str:
    parts = []
    if hotel.price_per_night is not None:
        parts.append(
            f"${hotel.price_per_night:,}/night (~${hotel.price_per_night * nights:,} for "
            f"{nights} night{'s' if nights > 1 else ''})"
        )
    else:
        parts.append("price unavailable")
    if hotel.hotel_class:
        parts.append(f"{hotel.hotel_class}-star")
    if hotel.vacation_rental:
        parts.append("vacation rental")
    if hotel.rating is not None:
        parts.append(f"{hotel.rating}★" + (f" ({hotel.reviews} reviews)" if hotel.reviews else ""))
    if hotel.deal:
        parts.append(hotel.deal.lower())
    if hotel.amenities:
        parts.append(", ".join(hotel.amenities))
    return f"{n}. {hotel.name} · " + " · ".join(parts)


@tool
async def search_hotels(
    location: str,
    check_in: date,
    check_out: date,
    max_price_per_night: int | None = None,
    min_rating: float | None = None,
    include_vacation_rentals: bool = False,
    max_results: int = 5,
) -> str:
    """Search live hotel prices on Google Hotels for a place and dates.

    Use this whenever the user asks where to stay, for hotel options, or for hotel prices;
    never guess hotel prices. Takes 10-20 seconds.

    Args:
        location: Town, neighbourhood or area, e.g. "Kailua-Kona, Hawaii" or "Trastevere, Rome".
        check_in: Check-in date (YYYY-MM-DD).
        check_out: Check-out date (YYYY-MM-DD).
        max_price_per_night: Only show hotels at or under this nightly price (USD).
        min_rating: Only show hotels rated at least this (out of 5), e.g. 4.0.
        include_vacation_rentals: Also include apartments/condos listed among the hotels.
        max_results: How many options to return (max 10).

    Returns hotels in Google's ranking order with nightly price (as Google shows it; taxes and
    fees may be extra), total for the stay, star class, guest rating, deals and key amenities,
    plus a Google Hotels link to compare and book. To remember a hotel the user picks, save it
    to their trip with save_trip_details(lodging=...).
    """
    location = location.strip()
    if not location:
        return "Give a town or area to search, e.g. 'Kailua-Kona, Hawaii'."
    today = date.today()
    if check_in < today:
        return f"{check_in} is in the past; ask the user for upcoming dates."
    if check_out <= check_in:
        return "Check-out must be after check-in."
    if (check_out - today).days > MAX_DAYS_AHEAD:
        return "Google Hotels only takes dates within about the next year; ask for nearer dates."

    try:
        cards, url = await fetch_hotel_cards(location, check_in, check_out)
    except Exception as exc:  # timeouts, consent pages, Google layout changes
        logger.warning("hotel search failed for %s: %s", location, exc)
        return (
            f"Hotel search failed ({type(exc).__name__}). Suggest the user search "
            f"{search_url(location)} for {check_in} to {check_out}."
        )

    hotels = [h for c in cards if (h := parse_card(c["label"], c["name"], c["text"]))]
    if not include_vacation_rentals:
        hotels = [h for h in hotels if not h.vacation_rental]
    if max_price_per_night is not None:
        hotels = [
            h for h in hotels if h.price_per_night and h.price_per_night <= max_price_per_night
        ]
    if min_rating is not None:
        hotels = [h for h in hotels if h.rating and h.rating >= min_rating]
    if not hotels:
        return f"No hotels in {location} matched for {check_in} to {check_out}. Search: {url}"

    nights = (check_out - check_in).days
    shown = hotels[: max(1, min(max_results, MAX_RESULTS))]
    header = (
        f"Hotels in {location}, {check_in:%a %b %-d} → {check_out:%a %b %-d, %Y} "
        f"({nights} nights), from Google Hotels. Nightly prices in USD as Google shows them "
        "(taxes and fees may be extra):"
    )
    lines = [header, *(format_hotel(i, h, nights) for i, h in enumerate(shown, 1))]
    lines.append(f"Compare and book: {url}")
    return "\n".join(lines)

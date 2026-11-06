"""Indian train search over ConfirmTkt's JSON search endpoint (no API key, no browser).

The endpoint is undocumented: it can change or block us, so failures come back as a message with
a search link. It answers from a cache, so results say how old the availability is. It may also
list trains from nearby stations, so each option shows the stations it actually uses.
"""

import logging
import re
from datetime import date, datetime

import httpx
from langchain.tools import tool

from travel_agent.agent.schemas import TrainClass, TrainOption

logger = logging.getLogger(__name__)

SEARCH_URL = "https://cttrainsapi.confirmtkt.com/api/v1/trains/search"
TIMEOUT_SECONDS = 20
MAX_RESULTS = 10
BOOKING_WINDOW_DAYS = 60  # Indian Railways opens reservations about this far ahead
_STATION = re.compile(r"^[A-Z]{1,5}$")


def booking_url(origin: str, destination: str, travel_date: date) -> str:
    return (
        "https://www.confirmtkt.com/rbooking-d/trains/from/"
        f"{origin}/to/{destination}/{travel_date:%d-%m-%Y}"
    )


def _fare(value) -> int | None:
    """A fare as a whole number of rupees; the endpoint sends text and can leave it empty."""
    try:
        return round(float(value))
    except (TypeError, ValueError):
        return None


def to_option(train: dict) -> TrainOption:
    availability = train.get("availabilityCache") or {}
    classes = []
    for code in train.get("avlClassesSorted") or train.get("avlClasses") or []:
        seats = availability.get(code) or {}
        classes.append(
            TrainClass(
                code=code,
                fare=_fare(seats.get("fare")),
                availability=seats.get("availabilityDisplayName") or None,
            )
        )
    return TrainOption(
        number=train["trainNumber"],
        name=train["trainName"].title(),
        from_station=train["fromStnCode"],
        to_station=train["toStnCode"],
        departs=train["departureTime"],
        arrives=train["arrivalTime"],
        duration_minutes=train.get("duration"),
        classes=classes,
    )


def is_cancelled(train: dict) -> bool:
    """Every class says "Train Cancelled" for this date."""
    shown = [
        s.get("availabilityDisplayName") or ""
        for s in (train.get("availabilityCache") or {}).values()
    ]
    return bool(shown) and all("cancel" in s.lower() for s in shown)


def checked_at(trains: list[dict]) -> datetime | None:
    """When the oldest availability among these trains was last refreshed."""
    times = []
    for train in trains:
        for seats in (train.get("availabilityCache") or {}).values():
            try:
                times.append(datetime.fromisoformat(seats["cacheTime"]))
            except (KeyError, TypeError, ValueError):
                continue
    return min(times) if times else None


async def fetch_trains(origin: str, destination: str, travel_date: date) -> dict:
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        response = await client.get(
            SEARCH_URL,
            params={
                "sourceStationCode": origin,
                "destinationStationCode": destination,
                "dateOfJourney": f"{travel_date:%d-%m-%Y}",
                "addAvailabilityCache": "true",
            },
        )
    response.raise_for_status()
    return response.json().get("data") or {}


@tool
async def search_trains(
    origin: str,
    destination: str,
    travel_date: date,
    travel_class: str | None = None,
    max_results: int = 5,
) -> str:
    """Search Indian Railways trains with fares and seat availability, via ConfirmTkt.

    Use this whenever the user asks about trains within India; never guess train times, fares or
    availability. Trains in other countries aren't covered.

    Args:
        origin: Indian Railways station code, e.g. "NDLS" for New Delhi, "CSMT" for Mumbai.
        destination: Station code, e.g. "AGC" for Agra Cantt, "SBC" for Bengaluru.
        travel_date: Date of travel (YYYY-MM-DD). Reservations open about 60 days ahead.
        travel_class: Only trains with this class: SL (sleeper), 3A, 2A, 1A (AC tiers), 3E,
            CC (AC chair), EC (executive chair) or 2S (second sitting). Omit for all.
        max_results: How many trains to return (max 10).

    Returns trains on the requested route first, then ones using nearby stations, each with its
    actual stations, times, and per-class fare and availability. Fares are in rupees per person
    (general quota). AVL means seats are available, WL is a waitlist, RAC shares a berth, Regret
    means no more bookings. Availability comes from a cache and can be days old: results say how
    old it is, so tell the user. They end with a link to check and book. To remember a train the
    user picks, save it to their trip with save_trip_details(transport=...).
    """
    origin, destination = origin.strip().upper(), destination.strip().upper()
    if not (_STATION.match(origin) and _STATION.match(destination)):
        return "Use Indian Railways station codes, e.g. NDLS for New Delhi and AGC for Agra Cantt."
    if travel_date < date.today():
        return f"{travel_date} is in the past; ask the user for an upcoming date."
    travel_class = travel_class.strip().upper() if travel_class else None
    link = booking_url(origin, destination, travel_date)

    try:
        data = await fetch_trains(origin, destination, travel_date)
    except Exception as exc:  # network errors, the endpoint changing or blocking us
        logger.warning("train search failed for %s-%s: %s", origin, destination, exc)
        return f"Train search failed ({type(exc).__name__}). Suggest checking {link}"

    trains = [
        t for t in data.get("trainList") or [] if not t.get("hasDeparted") and not is_cancelled(t)
    ]
    if not trains:
        if (travel_date - date.today()).days > BOOKING_WINDOW_DAYS:
            return (
                f"No trains listed {origin} → {destination} on {travel_date}: reservations open "
                f"about {BOOKING_WINDOW_DAYS} days ahead, so try a nearer date. Search: {link}"
            )
        return (
            f"No direct trains found {origin} → {destination} on {travel_date}. Check the "
            f"station codes, or try a nearby station. Search: {link}"
        )

    if travel_class:
        trains = [t for t in trains if travel_class in (t.get("avlClasses") or [])]
        if not trains:
            return (
                f"No {travel_class} trains found {origin} → {destination} on {travel_date}. "
                f"Search: {link}"
            )

    # The requested stations first, since nearby ones can be far from where the user is going
    trains.sort(
        key=lambda t: (
            t["toStnCode"] != destination,
            t["fromStnCode"] != origin,
            t["departureTime"],
        )
    )
    shown = trains[: max(1, min(max_results, MAX_RESULTS))]
    options = [to_option(t) for t in shown]
    if travel_class:
        for option in options:
            option.classes = [c for c in option.classes if c.code == travel_class]

    header = (
        f"Trains {origin} → {destination}, {travel_date:%a %b %-d, %Y}, "
        f"{len(shown)} of {len(trains)}. Fares in rupees per person:"
    )
    lines = [header, *(f"{i}. {o.summary()}" for i, o in enumerate(options, 1))]
    if refreshed := checked_at(shown):
        lines.append(f"Availability as of {refreshed:%b %-d, %H:%M}; it changes quickly.")
    lines.append(f"Check and book: {link}")
    return "\n".join(lines)

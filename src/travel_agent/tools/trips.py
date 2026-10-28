"""Trips: the second kind of long-term memory, next to profile facts (`tools/memory.py`).

A trip holds what's specific to one journey: destination, dates, travellers, where the user is
staying, transport, budget, notes, and a link to its saved itinerary. Trips are stored per user in
the LangGraph store, summarised one line each in the system prompt, and expanded on demand with
`get_trip`. `present_itinerary` creates or updates the trip for every plan it shows, so a plan made
in one chat is known in the next without relying on the model to save it.
"""

import re
from datetime import UTC, date, datetime, timedelta

from langchain.tools import ToolRuntime, tool
from langgraph.store.base import BaseStore
from pydantic import BaseModel, Field

from travel_agent.agent.schemas import Itinerary, render_itinerary
from travel_agent.context import UserContext
from travel_agent.memory import user_namespace
from travel_agent.tools.guidebooks import normalize

MAX_TRIPS_IN_PROMPT = 8
MAX_FIELD_CHARS = 200


class Trip(BaseModel):
    trip_id: str
    destination: str
    start_date: date | None = None
    end_date: date | None = None
    travellers: str | None = None
    lodging: list[str] = Field(default_factory=list)
    transport: list[str] = Field(default_factory=list)
    budget: str | None = None
    notes: list[str] = Field(default_factory=list)
    itinerary_thread: str | None = None  # key of the linked plan in (user, "itineraries")
    updated_at: str = ""


def namespace(user_id: str) -> tuple[str, ...]:
    return user_namespace(user_id, "trips")


def itineraries_namespace(user_id: str) -> tuple[str, ...]:
    return user_namespace(user_id, "itineraries")


def make_trip_id(destination: str, start_date: date | None) -> str:
    # normalize() folds accents and the Hawaiian ʻokina, so "Hawaiʻi" becomes "hawaii"
    slug = re.sub(r"[^a-z0-9]+", "-", normalize(destination)).strip("-")[:40] or "trip"
    return f"{slug}-{start_date:%Y-%m}" if start_date else f"{slug}-undated"


async def load_trips(store: BaseStore | None, user_id: str) -> list[Trip]:
    """The user's trips, dated ones in date order, then undated, newest update first."""
    if store is None:
        return []
    items = await store.asearch(namespace(user_id), limit=100)
    trips = [Trip(**item.value) for item in items]
    dated = sorted((t for t in trips if t.start_date), key=lambda t: t.start_date)
    undated = sorted(
        (t for t in trips if not t.start_date), key=lambda t: t.updated_at, reverse=True
    )
    return [*dated, *undated]


def format_trips(trips: list[Trip], today: date | None = None, include_past: bool = False) -> str:
    """One line per trip; unless `include_past`, trips that ended over 30 days ago are left out."""
    cutoff = (today or date.today()) - timedelta(days=30)
    if not include_past:
        trips = [t for t in trips if not t.end_date or t.end_date >= cutoff]
    current = trips[:MAX_TRIPS_IN_PROMPT]
    if not current:
        return "(no trips saved yet)"
    lines = []
    for t in current:
        parts = [t.destination]
        if t.start_date:
            parts.append(f"{t.start_date} → {t.end_date or '?'}")
        if t.travellers:
            parts.append(t.travellers)
        if t.lodging:
            parts.append("staying: " + "; ".join(t.lodging))
        if t.transport:
            parts.append("transport: " + "; ".join(t.transport))
        if t.budget:
            parts.append(f"budget: {t.budget}")
        if t.notes:
            parts.append("notes: " + "; ".join(t.notes))
        if t.itinerary_thread:
            parts.append("itinerary saved")
        lines.append(f"- [{t.trip_id}] " + " · ".join(parts))
    return "\n".join(lines)


def _add_unique(items: list[str], new: list[str]) -> list[str]:
    seen = {i.lower() for i in items}
    for value in new:
        value = value.strip()[:MAX_FIELD_CHARS]
        if value and value.lower() not in seen:
            items.append(value)
            seen.add(value.lower())
    return items


async def upsert_trip(
    store: BaseStore,
    user_id: str,
    *,
    destination: str,
    trip_id: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    travellers: str | None = None,
    lodging: list[str] = (),
    transport: list[str] = (),
    budget: str | None = None,
    notes: list[str] = (),
    itinerary_thread: str | None = None,
) -> Trip:
    """Create a trip or merge details into an existing one.

    Scalar fields overwrite when given; lodging, transport and notes accumulate.
    """
    trip_id = trip_id or make_trip_id(destination, start_date)
    existing = await store.aget(namespace(user_id), trip_id)
    trip = Trip(**existing.value) if existing else Trip(trip_id=trip_id, destination=destination)
    trip.destination = destination or trip.destination
    for field, value in {
        "start_date": start_date,
        "end_date": end_date,
        "travellers": travellers,
        "budget": budget,
        "itinerary_thread": itinerary_thread,
    }.items():
        if value:
            setattr(trip, field, value)
    _add_unique(trip.lodging, list(lodging))
    _add_unique(trip.transport, list(transport))
    _add_unique(trip.notes, list(notes))
    trip.updated_at = datetime.now(UTC).isoformat()
    await store.aput(namespace(user_id), trip_id, trip.model_dump(mode="json"))
    return trip


async def link_itinerary(
    store: BaseStore, user_id: str, itinerary: Itinerary, thread_id: str
) -> Trip:
    """Create or update the trip a presented itinerary belongs to."""
    end = (
        itinerary.start_date + timedelta(days=itinerary.trip_length_days - 1)
        if itinerary.start_date
        else None
    )
    return await upsert_trip(
        store,
        user_id,
        trip_id=itinerary.trip_id,
        destination=itinerary.destination,
        start_date=itinerary.start_date,
        end_date=end,
        travellers=itinerary.travellers,
        lodging=[s.area for s in itinerary.stays],
        itinerary_thread=thread_id,
    )


@tool
async def save_trip_details(
    destination: str,
    runtime: ToolRuntime[UserContext],
    trip_id: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    travellers: str | None = None,
    lodging: str | None = None,
    transport: str | None = None,
    budget: str | None = None,
    note: str | None = None,
) -> str:
    """Remember details of one of the user's trips for future conversations.

    Call whenever the user shares something specific to a trip: dates, where they're staying,
    who's going, flights or car, the budget for that trip, or other plans. Only pass what's new;
    lodging, transport and notes are added to what's already saved.

    Args:
        destination: Where the trip goes, e.g. "Big Island, Hawaii".
        trip_id: Id of an existing trip from your instructions; omit to create a new trip.
        start_date: First day (YYYY-MM-DD) if known.
        end_date: Last day (YYYY-MM-DD) if known.
        travellers: Who's going, e.g. "2 adults".
        lodging: Where they're staying, e.g. "Royal Kona Resort, Kailua-Kona".
        transport: e.g. "Flying SFO→KOA Dec 28", "rental car".
        budget: Budget for this trip, e.g. "$3000 total".
        note: Anything else worth remembering about this trip.
    """
    trip = await upsert_trip(
        runtime.store,
        runtime.context.user_id,
        destination=destination,
        trip_id=trip_id,
        start_date=start_date,
        end_date=end_date,
        travellers=travellers,
        lodging=[lodging] if lodging else [],
        transport=[transport] if transport else [],
        budget=budget,
        notes=[note] if note else [],
    )
    return f"Saved trip {trip.trip_id}:\n{format_trips([trip], include_past=True)}"


@tool
async def get_trip(trip_id: str, runtime: ToolRuntime[UserContext]) -> str:
    """Get everything saved about one trip, including its full day-by-day itinerary if any.

    Args:
        trip_id: Id of the trip, as listed in your instructions.
    """
    user = runtime.context.user_id
    item = await runtime.store.aget(namespace(user), trip_id)
    if item is None:
        return f"No trip {trip_id!r}. Saved trips are listed in your instructions."
    trip = Trip(**item.value)
    details = format_trips([trip], include_past=True)
    if not trip.itinerary_thread:
        return f"{details}\n\nNo itinerary saved for this trip yet."
    plan = await runtime.store.aget(itineraries_namespace(user), trip.itinerary_thread)
    if plan is None:
        return f"{details}\n\nThe linked itinerary is no longer saved."
    return (
        f"{details}\n\nSaved itinerary:\n\n{render_itinerary(Itinerary(**plan.value['itinerary']))}"
    )


@tool
async def delete_trip(trip_id: str, runtime: ToolRuntime[UserContext]) -> str:
    """Forget a saved trip (when the user cancels it or asks you to forget it).

    Args:
        trip_id: Id of the trip, as listed in your instructions.
    """
    ns = namespace(runtime.context.user_id)
    if await runtime.store.aget(ns, trip_id) is None:
        return f"No trip {trip_id!r}."
    await runtime.store.adelete(ns, trip_id)
    return f"Forgot trip {trip_id}."


async def backfill_trips(store: BaseStore) -> int:
    """Create trips for itineraries saved before trips existed. Idempotent; returns the count."""
    created = 0
    for ns in await store.alist_namespaces(suffix=("itineraries",), limit=10_000):
        user_id = ns[0].replace("%2E", ".")
        for item in await store.asearch(ns, limit=1000):
            if item.value.get("trip_id"):
                continue
            trip = await link_itinerary(
                store, user_id, Itinerary(**item.value["itinerary"]), item.key
            )
            await store.aput(ns, item.key, {**item.value, "trip_id": trip.trip_id})
            created += 1
    return created


TRIP_TOOLS = [save_trip_details, get_trip, delete_trip]

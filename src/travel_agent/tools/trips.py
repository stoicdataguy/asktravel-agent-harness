"""Trips: the second kind of long-term memory, next to profile facts (`tools/memory.py`).

A trip holds what's specific to one journey: destination, dates, travellers, where the user is
staying, transport, budget, notes, and a link to its saved itinerary. Trips are stored per user in
the LangGraph store, summarised one line each in the system prompt, and expanded on demand with
`get_trip`. `present_itinerary` creates or updates the trip for every plan it shows, so a plan made
in one chat is known in the next without relying on the model to save it.

Trip ids are opaque (`trip-7k2p9q`) and never change, so a trip that starts as "Europe, undecided"
and becomes Rome keeps an id that isn't misleading. When the model saves a trip without a known id,
it's matched to an existing trip by destination words and dates (`match_trip`) instead of creating
a duplicate. Ids from before this scheme are kept as aliases.
"""

import re
import secrets
import string
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
TRIP_ID = re.compile(r"^trip-[a-z0-9]{6}$")
# Destinations match when more than this share of the shorter one's words are shared
MATCH_OVERLAP = 0.5
_STOPWORDS = {"the", "and", "of", "in", "on", "de", "la", "le", "trip", "to"}


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
    source: str | None = None  # thread the trip was first saved from
    aliases: list[str] = Field(default_factory=list)  # earlier ids, still accepted by get_trip
    updated_at: str = ""


def namespace(user_id: str) -> tuple[str, ...]:
    return user_namespace(user_id, "trips")


def itineraries_namespace(user_id: str) -> tuple[str, ...]:
    return user_namespace(user_id, "itineraries")


def new_trip_id() -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "trip-" + "".join(secrets.choice(alphabet) for _ in range(6))


def destination_words(destination: str) -> set[str]:
    # Drop possessives first ("Hawaii's" → "Hawaii"); normalize() then folds accents and the ʻokina
    text = normalize(re.sub(r"['’]s\b", "", destination))
    return {w for w in re.split(r"[^a-z0-9]+", text) if len(w) > 1 and w not in _STOPWORDS}


def _dates_compatible(trip: Trip, start: date | None, end: date | None) -> bool:
    if not (trip.start_date and start):
        return True  # an undated side can be the same trip
    return trip.start_date <= (end or start) and start <= (trip.end_date or trip.start_date)


def match_trip(
    trips: list[Trip],
    destination: str,
    start_date: date | None = None,
    end_date: date | None = None,
    today: date | None = None,
) -> Trip | None:
    """The one existing, not-yet-ended trip this destination and dates most likely refer to.

    Returns None when nothing matches or more than one trip could, so ambiguous cases stay
    separate rather than merging two real trips.
    """
    today = today or date.today()
    words = destination_words(destination)
    candidates = []
    for trip in trips:
        if (trip.end_date or trip.start_date or today) < today:
            continue  # a past trip to the same place is a different trip
        theirs = destination_words(trip.destination)
        shared = len(words & theirs) / (min(len(words), len(theirs)) or 1)
        if shared > MATCH_OVERLAP and _dates_compatible(trip, start_date, end_date):
            candidates.append(trip)
    return candidates[0] if len(candidates) == 1 else None


def trip_status(trip: Trip, today: date | None = None) -> str:
    """When the trip is relative to today, so the model doesn't have to do date arithmetic."""
    today = today or date.today()
    if not trip.start_date:
        return "dates not set"
    end = trip.end_date or trip.start_date
    if trip.start_date > today:
        days = (trip.start_date - today).days
        return "upcoming, tomorrow" if days == 1 else f"upcoming, in {days} days"
    if end >= today:
        return "travelling now"
    days = (today - end).days
    return "ended yesterday" if days == 1 else f"ended {days} days ago"


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
    today = today or date.today()
    cutoff = today - timedelta(days=30)
    if not include_past:
        trips = [t for t in trips if not t.end_date or t.end_date >= cutoff]
    current = trips[:MAX_TRIPS_IN_PROMPT]
    if not current:
        return "(no trips saved yet)"
    lines = []
    for t in current:
        parts = [t.destination]
        dates = f"{t.start_date} → {t.end_date or '?'} " if t.start_date else ""
        parts.append(f"{dates}({trip_status(t, today)})")
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
    source: str | None = None,
) -> Trip:
    """Create a trip or merge details into an existing one.

    The trip is found by `trip_id` (or one of its aliases); without a known id, by `match_trip`;
    otherwise a new trip with a fresh id is created. Scalar fields overwrite when given; lodging,
    transport and notes accumulate.
    """
    trip = await find_trip(store, user_id, trip_id) if trip_id else None
    if trip is None:
        trip = match_trip(await load_trips(store, user_id), destination, start_date, end_date)
    if trip is None:
        trip = Trip(trip_id=new_trip_id(), destination=destination, source=source)
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
    await store.aput(namespace(user_id), trip.trip_id, trip.model_dump(mode="json"))
    return trip


async def find_trip(store: BaseStore, user_id: str, trip_id: str) -> Trip | None:
    """A trip by its id, or by an earlier id it was migrated from."""
    item = await store.aget(namespace(user_id), trip_id)
    if item is not None:
        return Trip(**item.value)
    return next((t for t in await load_trips(store, user_id) if trip_id in t.aliases), None)


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
        source=thread_id,
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
        source=runtime.config.get("configurable", {}).get("thread_id"),
    )
    return f"Saved trip {trip.trip_id}:\n{format_trips([trip], include_past=True)}"


@tool
async def get_trip(trip_id: str, runtime: ToolRuntime[UserContext]) -> str:
    """Get everything saved about one trip, including its full day-by-day itinerary if any.

    Args:
        trip_id: Id of the trip, as listed in your instructions.
    """
    user = runtime.context.user_id
    trip = await find_trip(runtime.store, user, trip_id)
    if trip is None:
        return f"No trip {trip_id!r}. Saved trips are listed in your instructions."
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
    user = runtime.context.user_id
    trip = await find_trip(runtime.store, user, trip_id)
    if trip is None:
        return f"No trip {trip_id!r}."
    await runtime.store.adelete(namespace(user), trip.trip_id)
    return f"Forgot trip {trip.trip_id} ({trip.destination})."


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


def _merge(into: Trip, other: Trip) -> None:
    """Fold a duplicate trip into `into`: the newer record wins scalar fields, lists combine."""
    newer, older = (other, into) if other.updated_at > into.updated_at else (into, other)
    for field in ("destination", "start_date", "end_date", "travellers", "budget"):
        setattr(into, field, getattr(newer, field) or getattr(older, field))
    into.itinerary_thread = newer.itinerary_thread or older.itinerary_thread
    into.source = into.source or other.source
    for field in ("lodging", "transport", "notes", "aliases"):
        _add_unique(getattr(into, field), getattr(other, field))
    into.updated_at = max(into.updated_at, other.updated_at)


async def migrate_trip_ids(store: BaseStore) -> int:
    """Re-key trips saved under old destination-based ids, merging duplicates of the same trip.

    Old ids are kept as aliases and linked itineraries are repointed. Idempotent: trips that
    already have an opaque id are left alone. Returns the number of trips migrated.
    """
    migrated = 0
    for ns in await store.alist_namespaces(suffix=("trips",), limit=10_000):
        user_id = ns[0].replace("%2E", ".")
        trips = await load_trips(store, user_id)
        old = sorted((t for t in trips if not TRIP_ID.match(t.trip_id)), key=lambda t: t.updated_at)
        if not old:
            continue
        kept = [t for t in trips if TRIP_ID.match(t.trip_id)]
        renamed: dict[str, str] = {}
        for trip in old:
            old_id = trip.trip_id
            target = match_trip(kept, trip.destination, trip.start_date, trip.end_date)
            if target is None:
                trip.trip_id = new_trip_id()
                trip.aliases = [*trip.aliases, old_id]
                kept.append(trip)
            else:
                trip.aliases = [*trip.aliases, old_id]
                _merge(target, trip)
            renamed[old_id] = (target or trip).trip_id
            migrated += 1
        for trip in kept:
            await store.aput(ns, trip.trip_id, trip.model_dump(mode="json"))
        for old_id in renamed:
            await store.adelete(ns, old_id)
        plans = itineraries_namespace(user_id)
        for item in await store.asearch(plans, limit=1000):
            if item.value.get("trip_id") in renamed:
                value = {**item.value, "trip_id": renamed[item.value["trip_id"]]}
                await store.aput(plans, item.key, value)
    return migrated


TRIP_TOOLS = [save_trip_details, get_trip, delete_trip]

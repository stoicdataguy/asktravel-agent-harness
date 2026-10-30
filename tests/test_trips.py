import itertools
from datetime import date

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from tests.fakes import FakeToolModel
from tests.test_itinerary import PLAN
from travel_agent.agent.graph import build_agent
from travel_agent.context import UserContext
from travel_agent.tools import trips as trips_module
from travel_agent.tools.trips import (
    TRIP_ID,
    Trip,
    backfill_trips,
    format_trips,
    itineraries_namespace,
    load_trips,
    match_trip,
    migrate_trip_ids,
    namespace,
    new_trip_id,
    trip_status,
    upsert_trip,
)

ANA = "ana@example.com"
TODAY = date(2026, 10, 29)


@pytest.fixture(autouse=True)
def predictable_ids(monkeypatch):
    """New trips get trip-000001, trip-000002, ... so tests can refer to them."""
    counter = itertools.count(1)
    monkeypatch.setattr(trips_module, "new_trip_id", lambda: f"trip-{next(counter):06d}")


def call(name: str, **args) -> AIMessage:
    return AIMessage("", tool_calls=[{"name": name, "args": args, "id": f"call_{name}"}])


def make_agent(store, *replies):
    model = FakeToolModel(messages=iter(replies), disable_streaming=True)
    return build_agent(InMemorySaver(), store, model=model), model


async def ask(agent, chat: str, user: str = ANA, text: str = "hi"):
    return await agent.ainvoke(
        {"messages": [HumanMessage(text)]},
        {"configurable": {"thread_id": f"{user}:{chat}"}},
        context=UserContext(user),
    )


def test_new_trip_ids_are_opaque_and_distinct():
    ids = {new_trip_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(TRIP_ID.match(i) for i in ids)


async def test_upsert_merges_details():
    store = InMemoryStore()
    first = await upsert_trip(
        store, ANA, destination="Cusco", lodging=["Casa Andina"], budget="$2000"
    )
    trip = await upsert_trip(
        store,
        ANA,
        trip_id=first.trip_id,
        destination="Cusco",
        lodging=["casa andina", "Sacred Valley lodge"],  # duplicate ignored, case-insensitive
        notes=["altitude: take it slow"],
    )
    assert trip.lodging == ["Casa Andina", "Sacred Valley lodge"]
    assert trip.budget == "$2000"  # kept: not overwritten by an empty value
    assert trip.notes == ["altitude: take it slow"]


def test_format_trips_skips_long_past_trips():
    trips = [
        Trip(
            trip_id="big-island-hawaii-2026-12",
            destination="Big Island, Hawaii",
            start_date=date(2026, 12, 28),
            end_date=date(2027, 1, 2),
            travellers="2 adults",
            lodging=["Royal Kona Resort, Kailua-Kona"],
            itinerary_thread="t1",
        ),
        Trip(trip_id="old", destination="Rome", end_date=date(2026, 1, 5)),
    ]
    text = format_trips(trips, today=date(2026, 10, 28))
    assert text == (
        "- [big-island-hawaii-2026-12] Big Island, Hawaii · 2026-12-28 → 2027-01-02 "
        "(upcoming, in 61 days) · 2 adults · staying: Royal Kona Resort, Kailua-Kona"
        " · itinerary saved"
    )
    undated = format_trips([Trip(trip_id="trip-000001", destination="Peru")], today=TODAY)
    assert undated == "- [trip-000001] Peru · (dates not set)"
    assert format_trips([]) == "(no trips saved yet)"


async def test_present_itinerary_files_plan_under_a_trip():
    store = InMemoryStore()
    plan = {**PLAN, "start_date": "2026-12-28"}
    agent, _ = make_agent(
        store,
        AIMessage(
            "", tool_calls=[{"name": "present_itinerary", "args": {"itinerary": plan}, "id": "c"}]
        ),
        AIMessage("Want changes?"),
    )
    await ask(agent, "c1")
    [trip] = await load_trips(store, ANA)
    assert trip.trip_id == "trip-000001"
    assert trip.source == f"{ANA}:c1"
    assert (trip.start_date, trip.end_date) == (date(2026, 12, 28), date(2026, 12, 29))
    assert trip.lodging == ["Kailua-Kona"]
    assert trip.itinerary_thread == f"{ANA}:c1"
    saved = await store.aget(itineraries_namespace(ANA), f"{ANA}:c1")
    assert saved.value["trip_id"] == trip.trip_id


async def test_trip_saved_in_one_chat_is_in_the_next_chats_prompt():
    store = InMemoryStore()
    agent, model = make_agent(
        store,
        call(
            "save_trip_details", destination="Cusco", start_date="2027-05-10", lodging="Casa Andina"
        ),
        AIMessage("Noted."),
        AIMessage("Day trips: ..."),
    )
    await ask(agent, "chat-1", text="I'll stay at Casa Andina in Cusco from May 10")
    await ask(agent, "chat-2", text="Day trips from my hotel?")
    assert "- [trip-000001] Cusco · 2027-05-10 → ? (upcoming, in " in model.prompts[-1]
    assert "staying: Casa Andina" in model.prompts[-1]


async def test_get_trip_returns_saved_itinerary():
    store = InMemoryStore()
    agent, _ = make_agent(
        store,
        AIMessage(
            "", tool_calls=[{"name": "present_itinerary", "args": {"itinerary": PLAN}, "id": "c"}]
        ),
        AIMessage("ok"),
        call("get_trip", trip_id="trip-000001"),
        AIMessage("Here it is"),
    )
    await ask(agent, "c1")
    result = await ask(agent, "c2", text="what was day 1 of my plan?")
    tool_message = [m for m in result["messages"] if m.type == "tool"][-1]
    assert "## Day 1 · Kona coast" in tool_message.content


async def test_trips_are_per_user_and_deletable():
    store = InMemoryStore()
    agent, model = make_agent(
        store,
        call("save_trip_details", destination="Cusco"),
        AIMessage("ok"),
        AIMessage("hi bo"),
        call("delete_trip", trip_id="trip-000001"),
        AIMessage("forgotten"),
    )
    await ask(agent, "c1")
    await ask(agent, "c1", user="bo@example.com")
    assert "(no trips saved yet)" in model.prompts[-1]
    await ask(agent, "c2", text="forget my Peru trip")
    assert await load_trips(store, ANA) == []


async def test_backfill_creates_trips_for_old_itineraries_once():
    store = InMemoryStore()
    await store.aput(itineraries_namespace(ANA), f"{ANA}:old", {"itinerary": PLAN})
    assert await backfill_trips(store) == 1
    assert await backfill_trips(store) == 0
    [trip] = await load_trips(store, ANA)
    assert (trip.destination, trip.itinerary_thread) == ("Big Island, Hawaii", f"{ANA}:old")


def test_trip_status_is_relative_to_today():
    def status(start=None, end=None):
        return trip_status(trip("t", "x", start, end), TODAY)

    assert status() == "dates not set"
    assert status(date(2026, 11, 23), date(2026, 11, 29)) == "upcoming, in 25 days"
    assert status(date(2026, 10, 30)) == "upcoming, tomorrow"
    assert status(date(2026, 10, 25), date(2026, 11, 2)) == "travelling now"
    assert status(date(2026, 10, 29)) == "travelling now"  # no end date: just that day
    assert status(date(2026, 10, 20), date(2026, 10, 26)) == "ended 3 days ago"


def trip(trip_id, destination, start=None, end=None):
    return Trip(trip_id=trip_id, destination=destination, start_date=start, end_date=end)


def test_match_trip_by_destination_words_and_dates():
    hawaii = trip("trip-aaaaaa", "Hawaiʻi, the Big Island", date(2026, 12, 28), date(2027, 1, 2))
    rome = trip("trip-bbbbbb", "Rome, Italy", date(2026, 11, 23), date(2026, 11, 29))
    trips = [hawaii, rome]
    assert match_trip(trips, "Kona, Hawaii's Big Island", today=TODAY) is hawaii
    assert match_trip(trips, "Big Island", date(2026, 12, 30), today=TODAY) is hawaii
    assert match_trip(trips, "Rome", today=TODAY) is rome
    # Same words, but dates that don't overlap: a different trip
    assert match_trip(trips, "Rome, Italy", date(2027, 4, 10), today=TODAY) is None
    # One shared country word isn't enough
    assert match_trip(trips, "Florence, Italy", today=TODAY) is None


def test_match_trip_skips_past_trips_and_ambiguity():
    past = trip("trip-aaaaaa", "Rome", date(2026, 3, 1), date(2026, 3, 5))
    assert match_trip([past], "Rome", today=TODAY) is None
    two = [trip("trip-aaaaaa", "Peru"), trip("trip-bbbbbb", "Cusco, Peru")]
    assert match_trip(two, "Peru", today=TODAY) is None


async def test_upsert_without_id_reuses_the_matching_trip():
    store = InMemoryStore()
    first = await upsert_trip(store, ANA, destination="Europe")
    # The model renames the destination and adds dates: same id, nothing stale
    rome = await upsert_trip(
        store, ANA, trip_id=first.trip_id, destination="Rome, Italy", start_date=date(2027, 4, 10)
    )
    assert rome.trip_id == first.trip_id == "trip-000001"
    # Later chats describe it again without the id, or with an id the model made up
    again = await upsert_trip(store, ANA, destination="Rome", lodging=["Hotel Artemide"])
    made_up = await upsert_trip(store, ANA, trip_id="rome-2027-04", destination="Rome")
    assert again.trip_id == made_up.trip_id == "trip-000001"
    [saved] = await load_trips(store, ANA)
    assert (saved.destination, saved.lodging) == ("Rome", ["Hotel Artemide"])


async def test_migration_rekeys_merges_duplicates_and_repoints_itineraries():
    store = InMemoryStore()
    ns = namespace(ANA)
    old = [
        ("hawaii-the-big-island-undated", "Hawaii, the Big Island", ["Royal Kona Resort"], "1"),
        ("kona-big-island-hawaii-undated", "Kona, Big Island, Hawaii", ["rental car"], "2"),
        ("international-destination-tbd-undated", "Rome, Italy", [], "3"),
    ]
    for key, destination, lodging, updated in old:
        value = Trip(trip_id=key, destination=destination, lodging=lodging, updated_at=updated)
        await store.aput(ns, key, value.model_dump(mode="json"))
    plan = {"itinerary": PLAN, "trip_id": "kona-big-island-hawaii-undated"}
    await store.aput(itineraries_namespace(ANA), f"{ANA}:c1", plan)

    assert await migrate_trip_ids(store) == 3
    assert await migrate_trip_ids(store) == 0  # idempotent
    trips = {t.destination: t for t in await load_trips(store, ANA)}
    assert set(trips) == {"Kona, Big Island, Hawaii", "Rome, Italy"}  # newer name wins
    hawaii, rome = trips["Kona, Big Island, Hawaii"], trips["Rome, Italy"]
    assert all(TRIP_ID.match(t.trip_id) for t in trips.values())
    assert hawaii.aliases == ["hawaii-the-big-island-undated", "kona-big-island-hawaii-undated"]
    assert hawaii.lodging == ["Royal Kona Resort", "rental car"]
    assert rome.aliases == ["international-destination-tbd-undated"]
    saved = await store.aget(itineraries_namespace(ANA), f"{ANA}:c1")
    assert saved.value["trip_id"] == hawaii.trip_id


async def test_old_ids_still_resolve_after_migration():
    store = InMemoryStore()
    old = Trip(trip_id="cusco-undated", destination="Cusco")
    await store.aput(namespace(ANA), "cusco-undated", old.model_dump(mode="json"))
    await migrate_trip_ids(store)
    agent, _ = make_agent(
        store,
        call("get_trip", trip_id="cusco-undated"),
        AIMessage("ok"),
        call("delete_trip", trip_id="cusco-undated"),
        AIMessage("forgotten"),
    )
    result = await ask(agent, "c1")
    assert "Cusco" in [m for m in result["messages"] if m.type == "tool"][-1].content
    await ask(agent, "c1", text="forget it")
    assert await load_trips(store, ANA) == []

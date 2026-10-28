from datetime import date

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from tests.fakes import FakeToolModel
from tests.test_itinerary import PLAN
from travel_agent.agent.graph import build_agent
from travel_agent.context import UserContext
from travel_agent.tools.trips import (
    Trip,
    backfill_trips,
    format_trips,
    itineraries_namespace,
    load_trips,
    make_trip_id,
    upsert_trip,
)

ANA = "ana@example.com"


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


def test_make_trip_id():
    assert make_trip_id("Big Island, Hawaii", date(2026, 12, 28)) == "big-island-hawaii-2026-12"
    assert make_trip_id("Cusco", None) == "cusco-undated"
    assert make_trip_id("Hawaiʻi, the Big Island", None) == "hawaii-the-big-island-undated"


async def test_upsert_merges_details():
    store = InMemoryStore()
    await upsert_trip(store, ANA, destination="Cusco", lodging=["Casa Andina"], budget="$2000")
    trip = await upsert_trip(
        store,
        ANA,
        trip_id="cusco-undated",
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
        "- [big-island-hawaii-2026-12] Big Island, Hawaii · 2026-12-28 → 2027-01-02 · 2 adults"
        " · staying: Royal Kona Resort, Kailua-Kona · itinerary saved"
    )
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
    assert trip.trip_id == "big-island-hawaii-2026-12"
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
    assert "- [cusco-2027-05] Cusco · 2027-05-10 → ? · staying: Casa Andina" in model.prompts[-1]


async def test_get_trip_returns_saved_itinerary():
    store = InMemoryStore()
    agent, _ = make_agent(
        store,
        AIMessage(
            "", tool_calls=[{"name": "present_itinerary", "args": {"itinerary": PLAN}, "id": "c"}]
        ),
        AIMessage("ok"),
        call("get_trip", trip_id="big-island-hawaii-undated"),
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
        call("delete_trip", trip_id="cusco-undated"),
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

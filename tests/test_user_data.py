import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from tests.fakes import FakeToolModel
from tests.test_itinerary import PLAN
from travel_agent import user_data
from travel_agent.agent.graph import build_agent
from travel_agent.api.app import app
from travel_agent.context import UserContext
from travel_agent.memory import user_namespace

ANA, BO = "ana.lee@example.com", "bo@example.com"


async def seed() -> tuple[InMemoryStore, InMemorySaver]:
    """Two users, each with a fact, a trip, a saved plan and a chat."""
    store, saver = InMemoryStore(), InMemorySaver()
    replies = iter([AIMessage("Hello Ana"), AIMessage("Hello Bo")])
    agent = build_agent(saver, store, model=FakeToolModel(messages=replies))
    for user in (ANA, BO):
        await store.aput(user_namespace(user, "memories"), "diet", {"value": "vegetarian"})
        trip = {"trip_id": "trip-aaaaaa", "destination": "Rome"}
        await store.aput(user_namespace(user, "trips"), "trip-aaaaaa", trip)
        plan = {"itinerary": PLAN, "trip_id": "trip-aaaaaa"}
        await store.aput(user_namespace(user, "itineraries"), f"{user}:c1", plan)
        await agent.ainvoke(
            {"messages": [HumanMessage(f"hi from {user}")]},
            {"configurable": {"thread_id": f"{user}:c1"}},
            context=UserContext(user),
        )
    return store, saver


async def test_view_lists_facts_trips_plans_and_chats():
    store, saver = await seed()
    view = await user_data.remembered(store, saver, ANA)
    assert view["facts"] == {"diet": {"value": "vegetarian"}}
    assert view["trips"] == [{"trip_id": "trip-aaaaaa", "destination": "Rome"}]
    assert view["itineraries"] == [
        {"thread_id": f"{ANA}:c1", "title": PLAN["title"], "trip_id": "trip-aaaaaa"}
    ]
    [chat] = view["chats"]
    assert (chat["thread_id"], chat["first_message"]) == (f"{ANA}:c1", f"hi from {ANA}")


async def test_export_has_every_item_and_message():
    store, saver = await seed()
    data = await user_data.export(store, saver, ANA)
    assert set(data["store"]) >= {"memories", "trips", "itineraries"}
    assert [m["content"] for m in data["chats"][f"{ANA}:c1"]] == [f"hi from {ANA}", "Hello Ana"]


async def test_delete_all_removes_one_user_only():
    store, saver = await seed()
    deleted = await user_data.delete_all(store, saver, ANA)
    assert deleted["chats"] == 1 and deleted["store_items"] >= 3
    assert await user_data.store_items(store, ANA) == {}
    assert await user_data.thread_ids(saver, ANA) == []
    # Bo is untouched
    assert (await user_data.store_items(store, BO))["memories"] == {"diet": {"value": "vegetarian"}}
    assert await user_data.thread_ids(saver, BO) == [f"{BO}:c1"]


@pytest.fixture
async def client():
    app.state.store, app.state.checkpointer = await seed()
    return TestClient(app)  # no `with`: skips the lifespan, so no Postgres


async def test_memory_endpoints(client):
    headers = {"X-OpenWebUI-User-Email": "Ana.Lee@Example.com"}
    assert client.get("/api/memory", headers=headers).json()["facts"]["diet"]["value"]
    assert f"{ANA}:c1" in client.get("/api/memory/export", headers=headers).json()["chats"]
    # Delete needs the email repeated
    assert client.delete("/api/memory", headers=headers).status_code == 400
    response = client.delete("/api/memory", params={"confirm": ANA}, headers=headers)
    assert response.json()["deleted"]["chats"] == 1
    assert client.get("/api/memory", headers=headers).json()["facts"] == {}

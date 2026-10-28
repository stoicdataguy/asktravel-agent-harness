import json

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from pydantic import ValidationError

from tests.fakes import FakeToolModel
from travel_agent.agent.graph import build_agent
from travel_agent.agent.schemas import Itinerary, render_itinerary
from travel_agent.api.app import app
from travel_agent.api.openai_compat import tool_status
from travel_agent.context import UserContext
from travel_agent.tools.itinerary import list_itineraries

PLAN = {
    "title": "2 days on the Big Island",
    "destination": "Big Island, Hawaii",
    "trip_length_days": 2,
    "travellers": "2 adults",
    "summary": "Kona snorkelling, then the volcano.",
    "stays": [{"area": "Kailua-Kona", "nights": 2, "why": "Central for the west coast"}],
    "days": [
        {
            "day": 1,
            "title": "Kona coast",
            "base": "Kailua-Kona",
            "activities": [
                {
                    "time_of_day": "evening",
                    "title": "Manta ray night snorkel",
                    "details": "Book a boat from Keauhou Bay.",
                    "location": "Keauhou Bay",
                    "source": "Lonely Planet Hawaii, p. 90",
                }
            ],
        },
        {
            "day": 2,
            "title": "Volcanoes",
            "activities": [
                {"time_of_day": "full day", "title": "Hawaiʻi Volcanoes NP", "details": "Hike."}
            ],
        },
    ],
    "bookings_to_make": ["Manta snorkel tour"],
    "caveats": ["Guidebook prices are from 2016"],
}


def present(plan=PLAN) -> AIMessage:
    call = {"name": "present_itinerary", "args": {"itinerary": plan}, "id": "call_it"}
    return AIMessage("", tool_calls=[call])


def test_schema_rejects_misnumbered_or_mismatched_days():
    with pytest.raises(ValidationError, match="numbered 1..N"):
        Itinerary(**{**PLAN, "days": [PLAN["days"][1] | {"day": 2}]} | {"trip_length_days": 1})
    with pytest.raises(ValidationError, match="trip_length_days is 3"):
        Itinerary(**{**PLAN, "trip_length_days": 3})


def test_render_itinerary():
    md = render_itinerary(Itinerary(**PLAN))
    assert md.startswith("# 2 days on the Big Island")
    assert "*Big Island, Hawaii · 2 days · 2 adults*" in md
    assert "## Day 1 · Kona coast" in md
    assert (
        "- **Evening: Manta ray night snorkel** (Keauhou Bay): Book a boat from Keauhou Bay. "
        "*(Lonely Planet Hawaii, p. 90)*"
    ) in md
    assert "- **Sleep:** Kailua-Kona" in md
    assert "**Where to stay**\n\n- **Kailua-Kona** (2 nights): Central for the west coast" in md
    assert "## Book ahead\n\n- Manta snorkel tour" in md


def test_tool_status_truncates_long_args():
    status = tool_status({"name": "present_itinerary", "args": {"itinerary": PLAN}})
    assert len(status) < 140
    assert status.strip().endswith("…)`")


async def test_present_itinerary_saves_structured_plan():
    store = InMemoryStore()
    model = FakeToolModel(messages=iter([present(), AIMessage("Want a slower pace?")]))
    model.disable_streaming = True
    agent = build_agent(InMemorySaver(), store, model=model)
    result = await agent.ainvoke(
        {"messages": [HumanMessage("plan it")]},
        {"configurable": {"thread_id": "ana@example.com:c1"}},
        context=UserContext("ana@example.com"),
    )
    tool_message = next(m for m in result["messages"] if m.type == "tool")
    assert tool_message.artifact.startswith("# 2 days on the Big Island")
    assert "Don't summarize or repeat" in tool_message.content

    [saved] = await list_itineraries(store, "ana@example.com")
    assert saved["thread_id"] == "ana@example.com:c1"
    assert Itinerary(**saved["itinerary"]) == Itinerary(**PLAN)


async def test_invalid_itinerary_goes_back_to_the_model():
    bad = {**PLAN, "trip_length_days": 5}
    model = FakeToolModel(messages=iter([present(bad), present(), AIMessage("Done")]))
    model.disable_streaming = True
    store = InMemoryStore()
    agent = build_agent(InMemorySaver(), store, model=model)
    result = await agent.ainvoke(
        {"messages": [HumanMessage("plan it")]},
        {"configurable": {"thread_id": "t"}},
        context=UserContext("ana@example.com"),
    )
    first, second = [m for m in result["messages"] if m.type == "tool"]
    assert first.status == "error" and "trip_length_days is 5" in first.content
    assert second.artifact.startswith("# 2 days")
    assert len(await list_itineraries(store, "ana@example.com")) == 1


@pytest.fixture
def client():
    store = InMemoryStore()
    model = FakeToolModel(
        messages=iter([present(), AIMessage("Want a slower pace?")] * 2), disable_streaming=True
    )
    app.state.store = store
    app.state.agent = build_agent(InMemorySaver(), store, model=model)
    return TestClient(app)


def chat(client, stream: bool):
    return client.post(
        "/v1/chat/completions",
        json={
            "model": "travel-agent",
            "stream": stream,
            "messages": [{"role": "user", "content": "x"}],
        },
        headers={"X-OpenWebUI-User-Email": "ana@example.com", "X-OpenWebUI-Chat-Id": "c1"},
    )


def test_stream_shows_rendered_itinerary(client):
    events = [e for e in chat(client, stream=True).text.splitlines() if e.startswith("data: {")]
    text = "".join(json.loads(e[6:])["choices"][0]["delta"].get("content", "") for e in events)
    assert "`🔧 present_itinerary(itinerary=" in text
    assert "## Day 2 · Volcanoes" in text
    assert text.rstrip().endswith("Want a slower pace?")


def test_non_stream_includes_itinerary_and_api_lists_it(client):
    content = chat(client, stream=False).json()["choices"][0]["message"]["content"]
    assert content.startswith("# 2 days on the Big Island")
    assert content.endswith("Want a slower pace?")

    data = client.get("/api/itineraries", headers={"X-OpenWebUI-User-Email": "ana@example.com"})
    [entry] = data.json()["data"]
    assert entry["itinerary"]["days"][0]["title"] == "Kona coast"

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from tests.fakes import FakeToolModel
from travel_agent.agent.graph import build_agent
from travel_agent.context import UserContext
from travel_agent.tools.memory import (
    MAX_LIST_ITEMS,
    format_memories,
    load_memories,
    migrate_memories,
    namespace,
    normalize_key,
)


def call(name: str, **args) -> AIMessage:
    return AIMessage("", tool_calls=[{"name": name, "args": args, "id": f"call_{name}"}])


def make_agent(store, *replies):
    model = FakeToolModel(messages=iter(replies), disable_streaming=True)
    return build_agent(InMemorySaver(), store, model=model), model


async def ask(agent, user: str, thread: str, text: str = "hi"):
    return await agent.ainvoke(
        {"messages": [HumanMessage(text)]},
        {"configurable": {"thread_id": f"{user}:{thread}"}},
        context=UserContext(user),
    )


def test_normalize_key():
    assert normalize_key("Home Airport!") == "home_airport"


def test_namespace_encodes_dots_in_email():
    assert namespace("ana.b@example.com") == ("ana%2Eb@example%2Ecom", "memories")


def test_format_memories():
    assert format_memories({}) == "(nothing saved yet)"
    assert format_memories({"diet": "vegetarian"}) == "- diet: vegetarian"


async def test_saved_fact_reaches_prompt_in_a_new_conversation():
    store = InMemoryStore()
    agent, model = make_agent(
        store,
        call("save_memory", key="Diet", value="vegetarian"),
        AIMessage("Noted!"),
        AIMessage("Here are some ideas."),
    )
    await ask(agent, "ana@example.com", "chat-1", "I'm vegetarian")
    assert (await store.aget(namespace("ana@example.com"), "diet")).value["value"] == "vegetarian"

    await ask(agent, "ana@example.com", "chat-2", "Dinner ideas in Rome?")
    assert "- diet: vegetarian" in model.prompts[-1]


async def test_memories_are_per_user():
    store = InMemoryStore()
    agent, model = make_agent(
        store,
        call("save_memory", key="home_airport", value="SFO"),
        AIMessage("Ok"),
        AIMessage("Hi"),
    )
    await ask(agent, "ana@example.com", "c1")
    await ask(agent, "bo@example.com", "c1")
    assert "home_airport" not in model.prompts[-1]
    assert "(nothing saved yet)" in model.prompts[-1]


async def test_saving_same_key_updates_and_delete_forgets():
    store = InMemoryStore()
    agent, _ = make_agent(
        store,
        call("save_memory", key="budget_level", value="mid-range"),
        call("save_memory", key="budget level", value="luxury"),
        AIMessage("Updated"),
        call("delete_memory", key="budget_level"),
        AIMessage("Forgotten"),
    )
    await ask(agent, "ana@example.com", "c1")
    assert await load_memories(store, "ana@example.com") == {"budget_level": "luxury"}

    await ask(agent, "ana@example.com", "c1", "forget my budget")
    assert await load_memories(store, "ana@example.com") == {}


async def test_delete_unknown_key_reports_nothing_saved():
    store = InMemoryStore()
    agent, _ = make_agent(store, call("delete_memory", key="pets"), AIMessage("Ok"))
    result = await ask(agent, "ana@example.com", "c1")
    tool_message = next(m for m in result["messages"] if m.type == "tool")
    assert tool_message.content == "Nothing saved under 'pets'."


def tool_results(result) -> list[str]:
    return [m.content for m in result["messages"] if m.type == "tool"]


async def test_list_key_adds_items_and_dedupes():
    store = InMemoryStore()
    agent, _ = make_agent(
        store,
        call("save_memory", key="interests", value="astronomy"),
        call("save_memory", key="Interests", value="Astronomy; hiking"),
        call("save_memory", key="interests", value="hiking"),
        AIMessage("Noted"),
    )
    result = await ask(agent, "ana@example.com", "c1")
    assert tool_results(result) == [
        "Added to interests: astronomy. interests now: astronomy",
        "Added to interests: hiking. Already saved: Astronomy. interests now: astronomy; hiking",
        "No change to interests. Already saved: hiking. interests now: astronomy; hiking",
    ]
    saved = (await store.aget(namespace("ana@example.com"), "interests")).value
    assert saved["items"] == ["astronomy", "hiking"]
    assert await load_memories(store, "ana@example.com") == {"interests": "astronomy; hiking"}


async def test_list_key_caps_item_count():
    store = InMemoryStore()
    many = "; ".join(f"thing {i}" for i in range(MAX_LIST_ITEMS + 2))
    agent, _ = make_agent(store, call("save_memory", key="likes", value=many), AIMessage("Ok"))
    result = await ask(agent, "ana@example.com", "c1")
    saved = (await store.aget(namespace("ana@example.com"), "likes")).value
    assert len(saved["items"]) == MAX_LIST_ITEMS
    assert f"Not added (list full at {MAX_LIST_ITEMS} items" in tool_results(result)[0]


async def test_overwriting_scalar_reports_previous_value():
    store = InMemoryStore()
    agent, _ = make_agent(
        store,
        call("save_memory", key="diet", value="vegetarian"),
        call("save_memory", key="diet", value="pescatarian, no meat"),
        call("save_memory", key="diet", value="Pescatarian, no meat"),
        AIMessage("Updated"),
    )
    result = await ask(agent, "ana@example.com", "c1")
    assert tool_results(result) == [
        "Saved diet: vegetarian",
        "Updated diet (was: vegetarian): pescatarian, no meat",
        "No change: diet is already pescatarian, no meat",
    ]


async def test_facts_record_source_thread_and_confirmation():
    store = InMemoryStore()
    agent, _ = make_agent(
        store,
        call("save_memory", key="diet", value="vegetarian"),
        AIMessage("Ok"),
        call("save_memory", key="diet", value="vegetarian"),
        AIMessage("Ok"),
    )
    await ask(agent, "ana@example.com", "c1")
    first = (await store.aget(namespace("ana@example.com"), "diet")).value
    assert first["source"] == "ana@example.com:c1"
    assert first["confirmed_at"] == first["updated_at"]

    await ask(agent, "ana@example.com", "c2")
    again = (await store.aget(namespace("ana@example.com"), "diet")).value
    assert again["source"] == "ana@example.com:c2"
    assert again["updated_at"] == first["updated_at"]  # unchanged value keeps its update time
    assert again["confirmed_at"] >= first["confirmed_at"]  # equal if both saves share a µs


async def test_delete_one_item_from_a_list():
    store = InMemoryStore()
    agent, _ = make_agent(
        store,
        call("save_memory", key="likes", value="museums; street food"),
        call("delete_memory", key="likes", item="Museums"),
        call("delete_memory", key="likes", item="opera"),
        call("delete_memory", key="likes", item="street food"),
        AIMessage("Ok"),
    )
    result = await ask(agent, "ana@example.com", "c1")
    assert tool_results(result)[1:] == [
        "Removed Museums from likes. likes now: street food",
        "'opera' isn't in likes. likes holds: street food",
        "Removed street food from likes; likes is now empty, so it was forgotten.",
    ]
    assert await load_memories(store, "ana@example.com") == {}


async def test_migrate_memories_splits_old_list_strings():
    store = InMemoryStore()
    ns = namespace("ana.b@example.com")
    old = "Big astronomy nerd; owns a smart telescope;  adventure sports ; big astronomy nerd"
    await store.aput(ns, "interests", {"value": old, "updated_at": "2026-10-01T00:00:00+00:00"})
    await store.aput(ns, "diet", {"value": "vegan; no nuts", "updated_at": "2026-10-01"})

    assert await migrate_memories(store) == 1
    migrated = (await store.aget(ns, "interests")).value
    assert migrated["items"] == ["Big astronomy nerd", "owns a smart telescope", "adventure sports"]
    assert migrated["value"] == "Big astronomy nerd; owns a smart telescope; adventure sports"
    assert migrated["updated_at"] == "2026-10-01T00:00:00+00:00"
    assert migrated["source"] is None
    assert (await store.aget(ns, "diet")).value["value"] == "vegan; no nuts"

    assert await migrate_memories(store) == 0
    assert (await store.aget(ns, "interests")).value == migrated


async def test_sensitive_facts_are_marked_and_stay_marked():
    store = InMemoryStore()
    agent, _ = make_agent(
        store,
        call("save_memory", key="mobility", value="uses a wheelchair", sensitive=True),
        AIMessage("Noted"),
        call("save_memory", key="mobility", value="uses a wheelchair, can walk short distances"),
        AIMessage("Updated"),
    )
    await ask(agent, "ana@example.com", "c1")
    await ask(agent, "ana@example.com", "c2")
    saved = (await store.aget(namespace("ana@example.com"), "mobility")).value
    assert saved["value"] == "uses a wheelchair, can walk short distances"
    assert saved["sensitive"] is True  # an update without the flag doesn't unmark it

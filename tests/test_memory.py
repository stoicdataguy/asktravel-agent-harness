from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from tests.fakes import FakeToolModel
from travel_agent.agent.graph import build_agent
from travel_agent.context import UserContext
from travel_agent.tools.memory import format_memories, load_memories, namespace, normalize_key


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

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from tests.fakes import FakeToolModel
from travel_agent.agent.graph import build_agent
from travel_agent.context import UserContext
from travel_agent.tools.conversations import (
    backfill_chat_index,
    chats_namespace,
    matching_turns,
    query_words,
)

ANA = "ana@example.com"


def recall(query: str) -> AIMessage:
    call = {"name": "recall_conversations", "args": {"query": query}, "id": "r1"}
    return AIMessage("", tool_calls=[call])


async def ask(agent, chat: str, text: str, user: str = ANA):
    return await agent.ainvoke(
        {"messages": [HumanMessage(text)]},
        {"configurable": {"thread_id": f"{user}:{chat}"}},
        context=UserContext(user),
    )


def make_agent(*replies):
    store, saver = InMemoryStore(), InMemorySaver()
    model = FakeToolModel(messages=iter(replies), disable_streaming=True)
    return build_agent(saver, store, model=model), store, saver


def tool_result(result) -> str:
    return [m for m in result["messages"] if isinstance(m, ToolMessage)][-1].text


def test_query_words_drop_filler():
    assert query_words("Which gelato shops did you recommend in Rome?") == {
        "gelato",
        "shops",
        "rome",
    }


def test_matching_turns_scores_question_and_answer():
    messages = [
        HumanMessage("Recommend 3 gelato shops in Rome"),
        AIMessage("Fatamorgana, Fior di Luna, Gelateria del Teatro"),
        HumanMessage("And pizza?"),
        AIMessage("Pizzarium."),
    ]
    [(score, question, answer)] = matching_turns(messages, {"gelato", "rome"})
    assert (score, question) == (2, "Recommend 3 gelato shops in Rome")
    assert answer.startswith("Fatamorgana")


async def test_each_run_indexes_its_chat():
    agent, store, _ = make_agent(AIMessage("Fatamorgana, Fior di Luna."))
    await ask(agent, "c1", "Recommend gelato shops in Rome, names only")
    [item] = await store.asearch(chats_namespace(ANA))
    assert item.key == f"{ANA}:c1"
    assert item.value["title"] == "Recommend gelato shops in Rome, names only"


async def test_recall_finds_an_earlier_chat_but_not_the_current_one():
    agent, _, _ = make_agent(
        AIMessage("Fatamorgana, Fior di Luna, Gelateria del Teatro"),
        recall("gelato Rome"),
        AIMessage("They were Fatamorgana, ..."),
        recall("gelato"),
        AIMessage("Nothing"),
    )
    await ask(agent, "c1", "Recommend 3 gelato shops in Rome")
    found = tool_result(await ask(agent, "c2", "Which gelato shops did you recommend?"))
    assert 'Chat "Recommend 3 gelato shops in Rome"' in found
    assert "You: Fatamorgana, Fior di Luna, Gelateria del Teatro" in found
    # Another user's chats are never searched
    other = tool_result(await ask(agent, "c9", "gelato?", user="bo@example.com"))
    assert other == "No earlier chats mention 'gelato'."


async def test_backfill_indexes_chats_saved_before_the_index():
    agent, store, saver = make_agent(AIMessage("Hello!"))
    await ask(agent, "old", "Hi there")
    await store.adelete(chats_namespace(ANA), f"{ANA}:old")  # as if saved before M3

    assert await backfill_chat_index(saver, store) == 1
    assert await backfill_chat_index(saver, store) == 0
    [item] = await store.asearch(chats_namespace(ANA))
    assert (item.key, item.value["title"]) == (f"{ANA}:old", "Hi there")

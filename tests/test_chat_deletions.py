from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from tests.fakes import FakeToolModel
from travel_agent.agent.graph import build_agent
from travel_agent.chat_deletions import Deletion, forget_chats
from travel_agent.context import UserContext
from travel_agent.tools.conversations import chats_namespace

ANA = "ana.lee@example.com"


async def test_deleted_chat_loses_its_thread_and_index_entry_only():
    store, saver = InMemoryStore(), InMemorySaver()
    agent = build_agent(saver, store, model=FakeToolModel(messages=iter([AIMessage("a")] * 2)))
    for chat in ("gone", "kept"):
        await agent.ainvoke(
            {"messages": [HumanMessage("hi")]},
            {"configurable": {"thread_id": f"{ANA}:{chat}"}},
            context=UserContext(ANA),
        )

    await forget_chats([Deletion(id=1, user_email=ANA, chat_id="gone")], saver, store)

    assert await saver.aget_tuple({"configurable": {"thread_id": f"{ANA}:gone"}}) is None
    assert await saver.aget_tuple({"configurable": {"thread_id": f"{ANA}:kept"}}) is not None
    assert [i.key for i in await store.asearch(chats_namespace(ANA))] == [f"{ANA}:kept"]

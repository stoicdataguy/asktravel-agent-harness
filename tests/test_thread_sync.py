import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from tests.fakes import FakeToolModel
from travel_agent.agent.graph import build_agent
from travel_agent.api.thread_sync import SyncAction, Turn, plan_run
from travel_agent.config import get_settings
from travel_agent.context import UserContext

CONFIG = {"configurable": {"thread_id": "ana@example.com:c1"}}


@pytest.fixture(autouse=True, params=["exit", "async"])
def durability(request, monkeypatch):
    """Every test runs under both checkpoint modes: the API's default ("exit") and per-step."""
    monkeypatch.setenv("CHECKPOINT_DURABILITY", request.param)
    return request.param


def make_agent(*replies: str):
    model = FakeToolModel(messages=iter(AIMessage(r) for r in replies), disable_streaming=True)
    return build_agent(InMemorySaver(), InMemoryStore(), model=model)


def transcript(*turns: str) -> list[Turn]:
    """Alternating user/assistant turns, starting with the user."""
    return [Turn("user" if i % 2 == 0 else "assistant", t) for i, t in enumerate(turns)]


async def run(agent, *turns: str, config=CONFIG) -> SyncAction:
    plan = await plan_run(agent, config, transcript(*turns))
    await agent.ainvoke(
        plan.inputs,
        plan.config,
        context=UserContext("ana@example.com"),
        durability=get_settings().checkpoint_durability,
    )
    return plan.action


async def thread(agent, config=CONFIG) -> list[str]:
    state = await agent.aget_state(config)
    return [f"{m.type}:{m.text}" for m in state.values["messages"]]


async def test_in_sync_appends():
    agent = make_agent("a1", "a2")
    assert await run(agent, "u1") == SyncAction.APPEND
    assert await run(agent, "u1", "a1", "u2") == SyncAction.APPEND
    assert await thread(agent) == ["human:u1", "ai:a1", "human:u2", "ai:a2"]


async def test_fork_seeds_empty_thread_from_transcript():
    agent = make_agent("updated plan")
    shown = '`🔧 search_guidebooks(query="Kona")`\n\n# 2 days in Kona\n## Day 1 · Coast'
    action = await run(agent, "plan Kona", shown, "update day 2")
    assert action == SyncAction.SEED
    assert await thread(agent) == [
        "human:plan Kona",
        "ai:# 2 days in Kona\n## Day 1 · Coast",  # tool status line stripped
        "human:update day 2",
        "ai:updated plan",
    ]


async def test_regenerate_rewinds_instead_of_duplicating():
    agent = make_agent("a1", "a2", "a2 again")
    await run(agent, "u1")
    await run(agent, "u1", "a1", "u2")
    assert await run(agent, "u1", "a1", "u2") == SyncAction.REWIND
    assert await thread(agent) == ["human:u1", "ai:a1", "human:u2", "ai:a2 again"]
    # The replaced answer is still in checkpoint history
    texts = [
        m.text async for s in agent.aget_state_history(CONFIG) for m in s.values.get("messages", [])
    ]
    assert "a2" in texts


async def test_edit_later_message_branches_from_matching_turn():
    agent = make_agent("a1", "a2", "a2 for edit")
    await run(agent, "u1")
    await run(agent, "u1", "a1", "u2")
    assert await run(agent, "u1", "a1", "u2 edited") == SyncAction.REWIND
    assert await thread(agent) == ["human:u1", "ai:a1", "human:u2 edited", "ai:a2 for edit"]


async def test_edit_first_message_replaces_thread():
    agent = make_agent("a1", "a1 for edit")
    await run(agent, "u1")
    assert await run(agent, "u1 edited") == SyncAction.REPLACE
    assert await thread(agent) == ["human:u1 edited", "ai:a1 for edit"]


async def test_regenerate_first_answer_replaces_thread():
    agent = make_agent("a1", "a1 again")
    await run(agent, "u1")
    assert await run(agent, "u1") == SyncAction.REPLACE
    assert await thread(agent) == ["human:u1", "ai:a1 again"]


async def test_exit_durability_saves_one_checkpoint_per_turn(durability):
    agent = make_agent("a1", "a2")
    await run(agent, "u1")
    await run(agent, "u1", "a1", "u2")
    checkpoints = [s async for s in agent.aget_state_history(CONFIG)]
    if durability == "exit":
        assert len(checkpoints) == 2
    else:
        assert len(checkpoints) > 4
    assert all(not s.next for s in checkpoints) == (durability == "exit")

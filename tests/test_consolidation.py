from datetime import UTC, datetime, timedelta

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from tests.fakes import FakeToolModel
from travel_agent.agent.graph import build_agent
from travel_agent.consolidation import ConsolidationPlan, MemoryOp, apply_plan, consolidate_user
from travel_agent.context import UserContext
from travel_agent.memory import user_namespace
from travel_agent.tools.conversations import chats_namespace
from travel_agent.tools.memory import namespace

ANA = "ana.lee@example.com"
MIXED = "enjoys adventure sports but dislikes watersport activities"


async def seed_interests(store, *items, **extra):
    await store.aput(
        namespace(ANA), "interests", {"items": list(items), "value": "; ".join(items), **extra}
    )


async def facts(store) -> dict:
    return {
        i.key: i.value.get("items") or i.value["value"] for i in await store.asearch(namespace(ANA))
    }


async def test_mixed_item_is_split_into_interest_and_dislike_and_logged():
    store = InMemoryStore()
    await seed_interests(store, "astronomy", MIXED)
    plan = ConsolidationPlan(
        ops=[
            MemoryOp(
                action="update",
                key="interests",
                old=MIXED,
                new="adventure sports",
                evidence="enjoys adventure sports",
            ),
            MemoryOp(
                action="add",
                key="dislikes",
                new="watersport activities",
                evidence="dislikes watersport activities",
            ),
            MemoryOp(action="noop", key="home_airport", evidence=""),
        ]
    )
    applied = await apply_plan(store, ANA, plan, [], "consolidation")

    assert await facts(store) == {
        "interests": ["astronomy", "adventure sports"],
        "dislikes": ["watersport activities"],
    }
    assert [(e["action"], e["key"]) for e in applied] == [
        ("update", "interests"),
        ("add", "dislikes"),
    ]
    log = await store.asearch(user_namespace(ANA, "memory_log"))
    assert {e.value["before"] for e in log} == {f"astronomy; {MIXED}", None}


async def test_changes_without_real_evidence_are_dropped():
    store = InMemoryStore()
    await store.aput(namespace(ANA), "diet", {"value": "vegetarian"})
    plan = ConsolidationPlan(
        ops=[
            MemoryOp(
                action="update",
                key="diet",
                old="vegetarian",
                new="vegan",
                evidence="I went vegan last year",
            ),  # never said
            MemoryOp(action="add", key="home_airport", new="JFK", evidence="I fly from JFK"),
        ]
    )
    applied = await apply_plan(store, ANA, plan, ["I usually fly from Newark"], "consolidation")
    assert applied == []
    assert await facts(store) == {"diet": "vegetarian"}


async def test_evidence_from_the_users_messages_is_accepted():
    store = InMemoryStore()
    plan = ConsolidationPlan(
        ops=[
            MemoryOp(
                action="add",
                key="dislikes",
                new="overnight flights",
                evidence="never booking overnight flights again",
            ),
        ]
    )
    messages = ["Ugh, I'm never booking overnight flights again. Anyway, Lisbon?"]
    assert len(await apply_plan(store, ANA, plan, messages, "consolidation")) == 1
    assert await facts(store) == {"dislikes": ["overnight flights"]}


async def test_sensitive_facts_and_mismatched_updates_are_left_alone():
    store = InMemoryStore()
    await store.aput(namespace(ANA), "mobility", {"value": "uses a wheelchair", "sensitive": True})
    await store.aput(namespace(ANA), "diet", {"value": "vegetarian"})
    plan = ConsolidationPlan(
        ops=[
            MemoryOp(
                action="delete",
                key="mobility",
                old="uses a wheelchair",
                evidence="uses a wheelchair",
            ),
            MemoryOp(
                action="update",
                key="diet",
                old="vegan",
                new="pescatarian",  # old doesn't match
                evidence="vegetarian",
            ),
        ]
    )
    assert await apply_plan(store, ANA, plan, [], "consolidation") == []
    assert await facts(store) == {"mobility": "uses a wheelchair", "diet": "vegetarian"}


async def test_consolidation_waits_for_idle_chats_and_reviews_only_new_messages():
    store, saver = InMemoryStore(), InMemorySaver()
    agent = build_agent(saver, store, model=FakeToolModel(messages=iter([AIMessage("ok")] * 3)))
    config = {"configurable": {"thread_id": f"{ANA}:c1"}}

    async def say(text):
        await agent.ainvoke({"messages": [HumanMessage(text)]}, config, context=UserContext(ANA))

    seen = []

    async def propose(facts, messages):
        seen.append(messages)
        return ConsolidationPlan()

    await say("I love hiking")
    await say("and I hate crowds")
    now = datetime.now(UTC)
    idle = timedelta(minutes=10)
    assert await consolidate_user(store, saver, propose, ANA, idle, now) is None  # still active
    assert await consolidate_user(store, saver, propose, ANA, idle, now + idle * 2) == []
    assert seen == [["I love hiking", "and I hate crowds"]]
    assert await consolidate_user(store, saver, propose, ANA, idle, now + idle * 3) is None

    # A new message in the same chat: the index keeps what was consolidated, so only it is new
    await say("also I'm vegetarian")
    [entry] = await store.asearch(chats_namespace(ANA))
    assert (entry.value["turns"], entry.value["consolidated_turns"]) == (3, 2)
    later = datetime.now(UTC) + idle * 2
    await consolidate_user(store, saver, propose, ANA, idle, later)
    assert seen[-1] == ["also I'm vegetarian"]


async def test_evidence_in_quotation_marks_still_counts():
    store = InMemoryStore()
    await seed_interests(store, MIXED)
    plan = ConsolidationPlan(
        ops=[
            MemoryOp(
                action="add",
                key="dislikes",
                new="watersport activities",
                evidence=f"\u201c{MIXED}\u201d",
            ),
        ]
    )
    assert len(await apply_plan(store, ANA, plan, [], "consolidation")) == 1

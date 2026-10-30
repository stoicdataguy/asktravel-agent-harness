from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from tests.fakes import FakeToolModel
from travel_agent.agent.compaction import NOTE_PREFIX, compact_messages
from travel_agent.agent.graph import build_agent
from travel_agent.context import UserContext


def calls(*pairs) -> AIMessage:
    """An AI message calling tools, given (id, name, args) triples."""
    return AIMessage(
        "", tool_calls=[{"id": i, "name": n, "args": a, "type": "tool_call"} for i, n, a in pairs]
    )


def result(call_id: str, text: str = "x" * 5000) -> ToolMessage:
    return ToolMessage(text, tool_call_id=call_id)


RESEARCH_TURN = [
    HumanMessage("Where should we stay in Rome?"),
    calls(("s1", "load_skill", {"name": "trip-intake"})),
    result("s1", "SKILL BODY"),
    calls(("g1", "search_guidebooks", {"query": "Rome neighbourhoods"})),
    result("g1"),
    calls(("g2", "read_guidebook_pages", {"book": "italy", "start_page": 88})),
    result("g2"),
    AIMessage("Stay in Monti or Prati."),
]


def test_current_turn_is_untouched_and_single_turn_is_a_noop():
    assert compact_messages(RESEARCH_TURN) == RESEARCH_TURN


def test_earlier_turns_keep_question_answer_and_skills_only():
    current = [HumanMessage("And where to eat?"), calls(("g3", "search_guidebooks", {"q": "food"}))]
    out = compact_messages([*RESEARCH_TURN, *current])

    assert out[-2:] == current  # current turn verbatim, incl. its pending tool call
    earlier = out[:-2]
    assert earlier[0].text == "Where should we stay in Rome?"
    # The skill call and its result survive, as a matching pair
    assert [c["name"] for c in earlier[1].tool_calls] == ["load_skill"]
    assert earlier[2].tool_call_id == "s1" and earlier[2].text == "SKILL BODY"
    # Guidebook calls and results are gone; the answer says what was used
    assert not any(isinstance(m, ToolMessage) and m.tool_call_id in ("g1", "g2") for m in out)
    answer = earlier[3]
    assert answer.text.startswith("Stay in Monti or Prati.")
    assert NOTE_PREFIX in answer.text
    assert "search_guidebooks(query=Rome neighbourhoods)" in answer.text
    assert "read_guidebook_pages(book=italy, start_page=88)" in answer.text
    assert len(earlier) == 4


def test_only_the_latest_itinerary_survives_and_parallel_calls_are_split():
    plan_v1 = {"itinerary": {"title": "v1"}}
    plan_v2 = {"itinerary": {"title": "v2"}}
    messages = [
        HumanMessage("Plan it"),
        calls(("p1", "present_itinerary", plan_v1)),
        result("p1", "shown v1"),
        AIMessage("Want changes?"),
        HumanMessage("Swap days, and check hotels"),
        calls(("p2", "present_itinerary", plan_v2), ("h1", "search_hotels", {"location": "Rome"})),
        result("p2", "shown v2"),
        result("h1"),
        AIMessage("Swapped. Hotels listed."),
        HumanMessage("Thanks"),
    ]
    out = compact_messages(messages)
    kept_calls = [c["id"] for m in out if isinstance(m, AIMessage) for c in m.tool_calls]
    kept_results = [m.tool_call_id for m in out if isinstance(m, ToolMessage)]
    assert kept_calls == kept_results == ["p2"]
    # The v1 turn notes its call without dumping the itinerary argument
    assert "present_itinerary()" in out[1].text
    assert "search_hotels(location=Rome)" in out[-2].text


def test_turn_without_tools_has_no_note():
    messages = [HumanMessage("hi"), AIMessage("Hello!"), HumanMessage("bye")]
    assert compact_messages(messages) == messages


async def test_agent_sends_compacted_turns_but_saves_the_full_thread():
    model = FakeToolModel(
        messages=iter(
            [
                calls(("g1", "list_guidebooks", {})),
                AIMessage("Five books."),
                AIMessage("You're welcome."),
            ]
        ),
        disable_streaming=True,
    )
    agent = build_agent(InMemorySaver(), InMemoryStore(), model=model)
    config = {"configurable": {"thread_id": "ana@example.com:c1"}}
    context = UserContext("ana@example.com")
    await agent.ainvoke({"messages": [HumanMessage("Which books?")]}, config, context=context)
    await agent.ainvoke({"messages": [HumanMessage("Thanks")]}, config, context=context)

    sent = [m for m in model.requests[-1] if m.type != "system"]
    assert [m.type for m in sent] == ["human", "ai", "human"]
    assert "list_guidebooks()" in sent[1].text
    saved = (await agent.aget_state(config)).values["messages"]
    assert [m.type for m in saved] == ["human", "ai", "tool", "ai", "human", "ai"]

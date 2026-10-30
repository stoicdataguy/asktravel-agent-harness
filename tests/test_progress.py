import itertools

import pytest

from travel_agent.api.progress import ProgressRenderer, summarize


def renderer(labels="calls"):
    clock = itertools.count(0, 0.5)  # each call to now() is half a second later
    return ProgressRenderer(labels=labels, now=lambda: next(clock))


def call(id_, tool, **args):
    return {"id": id_, "name": tool, "args": args}


def test_single_call_gets_its_outcome_on_the_same_line():
    r = renderer()
    text = r.start() + r.calls([call("a", "load_skill", name="itinerary-builder")])
    text += r.result("a", "# Itinerary builder\n...")
    assert text == (
        "Starting…\n"
        '🔧 `load_skill(name="itinerary-builder")` → ✓ playbook loaded · 0.5 s\n'
        "Reading the results…\n"
    )


def test_parallel_batch_lists_calls_then_outcomes_as_they_finish():
    r = renderer()
    text = r.calls(
        [
            call("a", "search_guidebooks", query="Rome", book="italy"),
            call("b", "search_guidebooks", query="Vatican", book="italy"),
        ]
    )
    text += r.result("b", "5 matching paragraphs, top 5:\n[italy p.9 | Rome] …")
    assert "Reading" not in text  # one call still running
    text += r.result("a", "No matches for 'Rome' in italy.")
    assert text.splitlines() == [
        '🔧 `search_guidebooks(query="Rome", book="italy")`',
        '🔧 `search_guidebooks(query="Vatican", book="italy")`',
        "   ✓ search_guidebooks → 5 passages · 0.5 s",
        "   ✗ search_guidebooks → no matches · 1.5 s",
        "Reading the results…",
    ]


def test_plain_labels_replace_the_call_and_unknown_tools_fall_back():
    r = renderer(labels="plain")
    text = r.calls(
        [
            call("a", "search_flights", origin="JFK", destination="FCO", depart_date="2026-11-23"),
            call("b", "mystery_tool", x=1),
        ]
    )
    assert text.splitlines() == ["Checking fares JFK → FCO, 2026-11-23", "🔧 `mystery_tool(x=1)`"]


FLIGHTS = (
    "Flights JFK → FCO, Mon Nov 23, 2026, round trip, back Sun Nov 29, 2 adults, economy. "
    "Prices in USD:\n1. $1,555 · Brussels Airlines · 1 stop\n2. $2,165 · Delta · nonstop"
)
HOTELS = (
    "Hotels in Prati, Rome, Tue Nov 24 → Sun Nov 29, 2026 (5 nights), from Google Hotels.\n"
    "1. Hotel A · $212/night (~$1,060 for 5 nights) · 4.4★\n2. Hotel B · $189/night (~$945)"
)


SAVED = "Updated diet (was: vegetarian): pescatarian"
NOT_SAVED = "Not saved: key and value are required."
BOOKS = "a (10 pages)\nChapters: x\n\nb (20 pages)\nChapters: y"
TRIP = "- [trip-1] Rome …\n\nSaved itinerary:\n\n# Plan"
CHATS = 'Chat "a" (2026-10-29):\n…\n\nChat "b" (2026-10-28):'


@pytest.mark.parametrize(
    ("name", "result", "expected"),
    [
        ("search_guidebooks", "8 matching paragraphs, top 8:\n[x p.1 | y] z", (True, "8 passages")),
        ("read_guidebook_pages", "# Rome\n[p. 65]\ntext\n[p. 66]\ntext", (True, "2 pages")),
        ("read_guidebook_pages", "italy has pages 1-981.", (False, "italy has pages 1-981.")),
        ("list_guidebooks", BOOKS, (True, "2 books")),
        ("search_flights", FLIGHTS, (True, "2 options, from $1,555")),
        (
            "search_flights",
            "Google Flights found no flights JFK → XYZ on 2026-11-23.",
            (False, "no flights found"),
        ),
        ("search_hotels", HOTELS, (True, "2 hotels, from $189/night")),
        ("search_hotels", "No hotels in Nowhere matched for …", (False, "no hotels found")),
        ("get_trip", TRIP, (True, "trip loaded, with itinerary")),
        ("recall_conversations", CHATS, (True, "2 earlier chats")),
        ("recall_conversations", "No earlier chats mention 'x'.", (False, "nothing found")),
        ("save_memory", SAVED, (True, SAVED)),
        ("save_memory", NOT_SAVED, (False, NOT_SAVED)),
        ("present_itinerary", "Itinerary '3 days' (3 days) is now shown…", (True, "plan shown")),
    ],
)
def test_summaries_come_from_each_tools_result(name, result, expected):
    assert summarize(name, result) == expected

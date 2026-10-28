from datetime import date, timedelta

import pytest
from fast_flights import FlightsNotFound
from fast_flights.model import Airport, CarbonEmission, Flights, SimpleDatetime, SingleFlight

from tests.test_itinerary import PLAN
from travel_agent.agent.schemas import Itinerary, render_itinerary
from travel_agent.tools import flights
from travel_agent.tools.flights import search_flights, to_option

SOON = (date.today() + timedelta(days=60)).isoformat()


def leg(frm, to, dep, arr, minutes, plane="Boeing 757", day=28, arr_day=28):
    return SingleFlight(
        from_airport=Airport(name=frm, code=frm),
        to_airport=Airport(name=to, code=to),
        departure=SimpleDatetime(date=(2026, 12, day), time=dep),
        arrival=SimpleDatetime(date=(2026, 12, arr_day), time=arr),
        duration=minutes,
        plane_type=plane,
    )


def result(price, airline, *legs):
    carbon = CarbonEmission(typical_on_route=0, emission=0)
    return Flights(type="X", price=price, airlines=[airline], flights=list(legs), carbon=carbon)


NONSTOP = result(525, "United", leg("SFO", "KOA", (8, 10), (12, 1), 351))
CHEAP = result(477, "United", leg("SFO", "KOA", (16, 55), (20, 47), 352, "Boeing 737MAX 9"))
CONNECT = result(
    1209,
    "Alaska",
    leg("SFO", "LAX", (21, 59), (23, 30), 91),
    leg("LAX", "KOA", (7, 0), (10, 50), 350, day=29, arr_day=29),
)


@pytest.fixture
def google(monkeypatch):
    """Fake fast-flights' network call; tests set `google.results` or `google.error`."""

    class Fake:
        results = [NONSTOP, CHEAP, CONNECT]
        error: Exception | None = None
        queries: list = []

    def fake_get_flights(query):
        Fake.queries.append(query)
        if Fake.error:
            raise Fake.error
        return Fake.results

    monkeypatch.setattr(flights, "get_flights", fake_get_flights)
    return Fake


async def search(**args):
    return await search_flights.ainvoke(
        {"origin": "SFO", "destination": "KOA", "depart_date": SOON, **args}
    )


async def test_cheapest_first_with_totals_and_per_person(google):
    out = await search(adults=2)
    lines = out.splitlines()
    assert "2 adults" in lines[0] and "totals for all travellers" in lines[0]
    assert lines[1] == (
        "1. $477 total (~$238/person) · United · SFO 16:55 → KOA 20:47 · nonstop · 5h 52m"
        " · Boeing 737MAX 9"
    )
    assert lines[2].startswith("2. $525 total")
    assert (
        lines[3]
        == "3. $1,209 total (~$604/person) · Alaska · SFO 21:59 → KOA 10:50 (+1) · 1 stop via LAX"
    )
    assert lines[-1].startswith("See all options and book: https://www.google.com/travel/flights")


async def test_round_trip_labels_and_queries_both_legs(google):
    back = (date.fromisoformat(SOON) + timedelta(days=5)).isoformat()
    out = await search(return_date=back, max_results=1)
    assert "round trip" in out.splitlines()[0] and "(outbound shown)" in out
    assert len(google.queries[-1].flight_data) == 2


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"origin": "San Francisco"}, "3-letter IATA"),
        ({"depart_date": "2020-01-01"}, "in the past"),
        ({"return_date": "2020-01-01"}, "before the departure"),
        ({"adults": 0}, "1-9 travellers"),
    ],
)
async def test_invalid_input_is_explained(google, args, message):
    assert message in await search(**args)
    assert google.queries == []


async def test_no_results_and_errors_become_messages(google):
    google.error = FlightsNotFound()
    assert "found no flights" in await search()
    google.error = ConnectionError("boom")
    out = await search()
    assert "Flight search failed (ConnectionError)" in out and "google.com/travel/flights" in out


def test_itinerary_renders_chosen_flights():
    option = to_option(CHEAP, travellers=2, currency="USD")
    md = render_itinerary(Itinerary(**PLAN, flights=[option]))
    expected = "- United · nonstop · SFO 2026-12-28 16:55 → KOA 2026-12-28 20:47 · $477 total"
    assert f"**Flights**\n\n{expected}" in md

from datetime import date, timedelta

import httpx
import pytest

from tests.test_itinerary import PLAN
from travel_agent.agent.schemas import Itinerary, render_itinerary
from travel_agent.tools import trains
from travel_agent.tools.trains import search_trains, to_option

TRAVEL_DATE = date.today() + timedelta(days=30)


def seats(code, fare, shown, cached="2026-11-05T13:09:46.001"):
    return {
        "travelClass": code,
        "fare": fare,
        "availabilityDisplayName": shown,
        "cacheTime": cached,
    }


# Shaped like ConfirmTkt's response for New Delhi to Agra
SHATABDI = {
    "trainNumber": "12002",
    "trainName": "SHATABDI EXPRES",
    "fromStnCode": "NDLS",
    "toStnCode": "AGC",
    "departureTime": "06:00",
    "arrivalTime": "07:50",
    "duration": 110,
    "hasDeparted": False,
    "avlClasses": ["CC", "EC"],
    "avlClassesSorted": ["CC", "EC"],
    "availabilityCache": {
        "CC": seats("CC", "565", "AVL 101"),
        "EC": seats("EC", "960", "AVL 71", cached="2026-11-05T09:30:00"),
    },
}
NEARBY = {
    **SHATABDI,
    "trainNumber": "20434",
    "trainName": "JAMMU MAIL",
    "fromStnCode": "DLI",
    "toStnCode": "TDL",
    "departureTime": "04:20",
    "arrivalTime": "07:03",
    "duration": 163,
    "avlClasses": ["SL"],
    "avlClassesSorted": ["SL"],
    "availabilityCache": {"SL": seats("SL", "200", "WL 12")},
}
NIZAMUDDIN = {
    **NEARBY,
    "trainNumber": "22470",
    "trainName": "VANDE BHARAT EXPRESS",
    "fromStnCode": "NZM",
    "toStnCode": "AGC",
    "departureTime": "05:00",
    "arrivalTime": "06:40",
}
GONE = {**SHATABDI, "trainNumber": "11111", "trainName": "ALREADY LEFT", "hasDeparted": True}
CANCELLED = {
    **SHATABDI,
    "trainNumber": "11906",
    "trainName": "CANCELLED TRAIN",
    "avlClassesSorted": ["SL"],
    "availabilityCache": {"SL": seats("SL", "150", "Train Cancelled")},
}


@pytest.fixture
def api(monkeypatch):
    """Fake the endpoint; tests set `api.data` or `api.error`."""

    class Fake:
        data = {"trainList": [NEARBY, SHATABDI, NIZAMUDDIN, GONE, CANCELLED]}
        error: Exception | None = None
        calls: list = []

    async def fake_fetch(origin, destination, travel_date):
        Fake.calls.append((origin, destination, travel_date))
        if Fake.error:
            raise Fake.error
        return Fake.data

    monkeypatch.setattr(trains, "fetch_trains", fake_fetch)
    Fake.calls = []
    return Fake


async def search(**args):
    return await search_trains.ainvoke(
        {"origin": "NDLS", "destination": "AGC", "travel_date": TRAVEL_DATE, **args}
    )


def test_to_option_reads_the_train():
    assert to_option(SHATABDI).summary() == (
        "12002 Shatabdi Expres · NDLS 06:00 → AGC 07:50 · 1 hr 50 min · "
        "CC ₹565 AVL 101 · EC ₹960 AVL 71"
    )


@pytest.mark.parametrize("fare", [None, "", "n/a"])
def test_missing_fare_and_status_are_said_not_invented(fare):
    train = {**SHATABDI, "availabilityCache": {"CC": seats("CC", fare, "")}}
    assert "CC fare unknown availability unknown · EC fare unknown" in to_option(train).summary()


async def test_lists_the_requested_stations_first_and_skips_departed(api):
    lines = (await search()).splitlines()
    assert lines[0].startswith("Trains NDLS → AGC,") and "3 of 3" in lines[0]
    assert "rupees per person" in lines[0]
    assert [line.split(" ")[1] for line in lines[1:4]] == ["12002", "22470", "20434"]
    text = "\n".join(lines).upper()
    assert "ALREADY LEFT" not in text and "CANCELLED" not in text


async def test_says_how_old_the_availability_is_and_links_to_booking(api):
    lines = (await search()).splitlines()
    assert lines[-2].startswith("Availability as of Nov 5, 09:30")  # the oldest shown
    assert lines[-1].endswith(f"/from/NDLS/to/AGC/{TRAVEL_DATE:%d-%m-%Y}")


async def test_filters_by_class(api):
    out = await search(travel_class="cc")
    assert "12002" in out and "22470" not in out and "EC ₹960" not in out
    assert (await search(travel_class="1A")).startswith("No 1A trains found")


async def test_max_results_is_capped(api):
    api.data = {"trainList": [{**SHATABDI, "trainNumber": str(n)} for n in range(30)]}
    lines = (await search(max_results=99)).splitlines()
    assert sum(line[0].isdigit() for line in lines) == 10
    assert "10 of 30" in lines[0]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"origin": "New Delhi"}, "station codes"),
        ({"destination": "A1"}, "station codes"),
        ({"travel_date": date.today() - timedelta(days=1)}, "in the past"),
    ],
)
async def test_invalid_input_is_explained(api, args, message):
    assert message in await search(**args)
    assert api.calls == []


async def test_station_codes_are_normalised(api):
    await search(origin=" ndls ")
    assert api.calls[0][0] == "NDLS"


async def test_no_trains_and_failures_become_messages(api):
    api.data = {"trainList": [], "errorMessage": "No direct trains available"}
    assert (await search()).startswith("No direct trains found NDLS → AGC")
    far = date.today() + timedelta(days=200)
    assert "open about 60 days ahead" in await search(travel_date=far)
    api.error = httpx.ConnectError("refused")
    out = await search()
    assert "Train search failed (ConnectError)" in out and "confirmtkt.com" in out


async def test_fetch_sends_the_date_and_no_credentials(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"], seen["headers"] = request.url, request.headers
        return httpx.Response(200, json={"data": {"trainList": [SHATABDI]}})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        trains.httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )
    data = await trains.fetch_trains("NDLS", "AGC", date(2026, 12, 6))
    assert data["trainList"][0]["trainNumber"] == "12002"
    assert seen["url"].params["dateOfJourney"] == "06-12-2026"
    assert "apikey" not in seen["headers"]


def test_whole_hours_read_naturally():
    assert "· 2 hr ·" in to_option({**SHATABDI, "duration": 120}).summary()


def test_itinerary_renders_chosen_trains():
    md = render_itinerary(Itinerary(**PLAN, trains=[to_option(SHATABDI)]))
    assert "**Trains**\n\n- 12002 Shatabdi Expres · NDLS 06:00 → AGC 07:50" in md

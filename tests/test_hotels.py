from datetime import date, timedelta

import pytest

from tests.test_itinerary import PLAN
from travel_agent.agent.schemas import Itinerary, render_itinerary
from travel_agent.tools import hotels
from travel_agent.tools.hotels import parse_card, search_hotels

# Card text as captured from Google Hotels (Kailua-Kona, Dec 28 2026 - Jan 2 2027)
ROYAL_KONA = (
    "Prices starting from $360, Royal Kona Resort",
    "Royal Kona Resort",
    "Royal Kona Resort\n$360\n4.1\n(7.2K)\n·\nExcellent location\n"
    "Amenities for Royal Kona Resort, a 4-star hotel.: Breakfast ($), Wi-Fi ($), Parking ($), "
    "Outdoor pool, Air conditioning, Fitness center, Spa, Beach access,\n4-star hotel\n"
    "Breakfast ($)\nWi-Fi ($)\nView prices",
)
KONA_COAST = (
    "Prices starting from $342, Kona Coast Resort DEAL 18% less than usual",
    "Kona Coast Resort",
    "Kona Coast Resort\nDEAL\n$342\n18% less than usual\n4.4\n(2.3K)\n"
    "Amenities for Kona Coast Resort, a 3-star hotel.: Wi-Fi, Free parking, Outdoor pool, Hot tub,"
    " Air conditioning, Fitness center, Bar, Restaurant,\n3-star hotel\nWi-Fi\nFree parking",
)
MANAGO = (
    "Prices starting from $67, Manago Hotel and Restaurant",
    "Manago Hotel and Restaurant",
    "Manago Hotel and Restaurant\nGREAT PRICE\n$67\nfor a 3-star hotel\n4.5\n(855)\n"
    "Amenities for Manago Hotel and Restaurant, a 3-star hotel.: Free Wi-Fi, Free parking, "
    "Restaurant,\n3-star hotel",
)
CONDO = (
    "Prices starting from $235, Ocean Front Pool, Private and Newly Renovated Top Corner Unit",
    "Ocean Front Pool, Private and Newly Renovated Top Corner Unit",
    "VACATION RENTAL\nOcean Front Pool, Private and Newly Renovated Top Corner Unit\n$235\n4.7\n"
    "(310)\nApartment\nSleeps 2\n1 bedroom",
)
HOSTEL = (
    "Prices starting from $100, My Hawai‘i Hostel",
    "My Hawai‘i Hostel",
    "My Hawai‘i Hostel\n$100\n4.5\n(387)\n"
    "Amenities for My Hawai‘i Hostel: Free Wi-Fi, Free parking, Air conditioning,\nFree Wi-Fi",
)
CARDS = [ROYAL_KONA, KONA_COAST, MANAGO, CONDO, HOSTEL]

CHECK_IN = date.today() + timedelta(days=60)
CHECK_OUT = CHECK_IN + timedelta(days=5)


def test_parse_card_reads_price_rating_class_and_amenities():
    hotel = parse_card(*ROYAL_KONA)
    assert (hotel.name, hotel.price_per_night, hotel.rating, hotel.reviews) == (
        "Royal Kona Resort",
        360,
        4.1,
        "7.2K",
    )
    assert hotel.hotel_class == 4 and not hotel.vacation_rental and hotel.deal is None
    assert hotel.amenities[:3] == ["Breakfast ($)", "Wi-Fi ($)", "Parking ($)"]


def test_parse_card_deals_rentals_and_unclassed():
    assert parse_card(*KONA_COAST).deal == "DEAL 18% less than usual"
    assert parse_card(*MANAGO).deal == "GREAT PRICE"
    condo = parse_card(*CONDO)
    assert condo.vacation_rental and (condo.rating, condo.reviews) == (4.7, "310")
    hostel = parse_card(*HOSTEL)
    assert hostel.hotel_class is None and hostel.price_per_night == 100


@pytest.fixture
def google(monkeypatch):
    """Fake the browser step; tests set `google.cards` or `google.error`."""

    class Fake:
        cards = [{"label": c[0], "name": c[1], "text": c[2]} for c in CARDS]
        error: Exception | None = None
        calls: list = []

    async def fake_fetch(location, check_in, check_out, on_step=lambda text: None):
        Fake.calls.append((location, check_in, check_out))
        if Fake.error:
            raise Fake.error
        return Fake.cards, "https://www.google.com/travel/search?ts=abc"

    monkeypatch.setattr(hotels, "fetch_hotel_cards", fake_fetch)
    return Fake


async def search(**args):
    return await search_hotels.ainvoke(
        {"location": "Kailua-Kona", "check_in": CHECK_IN, "check_out": CHECK_OUT, **args}
    )


async def test_lists_hotels_with_stay_totals_and_link(google):
    lines = (await search()).splitlines()
    assert "(5 nights)" in lines[0] and "taxes and fees may be extra" in lines[0]
    assert lines[1] == (
        "1. Royal Kona Resort · $360/night (~$1,800 for 5 nights) · 4-star · 4.1★ (7.2K reviews)"
        " · Breakfast ($), Wi-Fi ($), Parking ($), Outdoor pool, Air conditioning, Fitness center"
    )
    assert "deal 18% less than usual" in lines[2]
    assert all("Ocean Front Pool" not in line for line in lines)  # rentals excluded by default
    assert lines[-1] == "Compare and book: https://www.google.com/travel/search?ts=abc"


async def test_filters(google):
    out = await search(max_price_per_night=150, min_rating=4.5, include_vacation_rentals=True)
    names = [line.split(" · ")[0][3:] for line in out.splitlines()[1:-1]]
    assert names == ["Manago Hotel and Restaurant", "My Hawai‘i Hostel"]
    out = await search(include_vacation_rentals=True, max_results=10)
    assert "vacation rental" in out


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"location": " "}, "Give a town"),
        ({"check_in": date(2020, 1, 1)}, "in the past"),
        ({"check_out": CHECK_IN}, "after check-in"),
        ({"check_out": date.today() + timedelta(days=400)}, "within about the next year"),
    ],
)
async def test_invalid_input_is_explained(google, args, message):
    google.calls = []
    assert message in await search(**args)
    assert google.calls == []


async def test_failures_and_no_matches_become_messages(google):
    out = await search(max_price_per_night=10)
    assert out.startswith("No hotels in Kailua-Kona matched")
    google.error = TimeoutError("page")
    out = await search()
    assert "Hotel search failed (TimeoutError)" in out and "google.com/travel/hotels" in out


def test_itinerary_renders_chosen_hotels():
    md = render_itinerary(Itinerary(**PLAN, hotels=[parse_card(*ROYAL_KONA)]))
    assert "**Hotels**\n\n- Royal Kona Resort · $360/night · 4-star · 4.1★ (7.2K reviews)" in md

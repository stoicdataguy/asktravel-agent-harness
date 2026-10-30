import pytest

from travel_agent.kb.ingest import slugify


@pytest.mark.parametrize(
    ("filename", "slug"),
    [
        (
            "Hawaii, the Big Island 4 - Full PDF eBook (Lonely Planet) (z-library.sk, 1lib.sk).pdf",
            "hawaii-the-big-island-4",
        ),
        (
            "Lonely Planet - Italy (2016) (Cristian Bonetto) (z-library.sk).pdf",
            "lonely-planet-italy-2016",
        ),
        ("Switzerland (Damien Simonis, Sarah Johnstone etc.) (z-library.sk).pdf", "switzerland"),
        (
            "Lonely Planet Spain (Travel Guide) -- coll_ -- Travel Guides, 2016 -- Lonely Planet "
            "Global Limited -- isbn13 9781786572110 -- 4b02070b -- Anna’s Archive.pdf",
            "lonely-planet-spain-2016",
        ),
        (
            "Lonely Planet Turkey (Travel Guide) -- Lonely Planet, James Bainbridge -- Lonely "
            "Planet guidebooks, 15th edition, Footscray, Vic_] -- isbn13 9781786572356 -- 40fc008e "
            "-- Anna’s Archive.pdf",
            "lonely-planet-turkey-15th-edition",
        ),
    ],
)
def test_slugify_keeps_title_and_edition_only(filename, slug):
    assert slugify(filename) == slug

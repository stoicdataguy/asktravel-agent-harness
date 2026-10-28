import pytest

from travel_agent.tools import guidebooks
from travel_agent.tools.guidebooks import (
    list_guidebooks,
    normalize,
    read_guidebook_pages,
    search_guidebooks,
)

HAWAII = """\
# hawaii-test
# Contents
[p. 1]
Kailua-Kona . . . . . 88
# On the Road
## Kailua-Kona
[p. 2]
Night dive with manta rays off the Kona coast.
Kona coffee farms line the slopes above town.
### Eating
[p. 3]
Hawaiʻi poke bowls at the harbour.
"""

ITALY = """\
# italy-test
# Rome
[p. 1]
Spaghetti cacio e pepe is a Roman classic.
"""


@pytest.fixture(autouse=True)
def books(tmp_path, monkeypatch):
    (tmp_path / "hawaii-test.md").write_text(HAWAII)
    (tmp_path / "italy-test.md").write_text(ITALY)
    monkeypatch.setenv("GUIDEBOOKS_DIR", str(tmp_path))
    guidebooks.load_books.cache_clear()
    yield
    guidebooks.load_books.cache_clear()


def test_normalize_folds_okina_and_accents():
    assert normalize("Hawaiʻi Pūʻuhonua") == "hawaii puuhonua"


def test_list_guidebooks():
    out = list_guidebooks.invoke({})
    assert "hawaii-test (3 pages)" in out
    assert "Chapters: Contents; On the Road" in out


def test_search_reports_page_and_section():
    out = search_guidebooks.invoke({"query": "manta rays", "book": "hawaii"})
    assert "[hawaii-test p.2 | On the Road > Kailua-Kona] Night dive with manta rays" in out


def test_search_skips_contents_and_matches_without_okina():
    assert "88" not in search_guidebooks.invoke({"query": "Kailua Kona"})
    assert "poke bowls" in search_guidebooks.invoke({"query": "Hawaii poke"})


def test_search_all_books_and_unknown_book():
    assert "cacio e pepe" in search_guidebooks.invoke({"query": "cacio pepe"})
    assert "No guidebook matches" in search_guidebooks.invoke({"query": "x", "book": "peru"})


def test_read_pages_groups_by_section():
    out = read_guidebook_pages.invoke({"book": "hawaii", "start_page": 2, "end_page": 3})
    assert "## p.2 | On the Road > Kailua-Kona" in out
    assert "## p.3 | On the Road > Kailua-Kona > Eating" in out


def test_read_pages_needs_one_book():
    out = read_guidebook_pages.invoke({"book": "all", "start_page": 1})
    assert "Specify exactly one book" in out

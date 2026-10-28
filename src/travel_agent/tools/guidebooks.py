"""Grep-style retrieval over the Lonely Planet guidebooks extracted by `travel-agent-ingest`.

Each book's markdown has one paragraph per line, `[p. N]` page markers and TOC headings, so a
search hit can report its page and section path without any embeddings.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache

from langchain.tools import tool

from travel_agent.config import get_settings

MAX_HIT_CHARS = 500
MAX_PAGES_PER_READ = 4
MAX_READ_CHARS = 15_000

_PAGE = re.compile(r"^\[p\. (\d+)\]$")
_HEADING = re.compile(r"^(#{1,6}) (.+)$")
# Contents/index/credits chapters only list names and page numbers; they'd swamp search results
_NOISE_CHAPTER = re.compile(
    r"^(?:\w+-\d+-)?(title|contents|index|front matter|map legend|our writers|behind the scenes)\b",
    re.IGNORECASE,
)
# Dot leaders in TOCs ("Kona . . . 88") and map keys ("Stop......B3")
_DOT_LEADER = re.compile(r"\.{4,}|(?:\. ){3}")
_STOPWORDS = {"a", "an", "and", "the", "of", "in", "on", "to", "for", "at", "or", "is", "with"}


def normalize(text: str) -> str:
    """Lowercase, strip accents and Hawaiian ʻokina/apostrophes so 'Hawaii' matches 'Hawaiʻi'."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[ʻ‘’'`]", "", text).lower()


@dataclass
class Paragraph:
    page: int
    section: str
    text: str
    norm: str


@dataclass
class Book:
    slug: str
    page_count: int = 0
    chapters: list[str] = field(default_factory=list)
    paragraphs: list[Paragraph] = field(default_factory=list)


def parse_book(slug: str, markdown: str) -> Book:
    book = Book(slug=slug)
    page = 0
    headings: list[str] = []
    # Line 0 is the "# <slug>" title, not part of the book's TOC
    for line in markdown.splitlines()[1:]:
        if m := _PAGE.match(line):
            page = int(m.group(1))
        elif m := _HEADING.match(line):
            level = len(m.group(1))
            headings = headings[: level - 1] + [m.group(2).strip()]
            if level == 1:
                book.chapters.append(m.group(2).strip())
        elif line.strip():
            book.paragraphs.append(Paragraph(page, " > ".join(headings), line, normalize(line)))
    book.page_count = page
    return book


@lru_cache
def load_books() -> dict[str, Book]:
    books_dir = get_settings().guidebooks_dir
    return {
        path.stem: parse_book(path.stem, path.read_text(encoding="utf-8"))
        for path in sorted(books_dir.glob("*.md"))
    }


def resolve_books(name: str) -> list[Book]:
    """'all'/'' → every book; otherwise exact slug, else books whose slug contains the name."""
    books = load_books()
    key = normalize(name).strip().replace(" ", "-")
    if key in ("", "all"):
        return list(books.values())
    if key in books:
        return [books[key]]
    return [b for slug, b in books.items() if key in slug]


@tool
def list_guidebooks() -> str:
    """List the available Lonely Planet guidebooks with their page counts and chapters.

    Call this first to find out which destinations are covered and the exact book ids to use
    with the other guidebook tools.
    """
    books = load_books()
    if not books:
        return "No guidebooks available. Run `uv run travel-agent-ingest` to extract them."
    return "\n\n".join(
        f"{b.slug} ({b.page_count} pages)\nChapters: {'; '.join(b.chapters)}"
        for b in books.values()
    )


@tool
def search_guidebooks(query: str, book: str = "all", max_results: int = 8) -> str:
    """Keyword search over Lonely Planet guidebook paragraphs.

    Args:
        query: A few specific keywords, e.g. "manta ray snorkel" or "Kona coffee farm".
            Paragraphs matching more of the keywords rank higher. Prefer distinctive nouns over
            generic words like "best" or "things".
        book: A book id from list_guidebooks, a destination name like "italy", or "all".
        max_results: Number of paragraphs to return (max 20).

    Returns hits as `[book p.N | Chapter > Section] paragraph`. Use read_guidebook_pages with the
    page number for more context. Note: cross-references inside the text like "(p88)" are the
    printed page numbers and do NOT match these page numbers; search for the topic instead.
    """
    books = resolve_books(book)
    if not books:
        return f"No guidebook matches {book!r}. Available: {', '.join(load_books()) or 'none'}"
    terms = [t for t in re.findall(r"\w+", normalize(query)) if t not in _STOPWORDS]
    if not terms:
        return "Query has no searchable keywords."

    phrase = " ".join(terms)
    # A paragraph must match at least half the keywords (rounded up) to count
    min_score = (len(terms) + 1) // 2
    scored = []
    for b in books:
        for p in b.paragraphs:
            if _NOISE_CHAPTER.match(p.section) or _DOT_LEADER.search(p.text):
                continue
            score = sum(t in p.norm for t in terms)
            if score >= min_score:
                # Exact phrase match beats scattered keywords
                scored.append((score + (len(terms) if phrase in p.norm else 0), b.slug, p))
    if not scored:
        return f"No matches for {query!r} in {', '.join(b.slug for b in books)}."

    scored.sort(key=lambda hit: -hit[0])
    hits = scored[: min(max_results, 20)]
    lines = [
        f"[{slug} p.{p.page} | {p.section}] "
        f"{p.text if len(p.text) <= MAX_HIT_CHARS else p.text[:MAX_HIT_CHARS] + '…'}"
        for _, slug, p in hits
    ]
    return f"{len(scored)} matching paragraphs, top {len(hits)}:\n" + "\n".join(lines)


@tool
def read_guidebook_pages(book: str, start_page: int, end_page: int | None = None) -> str:
    """Read full text of guidebook pages, e.g. to expand on a search hit.

    Args:
        book: A book id from list_guidebooks (or a destination name that uniquely matches one).
        start_page: First page to read (page numbers as shown in search results).
        end_page: Last page to read, inclusive; defaults to start_page. At most 4 pages per call.
    """
    books = resolve_books(book)
    if len(books) != 1:
        options = ", ".join(b.slug for b in books) or ", ".join(load_books())
        return f"Specify exactly one book; {book!r} matches: {options or 'none'}"
    b = books[0]
    end_page = min(end_page or start_page, start_page + MAX_PAGES_PER_READ - 1)
    if not 1 <= start_page <= b.page_count:
        return f"{b.slug} has pages 1-{b.page_count}."

    out, section = [], None
    for p in b.paragraphs:
        if start_page <= p.page <= end_page:
            if p.section != section:
                section = p.section
                out.append(f"\n## p.{p.page} | {section}")
            out.append(p.text)
    text = "\n".join(out).strip() or f"Pages {start_page}-{end_page} have no text (maps/photos)."
    return text if len(text) <= MAX_READ_CHARS else text[:MAX_READ_CHARS] + "\n…[truncated]"


GUIDEBOOK_TOOLS = [list_guidebooks, search_guidebooks, read_guidebook_pages]

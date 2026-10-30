"""Agent progress for the chat: which tools run, with what arguments, and what they found.

The API turns the agent's stream into short progress lines, rendered by `ProgressRenderer`:

    🔧 `search_guidebooks(query="Rome itinerary", book="italy")` → ✓ 6 passages · 0.4 s

A single call keeps its line open so the outcome lands on it; a parallel batch prints one line
per call, then an indented outcome line for each as it finishes. Outcomes are summarised in code
from each tool's compact result (counts and prices, never guidebook text): no extra model calls.

`PROGRESS_STYLE` decides where lines go. `reasoning` (default) sends them as `reasoning_content`,
which Open WebUI shows as a collapsible block above the answer and doesn't resend on the next
turn (docs/plans/streaming-progress.md §3). `inline` puts them in the answer text, as the old
`🔧` lines were. `off` sends nothing.
"""

import json
import re
import time
from collections.abc import Callable
from typing import Any

MAX_ARG_CHARS = 80
MAX_OUTCOME_CHARS = 90
START = "Starting…"
READING = "Reading the results…"


def _short(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= MAX_ARG_CHARS else text[: MAX_ARG_CHARS - 1] + "…"


def call_text(tool_call: dict) -> str:
    """`name(arg=value, …)`, long values truncated; the itinerary argument is left out."""
    args = ", ".join(f"{k}={_short(v)}" for k, v in tool_call["args"].items() if k != "itinerary")
    return f"{tool_call['name']}({args})"


# Optional plain-language labels (PROGRESS_LABELS=plain); unknown tools fall back to the call
PLAIN: dict[str, Callable[[dict], str]] = {
    "list_guidebooks": lambda a: "Checking which guidebooks exist",
    "search_guidebooks": lambda a: (
        f'Searching the {a.get("book", "all")} guidebook for "{a.get("query", "")}"'
    ),
    "read_guidebook_pages": lambda a: f"Reading {a.get('book', '')} p. {a.get('start_page', '')}",
    "search_flights": lambda a: (
        f"Checking fares {a.get('origin', '')} → "
        f"{a.get('destination', '')}, {a.get('depart_date', '')}"
    ),
    "search_hotels": lambda a: f"Searching hotels in {a.get('location', '')}",
    "load_skill": lambda a: f"Loading the {a.get('name', '')} playbook",
    "save_memory": lambda a: f"Remembering {a.get('key', '')}",
    "delete_memory": lambda a: f"Forgetting {a.get('key', '')}",
    "save_trip_details": lambda a: f"Saving trip details for {a.get('destination', '')}",
    "get_trip": lambda a: "Reading the saved trip",
    "delete_trip": lambda a: "Forgetting a trip",
    "present_itinerary": lambda a: "Presenting the itinerary",
    "recall_conversations": lambda a: f'Searching earlier chats for "{a.get("query", "")}"',
}


def _first_line(text: str) -> str:
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line if len(line) <= MAX_OUTCOME_CHARS else line[: MAX_OUTCOME_CHARS - 1] + "…"


def _numbered(text: str) -> list[str]:
    return [line for line in text.splitlines() if re.match(r"^\d+\. ", line)]


def _cheapest(lines: list[str], pattern: str) -> str | None:
    prices = [int(m.group(1).replace(",", "")) for line in lines if (m := re.search(pattern, line))]
    return f"${min(prices):,}" if prices else None


def summarize(name: str, text: str) -> tuple[bool, str]:
    """(ok, short outcome) from a tool's result text."""
    if name == "search_guidebooks":
        if m := re.match(r"(\d+) matching paragraphs, top (\d+)", text):
            return True, f"{m.group(1)} passage{'s' if m.group(1) != '1' else ''}"
        return False, "no matches" if text.startswith("No matches") else _first_line(text)
    if name == "read_guidebook_pages":
        pages = len(re.findall(r"^\[p\. \d+\]", text, re.M))
        return (
            (True, f"{pages} page{'s' if pages != 1 else ''}")
            if pages
            else (False, _first_line(text))
        )
    if name == "list_guidebooks":
        books = len(re.findall(r"\(\d+ pages\)", text))
        return (True, f"{books} books") if books else (False, _first_line(text))
    if name == "search_flights":
        options = _numbered(text)
        if text.startswith("Flights ") and options:
            cheapest = _cheapest(options, r"^\d+\. \$([\d,]+)")
            return True, f"{len(options)} options" + (f", from {cheapest}" if cheapest else "")
        return False, "no flights found" if "found no flights" in text else _first_line(text)
    if name == "search_hotels":
        options = _numbered(text)
        if text.startswith("Hotels in ") and options:
            cheapest = _cheapest(options, r"\$([\d,]+)/night")
            return True, f"{len(options)} hotels" + (f", from {cheapest}/night" if cheapest else "")
        return False, "no hotels found" if text.startswith("No hotels") else _first_line(text)
    if name == "load_skill":
        return (
            (False, _first_line(text)) if text.startswith("No skill") else (True, "playbook loaded")
        )
    if name == "get_trip":
        if text.startswith("No trip"):
            return False, _first_line(text)
        return True, "trip loaded" + (", with itinerary" if "Saved itinerary" in text else "")
    if name == "recall_conversations":
        chats = text.count('Chat "')
        return (
            (True, f"{chats} earlier chat{'s' if chats != 1 else ''}")
            if chats
            else (False, "nothing found")
        )
    if name == "present_itinerary":
        return True, "plan shown"
    # Memory and trip writes: their own one-line result says what happened
    failed = re.match(r"(Not saved|Nothing saved|No trip|.* isn't )", text)
    return not failed, _first_line(text) or "done"


class ProgressRenderer:
    """Turns agent events into progress text, keeping per-call timings."""

    def __init__(self, labels: str = "calls", now: Callable[[], float] = time.monotonic):
        self.labels, self.now = labels, now
        self.started: dict[str, tuple[str, float]] = {}  # tool call id -> (name, start time)
        self.open: str | None = None  # id of a call whose line still waits for its outcome

    def _close(self) -> str:
        if self.open is None:
            return ""
        self.open = None
        return "\n"

    def start(self) -> str:
        return f"{START}\n"

    def label(self, tool_call: dict) -> str:
        plain = PLAIN.get(tool_call["name"]) if self.labels == "plain" else None
        if plain:
            try:
                return plain(tool_call["args"])
            except Exception:  # a label must never break the stream
                pass
        return f"🔧 `{call_text(tool_call)}`"

    def calls(self, tool_calls: list[dict]) -> str:
        text = self._close()
        for call in tool_calls:
            self.started[call["id"]] = (call["name"], self.now())
            text += self.label(call)
            if len(tool_calls) == 1:
                self.open = call["id"]
            else:
                text += "\n"
        return text

    def result(self, tool_call_id: str, content: str) -> str:
        now = self.now()
        name, began = self.started.pop(tool_call_id, ("tool", now))
        ok, outcome = summarize(name, content)
        took = f"{now - began:.1f} s"
        mark = "✓" if ok else "✗"
        if self.open == tool_call_id:
            self.open = None
            text = f" → {mark} {outcome} · {took}\n"
        else:
            text = self._close() + f"   {mark} {name} → {outcome} · {took}\n"
        if not self.started:  # the whole batch is back; the model reads the results next
            text += f"{READING}\n"
        return text

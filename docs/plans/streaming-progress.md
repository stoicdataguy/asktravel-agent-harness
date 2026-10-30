# Plan: streaming agent progress

**Goal:** while the agent works, show the user what it's doing, how far along it is and why it's
waiting, without cluttering the answer, without slowing the agent, and without building our own UI.

*Status: proposal, 2026-10-30. Supersedes issue #36 ("streaming polish"). Open WebUI behaviour
checked in its 0.12.4 source; timings from Langfuse.*

## Contents

- [1. Today](#1-today)
- [2. What users can't see](#2-what-users-cant-see)
- [3. The channel: Open WebUI's reasoning blocks](#3-the-channel-open-webuis-reasoning-blocks)
- [4. Design](#4-design)
- [5. What the user sees](#5-what-the-user-sees)
- [6. Evaluation](#6-evaluation)
- [7. Roadmap](#7-roadmap)
- [8. Risks](#8-risks)
- [9. Open questions](#9-open-questions)

## 1. Today

`openai_compat._stream` runs the agent with `stream_mode=["messages", "updates"]` and emits
OpenAI-style chunks:

| Source | What the user sees |
|---|---|
| `messages`: model tokens | the answer, as it's written |
| `updates`: each finished model step | one `🔧 tool(args)` line per tool call, **inside the answer text** |
| `updates`: tool results with an artifact | the rendered itinerary |
| an exception | a readable error line, then `[DONE]` |

This works, but:
- **The answer is cluttered.** A research turn prints 8–15 `🔧` lines above the answer, and they
  stay in the chat for good.
- **They pollute the transcript.** Open WebUI sends them back on the next turn as part of the
  assistant text. `thread_sync` strips them with a regex (`_STATUS_LINE`) before seeding a forked
  thread.
- **They only say what started.** Nothing says when a tool finished, what it found, or that a
  step is still running.

## 2. What users can't see

Measured gaps, longest first:

| Moment | How long | What the user sees now |
|---|---|---|
| A rate-limited model call waiting to retry (#51) | 30–50 s, on ~1 in 9 calls | nothing: the last `🔧` line just sits there |
| A hotel search (Playwright on Google Hotels) | 25–30 s each; two barely overlap | one `🔧 search_hotels(…)` line |
| A model step deciding what to do next | 2–3 s, up to ~15 per research turn | nothing between `🔧` lines |
| Parallel tool batches (76 seen, up to 10 tools) | 1–10 s | 10 lines at once, no sign of which finished |
| Memory being written or recalled | instant | a raw `🔧 save_memory(key=…)` line |

The first two are where users think the agent has hung.

## 3. The channel: Open WebUI's reasoning blocks

Open WebUI 0.12.4 (`utils/middleware.py`) treats a `reasoning_content` (or `reasoning`,
`thinking`) field in a streamed delta as reasoning:
- **Collapsible block.** Consecutive reasoning deltas fill one collapsible "Thinking" block with
  a live timer; the block closes, with its duration, when answer text starts.
- **Interleaving.** Reasoning that arrives *after* answer text opens a **new** block. So a turn
  can show *[▸ researched for 12 s] answer [▸ checked fares for 4 s] more answer*.
- **Not resent.** When it rebuilds the transcript for the next turn, Open WebUI **drops
  reasoning** for models like ours (`get_reasoning_format()` returns `None` unless the model is
  Ollama or llama.cpp). Progress never reaches the model or `thread_sync`.
- **Harmless elsewhere.** Other OpenAI-compatible clients ignore the unknown field.

So: **progress goes in `reasoning_content`, the answer stays in `content`.** No UI code, no
Open WebUI plugin. Rejected alternatives:

| Option | Why not |
|---|---|
| Keep `🔧` lines in `content` | The problems in §1 |
| Collapse the lines into a `<details>` block in `content` (#36's idea) | Still part of the answer, so still resent and regex-stripped; no live timer |
| Open WebUI "status" events (`__event_emitter__`) | Only available to Open WebUI functions and pipes running inside Open WebUI, not to an external OpenAI-compatible model |
| Our own UI | CLAUDE.md: no UI code of our own |

## 4. Design

**One progress stream, rendered in one place.**

```
agent.astream(stream_mode=["messages", "updates", "custom"])
   │ messages: model tokens ─────────────────────────────▶ content (the answer)
   │ updates:  model step with tool calls ─┐
   │ updates:  tool results ───────────────┤
   │ custom:   progress written by tools ──┼─▶ ProgressEvent ─▶ render ─▶ reasoning_content
   │ retry hook: waiting on a 429 ─────────┘
```

**`ProgressEvent`** (`api/progress.py`): kind, text, tool-call id, time. Kinds:

| Kind | Source | Example rendered line |
|---|---|---|
| `start` | stream opens | `Reading your trips and preferences…` (sent at once, so something shows in < 0.5 s) |
| `tool_start` | `updates` (model step) | `Searching the Italy guidebook: "Rome neighbourhoods"` |
| `tool_progress` | `custom` stream, from inside a tool | `Google Hotels: 12 hotels loaded, filtering by price` |
| `tool_end` | `updates` (tools) | `✓ 8 passages found · 0.4 s` or `✗ no flights found · 3.1 s` |
| `memory` | save/delete/recall tool calls | `Remembered: dislikes → overnight flights` |
| `waiting` | model retry hook | `The model provider is busy; retrying in 20 s (attempt 2)` |
| `step` | a model step that follows tool results | `Reading the results…` |

- **Human labels, not raw calls.** Each tool gets a short template in one table, e.g.
  `search_flights` → "Checking fares {origin} → {destination}, {depart_date}". Unknown tools
  fall back to today's `name(args)`.
- **Results summarised in code, not by the model.** A `summarize(tool, result)` per tool reads
  its compact result: hit counts, the cheapest fare, the number of hotels. No extra model calls.
- **Parallel batches** print one start line per call, then each `✓` as it finishes. This needs
  per-call results, which `updates` gives because `create_agent` runs each call as its own task.
- **Progress from inside tools.** Slow tools call LangGraph's `get_stream_writer()`
  (`"custom"` mode): hotels per page and card count, flights per query. It's a no-op outside a
  stream, so evals and tests are unaffected.
- **Retry visibility.** Today the OpenAI client retries 429s silently inside one call. Move the
  retry loop into LangChain's `ModelRetryMiddleware`, or wrap `get_chat_model()`'s retry, with an
  `on_retry` hook that writes a `waiting` event. This also gives #51 the 429 body it wants to log.
- **Render target.** `PROGRESS_STYLE=reasoning` (default) puts lines in `reasoning_content`.
  `inline` keeps today's `🔧` lines in `content`, for clients that hide reasoning. `off` emits
  nothing. Non-streaming responses are unchanged.
- **Tracing.** Progress events are UI only. Langfuse already records the steps.

## 5. What the user sees

A research turn ("plan 3 days in Rome") in Open WebUI, while it runs:

```
▾ Thinking… 9s
  Reading your trips and preferences…
  Loading the itinerary-builder playbook ✓
  Searching the Italy guidebook: "Rome one week itinerary" ✓ 6 passages
  Reading Italy guidebook p. 65 ✓
  Searching the Italy guidebook: "Colosseum Forum tickets" … 
  Searching the Italy guidebook: "Vatican Museums booking" ✓ 5 passages
```

When the answer starts, the block collapses to `▸ Thought for 14 s`. A later research step in
the same turn opens a second block. The itinerary still renders in the answer, as now. The
chat history keeps the collapsed blocks for the user to reopen; the model never sees them.

## 6. Evaluation

**`scripts/e2e_progress.py`** (browser, full stack, real model) checks:

| Check | Pass when |
|---|---|
| Time to first progress | a progress line is visible < 1 s after sending (today: ~2 s, after the first model step) |
| Answer is clean | the final answer text contains no `🔧` or progress lines |
| Progress is complete | every tool the trace shows has a start line and a `✓`/`✗` line, in the reasoning block |
| Interleaving | a turn that researches, writes, then searches again shows two reasoning blocks |
| Not resent | the next turn's request body (logged by the API in the test) has no progress text |
| Waiting is visible | with a forced 429 (test hook), a `waiting` line appears and the turn still completes |

**Regression:**
- `e2e_branching.py`: fork, regenerate and edit still work; seeding no longer needs the regex,
  though it stays for older chats.
- `e2e_memory.py`.
- Unit tests: event building from recorded `updates` and `custom` payloads, every label and
  summary template, and the three render styles.

## 7. Roadmap

| Phase | Scope | Effort |
|---|---|---|
| **P0 Spike** | Stream a hand-made `reasoning_content` sequence (reasoning, content, reasoning, content) to Open WebUI; confirm the two blocks, the timers, what's stored, and that the next request omits them. Record screenshots here | ½ day |
| **P1 Progress in reasoning blocks** | `ProgressEvent` + renderer; `start`, `tool_start`, `tool_end`, `memory`, `step`; human labels and code summaries for all 13 tools; `PROGRESS_STYLE` setting (+ `.env.example`); `e2e_progress.py` | 1–2 days |
| **P2 Progress from inside tools** | `get_stream_writer` in `search_hotels` and `search_flights`; `"custom"` stream mode | ½ day |
| **P3 Waiting is visible** | Model retries via a hook that emits `waiting` and logs the 429 body (with #51) | ½–1 day |
| **P4 Docs** | architecture.html (data flow: progress channel), README screenshots, CLAUDE.md conventions (labels table, "progress never in `content`") | ½ day |

Each phase is one PR. P1 alone fixes the clutter; P2 and P3 fix the "is it stuck?" moments.

## 8. Risks

| Risk | Mitigation |
|---|---|
| Open WebUI changes how it handles `reasoning_content` | P0 records the behaviour; `e2e_progress.py` catches a change; `PROGRESS_STYLE=inline` is the fallback |
| Reasoning blocks look like the model's thinking | Labels are plain actions ("Searching…", "Checking fares…"), not first-person thoughts; the block title is Open WebUI's |
| Very long research turns make long blocks | One line per tool; collapsed by default once the answer starts |
| Progress text leaks private arguments (emails, ids) | Labels template only chosen fields; trip ids and user ids never shown |
| Moving retries changes error behaviour | P3 keeps the same retry counts and backoff; `test_resilience.py` covers 429 → readable error |

## 9. Open questions

- **Keep `🔧` lines as an option at all?** They help when debugging in other clients; `inline`
  costs little to keep.
- **Should the itinerary also stream progressively** (day by day) instead of appearing at once?
  It's produced by one tool call, so it would need a different design. Out of scope here.
- **Percentages?** The agent can't know how many steps remain. Show counts ("3 of 5 searches
  done") only within a parallel batch, where the total is known.

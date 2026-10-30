# Plan: AskTravel memory architecture

**Goal:** make AskTravel's memory deliberate. Name every kind of memory the agent has, say where
each one lives today, decide what should expire and when, and design the context window the
ReAct agent reasons over. The framing follows *CS329Z Lecture 7: Agent Memory Architectures*
(CoALA memory types, the What/When/How questions on page 24, and the four architecture patterns).

*Status: proposal, revised 2026-10-29 after a review against the code and the installed
LangChain 1.4 / LangGraph 1.2 (see [What changed in this revision](#what-changed-in-this-revision)).
Inventory and numbers measured on the running system on 2026-10-28.*

## Contents

- [1. Memory types, in one table](#1-memory-types-in-one-table)
- [2. Where each memory lives today](#2-where-each-memory-lives-today)
  - [Working memory: the context window](#working-memory-the-context-window)
  - [Episodic memory](#episodic-memory)
  - [Semantic memory](#semantic-memory)
  - [Procedural memory](#procedural-memory)
- [3. What, When and How (lecture page 24)](#3-what-when-and-how-lecture-page-24)
- [4. Gaps against the lecture](#4-gaps-against-the-lecture)
- [5. Growth and TTL policy](#5-growth-and-ttl-policy)
- [6. Context window design](#6-context-window-design)
- [7. Roadmap](#7-roadmap)
  - [What changed in this revision](#what-changed-in-this-revision)
- [8. Evaluating memory](#8-evaluating-memory)
- [9. Open questions](#9-open-questions)

## 1. Memory types, in one table

The lecture uses CoALA's four types. Each can be **parametric** (in the model weights) or
**context** (retrieved into the prompt). AskTravel does no fine-tuning, so everything we control
is context memory.

| Type | Lecture definition | In AskTravel |
|---|---|---|
| **Working** | What the agent currently sees: the context window | System prompt (rules, skills index, profile facts, trips) + tool schemas + this chat's messages |
| **Episodic** | Past behaviour and conversations, usually raw logs | Checkpointed chat threads; also Open WebUI's chat copies and Langfuse traces (not readable by the agent) |
| **Semantic** | Facts about the world and the user | *User:* profile facts and trips. *World:* Lonely Planet guidebooks, live flight and hotel results, and the model's own knowledge (parametric) |
| **Procedural** | Rules and skills: *how* to do tasks | System prompt rules, 7 skill files, tool docstrings and schemas, and the model's instruction following (parametric) |

## 2. Where each memory lives today

| Memory element | Type | Stored in | Written | Read | Scope | Size today | TTL today |
|---|---|---|---|---|---|---|---|
| System prompt rules | Procedural | Code: `agent/prompts.py` | By developers | Every model call | All users | ~370 tokens | n/a (versioned in git) |
| Skills index (names + "Use when…") | Procedural | Files: `skills/*.md` frontmatter | By developers | Every model call | All users | ~470 tokens | n/a |
| Skill bodies (7 playbooks) | Procedural | Files: `skills/*.md` | By developers | On demand: `load_skill` | All users | 395–820 tokens each | n/a |
| Tool schemas (12 tools) | Procedural | Code: tool docstrings + Pydantic models | By developers | Every model call | All users | **~3.8k tokens** (`present_itinerary` alone 1.4k) | n/a |
| Profile facts (`diet`, `home_airport`, `interests`…) | Semantic (user) | Postgres `store`, namespace `(email, "memories")`, one row per key | Agent calls `save_memory` / `delete_memory`, in the conversation | Every model call: up to 50 facts injected | Per user | 1 fact (demo user) | **None** |
| Trips (destination, dates, lodging, transport, travellers, notes) | Semantic (user, time-bound) | Postgres `store`, `(email, "trips")`, one row per trip | Agent calls `save_trip_details`; automatically by `present_itinerary`; startup backfill | Every model call: one line per trip, up to 8, past trips hidden after 30 days; full detail via `get_trip` | Per user | 1–2 per test user | **None** |
| Saved itineraries (structured plan) | Episodic artifact / semantic | Postgres `store`, `(email, "itineraries")`, one per chat | `present_itinerary` (latest replaces) | `get_trip`, `GET /api/itineraries` | Per user + chat | 2 (demo user) | **None** |
| Chat threads (every message, tool call and tool result) | Episodic (raw) + working | Postgres `checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, key `email:chat_id` | Automatically after **every agent step** | Start of each turn (whole thread loaded) | Per user + chat | 17 threads, **209 checkpoints, ~7 MB** | **None** |
| Open WebUI chat copies | Episodic (UI) | Open WebUI's tables, in our Postgres (schema `open_webui`, since M4b) | Open WebUI, every message | Displayed to the user; resent as the transcript (used only to resync branches) | Per user | n/a | User deletes chats manually |
| Langfuse traces | Episodic (observability) | Langfuse (ClickHouse + Postgres + MinIO) | Every turn when tracing is on | By developers only | All | n/a | **None** configured |
| Guidebooks | Semantic (world) | Files: `data/guidebooks/*.md` (≈16 MB from 7 PDFs) | Offline: `travel-agent-ingest` | On demand: `search_guidebooks`, `read_guidebook_pages` | All users | 7 books | n/a (editions are 2016-era) |
| Live flights / hotels | Semantic (world, volatile) | Not stored; fetched from Google per call | n/a | On demand: `search_flights`, `search_hotels` | n/a | n/a | n/a (results live on only inside the thread) |
| Model knowledge and instruction following | Semantic + procedural (parametric) | `openai/gpt-6-luna` weights | n/a | Always | n/a | n/a | n/a |
| User feedback | Signal for procedural learning | Open WebUI only (see [feedback plan](feedback-audit.md)) | User clicks 👍/👎 | Nobody yet | Per message | 0 rows | n/a |

### Working memory: the context window

Rebuilt on **every model call**, and a single research-heavy turn makes about 15 model calls:

```
┌ system ─────────────────────────────────────────────────────┐
│ base rules (incl. today's date)                     ~370 tok │  static, except the date
│ skills index (7 × "Use when…")                      ~470 tok │  static
│ Memory: profile facts (≤ 50)                                │  per user; changes on save_memory
│ Trips: one line per trip (≤ 8)              ~660 tok together │  per user; changes on save_trip_details
├ tools ──────────────────────────────────────────────────────┤
│ 12 tool schemas                                    ~3,800 tok │  static
├ messages (this chat's checkpointed thread, in full) ────────┤
│ human / ai / tool-call / tool-result … for every past turn  │  grows without limit
│ newest user message                                         │
└─────────────────────────────────────────────────────────────┘
```

Measured across the 17 stored threads:

| Thread | User turns | Messages | ~Tokens | Share that is tool results |
|---|---|---|---|---|
| Largest | 4 | 54 | **27,284** | **73%** |
| 2nd | 4 | 25 | 20,403 | 72% |
| 3rd | 1 | 16 | 15,494 | **88%** |
| Median | – | – | 6,328 | – |

Guidebook page reads (up to 15,000 characters each) and search results make up most of every
thread, and they stay in context for the rest of the conversation.

### Episodic memory

- **The checkpointer is the only episodic memory the agent can read, and only for the current chat.**
  - In a new chat, the agent has **no access to past conversations**. Trips and profile facts are the only bridge, and they hold extracted facts, not what happened.
  - The checkpointer also stores a full snapshot after **every step**: 3 per plain turn, 5 for one tool call, 22 for a long research turn. That's what makes rewind and branching work (issue #3), and it's also the biggest growth driver: 5 MB of blobs for 17 threads.

### Semantic memory

- **User semantic memory is written in-band ("hot path"):** the agent decides during the conversation whether to call `save_memory` or `save_trip_details`. Nothing consolidates or checks these writes afterwards. Two consequences already visible in the data:
  - A single `interests` fact grew into a run-on blob ("Big astronomy nerd; owns a smart telescope …; adventure-sports junkie…") because the agent appends to one key. `save_memory` already caps values at 300 characters, which truncates the blob but doesn't fix it.
  - The same real trip got **different trip ids** in different chats (`hawaii-the-big-island-undated`, `kona-big-island-hawaii-undated`, `kona-hawaiis-big-island-undated`), because ids come from whatever destination string the model wrote.
  - **Trip ids never change after creation**, so they go stale. A trip created as `international-destination-tbd-undated` later became Rome, Nov 23–29, and kept its old id. A trip that gets dates keeps its `-undated` suffix.
  - **The agent misreads dates.** Asked on 2026-10-29 "where am I going in the next 2 months?", it listed Rome (Nov 23) and the Big Island (Dec 28) and then said only the Big Island trip "starts within the next two months". The prompt gives absolute dates only and leaves the arithmetic to the model.
- **No provenance:** a fact doesn't record which chat or message created it, so it can't be explained, audited or undone precisely.
- **World semantic memory is read-only and outdated:** the guidebooks are 2016 editions. Live tools cover prices, but their results aren't kept beyond the chat thread.

### Procedural memory

- **All of it is hand-written and static:** prompt rules, skills and tool descriptions, versioned in git.
- **There's no learning loop:** unlike Voyager's skill library or Agent Workflow Memory, the agent never turns a successful trajectory into a reusable procedure, and user feedback doesn't reach skills.
- **Skills follow the lecture's "skill files" pattern** (page 39): a name and description in context, the body loaded on demand. This is the right shape. What's missing is a safe way to update them.

## 3. What, When and How (lecture page 24)

The lecture asks, for any memory system: **what** gets stored and retrieved, **when** storage
and retrieval are triggered (agent tools on demand, background workers, event-driven), and
**how** it's stored and retrieved (filesystem, database/retrieval, knowledge graph).

### Today

| Memory | **What** | **When** (store / retrieve) | **How** (store / retrieve) |
|---|---|---|---|
| Working | Rules, skills index, facts, trips, tool schemas, full thread | Rebuilt every model call | In-process, by the `dynamic_prompt` middleware |
| Episodic: thread | Every message and tool result of one chat | **Store:** event-driven, after each agent step. **Retrieve:** start of every turn | Postgres checkpointer; loads the whole thread, no selection |
| Semantic: profile facts | Lasting preferences | **Store:** agent tool, on demand. **Retrieve:** every model call | Postgres store (key → value); injects all facts, no search |
| Semantic: trips | Per-trip details + linked plan | **Store:** agent tool + event (`present_itinerary`) + startup backfill. **Retrieve:** every call (summary), agent tool (`get_trip`) | Postgres store; summary line injected; detail fetched on demand |
| Semantic: world | Guidebooks; live fares and rates | **Store:** offline ingest (guidebooks), never (live). **Retrieve:** agent tools | Files + keyword grep; Google via fast-flights / Playwright |
| Procedural | Rules, skills, tool docs | **Store:** developer commits. **Retrieve:** index every call, body via `load_skill` | Filesystem (`skills/*.md`) + code |

### Proposed

| Memory | **What** (changes) | **When** (changes) | **How** (changes) |
|---|---|---|---|
| Working | Same parts, under a **token budget** (§6); earlier turns **compacted** to their user message and final answer | Every call, as now | Our own `wrap_model_call` middleware that edits only the request, never the saved thread (§6) |
| Episodic: thread | Unchanged. Pruning waits for production (§5) | Store per step (needed for rewind) | Same tables |
| **Episodic: past chats (new, M3)** | The user's other chats, as saved: each turn's user message and final answer | **Store:** nothing new; a small chat index `(email, "chats")` is written after every run (no model call). **Retrieve:** `recall_conversations(query)` tool, when the user refers back | Keyword search over the checkpointed threads (raw logs, CoALA's episodic memory). Summaries and pgvector only if keyword recall falls short: per-turn summaries would cost a model call per turn against the rate limit |
| Semantic: profile facts | Same facts, plus `source` (thread id) and `confirmed_at`; list-valued keys (`likes`, `dislikes`, `interests`) hold one item per entry | **Store:** agent tool, with rules applied on write (see How). LLM consolidation only for genuine conflicts ("vegetarian" vs "I eat fish now"), and only if evals still show them after M1. **Retrieve:** every call | Postgres store; `save_memory` adds items to list keys instead of rewriting one string, and returns the previous value when it overwrites |
| Semantic: trips | Same, with an **opaque, stable trip id** and a **status and relative date computed when the prompt is built** ("upcoming, in 25 days"; never stored, so it can't go stale) | **Store:** on write, a new trip is matched to an existing one by normalised destination and overlapping dates; an undated trip is claimed when its dates arrive. Destination can be renamed without changing the id | Postgres store; a one-off migration merges today's duplicates and re-keys trips |
| Semantic: world | Cache live results briefly (fares 6 h, hotel rates 24 h) so a follow-up doesn't re-scrape | Store on each live search; retrieve before searching | Store namespace `("cache", "flights"\|"hotels")` with TTL. Low priority: compacted turns make re-asking rarer than re-reading |
| Procedural | Skills stay hand-owned; **proposals** come from feedback and successful trajectories | **Background, weekly:** mine 👎 reasons and repeated tool sequences, open a PR against `skills/*.md` for a human to review | Filesystem + git (a human-reviewed PR is the safety boundary against poisoned procedures, lecture p. 98) |

## 4. Gaps against the lecture

Mapped to the lecture's "problems without memory" (p. 9) and open challenges (p. 96–100):

| Lecture concern | Where AskTravel stands | Priority |
|---|---|---|
| *Loss of progress* in long sessions | Threads grow unbounded; tool output is 72–92% of them; nothing is summarized | **High**: cost per turn and quality degrade first |
| *Loss of knowledge* across sessions | Facts and trips carry over; **conversations don't**: "what did we decide about Peru last week?" can't be answered | **High** |
| *Loss of procedure* | Skills fix this for known tasks; no learning from experience | Medium |
| Choosing what to remember / forget | Agent-decided writes, no consolidation, nothing ever forgotten; trip ids go stale and duplicate | **High** (duplicate and misnamed trips already exist) |
| Using memory correctly: time | Trips carry absolute dates only, and the model gets "within the next two months" wrong | **High** (seen in a real chat, §2) |
| Trust, privacy, user control | No provenance; no "what do you remember" view beyond asking the agent; no export/delete flow; deleting a chat in Open WebUI leaves its thread in our database; memories are agent-writable from any message (a prompt-injection vector) | **High before production**: must land before the v1 deploy (#30) |
| Using memory correctly (over-personalization, p. 100) | All facts and trips are injected into **every** call, relevant or not; the prompt asks the agent to mention facts it used, but that isn't measured | Medium |

## 5. Growth and TTL policy

**Principles:**
- Expire what's **derived or volatile**. Keep what the user **explicitly told us** until they change it.
- Prefer **archiving into a summary** over plain deletion, so episodic knowledge survives in compact form.
- Feedback and audit data have their own retention (2 years, [feedback plan](feedback-audit.md)) and are never expired by memory policies.
- **Deletion the user asks for comes first; storage housekeeping comes last.** Following a chat
  deletion through to its thread is part of user control (M4) and lands before production.
  Pruning and TTLs (M6) wait until production volume makes them matter: today all threads
  together are about 7 MB.

| Element | Policy | Mechanism |
|---|---|---|
| Checkpoints: intermediate step snapshots | Keep for **7 days**, then keep only **turn-boundary** checkpoints (those that rewind and branching need) | Nightly job with **our own SQL**: deletes non-boundary rows in `checkpoints` / `checkpoint_writes` and blob versions no remaining checkpoint references. LangGraph's `aprune(strategy="keep_latest")` can't be used: it keeps only the newest checkpoint, so regenerate and edit would fall back to rebuilding the thread from the UI transcript |
| Checkpoints: whole thread | Delete **180 days after the last message**, after its conversation summary exists; delete **immediately** when the user deletes the chat in Open WebUI | Nightly job; chat-deletion hook (once Open WebUI is on Postgres, per the feedback plan, a trigger on its `chat` table) |
| Conversation summaries (new) | **2 years** from last activity; refreshed if the chat resumes | Store TTL (`ttl` on `aput`, `refresh_on_read=False`) |
| Profile facts | **No automatic expiry.** Track `confirmed_at`; facts unconfirmed for **12 months** are shown to the agent as "may be outdated" so it re-confirms naturally; users can delete any time | Field + prompt wording; deletion via `delete_memory` / a user-facing view |
| Sensitive facts (health, nationality, companions' details) | Saved only with an explicit user statement; **12-month TTL** unless reconfirmed | `sensitivity` field + store TTL |
| Trips | Active until `end_date`; **archived 30 days after the trip ends** (dropped from the prompt, kept for "last time in Hawaii…"); **deleted 2 years after `end_date`**. Undated trips expire **180 days** after their last update | Store TTL computed from `end_date`. Archiving needs no job: status is computed from the dates whenever the prompt is built (already how `format_trips` hides past trips) |
| Saved itineraries | Follow their trip's lifecycle; orphans (no trip) deleted after 180 days | Same job as trips |
| Live-search cache (new) | Flights **6 hours**, hotels **24 hours** | Store TTL; LangGraph's Postgres store supports per-item `ttl` and a background sweeper (`start_ttl_sweeper`) |
| Langfuse traces | **90 days** | Langfuse data-retention setting |
| Open WebUI chats | User-controlled; agent-side data follows deletion (above) | Trigger on `open_webui.chat` → `chat_deletions` queue → API deletes the thread and index entry (M4b) |
| Guidebooks, skills, prompts | No TTL; versioned files. Guidebook refresh is a content task (newer editions) | n/a |

**Expected effect:**
- **Storage:** checkpoint growth becomes roughly linear in *turns*, not *steps*. That's about 4–7× fewer rows, judging by today's 209 checkpoints for about 40 turns.
- **Prompt size:** stays bounded per user, because past trips leave the prompt automatically.

## 6. Context window design

`gpt-6-luna` accepts about 1M tokens, so the limit isn't size. It's **cost, latency and attention**:
- **Cost:** every model call resends the whole window, and a research turn makes about 15 calls. A turn at the 27k-token thread size therefore sends **about 400k input tokens**. Cost and latency grow with conversation length, not with the new question.
- **Attention:** the lecture notes that models misuse what's in context (p. 73, p. 100). Old guidebook pages about a different day of the trip are noise.

**Observations about today's design:**
1. **The order is right; the volatile parts are in the wrong place.** Static instructions come first, which suits prompt caching. But today's date and the user's facts and trips sit **inside** the system prompt, so any `save_memory` mid-turn, and every new day, changes the cached prefix. **M0 confirmed caching works:** 91% of input tokens in the baseline were cache reads (§8), so a stable prefix is a real design constraint.
2. **Tool schemas are the largest fixed cost** (about 3.8k tokens, 70% of the fixed prefix). `present_itinerary`'s nested schema is 1.4k by itself and is needed only at the end of planning.
3. **Tool results are never released.** A guidebook page read for day 1 is still in context while the agent writes day 4, and in every later turn.
4. **Memory injection is all-or-nothing.** All facts and trips go into every call, even "thanks, that's all".
5. **The skills design is already efficient:** names and descriptions stay in context, bodies are loaded on demand. Keep it.

**Proposed layout and budget** (soft targets enforced by middleware; numbers are a starting point
to tune with the evals in §8):

| Order | Block | Budget | Change |
|---|---|---|---|
| 1 | Base rules + skills index | ~0.9k | Unchanged; **today's date moves out** |
| 2 | Tool schemas | ~3.8k → ~2.5k | Trim docstrings; flatten the `Itinerary` schema descriptions; optionally `LLMToolSelectorMiddleware` to expose only relevant tools per call |
| ― | *cache boundary: everything above is identical for all users and all calls* | | |
| 3 | User context: date, profile facts, current trips with computed status and relative dates | ≤ 1.5k | Separate message after the static prefix; trips limited to upcoming or active ones. Past conversations are **not** injected: the agent asks for them with `recall_conversations` (M3) |
| 4 | Earlier turns, **compacted** | ≤ 8k | Each earlier turn is sent as its user message, its final answer and a one-line stub of the tools it used ("used: read_guidebook_pages Hawaii pp. 88–91"); tool calls and results are dropped. The agent can re-run a tool if it needs the detail again |
| 5 | Current turn, verbatim | ≤ 12k | Unchanged inside the turn; `read_guidebook_pages` already caps at 15k characters |

**Turn compaction, not summarization.** Tool results are 72–88% of the measured threads, so
dropping them from earlier turns captures most of the saving. Compaction is our own small
`wrap_model_call` middleware:
- **No LLM call.** It's deterministic and unit-testable with fake models.
- **The saved thread is untouched.** It edits only the request sent to the model, so
  `thread_sync`, rewind and branching keep working.
- **Cache-friendly.** A turn is compacted once, when the next one starts, so the prefix changes
  once per turn rather than on every call.

Two library middlewares were considered and rejected:
- **`SummarizationMiddleware` breaks branching.** In LangChain 1.4 it writes
  `RemoveMessage(REMOVE_ALL_MESSAGES)` plus the summary **into the saved state**. After the first
  summary, `thread_sync.plan_run` no longer finds the transcript's user messages in the thread and
  takes the REPLACE path on every message, reseeding the thread from the UI's copy and losing
  tool results and the summary itself. If very long chats ever need summaries, compute them per
  request and cache them in the store keyed by thread and turn, never in the thread.
- **`ContextEditingMiddleware` is safe but the wrong trigger.** It also edits only the request,
  but `ClearToolUsesEdit` clears by total token count rather than by turn, so what gets cleared
  shifts from call to call.

**Target:** about 15–25k tokens per call regardless of chat length. Today a 4-turn chat is already
at 27k tokens plus a 5.3k prefix, and grows without limit.

## 7. Roadmap

Ordered by evidence first, then value and risk. Each phase is its own PR with unit tests and an
end-to-end check, like the features so far. Every phase after M0 reports its effect against the
M0 baseline in its PR.

| Phase | Scope | Lecture pattern | Effort |
|---|---|---|---|
| **M0 Baseline** ✅ | Log input (and cached) tokens per model call to Langfuse (already there: usage comes back on every generation); `scripts/eval_memory.py` with the §8 cases that run today; baseline recorded in §8; prompt caching confirmed | Memory evaluation (p. 68) | 1 day |
| **M1a Trips that stay correct** (#21) ✅ | Opaque, stable trip ids (`trip-xxxxxx`); on write without a known id, match an existing upcoming trip by destination words and compatible dates, never merging when ambiguous; status computed for the prompt ("upcoming, in 25 days"); `source` (thread id); startup migration that re-keys trips, merges duplicates, keeps old ids as aliases and repoints itineraries | Memory maintenance, done on write | 1 day |
| **M1b Facts that stay tidy** (#52) ✅ | List-valued fact keys (`likes`, `dislikes`, `interests`); `save_memory` returns the previous value on overwrite; `source` and `confirmed_at` on facts; migration that splits today's blobs | Memory maintenance, done on write | 1 day |
| **M2 Context budget** ✅ (turn compaction, #20) | Turn-compaction middleware (§6). Moving date, facts and trips out of the system prompt and trimming tool schemas (`present_itinerary` is 1.4k tokens) are split out as a follow-up: compaction alone met the budget | Tiered hierarchy (MemGPT/MemoryOS: working vs recall) | 1–2 days |
| **M3 Episodic recall** ✅ (#24) | Chat index written after every run + startup backfill; `recall_conversations(query)` keyword-searches the user's other chats (user message + final answer per turn) and returns the best turns from up to 3 chats. No summaries, no extra model calls; summaries/pgvector only if the eval shows keyword search falls short | Tiered hierarchy (archival search) | 1 day |
| **M4a User control** ✅ (#25) | `GET /api/memory` (what it remembers), `GET /api/memory/export` (everything incl. chat messages), `DELETE /api/memory?confirm=<email>` (every user namespace + threads, found generically); `sensitive` flag on facts. **Before the v1 deploy (#30)** | Trust, privacy, user control | 1 day |
| **M4b Chat deletion follow-through** ✅ (#58) | Open WebUI moved onto our Postgres (schema `open_webui` in the same database, so a trigger can reach our tables); existing SQLite data copied over; a trigger on its `chat` table queues deletions and the API deletes the thread and chat-index entry within seconds. The "facts only from the user's own words" guard moved to the web tool (#33) | Trust, privacy, user control | 1 day |
| **M5 LLM consolidation** | Only if M0's preference-evolution and duplicate-trip cases still fail after M1: a background Mem0-style ADD/UPDATE/DELETE/NOOP pass over facts and trips for conflicts that rules can't catch | Reflective consolidation | 1–2 days, maybe never |
| **M6 Retention** | Turn-boundary checkpoint pruning (our own SQL, §5); store TTLs + `start_ttl_sweeper`; Langfuse retention; live-search cache | Memory maintenance | 1 day, at production scale |
| **M7 Procedural learning** | Weekly job: cluster 👎 reasons and comments by skill and tool, and mine successful itinerary trajectories into "workflow" notes (AWM); open a **PR** proposing skill edits for human review | Learning procedures from experience (AWM, skill files) | 2 days, after the feedback plan |

### What changed in this revision

The first draft (2026-10-28) had the right inventory and diagnosis. A review against the code
changed the order and three designs:

| Change | Why |
|---|---|
| **M0 baseline added first** | §6's budgets were "to tune with the evals in §8", but the evals came last and token logging was buried in the old M1. Every phase now has numbers to beat |
| **Trip and fact hygiene moved to M1, done on write** | The stale and duplicate trip ids and the date mistake are live bugs (§2). Matching on write and computed status fix them without an LLM, a scheduler or a stored `status` field that goes stale |
| **`SummarizationMiddleware` dropped for turn compaction** | It rewrites the saved thread, which breaks `thread_sync` and rewind (§6). Compaction gets most of the saving with no LLM call |
| **Episodic summaries written after each turn, retrieved on demand** | The old "idle for 30 minutes" trigger needed a scheduler we don't have. Auto-injecting the top-3 summaries on every model call (~15 per research turn) is replaced by a tool. Keyword search before pgvector, which the `postgres:16-alpine` image doesn't include |
| **Chat deletion moved from retention to user control, ahead of production** | It's a privacy obligation, not housekeeping |
| **LLM consolidation demoted to "only if needed"** | Rules on write cover duplicates and blob facts; what's left is preference conflicts, which M0 will show are or aren't a problem |
| **Pruning deferred, and needs our own SQL** | Threads total ~7 MB. LangGraph's `aprune(keep_latest)` would break rewind (§5) |

Old to new phase numbers, for issues #20–#26: M1 → M2, M2 → M1, M3 → M4 (deletion) + M6
(pruning, TTLs), M4 → M3 (summaries) + M1 (rules) + M5 (LLM pass), M5 → M3, M6 → M4,
M7 unchanged.

## 8. Evaluating memory

The lecture's bar for memory evals (p. 68): **realistic, long-context, challenging, verifiable**.
We have small versions of the first two (`e2e_memory`, `e2e_branching`). M0 added
`scripts/eval_memory.py`: it runs the real agent in-process against Postgres (no API server),
each case as a throwaway user, 3 runs per case (the model isn't deterministic), and reports a
pass rate per case plus input and cached tokens per model call. Trips are seeded with dates
relative to the day it runs. `--quick` (one run each, no long chat) takes a few minutes; the full
suite takes about 15, paced by the provider's rate limit.

| Case | Checks | Inspired by |
|---|---|---|
| `cross_chat_recall` | A stay mentioned in one chat: a new chat names sunset spots near it and says which town, without asking | LoCoMo single-hop |
| `time_relative` | Trips seeded at +25, +60, +120 days (two under stale ids): count in the next 2 months, days until Hawaii, next trip, and the open wording from §2 | LongMemEval temporal reasoning |
| `trip_identity` | Europe (undated) → Rome with dates → a hotel, over three chats: one trip, Rome, hotel saved, id not stale | Consolidation |
| `preference_evolution` | "I'm vegetarian", later "I eat fish now": exactly one diet fact, mentioning fish | PersonaMem |
| `conversation_recall` | Chat 1: the agent recommends 3 gelato shops. Chat 2: "which did you recommend?" needs 2 of 3. The agent's own words never go into facts or trips, so only episodic memory can answer. (A first version recalled a *user's* decision and passed, because the agent saved it as a trip note) | LongMemEval |
| `over_personalization` | With facts and a trip saved, "what's the capital of Peru?" mentions none of them | Hu et al. (p. 100) |
| `forgetting` | "Forget my dietary preference" deletes that fact and keeps the others | TTL policy |
| `long_thread` | 10-turn Rome planning chat (in the guidebooks, so it reads pages): turn-1 constraints recalled at turn 10; no call over 25k input tokens | Context budget |

Not built yet: expiry and chat deletion (with M4/M6), and regenerate/edit after compaction (M2
runs `e2e_branching.py` for that).

### Baseline (2026-10-29, `openai/gpt-6-luna`)

| Case | Passed | Calls | Mean input | p95 | Max | Cached | Expected to change with |
|---|---|---|---|---|---|---|---|
| `cross_chat_recall` | 3/3 | 25 | 5,660 | 10,627 | 12,173 | 86% | |
| `time_relative` | 3/3 | 12 | 3,958 | 3,978 | 3,978 | 100% | |
| `trip_identity` | **0/3** | 18 | 3,959 | 4,083 | 4,083 | 49% | M1 |
| `preference_evolution` | 3/3 | 12 | 3,882 | 3,927 | 3,927 | 100% | |
| `conversation_recall` | **0/3** | 19 | 5,270 | 9,010 | 9,010 | 89% | M3 |
| `over_personalization` | 3/3 | 3 | 3,878 | 3,878 | 3,878 | 100% | |
| `forgetting` | 3/3 | 6 | 3,873 | 3,885 | 3,885 | 100% | |
| `long_thread` | **0/1** | 44 | **42,972** | 68,501 | **70,013** | 93% | M2 |
| **All** | **15/22** | 139 | 16,781 | 65,023 | 70,013 | 91% | |

What the baseline says about the roadmap:
- **M1's trip identity bug reproduces every time:** one trip, correctly Rome with its hotel, but
  always under the id `europe-undated`.
- **The date mistake from §2 does not reproduce.** All 12 time questions were answered correctly,
  including the open wording from the bug report. Relative dates in the prompt stay in M1 because
  they're cheap, but they're a hardening step, not a fix for a measured failure.
- **The context budget is the biggest measured gap.** The 10-turn Rome chat reaches 70k input
  tokens per call (the §6 target is 25k), and still recalls its turn-1 constraints.
- **Prompt caching works and changes the cost argument.** 91% of all input tokens were cache
  reads, 93% in the long chat. OpenRouter reports cache reads and writes for `gpt-6-luna` on
  every Langfuse generation. Cached tokens are cheaper, so M2's case rests more on latency,
  attention and the provider's rate limit than on price, and M2 must keep the prefix stable to
  keep these hit rates.
- **Episodic recall is absent, as expected:** "I can't see the earlier chat."
- **Preference updates, forgetting and restraint already work** (3/3 each), so M5's LLM
  consolidation has no failing case to justify it yet.

### Progress against the baseline

| Phase | Runs passed | Changed cases | Tokens |
|---|---|---|---|
| Baseline (2026-10-29) | 15/22 | | fixed prompt ~3.9k; long chat max 70,013 |
| M1a trips (2026-10-29) | **18/22** | `trip_identity` 0/3 → **3/3**; all others unchanged | fixed prompt +50 tokens (trip status and its prompt line); long chat max 71,326 (run-to-run noise) |
| M1b facts (2026-10-30) | 18/21 (long chat not run: unaffected) | none: `preference_evolution`, `forgetting`, `over_personalization` stay 3/3 | not recorded (run stopped before its token report); the prompt grew by the list-key instructions |
| M2 turn compaction (2026-10-30) | 2/2 (`long_thread`, `time_relative`, 1 run each) | `long_thread` 0/1 → **1/1**: max input per call 70,013 → **16,560**, mean 42,972 → **9,405** (−78%), 44 → 35 calls; turn-1 constraints still recalled | cache-read share 93% → 84% (each turn is compacted once, when the next starts) |
| M3 episodic recall (2026-10-30) | 4/4 (`conversation_recall` ×3, `over_personalization` ×1) | `conversation_recall` 0/3 → **3/3** (all three shops named each time); no unprompted recall | fixed prompt ~4.5k (+ the `recall_conversations` schema and prompt line); no extra model calls per turn |
| M4a user control (2026-10-30) | 2/2 (`forgetting`, `over_personalization`, 1 run each) | none (no eval case targets it); delete-all verified against Postgres: 0 rows left for the user, other users untouched | fixed prompt ~4.2k |
| **Full suite after M0–M4a** (2026-10-30) | **22/22** (baseline 15/22) | every case passes | fixed prompt ~4.4k (baseline ~3.9k: new tools and prompt lines); long chat max **13,917** (baseline 70,013); mean per call 5,469 (baseline 16,781); 115 calls (baseline 139); cache-read share 74% (baseline 91%) |

## 9. Open questions

- **Embeddings, if keyword recall falls short:** OpenRouter or a local model? Either way the Postgres image changes from `postgres:16-alpine` to `pgvector/pgvector:pg16`, and the store gets an `index` config.
- **Consolidation model (M5, if ever):** the same `gpt-6-luna` or a cheaper one? It counts against the provider's ~20 requests/min limit. (M3 avoided the question by searching raw chats instead of summarising them.)
- **Trip id format for M1:** a short random id (`t_3f9a`) or a readable slug that is regenerated on rename? Opaque ids are stable; readable ones show up in the `🔧 get_trip(...)` status lines users see.
- **Default expiry for profile facts:** should they expire at all? The proposal says no, but "may be outdated" after 12 months.
- **Who reviews skill-change PRs from M7,** and how often?

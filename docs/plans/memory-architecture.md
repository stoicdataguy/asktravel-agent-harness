# Plan: AskTravel memory architecture

**Goal:** make AskTravel's memory deliberate. Name every kind of memory the agent has, say where
each one lives today, decide what should expire and when, and design the context window the
ReAct agent reasons over. The framing follows *CS329Z Lecture 7: Agent Memory Architectures*
(CoALA memory types, the What/When/How questions on page 24, and the four architecture patterns).

*Status: proposal. Inventory and numbers measured on the running system on 2026-10-28.*

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
| Open WebUI chat copies | Episodic (UI) | Open WebUI SQLite volume | Open WebUI, every message | Displayed to the user; resent as the transcript (used only to resync branches) | Per user | n/a | User deletes chats manually |
| Langfuse traces | Episodic (observability) | Langfuse (ClickHouse + Postgres + MinIO) | Every turn when tracing is on | By developers only | All | n/a | **None** configured |
| Guidebooks | Semantic (world) | Files: `data/guidebooks/*.md` (≈10 MB from 5 PDFs) | Offline: `travel-agent-ingest` | On demand: `search_guidebooks`, `read_guidebook_pages` | All users | 5 books | n/a (editions are 2016-era) |
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
  - A single `interests` fact grew into a run-on blob ("Big astronomy nerd; owns a smart telescope …; adventure-sports junkie…") because the agent appends to one key.
  - The same real trip got **different trip ids** in different chats (`hawaii-the-big-island-undated`, `kona-big-island-hawaii-undated`, `kona-hawaiis-big-island-undated`), because ids come from whatever destination string the model wrote.
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
| Working | Same parts, under a **token budget** (§6); old tool results cleared; long threads summarized | Every call, as now | `ContextEditingMiddleware` + `SummarizationMiddleware`; cache-friendly ordering |
| Episodic: thread | Unchanged, but **pruned**: intermediate step checkpoints dropped after a week | Store per step (needed for rewind); **background pruning** | Same tables + nightly maintenance job |
| **Episodic: conversation summaries (new)** | One summary per chat: goal, decisions, open questions, trip id, outcome | **Background**, once a chat has been idle ~30 min ("sleep-time" consolidation, lecture p. 58) | Postgres store `(email, "episodes")` with **semantic search** (pgvector index) + `recall_conversations(query)` tool |
| Semantic: profile facts | Same facts, plus `source` (chat + message), `confirmed_at`, `sensitivity` | **Store:** agent tool *and* background consolidation that resolves duplicates and conflicts with Mem0's **ADD / UPDATE / DELETE / NOOP** (lecture p. 64). **Retrieve:** every call, capped by budget | Postgres store; one fact per key with a length limit; the consolidator splits blobs like `interests` |
| Semantic: trips | Same, with a **canonical trip identity** (destination + dates matched, not the model's free-text slug) and a `status` (planning / booked / travelling / past) | Consolidator merges duplicate trips; status updated from dates | Postgres store + TTL tied to `end_date` (§5) |
| Semantic: world | Cache live results briefly (fares 6 h, hotel rates 24 h) so a follow-up doesn't re-scrape | Store on each live search; retrieve before searching | Store namespace `("cache", "flights"\|"hotels")` with TTL |
| Procedural | Skills stay hand-owned; **proposals** come from feedback and successful trajectories | **Background, weekly:** mine 👎 reasons and repeated tool sequences, open a PR against `skills/*.md` for a human to review | Filesystem + git (a human-reviewed PR is the safety boundary against poisoned procedures, lecture p. 98) |

## 4. Gaps against the lecture

Mapped to the lecture's "problems without memory" (p. 9) and open challenges (p. 96–100):

| Lecture concern | Where AskTravel stands | Priority |
|---|---|---|
| *Loss of progress* in long sessions | Threads grow unbounded; tool output is 72–92% of them; nothing is summarized | **High**: cost per turn and quality degrade first |
| *Loss of knowledge* across sessions | Facts and trips carry over; **conversations don't**: "what did we decide about Peru last week?" can't be answered | **High** |
| *Loss of procedure* | Skills fix this for known tasks; no learning from experience | Medium |
| Choosing what to remember / forget | Agent-decided writes, no consolidation, nothing ever forgotten | **High** (duplicate trips already exist) |
| Trust, privacy, user control | No provenance; no "what do you remember" view beyond asking the agent; no export/delete flow; memories are agent-writable from any message (a prompt-injection vector) | Medium, must-have before production |
| Using memory correctly (over-personalization, p. 100) | All facts and trips are injected into **every** call, relevant or not; the prompt asks the agent to mention facts it used, but that isn't measured | Medium |

## 5. Growth and TTL policy

**Principles:**
- Expire what's **derived or volatile**. Keep what the user **explicitly told us** until they change it.
- Prefer **archiving into a summary** over plain deletion, so episodic knowledge survives in compact form.
- Feedback and audit data have their own retention (2 years, [feedback plan](feedback-audit.md)) and are never expired by memory policies.

| Element | Policy | Mechanism |
|---|---|---|
| Checkpoints: intermediate step snapshots | Keep for **7 days**, then keep only **turn-boundary** checkpoints (those that rewind and branching need) | Nightly job deletes non-boundary rows in `checkpoints` / `checkpoint_writes` and orphaned `checkpoint_blobs` |
| Checkpoints: whole thread | Delete **180 days after the last message**, after its conversation summary exists; delete **immediately** when the user deletes the chat in Open WebUI | Nightly job; chat-deletion hook (once Open WebUI is on Postgres, per the feedback plan, a trigger on its `chat` table) |
| Conversation summaries (new) | **2 years** from last activity; refreshed if the chat resumes | Store TTL (`ttl` on `aput`, `refresh_on_read=False`) |
| Profile facts | **No automatic expiry.** Track `confirmed_at`; facts unconfirmed for **12 months** are shown to the agent as "may be outdated" so it re-confirms naturally; users can delete any time | Field + prompt wording; deletion via `delete_memory` / a user-facing view |
| Sensitive facts (health, nationality, companions' details) | Saved only with an explicit user statement; **12-month TTL** unless reconfirmed | `sensitivity` field + store TTL |
| Trips | Active until `end_date`; **archived 30 days after the trip ends** (dropped from the prompt, kept for "last time in Hawaii…"); **deleted 2 years after `end_date`**. Undated trips expire **180 days** after their last update | Store TTL computed from `end_date`; archive = status `past` |
| Saved itineraries | Follow their trip's lifecycle; orphans (no trip) deleted after 180 days | Same job as trips |
| Live-search cache (new) | Flights **6 hours**, hotels **24 hours** | Store TTL; LangGraph's Postgres store supports per-item `ttl` and a background sweeper (`start_ttl_sweeper`) |
| Langfuse traces | **90 days** | Langfuse data-retention setting |
| Open WebUI chats | User-controlled; agent-side data follows deletion (above) | Hook |
| Guidebooks, skills, prompts | No TTL; versioned files. Guidebook refresh is a content task (newer editions) | n/a |

**Expected effect:**
- **Storage:** checkpoint growth becomes roughly linear in *turns*, not *steps*. That's about 4–7× fewer rows, judging by today's 209 checkpoints for about 40 turns.
- **Prompt size:** stays bounded per user, because past trips leave the prompt automatically.

## 6. Context window design

`gpt-6-luna` accepts about 1M tokens, so the limit isn't size. It's **cost, latency and attention**:
- **Cost:** every model call resends the whole window, and a research turn makes about 15 calls. A turn at the 27k-token thread size therefore sends **about 400k input tokens**. Cost and latency grow with conversation length, not with the new question.
- **Attention:** the lecture notes that models misuse what's in context (p. 73, p. 100). Old guidebook pages about a different day of the trip are noise.

**Observations about today's design:**
1. **The order is right; the volatile parts are in the wrong place.** Static instructions come first, which suits prompt caching. But today's date and the user's facts and trips sit **inside** the system prompt, so any `save_memory` mid-turn, and every new day, changes the cached prefix.
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
| 3 | User context: date, profile facts, current trips, **top-3 relevant conversation summaries** | ≤ 1.5k | Separate message after the static prefix; trips limited to upcoming or active ones; summaries retrieved by similarity to the latest user message |
| 4 | Conversation summary (older turns) | ≤ 1k | `SummarizationMiddleware` once the thread passes ~12k tokens, keeping the last ~6 turns verbatim |
| 5 | Recent turns, verbatim | ≤ 8k | Old tool results **cleared** after the turn that used them (`ContextEditingMiddleware`), replaced by a one-line stub ("read Hawaii pp. 88–91") the agent can re-fetch |
| 6 | Current turn's tool results | ≤ 12k | Unchanged inside the turn; `read_guidebook_pages` already caps at 15k characters |

**Target:** about 15–25k tokens per call regardless of chat length. Today a 4-turn chat is already
at 27k tokens plus a 5.3k prefix, and grows without limit. Clearing and summarization change only
what the **model sees**. The checkpointed thread keeps the full messages (until the pruning in §5),
so branching and rewind still work.

## 7. Roadmap

Ordered by value and risk. Each phase is its own PR with unit tests and an end-to-end check, like
the features so far.

| Phase | Scope | Lecture pattern | Effort |
|---|---|---|---|
| **M1 Context budget** | Move date + facts + trips out of the system prompt into a user-context message; `ContextEditingMiddleware` to clear old tool results; `SummarizationMiddleware` for long threads; trim tool schemas; log per-call input tokens to Langfuse | Tiered hierarchy (MemGPT/MemoryOS: working vs recall) | 1–2 days |
| **M2 Provenance + hygiene** | `source`, `confirmed_at`, `sensitivity` on facts and trips; canonical trip identity (destination + dates matching); length limits per fact; migrate existing data | Memory maintenance | 1 day |
| **M3 Retention** | Nightly job: checkpoint pruning, thread deletion, trip archiving; store TTLs + sweeper; Langfuse retention; chat-deletion hook (after the feedback plan's F2) | Memory maintenance | 1 day |
| **M4 Background consolidation** | After a chat goes idle: write the conversation summary; Mem0-style ADD/UPDATE/DELETE/NOOP over facts and trips (dedupe trips, split blob facts, resolve "I'm vegetarian" vs "I eat fish now") | Reflective consolidation; sleep-time compute | 2 days |
| **M5 Episodic recall** | pgvector on the Postgres store for summaries; `recall_conversations(query)` tool; top-3 summaries auto-injected by relevance | Tiered hierarchy (archival search) | 1–2 days |
| **M6 User control** | "What AskTravel remembers" endpoint + notebook view; export and delete-all per user; facts only writable from the user's own words (guard against injected instructions) | Trust, privacy, user control | 1 day |
| **M7 Procedural learning** | Weekly job: cluster 👎 reasons and comments by skill and tool, and mine successful itinerary trajectories into "workflow" notes (AWM); open a **PR** proposing skill edits for human review | Learning procedures from experience (AWM, skill files) | 2 days, after the feedback plan |

## 8. Evaluating memory

The lecture's bar for memory evals (p. 68): **realistic, long-context, challenging, verifiable**.
We have small versions of the first two (`e2e_memory`, `e2e_branching`). Proposed
`scripts/eval_memory.py`, run against the real model:

| Case | Checks | Inspired by |
|---|---|---|
| Cross-chat trip recall | New chat answers "near where I'm staying" without asking (exists: `e2e_memory` A/B) | LoCoMo single-hop |
| Preference evolution | "I'm vegetarian" … later "I eat fish now" → the fact is updated, not duplicated; suggestions follow the newer preference | PersonaMem |
| Conversation recall | "What did we decide for day 2 in Rome last week?" answered from an episode summary (after M5) | LongMemEval |
| Duplicate-trip resistance | The same trip described three ways in three chats → one trip | Consolidation |
| Over-personalization | Unrelated question ("what's the capital of Peru?") → no unprompted mention of saved facts | Hu et al. (p. 100) |
| Long-thread quality and cost | 10-turn planning chat: answer quality holds; input tokens per call stay within the §6 budget | Context budget |
| Forgetting | Expired trip leaves the prompt; deleted chat removes its thread; "forget my diet" removes it everywhere | TTL policy |

## 9. Open questions

- **Embeddings for M5:** OpenRouter or a local model? It adds a dependency, but `pgvector` keeps it all in the existing Postgres.
- **Consolidation model:** use the same `gpt-6-luna` or a cheaper one? It runs in the background, so latency doesn't matter, but the rate limit does.
- **Default expiry for profile facts:** should they expire at all? The proposal says no, but "may be outdated" after 12 months.
- **Who reviews skill-change PRs from M7,** and how often?

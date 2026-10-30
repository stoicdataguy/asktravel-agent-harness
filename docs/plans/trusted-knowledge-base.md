# Plan: a trusted knowledge base

**Goal:** turn the guidebooks into the agent's *trusted knowledge base*: curated, versioned
sources whose answers are correct and cited, found by meaning as well as keywords. The agent's
tools stay the same; what changes is the index behind them. Future backend: **pgvector** in our
Postgres, or **OpenSearch** if the corpus outgrows it.

*Status: proposal, 2026-10-30. Tracks issue #34 (milestone #11 in [PLAN.md](../PLAN.md)).
Replaces an earlier draft that proposed a guidebook research subagent; [§2](#2-a-knowledge-base-not-an-agent)
explains why that was dropped. Numbers measured on the local POC database (23 threads, 59 turns)
and in Langfuse.*

## Contents

- [1. Today](#1-today)
- [2. A knowledge base, not an agent](#2-a-knowledge-base-not-an-agent)
- [3. What "trusted" means](#3-what-trusted-means)
- [4. Target design](#4-target-design)
- [5. Backend: keyword, pgvector or OpenSearch](#5-backend-keyword-pgvector-or-opensearch)
- [6. Evaluation](#6-evaluation)
- [7. Roadmap](#7-roadmap)
- [8. Risks](#8-risks)
- [9. Open questions](#9-open-questions)

## 1. Today

| Part | How it works |
|---|---|
| **Content** | 7 Lonely Planet books (Big Island, Italy 2016, Peru, Spain 2016, Thailand 2016, Turkey 15th edition, Switzerland), converted from PDF by `travel-agent-ingest` to markdown with `[p. N]` markers and TOC headings. Copyrighted: never committed |
| **Index** | None. Each book is parsed into paragraphs in memory at startup |
| **Search** | `search_guidebooks(query, book?)`: counts how many query words appear in each paragraph as substrings (accents and ʻokina folded), plus a bonus for the exact phrase. No word weighting, no stemming, no synonyms |
| **Read** | `read_guidebook_pages(book, start, end?)`: up to 4 pages, 15k characters |
| **Trust signals** | Every hit carries book, page and section; the prompt says to cite pages and to flag prices and hours as possibly outdated |

**How it's used** (59 turns):
- **How often:** 21 turns used the guidebooks, with a median of 8 guidebook calls made in a
  median of **3** model steps (79% of calls run in parallel).
- **How much text:** ~6.6k tokens of results per research turn, which is 90% of all tool output
  in those turns.
- **Cost:** most of it is cheap to resend, because **81%** of input tokens are prompt-cache reads.

**What's weak:**
- **Retrieval by keyword only.** "Ice cream" misses "gelato"; "where to see lava" misses "volcano
  viewing". A paragraph that mentions a word once ranks like one about it.
- **No measurement.** Nothing checks that answers are right or that cited pages contain the fact.
- **Age and coverage.** The editions are 2016-era, and there are only 7 destinations.

## 2. A knowledge base, not an agent

An earlier draft moved retrieval into a research subagent that the travel agent would call like
a tool. Measured, the case didn't hold:

| The draft assumed | The data shows |
|---|---|
| 8 guidebook calls = 8 model steps re-reading pages | 3 steps: the agent already batches calls |
| Re-reading pages is expensive | 81% of input is cached; each page is paid in full once either way |
| — | A subagent roughly **doubles model calls** per research turn (~4 → ~8), and calls are the limit we actually hit (~20/min, #51) |
| — | The travel agent would lose raw page detail it needs for itineraries (opening days, travel times) |

So the problem is **retrieval quality**, not where retrieval runs. It's fixed in the index, behind
the tools the agent already uses well.

The subagent pattern is kept for where its real benefit, **isolation**, matters: research over
*untrusted* web pages (#33). An agent that reads web text without memory tools can't be talked
into saving anything.

## 3. What "trusted" means

1. **Curated sources only.** Guidebooks, and later vetted official pages (park services, tourism
   boards, transit operators). Never arbitrary web results: those stay in `fetch_page` (#33),
   outside the knowledge base.
2. **Provenance on every chunk.** Source, edition and year, page, section path, and ingestion
   date. Nothing enters the index without it.
3. **Verifiable citations.** Every result carries the page it came from. Answers cite pages, and
   the eval checks the cited page contains the fact.
4. **Freshness is visible.** The edition year travels with every result, so the agent can say
   "the 2016 guide lists…" and flag prices and hours.
5. **World knowledge only.** No user data in the knowledge base. User memory stays in the store
   (CoALA: semantic memory about the world vs about the user).

## 4. Target design

```
reference/*.pdf ──ingest──▶ data/guidebooks/*.md        (today)
                               │
                               ▼  chunk (section-aware, ~1–2 paragraphs, with page + section path)
                         chunks + provenance
                               │  embed (embedding model) + keyword index
                               ▼
                    ┌─────────────────────────┐
                    │ index: pgvector + FTS   │  or OpenSearch (BM25 + k-NN)
                    └───────────┬─────────────┘
                                │ hybrid query: keyword ∪ vector, merged (RRF), filtered by book
                                ▼
         search_guidebooks(query, book?) → ranked chunks with [book p.N | section]   (same tool)
         read_guidebook_pages(book, start, end?) → full pages from the markdown       (unchanged)
```

- **Same tool contract.** `search_guidebooks` keeps its name, arguments and output format, so
  prompts, skills and the agent don't change. Only its ranking does.
- **Hybrid retrieval.** Keyword search stays good at names ("Kealakekua", "Pizzarium"); vectors
  catch paraphrases ("ice cream" → gelato). Results are merged with reciprocal rank fusion. A
  reranker is optional, added only if the eval asks for it.
- **A backend setting.** `KB_BACKEND=keyword|pgvector|opensearch`, defaulting to `keyword` until
  a backend beats it in the eval.
- **Re-ingestion is idempotent.** `travel-agent-ingest --force` rebuilds markdown, chunks and
  index together, so the index never drifts from the source.

## 5. Backend: keyword, pgvector or OpenSearch

| | **0 · Better keyword search** (in process) | **A · Postgres + pgvector** | **B · OpenSearch** |
|---|---|---|---|
| What it is | Proper BM25 ranking with stemming over today's paragraphs | Vector column + Postgres full-text search, in the database that already holds memory | Search engine with BM25 and k-NN vectors in one hybrid query |
| Finds paraphrases | No | Yes | Yes |
| New infrastructure | None | `pgvector/pgvector:pg16` image, an embedding model | A cluster (one container locally; Amazon OpenSearch Service on AWS) |
| Scale | A handful of books | Comfortable to millions of chunks | Built for very large, multilingual corpora |
| Hybrid ranking | Keyword only | Done in SQL (two queries + RRF) | Native hybrid query and pipelines |
| Operations and backup | Nothing | Same as memory: one database, one backup | A second system to secure, back up and keep in sync |
| Bonus | Cheapest win | Also unlocks semantic search over **memory** (facts, past chats), the "memory is capped" gap | Faceting, highlighting, analyzers per language |
| LangChain | — | `langchain-postgres` (`PGVector`) | `OpenSearchVectorSearch` |
| AWS path | — | RDS for PostgreSQL with pgvector | Amazon OpenSearch Service |

**Recommendation:**
- **Measure first (K0).**
- **Then try option 0.** It costs nothing and fixes ranking.
- **Move to pgvector** if paraphrased questions still miss. At seven books it's the right size, and
  it shares the database, backups and connection pool with memory.
- **Choose OpenSearch only when a trigger is hit:**
  - the corpus grows to hundreds of sources or several languages;
  - search features we'd otherwise build by hand (faceting, highlighting, language analyzers);
  - an AWS deployment that already runs OpenSearch.

## 6. Evaluation

**`scripts/eval_guidebooks.py`** has two levels. Each question records the expected book, page
and a fact.

| Level | What it checks | Needs the LLM? |
|---|---|---|
| **Retrieval** | For each question, is the expected page in `search_guidebooks`'s top 5 / top 10? (recall@k, MRR) | **No**: deterministic, seconds, free; run on every change |
| **Answers** | Through the agent: is the answer right, is the citation on the expected page (±1), does it say when a destination isn't covered? Plus model calls, time, tokens | Yes: run once per phase |

The question set (~20) is split by what each kind of retrieval is good at:
- **Exact terms:** names, places ("Kealakekua Bay snorkelling").
- **Paraphrases:** "ice cream in Rome", "where to see lava", "is it safe to drive to the summit".
- **Facts spread across sections:** closing days *and* booking rules.
- **Uncovered destinations:** Lisbon, Tokyo.

Paraphrases are where keyword search should lose and hybrid should win. If it doesn't, we stop
there.

**Acceptance for switching the default backend:**
- [ ] Retrieval recall@5 up on paraphrased questions, and not down on exact-term ones
- [ ] Answer correctness and citation validity no worse than K0
- [ ] `search_guidebooks` latency under ~300 ms; no extra model calls per research turn
- [ ] `eval_skills.py` 9/9; `eval_memory.py --only long_thread --runs 1` passes

## 7. Roadmap

| Phase | Scope | Effort |
|---|---|---|
| **K0 Measure** | `eval_guidebooks.py` (retrieval + answers) on today's search; record the baseline here | ½ day |
| **K1 Better keyword ranking** | BM25 with stemming, section-aware chunks, same tool output; retrieval eval vs K0 | ½ day |
| **K2 pgvector hybrid** | `pgvector/pgvector:pg16` image; chunk + embed in `travel-agent-ingest`; hybrid query with RRF behind `KB_BACKEND=pgvector`; eval vs K1; default on if §6 holds | 1–2 days |
| **K3 OpenSearch** | Only if a §5 trigger is hit: same chunks and contract, `KB_BACKEND=opensearch` | 1–2 days |
| **K4 Fresher content** | Newer editions or vetted official sources, with edition and date in provenance. A content task as much as code | ongoing |
| **Related: web research (#33)** | Untrusted web pages via `fetch_page`, read by a memory-less research agent; never written into the knowledge base | with #33 |

## 8. Risks

| Risk | Mitigation |
|---|---|
| **Copyright:** the index holds guidebook text | Same rule as `data/guidebooks`: local only, never committed; note that database backups contain it |
| **Embedding cost, latency and rate limits** | Embed once at ingest (thousands of chunks, one-off); query embeddings are one short call. A local embedding model avoids OpenRouter's limits entirely |
| **Index drifts from the source** | One ingest command rebuilds everything; the eval runs against the index |
| **Vectors return confident nonsense for uncovered places** | A similarity threshold, plus a book filter from the destination; the "uncovered destination" eval cases |
| **Two backends to maintain** | The `keyword` backend stays only as the fallback and test double; a second vector backend (OpenSearch) only if triggered |

## 9. Open questions

- **Embedding model:** a local model (e.g. a small sentence-transformer, no rate limit, runs at
  ingest) or an API model through OpenRouter?
- **Chunk size:** paragraph, paragraph window, or section? Decided by retrieval recall in K1/K2.
- **Reranker:** is a cross-encoder worth its latency? Only if recall@5 is good but answers still
  pick the wrong passage.
- **Official sources:** which ones count as trusted for K4, and who reviews additions?

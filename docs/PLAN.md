# AskTravel: Plan

A lightweight LangChain ReAct agent that helps plan trips. It is served through an
OpenAI-compatible API so an off-the-shelf chat UI (Open WebUI) is the frontend. This document
covers the architecture, the decisions behind it, what's built, what's next, and how to deploy it.

*Last updated: 2026-10-28. Milestone numbers (#1–#14) are stable and referenced from PRs.*

## Contents

- [Architecture](#architecture)
- [Key decisions](#key-decisions)
- [Roadmap](#roadmap)
  - [Status at a glance](#status-at-a-glance)
  - [Next up](#next-up)
  - [Done: notes and deviations](#done-notes-and-deviations)
- [Skills](#skills)
- [Deployment on AWS](#deployment-on-aws)
  - [Target architecture](#target-architecture)
  - [Required changes before deploying](#required-changes-before-deploying)
  - [Infrastructure as code](#infrastructure-as-code)
  - [Phasing and cost](#phasing-and-cost)
- [Detailed plans](#detailed-plans)
- [Known limitations](#known-limitations)
- [Open questions](#open-questions)

## Architecture

> A visual version, with pages on data flow / state persistence and on the MVP's tools and skills, is in
> [architecture.html](architecture.html) ([view rendered](https://raw.githack.com/stoicdataguy/travel-agent-react/main/docs/architecture.html)).

```
Open WebUI (:3000) ── OpenAI-compatible HTTP + X-OpenWebUI-User-Email / -Chat-Id ──▶ agent API (FastAPI :8000)
                                                                                          │
                                                            create_agent (LangChain v1 ReAct loop)
                                                            middleware: system prompt + skills index + user memories
                                                                                          │
   ┌──────────────┬─────────────────┬──────────────┬────────────────┬───────────────────┼────────────────────────┐
 OpenRouter    Guidebook tools   Skills          Memory tools     present_itinerary     Planned
 (gpt-6-luna)  list / search /   load_skill      save_memory /    (Pydantic Itinerary   · flights, hotels, web (#7)
               read pages        (7 playbooks)   delete_memory    → chat + store)       · guidebook subagent (#11)
                                                                                         · MCP tools (#12)

 Postgres (:5433)  checkpointer = chat history per thread · store = user facts + itineraries
 Langfuse (:3001)  traces: session = chat, user = email
```

- **Threads:** `thread_id = "<email>:<chat id>"`. The checkpointer owns the history; the API
  passes only the newest user message unless the chat has branched (fork, regenerate, edit), in
  which case `api/thread_sync.py` seeds or rewinds the thread to match the transcript first.
- **Users:** the signed-in email (forwarded by Open WebUI) is the `user_id` for memory, saved
  itineraries and traces.

## Key decisions

| Topic | Decision | Status |
|---|---|---|
| Frontend | Open WebUI via docker compose, talking to our OpenAI-compatible `/v1` API. No UI code. | Built |
| LLM | OpenRouter, `openai/gpt-6-luna` via `ChatOpenAI(base_url=...)` (`OPENROUTER_MODEL`; switched from `anthropic/claude-sonnet-5.5`, ~20× cheaper, same skill-trigger results). | Built |
| Users | Open WebUI signup as a dummy login: any email, active immediately. `ENABLE_PERSISTENT_CONFIG=false` keeps signup open. | Built |
| Short-term memory | `AsyncPostgresSaver` checkpointer, one thread per user + chat. | Built |
| Long-term memory | `AsyncPostgresStore` with two kinds: profile facts keyed by topic, and **trips** (destination, dates, lodging, travellers, linked itinerary). Both are injected into the prompt every model call. | Built |
| Structured output | Pydantic schema as the argument of a `present_itinerary` tool, not `response_format` (which forces a tool call on every turn). | Built |
| Skills | Markdown playbooks with name/description frontmatter, loaded on demand via `load_skill`. | Built |
| Observability | Self-hosted Langfuse v4 in compose. LangSmith removed. | Built |
| Guidebooks | PDFs in `reference/` → `travel-agent-ingest` → `data/guidebooks/<slug>.md`, keyword search (no embeddings). Both gitignored. | Built |
| Flights | `search_flights` via `fast-flights` (reads Google Flights; no API key). Prices are totals for all travellers; results come with a Google Flights booking link. | Built |
| Hotels | `search_hotels`: Playwright drives Google Hotels (picks dates in its calendar, reads result cards). Nightly prices as Google shows them, stay totals, star class, rating, deals, amenities, booking link. | Built |
| Web pages | Playwright: fetch URL → extract readable text. No search provider. | Planned (#7) |
| Deployment | ECS Fargate + RDS + Cognito, CDK in Python. | Planned (#14) |

## Roadmap

### Status at a glance

| # | Milestone | Status |
|---|---|---|
| 1 | Skeleton: uv, FastAPI, compose, CLAUDE.md | ✅ Done |
| 2 | Chat round-trip: `/v1/chat/completions` (stream + non-stream) → `create_agent` | ✅ Done |
| 3 | Short-term memory: Postgres checkpointer | ✅ Done |
| 4 | Guidebook ingestion | ✅ Done |
| 5 | Observability: Langfuse | ✅ Done |
| 6 | Guidebook tools + tool-call status lines in the stream | ✅ Done |
| 7 | Flight, hotel and web tools | 🟨 Flights ([PR #8](https://github.com/stoicdataguy/travel-agent-react/pull/8)) and hotels ([PR #10](https://github.com/stoicdataguy/travel-agent-react/pull/10)) built; `fetch_page` still to do |
| 8 | Long-term memory | ✅ Done ([PR #1](https://github.com/stoicdataguy/travel-agent-react/pull/1)) |
| 9 | Structured output | ✅ Done ([PR #2](https://github.com/stoicdataguy/travel-agent-react/pull/2)) |
| 10 | Skills + trigger eval | ✅ Done |
| 11 | Knowledge-base subagent | ⬜ Planned |
| 12 | MCP tools | ⬜ Planned |
| 13 | Streaming polish | ⬜ Planned |
| 14 | AWS deployment | ⬜ Planned |

### Next up

#### #7 Flight, hotel and web tools
- `search_flights` with `fast-flights`: origin, destination, dates, passengers → a short list of
  `FlightOption` (Pydantic: airline, times, stops, duration, price).
- `search_hotels` with Playwright on Google Hotels → `HotelOption` (name, area, rating, price
  per night). Scraping is fragile; keep selectors in one place and fail with a clear message.
- `fetch_page(url)` with Playwright → readable text, trimmed for the LLM.
- Add optional `flights` / `hotels` to the `Itinerary` schema, and update `budget-planner` to use
  live prices instead of guidebook estimates.
- Tests against recorded fixtures (saved HTML/JSON), never live sites. One-time setup:
  `uv run playwright install chromium`.

#### #11 Knowledge-base subagent
- Move the guidebook tools into their own `create_agent`, exposed to the main agent as a single
  `ask_guidebook(question, destination)` tool that returns a cited summary.
- Why: a single itinerary request made 13 guidebook calls on the main agent, and every page read
  stays in the main conversation's context. A subagent keeps that out and returns only the answer.
- Trace it as a child run so Langfuse shows the nesting.

#### #12 MCP tools
- Load servers from an `mcp.json` file via `langchain-mcp-adapters` (`MultiServerMCPClient`) at
  startup and add their tools to the agent.
- Candidates: weather or currency conversion servers. Status lines and tracing already cover
  any tool.

#### #13 Streaming polish
- Collapse the `🔧 tool(...)` status lines into a single expandable block (e.g. a `<details>`
  element in Open WebUI) so long research runs don't push the answer down.
- Use Open WebUI's background task settings to bring back chat titles without routing them
  through the agent (see [Known limitations](#known-limitations)).

#### #14 AWS deployment
See [Deployment on AWS](#deployment-on-aws). The first step, locking down the agent API, is
worth doing even before any deployment.

### Done: notes and deviations

- **#4 Guidebook ingestion:** one paragraph per line, PDF bookmarks as markdown headings, `[p. N]`
  page markers. Icon-font glyphs and soft hyphens are stripped.
- **#6 Guidebook tools:** `list_guidebooks`, `search_guidebooks` (accent- and ʻokina-insensitive,
  skips contents/index/map-key noise), `read_guidebook_pages`. Currently on the main agent;
  moving to a subagent in #11.
- **#8 Long-term memory:** the planned `recall_memories` tool was dropped. All of a user's facts
  are injected into the prompt every model call instead, so recall is automatic. Store
  namespaces percent-encode dots in emails (`memory.user_namespace()`).
- **#9 Structured output:** `Itinerary` → `DayPlan` → `Activity`, plus `Stay`, as the argument
  schema of `present_itinerary`. Validation errors go back to the model to fix. Plans are saved
  per user + chat (`GET /api/itineraries`) and rendered to markdown in the chat.
  `FlightOption` / `HotelOption` are deferred to #7.
- **Branching (issue #3):** forked chats lost the agent's history, and regenerate/edit didn't
  rewind it. The API now reconciles the transcript with the thread before each turn (append /
  seed / rewind / replace); verified by `scripts/e2e_branching.py` in a real browser.
- **Trip memory (issue #5):** long-term memory only held preferences, so trip details and plans from one
  chat were unknown in the next. Trips are now a second kind of long-term memory
  (`save_trip_details`, `get_trip`, `delete_trip`); `present_itinerary` files every plan under a trip, and
  existing itineraries are backfilled at startup. Verified by `scripts/e2e_memory.py`.
- **#7 Flight search (issue #7):** `search_flights` returns options cheapest first with total and
  per-person prices, times, stops and a booking link; `FlightOption` can be added to an itinerary.
  `scripts/e2e_flights.py` checks in a real browser that quoted fares match a live Google Flights
  lookup and that a chosen flight is saved to the trip.
- **#7 Hotel search (issue #9):** `search_hotels` scrapes Google Hotels with Playwright: URL date
  parameters are ignored, so it picks check-in/out in the calendar (`[data-iso]` day cells) and
  confirms the date box changed before reading cards. `parse_card` is a pure function unit-tested
  on captured card text. `scripts/e2e_hotels.py` checks that hotels and nightly prices in the reply
  match a live lookup and that the chosen hotel becomes the trip's lodging.
- **Rate limits (issue #12):** OpenRouter 429s used to cut replies off mid-stream and abort test
  suites. Model calls now retry with backoff (`LLM_MAX_RETRIES`, default 10, honouring
  `Retry-After`), optional client-side pacing is available (`LLM_REQUESTS_PER_MINUTE`), and a turn
  that still fails ends with a readable message instead of a broken stream.
- **#10 Skills:** see [Skills](#skills). `scripts/eval_skills.py` checks against the real model
  that each skill triggers (9/9 passing).

## Skills

Markdown playbooks in `src/travel_agent/skills/`. The system prompt lists each name and
description; the agent calls `load_skill(name)` when a request matches.

| Skill | Triggers on | What it does |
|---|---|---|
| `trip-intake` | Vague requests ("help me plan a trip") | Asks at most 4 questions, uses sensible defaults, writes a trip brief |
| `itinerary-builder` | Day-by-day plans, reworking a plan | Starts from the guidebook's own itineraries, clusters by area, delivers via `present_itinerary` |
| `budget-planner` | Costs, "does it fit my budget" | Per-person cost table; flags guidebook prices as old |
| `packing-list` | What to pack | Checks climate in the guidebook; checkbox list |
| `visa-and-entry` | Visas, passports, vaccinations | Asks nationality, never states rules as certain, links official sources |
| `local-transport` | Trains, ferries, airports, car rental | Compares options in a table; car vs no car; pass break-even |
| `food-and-culture` | Dishes, restaurants, tipping, etiquette | Dishes to try, cited places to eat, dietary needs, customs |

To add one: create `<name>.md` with `name` / `description` frontmatter, and add a case to
`scripts/eval_skills.py`.

## Deployment on AWS

Goal: a private, authenticated deployment for a small group, with the same architecture as local.
Everything is already containerised or a managed equivalent exists, so this is mostly packaging,
hardening the two trust boundaries, and infrastructure as code.

### Target architecture

ECS Fargate, two services behind one public load balancer:

```
                    Route 53 + ACM (HTTPS)
                            │
                ┌───────────▼────────────┐
 users ───────▶ │ ALB (public, HTTPS)    │  idle timeout ≥ 300 s (agent runs take 30-90 s)
                └───────────┬────────────┘
                            │ :8080
              ┌─────────────▼─────────────┐      OIDC login
              │ ECS service: Open WebUI   │ ◀──── Amazon Cognito (replaces dummy signup)
              └─────────────┬─────────────┘
                            │ Service Connect (private) + bearer token
              ┌─────────────▼─────────────┐      HTTPS out ──▶ OpenRouter (LLM)
              │ ECS service: agent API    │ ──────────────▶ Langfuse (traces)
              │ (FastAPI, this repo)      │
              └──────┬───────────────┬────┘
                     │               │
      ┌──────────────▼───┐   ┌───────▼─────────────────┐
      │ RDS PostgreSQL   │   │ S3 (private)             │
      │ • agent: checkp. │   │ • guidebook markdown     │
      │   + store        │   │ • Open WebUI uploads     │
      │ • open_webui DB  │   └──────────────────────────┘
      └──────────────────┘
 Secrets Manager: OpenRouter key, agent API token, DB creds, Langfuse keys
 CloudWatch Logs: both services · ECR: agent image
```

| Local (compose) | AWS | Notes |
|---|---|---|
| `uv run travel-agent` | ECS Fargate service, image in ECR | 1 vCPU / 2 GB to start; scale on CPU or request count |
| Open WebUI container | ECS Fargate service (official image) | `DATABASE_URL` → RDS instead of the SQLite volume; uploads to S3 (`STORAGE_PROVIDER=s3`); Redis for websockets if >1 task |
| `postgres` | RDS PostgreSQL (db.t4g.micro → small) | Separate databases for agent and Open WebUI; `sslmode=require`; automated backups |
| Langfuse stack (6 containers) | **Langfuse Cloud** (recommended), or self-host | Self-hosting means ClickHouse + Redis + S3 + Postgres; only worth it for data-residency needs |
| `data/guidebooks/*.md` | Private S3 bucket, synced at container start | Keep out of the image and the public repo (copyrighted); ~10 MB |
| `.env` | Secrets Manager + ECS task-definition env | Settings already come from env via pydantic-settings |
| Dummy email signup | Cognito user pool via Open WebUI OIDC (`ENABLE_OAUTH_SIGNUP`, `OPENID_PROVIDER_URL`), password signup off | The email from the OIDC token still becomes `user_id` |

### Required changes before deploying

1. **Lock down the agent API.** Today it trusts `X-OpenWebUI-User-Email` from anyone. Require a
   bearer token: Open WebUI already sends `OPENAI_API_KEY` as `Authorization: Bearer …`, so set
   it to a real secret and reject requests without it (also on `/api/itineraries`). Keep the
   service private: no public ALB route, and a security group that only admits Open WebUI.
2. **Dockerfile for the agent:** multi-stage `uv sync --frozen --no-dev` on `python:3.12-slim`,
   non-root user, `uvicorn travel_agent.api.app:app --host 0.0.0.0` without `--reload`, container
   health check on `/health`. Add Chromium only when the Playwright tools (#7) land (+~400 MB).
3. **Guidebook loading from S3:** at startup, sync `s3://<bucket>/guidebooks/` to
   `GUIDEBOOKS_DIR` (entrypoint script or a small startup hook). `travel-agent-ingest` stays a
   local/offline step.
4. **Production settings:** `DATABASE_URL` from Secrets Manager; Langfuse host and keys for Cloud;
   Open WebUI with `WEBUI_SECRET_KEY` set (so sessions survive restarts), `ENABLE_SIGNUP=false`,
   and a pinned image tag instead of `:main`.
5. **Graceful shutdown:** ECS sends SIGTERM; the lifespan already flushes traces and closes the
   DB pool. Set the ECS stop timeout to ≥ 60 s so in-flight agent runs can finish.
6. **CI:** GitHub Actions running ruff + pytest on PRs (none today), then build → push to ECR →
   `ecs update-service` on merge to `main`, using GitHub OIDC to assume an AWS role (no stored
   AWS keys).

### Infrastructure as code

AWS CDK in Python, in `infra/` (same language and uv tooling as the app). Stacks:

- `Network`: VPC across 2 AZs.
- `Data`: RDS, S3, Secrets Manager.
- `Services`: ECS cluster, the two services, ALB, Cognito.
- `Pipeline`: ECR, GitHub OIDC role.

Terraform is equally fine if that's the team's standard.

To keep costs down, put Fargate tasks in public subnets with security groups that allow no
inbound traffic except from the ALB / Open WebUI; this avoids a NAT gateway (~$35/month).
Alternatively, use private subnets with VPC endpoints for ECR, S3, Secrets Manager and Logs.

### Phasing and cost

| Phase | What | When to use | Rough cost / month (us-east-1) |
|---|---|---|---|
| 0. Demo box | One EC2 instance (t3.large, or t3.xlarge if Langfuse is self-hosted on it) running today's `docker compose`, Caddy for HTTPS, data on EBS, nightly snapshots | Fastest path to a shareable URL; single point of failure | ~$60–120 |
| 1. Managed | ECS Fargate + RDS + ALB + Cognito + Langfuse Cloud, as above | Real users, backups, rolling deploys | ~$110–150 (2 tasks ~$72, ALB ~$20, RDS ~$15, misc) |
| 2. Scale | Multiple agent tasks (already stateless: state is in Postgres), Open WebUI with Redis (ElastiCache) for websockets, RDS Multi-AZ | More users / uptime needs | Depends on load |

These are rough estimates; check the AWS Pricing Calculator. They exclude LLM usage, which will
likely dominate: a tool-heavy itinerary run is 15+ model calls.

## Detailed plans

- [User feedback → audit tables](plans/feedback-audit.md): capture every 👍/👎 in an append-only
  Postgres audit table, linked to the agent turn and its Langfuse trace.
- [Memory architecture](plans/memory-architecture.md): procedural / semantic / episodic inventory,
  What-When-How, TTL policy, context-window budget, and the M1–M7 roadmap.

## Known limitations

- **Open WebUI background tasks are off.** Title, tag and follow-up generation would otherwise
  run through the agent and pollute threads, so chats are titled by their first message.
- **The agent API is unauthenticated** and trusts the user-email header. Fine on a laptop; must
  be fixed before any deployment (see [Required changes](#required-changes-before-deploying)).
- **Open WebUI admin-panel changes don't persist** across restarts
  (`ENABLE_PERSISTENT_CONFIG=false`); `docker-compose.yml` is the source of truth for its settings.
- **Guidebooks are 2016-era editions.** The agent flags prices, hours and contacts as possibly
  outdated; live flight and hotel data arrive with #7.
- **Guidebook search is keyword-only.** Vague queries ("romantic dinner") work less well than
  specific ones ("restaurant Trastevere"); the agent compensates with several focused searches.


## Open questions

- **Langfuse:** Cloud or self-hosted (data residency, budget)?
- **Users in production:** invite-only Cognito pool, or an existing identity provider (Google
  Workspace, Okta)?
- **AWS:** which region, and a custom domain?
- **LLM provider:** keep OpenRouter, or call providers directly (e.g. Bedrock) to keep traffic
  inside AWS?
- **#11 subagent model:** same model as the main agent, or a cheaper one for guidebook research?

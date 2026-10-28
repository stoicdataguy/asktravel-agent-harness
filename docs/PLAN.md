# Plan

## Architecture

```
Open WebUI (docker, :3000) ── OpenAI-compatible HTTP + X-OpenWebUI-User-Email / -Chat-Id ──▶ FastAPI (:8000)
                                                                                              │
                                                              create_agent (LangChain v1 ReAct loop)
                                                                                              │
              ┌──────────────┬────────────────┬──────────────┬────────────────┬───────────────┼─────────────────┐
          OpenRouter     Python tools      MCP tools      Skills (.md)     Subagent:         Memory (Postgres :5433)
          (ChatOpenAI,   flights (fast-    (langchain-    load_skill tool  knowledge_base    checkpointer → thread
           sonnet 5.5)   flights), hotels  mcp-adapters)                   (grep guidebooks)  store → user facts
                         + web (playwright)
```

## Decisions

| Topic | Decision |
|---|---|
| Frontend | Open WebUI via docker-compose, talking to our OpenAI-compatible `/v1` API. No UI code. |
| LLM | OpenRouter, `anthropic/claude-sonnet-5.5` via `ChatOpenAI(base_url=...)`. |
| Users | Open WebUI's own signup used as a dummy login: any email, active immediately. Email → `user_id`. |
| Threads | `thread_id = "<email>:<X-OpenWebUI-Chat-Id>"`; falls back to a hash of the first message. |
| Memory | Postgres from day one: `AsyncPostgresSaver` + `AsyncPostgresStore` sharing one pool. |
| Flights | `fast-flights` (reads Google Flights; no API key). |
| Hotels | Playwright scraping of Google Hotels. |
| Web | Playwright: fetch URL → extract readable text. No search provider. |
| Observability | Self-hosted Langfuse v4 in compose (:3001), chat = session, email = user. |
| Guidebooks | PDFs in `reference/` → `travel-agent-ingest` → `data/guidebooks/<slug>.md` (both gitignored). |

## Known limitations
- Open WebUI's background LLM tasks (title/tag/follow-up generation) are disabled in compose, since they
  would otherwise run through the agent and pollute threads. Chats are titled by their first message.
- Editing or regenerating an earlier message in the UI doesn't rewind the agent's checkpointed thread;
  the agent only sees the newest user message. Fix later by detecting divergence and forking the thread.

## Milestones

1. ✅ **Skeleton**: uv, FastAPI, compose, CLAUDE.md.
2. ✅ **Chat round-trip**: `/v1/chat/completions` (stream + non-stream) → `create_agent`.
3. ✅ **Short-term memory**: Postgres checkpointer keyed by user + chat id.
4. ✅ **Guidebook ingestion**: one paragraph per line, TOC headings, `[p. N]` page markers.
5. ✅ **Observability**: self-hosted Langfuse with per-session and per-user traces.
6. ✅ **Guidebook tools** (on the main agent for now): `list_guidebooks`, `search_guidebooks`,
   `read_guidebook_pages`; tool calls streamed to the chat as status lines.
7. **Tools**: `search_flights` (fast-flights), `search_hotels` + `fetch_page` (Playwright; run
   `uv run playwright install chromium` once). Unit tests against recorded fixtures.
8. ✅ **Long-term memory**: `save_memory` / `delete_memory` tools on the Postgres store, keyed by topic;
   a middleware injects all of the user's facts into the prompt every model call (no recall tool needed).
9. ✅ **Structured output**: Pydantic `Itinerary` / `DayPlan` / `Activity` / `Stay` as the argument schema of a
   `present_itinerary` tool (not `response_format`, which forces `tool_choice="any"` on every turn of a chat
   agent). Validated plans are saved per user + chat (`GET /api/itineraries`) and rendered to markdown in the
   chat. `FlightOption` / `HotelOption` land with the flight/hotel tools (#7).
10. ✅ **Skills**: 7 markdown playbooks + `load_skill` tool; trigger eval in `scripts/eval_skills.py`.
11. **Knowledge-base subagent**: move the guidebook tools into their own `create_agent` with `list_guidebooks`, `grep_guidebook`
   (returns hits with section heading + page) and `read_pages`; exposed to the main agent as `ask_guidebook`.
12. **MCP**: load servers from `mcp.json` via `langchain-mcp-adapters`.
13. **Streaming polish**: collapse tool status lines (e.g. Open WebUI `<details>` blocks).
14. **AWS deployment**: lock down the agent API, Dockerfile, CDK in `infra/`, CI/CD. See
   [Deployment on AWS](#deployment-on-aws).

## Deployment on AWS

Goal: a private, authenticated deployment for a small group, with the same architecture as local.
Everything is already containerised or a managed equivalent exists, so this is mostly packaging,
hardening the two trust boundaries, and infrastructure as code.

### Target architecture (ECS Fargate)

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
| Open WebUI container | ECS Fargate service (official image) | `DATABASE_URL` → RDS instead of SQLite volume; uploads to S3 (`STORAGE_PROVIDER=s3`); Redis for websockets if >1 task |
| `postgres` | RDS PostgreSQL (db.t4g.micro → small) | Separate databases for agent and Open WebUI; `sslmode=require`; automated backups |
| Langfuse stack (6 containers) | **Langfuse Cloud** (recommended), or self-host | Self-hosting means ClickHouse + Redis + S3 + Postgres. Only worth it for data-residency needs; Langfuse publishes AWS self-hosting guidance |
| `data/guidebooks/*.md` | Private S3 bucket, synced at container start | Keep out of the image and the public repo (copyrighted); ~10 MB |
| `.env` | Secrets Manager + ECS task-definition env | Settings already come from env via pydantic-settings, so no code changes are needed for config |
| Dummy email signup | Cognito user pool via Open WebUI OIDC (`ENABLE_OAUTH_SIGNUP`, `OPENID_PROVIDER_URL`), password signup disabled | Email from the OIDC token still becomes `user_id` |

### Required code/config changes before deploying

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
`Network` (VPC with 2 AZs), `Data` (RDS, S3, Secrets), `Services` (ECS cluster, two services,
ALB, Cognito), `Pipeline` (ECR, GitHub OIDC role). Terraform is equally fine if that's the
team's standard.

To keep costs down: put Fargate tasks in public subnets with security groups but no inbound
ports except from the ALB / Open WebUI, which avoids a NAT gateway (~$35/month). Alternatively,
use private subnets with VPC endpoints for ECR, S3, Secrets Manager and Logs.

### Phasing

| Phase | What | When to use | Rough cost / month (us-east-1) |
|---|---|---|---|
| 0. Demo box | One EC2 instance (t3.large, or t3.xlarge if Langfuse is self-hosted on it) running today's `docker compose`, Caddy for HTTPS, data on EBS, nightly EBS snapshots | Fastest path to a shareable URL; single point of failure | ~$60–120 |
| 1. Managed | ECS Fargate + RDS + ALB + Cognito + Langfuse Cloud, as above | Real users, backups, rolling deploys | ~$110–150 (2 tasks ~$72, ALB ~$20, RDS ~$15, misc) |
| 2. Scale | Multiple agent tasks (stateless already: state is in Postgres), Open WebUI with Redis (ElastiCache) for websockets, RDS Multi-AZ | More users / uptime needs | depends on load |

Figures exclude LLM usage, which will dominate: a tool-heavy itinerary run is 15+ model calls.
Check current prices in the AWS Pricing Calculator.

### Open questions
- Langfuse Cloud vs self-hosted (data residency, budget)?
- Who are the users: invite-only Cognito pool, or an existing IdP (Google Workspace, Okta)?
- Region, and a custom domain?
- Keep OpenRouter or call providers directly (e.g. Bedrock) to keep traffic inside AWS?

## Skill ideas (`skills/*.md`)
- `trip-intake`: questions to ask before planning (dates, origin, party, budget, interests, mobility).
- `itinerary-builder`: sequencing days, pacing, buffer time, filling the `Itinerary` schema.
- `budget-planner`: cost breakdown (flights/stay/food/transport/activities), currency handling.
- `packing-list`: climate- and activity-aware packing list.
- `visa-and-entry`: what to verify (visa, passport validity, vaccines), always pointing to official sources.
- `local-transport`: rail passes, airport transfers, when to rent a car.
- `food-and-culture`: etiquette, tipping, dishes to try (pairs with the guidebook subagent).

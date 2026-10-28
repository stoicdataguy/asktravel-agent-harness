# travel-agent

Lightweight LangChain ReAct agent for organizing travel itineraries. LLM calls go through OpenRouter,
and the agent is served by FastAPI over an OpenAI-compatible API so Open WebUI (an Ollama-style chat UI)
can be the frontend.

## Quickstart

```bash
cp .env.example .env          # add your OPENROUTER_API_KEY
uv sync
docker compose up -d          # Postgres (:5433), Open WebUI (:3000), Langfuse (:3001)
uv run travel-agent-ingest    # one-time: reference/*.pdf -> data/guidebooks/*.md
uv run travel-agent           # API on http://localhost:8000
```

Open http://localhost:3000 and sign up with any email (the password isn't checked by the agent).
Each email is a separate user with its own long-term memory, so you can sign up several to
simulate multiple users.

### Tracing

With `LANGFUSE_TRACING=true` in `.env`, every agent run (LLM calls, tool calls, middleware steps)
is traced to the self-hosted Langfuse at http://localhost:3001 (login `admin@travel.local` /
`travel-agent-dev`). Traces carry the chat as **session** and the email as **user**, so Langfuse's
Sessions and Users views show whole conversations per (dummy) user. Langfuse is about six containers
(incl. ClickHouse); to skip it run `docker compose up -d postgres open-webui`.

LangSmith can be enabled alongside it with `LANGSMITH_TRACING=true` + `LANGSMITH_API_KEY`.

Tests and lint: `uv run pytest` · `uv run ruff check . && uv run ruff format --check .`

See [docs/PLAN.md](docs/PLAN.md) for the architecture and roadmap.

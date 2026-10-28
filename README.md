# travel-agent

Lightweight LangChain ReAct agent for organizing travel itineraries. LLM calls go through OpenRouter,
and the agent is served by FastAPI over an OpenAI-compatible API so Open WebUI (an Ollama-style chat UI)
can be the frontend.

## Quickstart

```bash
cp .env.example .env          # add your OPENROUTER_API_KEY
uv sync
docker compose up -d          # Postgres (:5433) + Open WebUI (:3000)
uv run travel-agent-ingest    # one-time: reference/*.pdf -> data/guidebooks/*.md
uv run travel-agent           # API on http://localhost:8000
```

Open http://localhost:3000 and sign up with any email (the password isn't checked by the agent).
Each email is a separate user with its own long-term memory, so you can sign up several to
simulate multiple users.

### Tracing

Set `LANGSMITH_TRACING=true` and `LANGSMITH_API_KEY` in `.env` to send every agent run (LLM calls,
tool calls, middleware steps) to LangSmith under the `travel-agent` project. Runs carry `thread_id`
and `user_id` metadata, so the **Threads** tab shows whole conversations and you can filter by user.

Tests and lint: `uv run pytest` · `uv run ruff check . && uv run ruff format --check .`

See [docs/PLAN.md](docs/PLAN.md) for the architecture and roadmap.

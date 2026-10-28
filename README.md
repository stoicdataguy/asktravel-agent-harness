# travel-agent

Lightweight LangChain ReAct agent for organizing travel itineraries. LLM calls go through OpenRouter,
and the agent is served by FastAPI over an OpenAI-compatible API so Open WebUI (an Ollama-style chat UI)
can be the frontend.

## Quickstart

```bash
cp .env.example .env          # add your OPENROUTER_API_KEY
uv sync
uv run travel-agent           # API on http://localhost:8000
docker compose up -d          # chat UI on http://localhost:3000
```

Tests and lint: `uv run pytest` · `uv run ruff check . && uv run ruff format --check .`

See [docs/PLAN.md](docs/PLAN.md) for the architecture and roadmap.

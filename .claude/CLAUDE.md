# Travel Agent — project instructions

Lightweight LangChain ReAct agent for planning travel itineraries, served by FastAPI
behind an off-the-shelf chat UI. Living document: append new conventions as sections below.

## Stack
- Python 3.12, managed with **uv** only (never pip/poetry/conda). `uv add <pkg>`, `uv run <cmd>`.
- Backend: **FastAPI** + uvicorn. Agent: **LangChain v1** `create_agent` (LangGraph under the hood).
- LLM calls go through **OpenRouter** via `ChatOpenAI(base_url=OPENROUTER_BASE_URL)`. No direct provider SDKs.
- Frontend: **none of our own.** We expose an OpenAI-compatible API (`/v1/models`, `/v1/chat/completions`)
  and use Open WebUI (docker-compose) as the chat client. Don't build UI code.

## Commands
- Run API: `uv run travel-agent` (or `uv run uvicorn travel_agent.api.app:app --reload`)
- Chat UI: `docker compose up -d` → http://localhost:3000
- Tests: `uv run pytest` · Lint/format: `uv run ruff check --fix . && uv run ruff format .`

## Layout
- `src/travel_agent/` — package (src layout). `config.py` holds all settings (pydantic-settings, `.env`).
- `src/travel_agent/api/` — FastAPI app and OpenAI-compatible routes.
- See `docs/PLAN.md` for the target architecture and milestones.

## Conventions
- Settings only via `travel_agent.config.get_settings()`; never read `os.environ` directly.
- Structured data = Pydantic v2 models. Agent final answers use `response_format=<PydanticModel>`.
- Tools: plain typed Python functions with docstrings (docstring = tool description the LLM sees).
  Tools must return compact, LLM-friendly output (trim large payloads) and never raise into the agent
  loop — return an error string instead.
- Memory: thread-level via checkpointer keyed by `thread_id`; cross-thread facts via LangGraph `Store`
  namespaced by `(user_id, "memories")`.
- Async first in the API layer (`ainvoke` / `astream`).
- Secrets live in `.env` (gitignored). Keep `.env.example` in sync when adding settings.

## Workflow
- Small commits; run ruff + pytest before committing.
- Tests must not hit real LLMs or the network — use fake chat models / mocks.

<!-- Add new sections below as the project evolves -->

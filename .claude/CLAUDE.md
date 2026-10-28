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
- Infra: `docker compose up -d` → Postgres :5433, Open WebUI http://localhost:3000, Langfuse http://localhost:3001
  (Langfuse stack lives in `docker/langfuse.compose.yml`; local dev secrets only)
- Run API: `uv run travel-agent` (or `uv run uvicorn travel_agent.api.app:app --reload`)
- Guidebooks: `uv run travel-agent-ingest [--force]` converts `reference/*.pdf` → `data/guidebooks/*.md`
- Demo GIF: `uv run python scripts/record_demo.py` → `docs/demo.gif` (needs the full stack running)
- Tests: `uv run pytest` · Lint/format: `uv run ruff check --fix . && uv run ruff format .`

## Layout
- `src/travel_agent/` — package (src layout). `config.py` holds all settings (pydantic-settings, `.env`).
- `src/travel_agent/api/` — FastAPI app and OpenAI-compatible routes.
- `src/travel_agent/agent/` — `build_agent()` (create_agent + middleware), prompts, `UserContext`.
- `src/travel_agent/memory.py` — Postgres checkpointer + store on one connection pool.
- `src/travel_agent/observability.py` — Langfuse (primary) / LangSmith (optional) callbacks + `run_config()`.
- `src/travel_agent/tools/` — agent tools; `guidebooks.py` = keyword search / page reads over extracted guidebooks.
- `src/travel_agent/kb/` — guidebook ingestion (PDF → markdown with `[p. N]` markers + TOC headings).
- See `docs/PLAN.md` for the target architecture and milestones.

## Conventions
- Settings only via `travel_agent.config.get_settings()`; never read `os.environ` directly.
- Structured data = Pydantic v2 models. Agent final answers use `response_format=<PydanticModel>`.
- Tools: plain typed Python functions with docstrings (docstring = tool description the LLM sees).
  Tools must return compact, LLM-friendly output (trim large payloads) and never raise into the agent
  loop — return an error string instead.
- Memory is **Postgres only** (no SQLite/in-memory outside tests). Thread memory via checkpointer keyed
  `thread_id = "<user_email>:<chat_id>"`; cross-thread facts via `Store` namespaced `(user_id, "memories")`.
- Identity: `user_id` = `X-OpenWebUI-User-Email` header (lowercased), passed as `UserContext` runtime
  context. Tools read it from `runtime.context.user_id`; never take user_id as an LLM-visible argument.
- The API passes only the latest user message to the agent; the checkpointer owns history.
- Flights: `fast-flights`. Hotels and web pages: Playwright scraping. No paid search APIs.
- `reference/` (raw PDFs) and `data/guidebooks/*.md` are copyrighted material: never commit them.
- Every agent invocation (incl. future subagents) uses `observability.run_config(user_id, thread_id)`
  so traces get tracing callbacks plus session/user metadata (`langfuse_session_id`, `langfuse_user_id`).
- Langfuse v4 runs in events-only mode: query traces via `/api/public/v2/observations`, not `/traces`.
- Streaming shows each tool call to the user as a `🔧 name(args)` status line (from the agent's
  "updates" stream), so keep tool names and args human-readable.
- Open WebUI runs with `ENABLE_PERSISTENT_CONFIG=false`: docker-compose.yml is the source of truth for
  its settings (it otherwise disables signup in its DB after the first account). Admin-panel changes
  don't survive restarts; put them in compose instead.
- Async first in the API layer (`ainvoke` / `astream`).
- Secrets live in `.env` (gitignored). Keep `.env.example` in sync when adding settings.

## Workflow
- Small commits; run ruff + pytest before committing.
- Tests must not hit real LLMs or the network — use fake chat models / mocks. `tests/conftest.py`
  forces tracing off. If a test needs tracing on, use `FakeListChatModel`: tracer serialization
  consumes `GenericFakeChatModel`'s message iterator. For agents with tools use `tests.fakes.FakeToolModel`
  (script tool calls as `AIMessage(tool_calls=...)`, with `disable_streaming=True`).

<!-- Add new sections below as the project evolves -->

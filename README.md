# travel-agent

Lightweight LangChain ReAct agent for organizing travel itineraries. LLM calls go through OpenRouter,
and the agent is served by FastAPI over an OpenAI-compatible API so Open WebUI (an Ollama-style chat UI)
can be the frontend.

![Demo: the agent searches Lonely Planet guidebooks with tool calls, then answers with a cited 3-day plan](docs/demo.gif)

*Open WebUI talking to the agent: each `🔧` line is a tool call (guidebook search / page read)
streamed live, followed by a day-by-day plan citing guidebook pages. Playback of the wait is 2× speed.*

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

### Memory

- **Within a chat:** the Postgres checkpointer keeps the conversation (thread = user email + chat id).
- **Across chats:** when you mention lasting preferences ("I'm vegetarian", "I fly out of SFO"),
  the agent calls `save_memory`; those facts are injected into its instructions in every later
  conversation for that user. Ask "what do you remember about me?" or "forget my budget" to
  inspect or remove them. Each signed-in email has its own memory.

### Structured itineraries

Finished day-by-day plans are produced as a validated Pydantic `Itinerary`
([schemas.py](src/travel_agent/agent/schemas.py)) through the `present_itinerary` tool: the chat shows
it as formatted markdown, and the JSON is stored per user and chat:

```bash
curl -s localhost:8000/api/itineraries -H "X-OpenWebUI-User-Email: you@example.com" | jq
```

### Skills

The agent has seven playbooks in [src/travel_agent/skills/](src/travel_agent/skills/):
trip-intake, itinerary-builder, budget-planner, packing-list, visa-and-entry, local-transport and
food-and-culture. Only their names and descriptions sit in the system prompt; the agent calls
`load_skill(name)` when a request matches (you'll see `🔧 load_skill(...)` in the chat).
To add one, drop a `<name>.md` with `name` / `description` frontmatter in that folder and add a
case to `scripts/eval_skills.py`, which checks against the real model that each skill triggers:

```bash
uv run python scripts/eval_skills.py   # 9/9 cases passing
```

### Re-recording the demo

With the stack running: `uv run python scripts/record_demo.py` (one-time:
`uv run playwright install chromium`). It signs in a demo user, asks the prompt (`--prompt`),
screenshots while the answer streams, and writes `docs/demo.gif`.

Tests and lint: `uv run pytest` · `uv run ruff check . && uv run ruff format --check .`

See [docs/PLAN.md](docs/PLAN.md) for the architecture and roadmap.

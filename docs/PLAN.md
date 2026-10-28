# Plan

## Architecture

```
Open WebUI (docker, :3000)  ── OpenAI-compatible HTTP (+ chat/user id headers) ──▶  FastAPI (:8000)
                                                                                     │
                                                                  create_agent (LangChain v1 ReAct loop)
                                                                                     │
                     ┌──────────────┬───────────────┬──────────────┬────────────────┼──────────────┐
                 OpenRouter     Python tools     MCP tools      Skills (.md)    Subagent:        Memory
                 (ChatOpenAI)   flights/hotels/  (langchain-    load_skill tool  knowledge_base   checkpointer (thread)
                                web (playwright) mcp-adapters)                  (grep guidebooks) store (user facts)
```

**Why OpenAI-compatible API + Open WebUI:** zero frontend code, the UI looks/feels like the Ollama app,
and any other OpenAI-compatible client (LibreChat, Chatbox, `curl`) works too. Open WebUI forwards
`X-OpenWebUI-Chat-Id` → our `thread_id` and `X-OpenWebUI-User-Id` → our store namespace.

## Target layout

```
src/travel_agent/
  config.py                 settings (.env)
  llm.py                    OpenRouter ChatOpenAI factory
  api/app.py                FastAPI app
  api/openai_compat.py      /v1/chat/completions (stream + non-stream), request→thread mapping
  agent/graph.py            create_agent(model, tools, middleware, checkpointer, store, response_format)
  agent/prompts.py          system prompt (includes skill index)
  agent/schemas.py          Pydantic: Itinerary, DayPlan, FlightOption, HotelOption, ...
  memory/                   checkpointer + store factories, save/recall memory tools
  tools/flights.py          Google Flights search
  tools/hotels.py           Google Hotels search
  tools/web.py              playwright: fetch + extract page text
  tools/mcp.py              MultiServerMCPClient loader (config from mcp.json)
  skills/*.md               skill files (frontmatter: name, description) + loader tool
  subagents/knowledge_base.py   KB agent wrapped as a tool (grep/read over data/guidebooks)
```

## Milestones

1. **Skeleton** ✅ uv project, FastAPI `/health` + `/v1/models`, Open WebUI compose, CLAUDE.md.
2. **Chat round-trip** — `/v1/chat/completions` → `create_agent` (OpenRouter), no tools. Verify in Open WebUI.
3. **Short-term memory** — `AsyncSqliteSaver` keyed by chat-id header; only send the *latest* user
   message to the agent (the checkpointer owns history). Fallback: hash of first message if no header.
4. **Tools** — flights, hotels, playwright web fetch. Each with unit tests against mocked responses.
5. **Long-term memory** — `Store` (InMemory → SQLite/Postgres) with `save_memory` / `search_memories`
   tools + a middleware that injects relevant user facts (home airport, budget, dietary needs,
   travel companions, loyalty programs) into the system prompt.
6. **Structured output** — `Itinerary` Pydantic model as `response_format` for the "build itinerary"
   path; API renders it to markdown for the chat UI.
7. **Skills** — `skills/*.md` with frontmatter; system prompt lists names+descriptions, `load_skill(name)`
   tool returns the body (progressive disclosure, Claude-Skills style).
8. **Knowledge-base subagent** — own `create_agent` with `grep_guidebooks` / `read_section` tools over
   text extracted from guidebook PDFs; exposed to main agent as a single `ask_guidebook` tool.
9. **MCP** — load servers from `mcp.json` via `langchain-mcp-adapters`.
10. **Streaming** — SSE `chat.completion.chunk`s from `agent.astream(stream_mode="messages")`;
    tool calls surfaced as short status lines.

## Skill ideas (`skills/*.md`)
- `itinerary-builder` — how to sequence days, pacing, buffer time, output the `Itinerary` schema.
- `budget-planner` — break down costs (flights/stay/food/transport/activities), currency handling.
- `trip-intake` — questions to ask before planning (dates, origin, party, budget, interests, mobility).
- `packing-list` — climate/activity-aware packing list.
- `visa-and-entry` — checklist of what to verify (visa, passport validity, vaccines) with official-source caveat.
- `local-transport` — rail passes, airport transfers, when to rent a car.
- `food-and-culture` — etiquette, tipping, dishes to try (pairs well with guidebook subagent).

## Open questions
See the setup conversation; answers get folded back into this doc.

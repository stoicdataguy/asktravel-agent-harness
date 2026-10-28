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
5. **Tools**: `search_flights` (fast-flights), `search_hotels` + `fetch_page` (Playwright; run
   `uv run playwright install chromium` once). Unit tests against recorded fixtures.
6. **Long-term memory**: `save_memory` / `recall_memories` tools on the store + a middleware that injects
   the user's saved facts (home airport, budget, diet, companions, loyalty programs) into the prompt.
7. **Structured output**: Pydantic `Itinerary` / `DayPlan` / `FlightOption` / `HotelOption`; an
   `build_itinerary` path with `response_format`, rendered to markdown for the chat UI.
8. **Skills**: `skills/*.md` with frontmatter; system prompt lists name + description, `load_skill(name)`
   returns the body (progressive disclosure).
9. **Knowledge-base subagent**: its own `create_agent` with `list_guidebooks`, `grep_guidebook`
   (returns hits with section heading + page) and `read_pages`; exposed to the main agent as `ask_guidebook`.
10. **MCP**: load servers from `mcp.json` via `langchain-mcp-adapters`.
11. **Streaming polish**: surface tool calls as short status lines in the stream.

## Skill ideas (`skills/*.md`)
- `trip-intake`: questions to ask before planning (dates, origin, party, budget, interests, mobility).
- `itinerary-builder`: sequencing days, pacing, buffer time, filling the `Itinerary` schema.
- `budget-planner`: cost breakdown (flights/stay/food/transport/activities), currency handling.
- `packing-list`: climate- and activity-aware packing list.
- `visa-and-entry`: what to verify (visa, passport validity, vaccines), always pointing to official sources.
- `local-transport`: rail passes, airport transfers, when to rent a car.
- `food-and-culture`: etiquette, tipping, dishes to try (pairs with the guidebook subagent).

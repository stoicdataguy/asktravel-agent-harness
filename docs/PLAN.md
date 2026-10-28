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

## Skill ideas (`skills/*.md`)
- `trip-intake`: questions to ask before planning (dates, origin, party, budget, interests, mobility).
- `itinerary-builder`: sequencing days, pacing, buffer time, filling the `Itinerary` schema.
- `budget-planner`: cost breakdown (flights/stay/food/transport/activities), currency handling.
- `packing-list`: climate- and activity-aware packing list.
- `visa-and-entry`: what to verify (visa, passport validity, vaccines), always pointing to official sources.
- `local-transport`: rail passes, airport transfers, when to rent a car.
- `food-and-culture`: etiquette, tipping, dishes to try (pairs with the guidebook subagent).

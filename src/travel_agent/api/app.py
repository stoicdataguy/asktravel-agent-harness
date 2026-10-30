import asyncio
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request

from travel_agent import user_data
from travel_agent.agent.graph import build_agent
from travel_agent.api.openai_compat import resolve_user
from travel_agent.api.openai_compat import router as openai_router
from travel_agent.chat_deletions import run_chat_deletion_sync
from travel_agent.config import get_settings
from travel_agent.memory import open_memory
from travel_agent.observability import check_tracing_config, flush_traces
from travel_agent.tools.conversations import backfill_chat_index
from travel_agent.tools.itinerary import list_itineraries
from travel_agent.tools.memory import migrate_memories
from travel_agent.tools.trips import backfill_trips, migrate_trip_ids


@asynccontextmanager
async def lifespan(app: FastAPI):
    check_tracing_config()
    async with open_memory(get_settings().database_url) as (checkpointer, store):
        await migrate_memories(store)
        await migrate_trip_ids(store)
        await backfill_trips(store)
        await backfill_chat_index(checkpointer, store)
        app.state.store = store
        app.state.checkpointer = checkpointer
        app.state.agent = build_agent(checkpointer, store)
        settings = get_settings()
        sync = asyncio.create_task(
            run_chat_deletion_sync(
                checkpointer.conn,
                checkpointer,
                store,
                settings.openwebui_schema,
                settings.chat_deletion_sync_seconds,
            )
        )
        yield
        sync.cancel()
    flush_traces()


app = FastAPI(title="Travel Agent", lifespan=lifespan)
app.include_router(openai_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/v1/models")
async def list_models() -> dict:
    """OpenAI-compatible model list so chat UIs can discover the agent."""
    return {
        "object": "list",
        "data": [
            {
                "id": get_settings().agent_model_name,
                "name": get_settings().agent_display_name,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "travel-agent",
            }
        ],
    }


@app.get("/api/itineraries")
async def itineraries(request: Request) -> dict:
    """Structured itineraries the agent produced for this user, newest first.

    The user comes from the same X-OpenWebUI-User-Email header as chat requests (dev-only trust).
    """
    return {"data": await list_itineraries(request.app.state.store, resolve_user(request.headers))}


@app.get("/api/memory")
async def memory_view(request: Request) -> dict:
    """What AskTravel remembers about the calling user: facts, trips, saved plans and chats.

    Like /api/itineraries, the user comes from the X-OpenWebUI-User-Email header (dev-only trust
    until the API is authenticated, #27).
    """
    state = request.app.state
    return await user_data.remembered(
        state.store, state.checkpointer, resolve_user(request.headers)
    )


@app.get("/api/memory/export")
async def memory_export(request: Request) -> dict:
    """Everything stored for the calling user, including every chat's messages, as JSON."""
    state = request.app.state
    return await user_data.export(state.store, state.checkpointer, resolve_user(request.headers))


@app.delete("/api/memory")
async def memory_delete(request: Request, confirm: str = "") -> dict:
    """Delete everything AskTravel keeps about the calling user.

    `confirm` must repeat the user's email, so a stray request can't wipe someone's memory.
    """
    user_id = resolve_user(request.headers)
    if confirm.lower() != user_id:
        raise HTTPException(400, detail="Pass ?confirm=<your email> to delete all your data.")
    state = request.app.state
    return {"deleted": await user_data.delete_all(state.store, state.checkpointer, user_id)}

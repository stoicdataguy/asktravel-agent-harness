import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from travel_agent.agent.graph import build_agent
from travel_agent.api.openai_compat import resolve_user
from travel_agent.api.openai_compat import router as openai_router
from travel_agent.config import get_settings
from travel_agent.memory import open_memory
from travel_agent.observability import check_tracing_config, flush_traces
from travel_agent.tools.itinerary import list_itineraries
from travel_agent.tools.trips import backfill_trips


@asynccontextmanager
async def lifespan(app: FastAPI):
    check_tracing_config()
    async with open_memory(get_settings().database_url) as (checkpointer, store):
        await backfill_trips(store)
        app.state.store = store
        app.state.agent = build_agent(checkpointer, store)
        yield
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

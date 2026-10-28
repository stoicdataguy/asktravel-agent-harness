import time
from contextlib import asynccontextmanager

from fastapi import FastAPI

from travel_agent.agent.graph import build_agent
from travel_agent.api.openai_compat import router as openai_router
from travel_agent.config import get_settings
from travel_agent.memory import open_memory
from travel_agent.observability import flush_traces


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with open_memory(get_settings().database_url) as (checkpointer, store):
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
                "object": "model",
                "created": int(time.time()),
                "owned_by": "travel-agent",
            }
        ],
    }

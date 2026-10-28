import time

from fastapi import FastAPI

from travel_agent.config import get_settings

app = FastAPI(title="Travel Agent")


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

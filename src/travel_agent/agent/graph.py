from datetime import date

from langchain.agents import create_agent
from langchain.agents.middleware import ModelRequest, dynamic_prompt
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.store.base import BaseStore

from travel_agent.agent.prompts import SYSTEM_PROMPT
from travel_agent.context import UserContext
from travel_agent.llm import get_chat_model
from travel_agent.skills import load_skill, skills_index
from travel_agent.tools.flights import search_flights
from travel_agent.tools.guidebooks import GUIDEBOOK_TOOLS
from travel_agent.tools.itinerary import present_itinerary
from travel_agent.tools.memory import MEMORY_TOOLS, format_memories, load_memories
from travel_agent.tools.trips import TRIP_TOOLS, format_trips, load_trips

__all__ = ["UserContext", "build_agent", "render_system_prompt"]


@dynamic_prompt
async def system_prompt(request: ModelRequest) -> str:
    # Re-read every model call so facts and trips saved earlier in this turn are already visible
    store, user_id = request.runtime.store, request.runtime.context.user_id
    return render_system_prompt(
        await load_memories(store, user_id), await load_trips(store, user_id)
    )


def render_system_prompt(memories: dict[str, str] | None = None, trips=None) -> str:
    return SYSTEM_PROMPT.format(
        today=date.today().isoformat(),
        skills=skills_index(),
        memories=format_memories(memories or {}),
        trips=format_trips(trips or []),
    )


def build_agent(
    checkpointer: BaseCheckpointSaver,
    store: BaseStore,
    model: BaseChatModel | None = None,
):
    return create_agent(
        model or get_chat_model(),
        tools=[
            *GUIDEBOOK_TOOLS,
            search_flights,
            load_skill,
            *MEMORY_TOOLS,
            *TRIP_TOOLS,
            present_itinerary,
        ],
        middleware=[system_prompt],
        context_schema=UserContext,
        checkpointer=checkpointer,
        store=store,
        name="travel_agent",
    )

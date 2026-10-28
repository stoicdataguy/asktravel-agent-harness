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
from travel_agent.tools.guidebooks import GUIDEBOOK_TOOLS
from travel_agent.tools.memory import MEMORY_TOOLS, format_memories, load_memories

__all__ = ["UserContext", "build_agent", "render_system_prompt"]


@dynamic_prompt
async def system_prompt(request: ModelRequest) -> str:
    # Re-read every model call so facts saved earlier in this same turn are already visible
    memories = await load_memories(request.runtime.store, request.runtime.context.user_id)
    return render_system_prompt(memories)


def render_system_prompt(memories: dict[str, str] | None = None) -> str:
    return SYSTEM_PROMPT.format(
        today=date.today().isoformat(),
        skills=skills_index(),
        memories=format_memories(memories or {}),
    )


def build_agent(
    checkpointer: BaseCheckpointSaver,
    store: BaseStore,
    model: BaseChatModel | None = None,
):
    return create_agent(
        model or get_chat_model(),
        tools=[*GUIDEBOOK_TOOLS, load_skill, *MEMORY_TOOLS],
        middleware=[system_prompt],
        context_schema=UserContext,
        checkpointer=checkpointer,
        store=store,
        name="travel_agent",
    )

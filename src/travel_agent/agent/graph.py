from dataclasses import dataclass
from datetime import date

from langchain.agents import create_agent
from langchain.agents.middleware import ModelRequest, dynamic_prompt
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.store.base import BaseStore

from travel_agent.agent.prompts import SYSTEM_PROMPT
from travel_agent.llm import get_chat_model
from travel_agent.skills import load_skill, skills_index
from travel_agent.tools.guidebooks import GUIDEBOOK_TOOLS


@dataclass
class UserContext:
    """Per-request runtime context, available to tools and middleware as `runtime.context`."""

    user_id: str


@dynamic_prompt
def system_prompt(request: ModelRequest) -> str:
    return render_system_prompt()


def render_system_prompt() -> str:
    return SYSTEM_PROMPT.format(today=date.today().isoformat(), skills=skills_index())


def build_agent(
    checkpointer: BaseCheckpointSaver,
    store: BaseStore,
    model: BaseChatModel | None = None,
):
    return create_agent(
        model or get_chat_model(),
        tools=[*GUIDEBOOK_TOOLS, load_skill],
        middleware=[system_prompt],
        context_schema=UserContext,
        checkpointer=checkpointer,
        store=store,
        name="travel_agent",
    )

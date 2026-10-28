"""`present_itinerary`: the structured-output path for finished trip plans.

The tool's argument schema is the Pydantic `Itinerary`, so LangChain validates the plan and sends
any validation error back to the model to fix. The validated plan is saved per user and chat, and
the rendered markdown travels as the tool message's artifact: the API streams it to the chat, so
the model doesn't have to write the plan out a second time.
"""

from datetime import UTC, datetime

from langchain.tools import ToolRuntime, tool
from langgraph.store.base import BaseStore

from travel_agent.agent.schemas import Itinerary, render_itinerary
from travel_agent.context import UserContext
from travel_agent.memory import user_namespace
from travel_agent.tools.trips import link_itinerary


def namespace(user_id: str) -> tuple[str, ...]:
    return user_namespace(user_id, "itineraries")


@tool(response_format="content_and_artifact")
async def present_itinerary(
    itinerary: Itinerary, runtime: ToolRuntime[UserContext]
) -> tuple[str, str]:
    """Show the user a complete day-by-day itinerary.

    Call this instead of writing a day-by-day plan as text, once you have researched the trip.
    To change an itinerary, call it again with the full updated plan. The rendered plan is shown
    to the user directly; afterwards reply with at most one or two sentences (e.g. offer
    adjustments) and do not repeat the plan.
    """
    thread_id = runtime.config.get("configurable", {}).get("thread_id", "default")
    user_id = runtime.context.user_id
    # Every plan belongs to a trip, so other chats can see it via the Trips list
    trip = await link_itinerary(runtime.store, user_id, itinerary, thread_id)
    await runtime.store.aput(
        namespace(user_id),
        thread_id,
        {
            "itinerary": itinerary.model_dump(mode="json"),
            "trip_id": trip.trip_id,
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )
    content = (
        f"Itinerary '{itinerary.title}' ({len(itinerary.days)} days) is now displayed to the "
        f"user in full and saved to trip {trip.trip_id}. Reply with one short sentence offering a "
        "concrete adjustment or asking one question. Don't summarize or repeat any of the plan."
    )
    return content, render_itinerary(itinerary)


async def list_itineraries(store: BaseStore, user_id: str) -> list[dict]:
    items = await store.asearch(namespace(user_id), limit=100)
    items.sort(key=lambda item: item.value.get("updated_at", ""), reverse=True)
    return [{"thread_id": item.key, **item.value} for item in items]

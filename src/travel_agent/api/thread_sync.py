"""Keep the agent's checkpointed thread on the same branch as the chat UI's transcript.

The API normally passes only the newest user message; the checkpointer owns the history. That
breaks when the transcript and the thread disagree, which happens whenever Open WebUI branches:

- fork: a new chat id (so an empty thread) whose transcript already has earlier turns
- regenerate: the newest user message is already in the thread
- edit: an earlier user message differs from the thread

`plan_run` compares the transcript's earlier user messages with the thread's and returns the
inputs and config to run with. Assistant turns aren't compared: the UI's copy includes tool status
lines and rendered itineraries, so it never matches the model's own messages exactly.
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, RemoveMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.message import REMOVE_ALL_MESSAGES

# The `🔧 tool(args)` lines the API streams into the chat; noise when replayed to the model
_STATUS_LINE = re.compile(r"^`🔧 .*`\s*$", re.MULTILINE)


class SyncAction(StrEnum):
    APPEND = "append"  # thread matches the transcript
    SEED = "seed"  # empty thread, transcript has history (fork)
    REWIND = "rewind"  # branch from an earlier checkpoint (regenerate / edit)
    REPLACE = "replace"  # no matching checkpoint: rebuild the thread from the transcript


@dataclass
class Turn:
    role: str  # "user" | "assistant"
    text: str


@dataclass
class RunPlan:
    action: SyncAction
    inputs: dict[str, Any]
    config: RunnableConfig


def _humans(messages: list[BaseMessage]) -> list[str]:
    return [m.text.strip() for m in messages if isinstance(m, HumanMessage)]


def _seed_messages(history: list[Turn]) -> list[BaseMessage]:
    seeded: list[BaseMessage] = []
    for turn in history:
        if turn.role == "user":
            seeded.append(HumanMessage(turn.text))
        elif turn.role == "assistant":
            text = re.sub(r"\n{3,}", "\n\n", _STATUS_LINE.sub("", turn.text)).strip()
            if text:
                seeded.append(AIMessage(text))
    return seeded


async def plan_run(agent, config: RunnableConfig, transcript: list[Turn]) -> RunPlan:
    """Decide how to run the newest user message against the thread in `config`.

    `transcript` is the chat as the UI sent it (user/assistant turns), ending with the new user
    message.
    """
    *history, newest = transcript
    new_message = HumanMessage(newest.text)
    prior_users = [t.text.strip() for t in history if t.role == "user"]

    state = await agent.aget_state(config)
    thread_messages = state.values.get("messages", []) if state else []
    thread_users = _humans(thread_messages)

    if thread_users == prior_users:
        return RunPlan(SyncAction.APPEND, {"messages": [new_message]}, config)

    if not thread_messages:
        seeded = _seed_messages(history)
        return RunPlan(SyncAction.SEED, {"messages": [*seeded, new_message]}, config)

    if prior_users:
        # Latest finished turn (nothing left to run) whose user messages match the transcript.
        # Running from its checkpoint forks the thread there; the old branch stays in history.
        async for snapshot in agent.aget_state_history(config):
            messages = snapshot.values.get("messages", [])
            if not snapshot.next and _humans(messages) == prior_users:
                # Keep the caller's callbacks/metadata; take the checkpoint to fork from
                configurable = {**config["configurable"], **snapshot.config["configurable"]}
                branch = {**config, "configurable": configurable}
                return RunPlan(SyncAction.REWIND, {"messages": [new_message]}, branch)

    # E.g. the first message was edited: the only earlier checkpoint still carries the original
    # input, so replace the thread's messages with the transcript instead
    rebuilt = [RemoveMessage(id=REMOVE_ALL_MESSAGES), *_seed_messages(history), new_message]
    return RunPlan(SyncAction.REPLACE, {"messages": rebuilt}, config)

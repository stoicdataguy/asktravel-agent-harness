"""OpenAI-compatible chat completions endpoint backed by the agent.

The chat UI sends the full conversation every turn, but the agent's checkpointer already owns the
history, so normally only the latest user message is passed in; `thread_sync` handles the cases
where the UI has branched (fork, regenerate, edit). Threads are keyed by the chat id header
Open WebUI forwards; users by the forwarded email.
"""

import hashlib
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import openai
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import BaseModel, ConfigDict

from travel_agent.api.thread_sync import Turn, plan_run
from travel_agent.context import UserContext
from travel_agent.observability import run_config

logger = logging.getLogger(__name__)
router = APIRouter()

ANONYMOUS_USER = "anonymous"


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: str
    content: str | list[dict[str, Any]] | None = None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str
    messages: list[ChatMessage]
    stream: bool = False


def message_text(message: ChatMessage) -> str:
    if isinstance(message.content, list):
        parts = message.content
        return "".join(p.get("text", "") for p in parts if p.get("type") == "text")
    return message.content or ""


def resolve_user(headers: Any) -> str:
    return (headers.get("x-openwebui-user-email") or ANONYMOUS_USER).lower()


def resolve_ids(headers: Any, messages: list[ChatMessage]) -> tuple[str, str]:
    """Return (user_id, thread_id) for a request."""
    user_id = resolve_user(headers)
    chat_id = headers.get("x-openwebui-chat-id")
    if not chat_id:
        # Clients without a chat id: a conversation is identified by its first user message
        first = next((message_text(m) for m in messages if m.role == "user"), "")
        chat_id = hashlib.sha256(first.encode()).hexdigest()[:16]
    return user_id, f"{user_id}:{chat_id}"


def _chunk(completion_id: str, model: str, delta: dict, finish_reason: str | None = None) -> str:
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload)}\n\n"


MAX_STATUS_ARG_CHARS = 80


def _short(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= MAX_STATUS_ARG_CHARS else text[: MAX_STATUS_ARG_CHARS - 1] + "…"


def tool_status(tool_call: dict) -> str:
    """One-line markdown note shown in the chat while a tool runs (long args truncated)."""
    args = ", ".join(f"{k}={_short(v)}" for k, v in tool_call["args"].items())
    return f"\n\n`🔧 {tool_call['name']}({args})`\n\n"


def displayed_artifacts(messages: list) -> list[str]:
    """Rendered markdown that tools (present_itinerary) produced for the user to see."""
    return [
        m.artifact for m in messages if isinstance(m, ToolMessage) and isinstance(m.artifact, str)
    ]


RATE_LIMITED = (
    "⚠️ The AI model provider is rate-limiting requests right now, so I couldn't finish this "
    "answer. Please try again in a minute."
)


def error_message(exc: Exception) -> str:
    """What the user sees in the chat when a turn fails."""
    if isinstance(exc, openai.RateLimitError) or getattr(exc, "status_code", None) == 429:
        return RATE_LIMITED
    return f"⚠️ Something went wrong while answering ({type(exc).__name__}). Please try again."


async def _stream(agent, inputs, config, context, model: str) -> AsyncIterator[str]:
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    yield _chunk(completion_id, model, {"role": "assistant", "content": ""})
    # "messages" streams model tokens; "updates" delivers each completed model turn, whose
    # tool_calls (with full args) we surface as status lines
    try:
        async for mode, payload in agent.astream(
            inputs, config, context=context, stream_mode=["messages", "updates"]
        ):
            if mode == "messages":
                message, metadata = payload
                # Token chunks when the model streams; one whole AIMessage when it doesn't
                if metadata.get("langgraph_node") == "model" and isinstance(message, AIMessage):
                    if message.text:
                        yield _chunk(completion_id, model, {"content": message.text})
            elif "model" in payload and isinstance(payload["model"], dict):
                for message in payload["model"].get("messages", []):
                    for tool_call in getattr(message, "tool_calls", None) or []:
                        yield _chunk(completion_id, model, {"content": tool_status(tool_call)})
            elif "tools" in payload and isinstance(payload["tools"], dict):
                for artifact in displayed_artifacts(payload["tools"].get("messages", [])):
                    yield _chunk(completion_id, model, {"content": f"\n\n{artifact}\n\n"})
    except Exception as exc:
        # Headers are already sent: end the stream properly with a readable message instead of
        # dropping the connection mid-answer
        logger.exception("agent run failed mid-stream")
        yield _chunk(completion_id, model, {"content": f"\n\n{error_message(exc)}"})
    yield _chunk(completion_id, model, {}, finish_reason="stop")
    yield "data: [DONE]\n\n"


@router.post("/v1/chat/completions")
async def chat_completions(body: ChatCompletionRequest, request: Request):
    user_messages = [m for m in body.messages if m.role == "user"]
    if not user_messages:
        raise HTTPException(status_code=400, detail="No user message in request")

    user_id, thread_id = resolve_ids(request.headers, body.messages)
    agent = request.app.state.agent
    last_user = max(i for i, m in enumerate(body.messages) if m.role == "user")
    transcript = [
        Turn(m.role, message_text(m))
        for m in body.messages[: last_user + 1]
        if m.role in ("user", "assistant")
    ]
    plan = await plan_run(agent, run_config(user_id, thread_id), transcript)
    plan.config["metadata"]["thread_sync"] = plan.action.value
    if plan.action != "append":
        logger.info("thread %s: %s", thread_id, plan.action.value)
    inputs, config = plan.inputs, plan.config
    context = UserContext(user_id=user_id)

    if body.stream:
        return StreamingResponse(
            _stream(agent, inputs, config, context, body.model), media_type="text/event-stream"
        )

    try:
        result = await agent.ainvoke(inputs, config, context=context)
    except Exception as exc:
        logger.exception("agent run failed")
        content = error_message(exc)
    else:
        # Only this turn's messages: everything after the last human message
        last_human = max(i for i, m in enumerate(result["messages"]) if m.type == "human")
        turn = result["messages"][last_human + 1 :]
        content = "\n\n".join([*displayed_artifacts(turn), result["messages"][-1].text]).strip()
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }

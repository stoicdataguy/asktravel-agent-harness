"""OpenAI-compatible chat completions endpoint backed by the agent.

The chat UI sends the full conversation every turn, but the agent's checkpointer already owns the
history, so only the latest user message is passed in. Threads are keyed by the chat id header
Open WebUI forwards; users by the forwarded email.
"""

import hashlib
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessageChunk, HumanMessage
from pydantic import BaseModel, ConfigDict

from travel_agent.agent.graph import UserContext

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


def resolve_ids(headers: Any, messages: list[ChatMessage]) -> tuple[str, str]:
    """Return (user_id, thread_id) for a request."""
    user_id = (headers.get("x-openwebui-user-email") or ANONYMOUS_USER).lower()
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


async def _stream(agent, inputs, config, context, model: str) -> AsyncIterator[str]:
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    yield _chunk(completion_id, model, {"role": "assistant", "content": ""})
    async for message, metadata in agent.astream(
        inputs, config, context=context, stream_mode="messages"
    ):
        if metadata.get("langgraph_node") == "model" and isinstance(message, AIMessageChunk):
            if message.text:
                yield _chunk(completion_id, model, {"content": message.text})
    yield _chunk(completion_id, model, {}, finish_reason="stop")
    yield "data: [DONE]\n\n"


@router.post("/v1/chat/completions")
async def chat_completions(body: ChatCompletionRequest, request: Request):
    user_messages = [m for m in body.messages if m.role == "user"]
    if not user_messages:
        raise HTTPException(status_code=400, detail="No user message in request")

    user_id, thread_id = resolve_ids(request.headers, body.messages)
    agent = request.app.state.agent
    inputs = {"messages": [HumanMessage(message_text(user_messages[-1]))]}
    config = {"configurable": {"thread_id": thread_id}}
    context = UserContext(user_id=user_id)

    if body.stream:
        return StreamingResponse(
            _stream(agent, inputs, config, context, body.model), media_type="text/event-stream"
        )

    result = await agent.ainvoke(inputs, config, context=context)
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": result["messages"][-1].text},
                "finish_reason": "stop",
            }
        ],
    }

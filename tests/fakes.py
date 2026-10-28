from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import BaseMessage
from pydantic import Field


class FakeToolModel(GenericFakeChatModel):
    """Scripted chat model that accepts bind_tools and records what it was sent.

    Script tool calls by putting AIMessage(tool_calls=[...]) in the message iterator.
    `prompts` collects the system prompt of every call, newest last.
    """

    prompts: list[str] = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        return self

    def _record(self, messages: list[BaseMessage]) -> None:
        self.prompts.append(messages[0].text if messages and messages[0].type == "system" else "")

    def _generate(self, messages, *args, **kwargs):
        self._record(messages)
        return super()._generate(messages, *args, **kwargs)

    def _stream(self, messages, *args, **kwargs):
        self._record(messages)
        yield from super()._stream(messages, *args, **kwargs)

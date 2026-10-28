from langchain_core.language_models.fake_chat_models import GenericFakeChatModel


class FakeToolModel(GenericFakeChatModel):
    """Scripted chat model that accepts bind_tools.

    Script tool calls by putting AIMessage(tool_calls=[...]) in the message iterator.
    """

    def bind_tools(self, tools, **kwargs):
        return self

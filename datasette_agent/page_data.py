from pydantic import BaseModel


class ConversationSummary(BaseModel):
    id: str
    title: str | None
    created_at: str
    updated_at: str


class IndexPageData(BaseModel):
    conversations: list[ConversationSummary]


class Message(BaseModel):
    id: int
    conversation_id: str
    role: str
    content: str | None = None
    tool_name: str | None = None
    tool_arguments: str | None = None
    tool_output: str | None = None
    created_at: str


class ConversationPageData(BaseModel):
    conversation_id: str
    title: str | None
    messages: list[Message]


__exports__ = [IndexPageData, ConversationPageData]

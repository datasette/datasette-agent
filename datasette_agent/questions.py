"""Human-in-the-loop questions for agent tools.

Tools ask questions through ``await context.ask_user(...)`` on the
ToolContext they receive (see tool_context.py). That call either
replays a previously stored answer (when the tool call is being
re-executed after the user answered) or records a pending question and
raises QuestionPending to suspend the turn.

The agent_questions table is the durable state: a suspended
conversation can resume after a server restart because everything
ask_user needs is keyed on (conversation_id, call_key, ask_index).
"""

import json

import llm


class QuestionPending(llm.PauseChain):
    """Raised by ask_user to suspend the current turn.

    As an llm.PauseChain subclass it propagates cleanly out of the
    chain: concurrent sibling tool calls run to completion first, no
    provider call is made with a placeholder result, and the agent
    loop catches it to end the turn. Resuming the chain with the
    persisted history re-executes the pending call, replaying answered
    questions from the agent_questions table.
    """

    def __init__(self, question):
        super().__init__(question["prompt"])
        self.question = question


class QuestionsNotSupported(Exception):
    """ask_user is unavailable (e.g. background agents, CLI chat)."""


def question_row_to_dict(row):
    """Shape an agent_questions row like the dict QuestionPending carries."""
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "call_key": row["call_key"],
        "ask_index": row["ask_index"],
        "tool_name": row["tool_name"],
        "question_type": row["question_type"],
        "prompt": row["prompt"],
        "options": json.loads(row["options_json"]) if row["options_json"] else None,
        "html": row["html"],
    }

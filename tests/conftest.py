import llm_echo
import pytest
from llm.parts import TextPart


def _echo_prompt_text(prompt):
    """The text the echo model should act on for this model call.

    datasette-agent sends the conversation as messages= with no prompt
    text, so prompt.prompt is empty. Echo only reads prompt.prompt, so
    use the text of the user message that ends the history instead. A
    call that follows tool results ends in a tool message and gets "",
    as it did when the prompt text was passed separately.
    """
    if prompt.prompt:
        return prompt.prompt
    messages = prompt.messages
    if messages and messages[-1].role == "user":
        for part in messages[-1].parts:
            if isinstance(part, TextPart) and part.text:
                return part.text
    return ""


class _EchoPrompt:
    "A prompt whose .prompt is the text from _echo_prompt_text()."

    def __init__(self, prompt):
        self._wrapped = prompt

    @property
    def prompt(self):
        return _echo_prompt_text(self._wrapped)

    def __getattr__(self, name):
        return getattr(self._wrapped, name)


@pytest.fixture
def echo_prompt_text():
    "Function returning the text the echo model acts on for a prompt."
    return _echo_prompt_text


@pytest.fixture(autouse=True)
def echo_reads_last_user_message(monkeypatch):
    """Let tests keep scripting the echo model through the user message,
    for example with JSON holding "tool_calls"."""
    original = llm_echo._Shared.shared

    def shared(self, prompt, stream, response, conversation):
        return original(self, _EchoPrompt(prompt), stream, response, conversation)

    monkeypatch.setattr(llm_echo._Shared, "shared", shared)

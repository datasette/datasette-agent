"""Chat turns running in this process, so they can be stopped.

A turn keeps running when the browser disconnects, so stopping one goes
through its TurnControl rather than the request that started it.

Stopping is cooperative around tools: a tool that has started is never
interrupted (a write may already have happened), and the turn stops
before its next model call. Only a model response that is streaming is
cancelled outright.
"""

import asyncio
from contextlib import asynccontextmanager

ALREADY_RUNNING_MESSAGE = (
    "The agent is still working on this conversation. "
    "Wait for it to finish, or stop it first."
)


class TurnControl:
    def __init__(self, task):
        self.task = task
        self.stop_requested = False
        # True while a model response streams: safe to cancel.
        self.streaming = False
        # Tools that have started and not finished: never cancelled.
        self.tools_running = 0

    def request_stop(self):
        self.stop_requested = True
        if self.streaming and not self.tools_running:
            self.task.cancel()


def _running_turns(datasette):
    turns = getattr(datasette, "_agent_running_turns", None)
    if turns is None:
        turns = datasette._agent_running_turns = {}
    return turns


class TurnAlreadyRunning(Exception):
    pass


@asynccontextmanager
async def track_turn(datasette, conversation_id):
    turns = _running_turns(datasette)
    # Check and register with no await in between, so two requests can't
    # both start a turn on one conversation.
    if conversation_id in turns:
        raise TurnAlreadyRunning(ALREADY_RUNNING_MESSAGE)
    control = TurnControl(asyncio.current_task())
    turns[conversation_id] = control
    try:
        yield control
    finally:
        if turns.get(conversation_id) is control:
            del turns[conversation_id]


def get_turn(datasette, conversation_id):
    return _running_turns(datasette).get(conversation_id)


def stop_turn(datasette, conversation_id):
    """Ask the running turn to stop. Returns False if none is running."""
    control = get_turn(datasette, conversation_id)
    if control is None:
        return False
    control.request_stop()
    return True

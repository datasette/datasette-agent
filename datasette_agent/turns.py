"""Chat turns running in this process, so they can be listed, watched
and stopped.

A turn keeps running when the browser disconnects, so watching and
stopping one go through its TurnControl rather than the request that
started it.

Stopping is cooperative around tools: a tool that has started is never
interrupted (a write may already have happened), and the turn stops
before its next model call. Only a model response that is streaming is
cancelled outright.

Watching: every event the turn sends is also published to watchers. The
turn keeps the events of its current step - everything since all earlier
messages were saved - so a page opened mid-turn can render the saved
messages up to the step's watermark and replay the rest.
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
        # Events since the current step began, and the id of the last
        # message saved before it.
        self.step_events = []
        self.watermark = None
        self.watchers = set()

    def request_stop(self):
        self.stop_requested = True
        if self.streaming and not self.tools_running:
            self.task.cancel()

    async def new_step(self, db, conversation_id):
        """Call when every earlier event has been saved as a message."""
        self.watermark = await _last_message_id(db, conversation_id)
        self.step_events = []

    def publish(self, chunk):
        self.step_events.append(chunk)
        for queue in self.watchers:
            queue.put_nowait(chunk)

    def watch(self):
        """A queue of this step's events so far, then each new one, then
        None when the turn ends."""
        queue = asyncio.Queue()
        for chunk in self.step_events:
            queue.put_nowait(chunk)
        self.watchers.add(queue)
        return queue

    def close(self):
        for queue in self.watchers:
            queue.put_nowait(None)
        self.watchers.clear()


class TurnWriter:
    """Sends a turn's events to the request that started it and to anyone
    watching. The turn carries on if the request's client has gone."""

    def __init__(self, writer, control):
        self.writer = writer
        self.control = control

    async def write(self, chunk):
        self.control.publish(chunk)
        try:
            await self.writer.write(chunk)
        except Exception:
            pass


async def _last_message_id(db, conversation_id):
    row = (
        await db.execute(
            "SELECT max(id) AS id FROM agent_messages WHERE conversation_id = ?",
            [conversation_id],
        )
    ).first()
    return (row["id"] or 0) if row else 0


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
        control.watermark = await _last_message_id(
            datasette.get_internal_database(), conversation_id
        )
        yield control
    finally:
        if turns.get(conversation_id) is control:
            del turns[conversation_id]
        control.close()


def get_turn(datasette, conversation_id):
    return _running_turns(datasette).get(conversation_id)


def stop_turn(datasette, conversation_id):
    """Ask the running turn to stop. Returns False if none is running."""
    control = get_turn(datasette, conversation_id)
    if control is None:
        return False
    control.request_stop()
    return True

    return set(_running_turns(datasette))

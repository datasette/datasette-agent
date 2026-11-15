"""Chat turns running in this process, so they can be listed, watched
and stopped.

A turn runs in a task of its own and sends its events to its
TurnControl, never directly to a request. The request that started it
watches it like any other page, so the turn keeps running when that
browser disconnects.

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

ALREADY_RUNNING_MESSAGE = (
    "The agent is still working on this conversation. "
    "Wait for it to finish, or stop it first."
)


class TurnControl:
    def __init__(self):
        # The task running the turn, set by start_turn().
        self.task = None
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

    def events(self):
        """An async iterator of this step's events so far, then each new
        one, ending when the turn does.

        The caller is subscribed before this returns, not when iteration
        starts, so it misses nothing published in between.
        """
        queue = asyncio.Queue()
        for chunk in self.step_events:
            queue.put_nowait(chunk)
        self.watchers.add(queue)
        return self._drain(queue)

    async def _drain(self, queue):
        try:
            while (chunk := await queue.get()) is not None:
                yield chunk
        finally:
            # Runs if the watcher goes away before the turn ends
            self.watchers.discard(queue)

    def close(self):
        for queue in self.watchers:
            queue.put_nowait(None)
        self.watchers.clear()


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


def start_turn(datasette, conversation_id, run):
    """Start a turn: run(control) in a new task. Returns its TurnControl.

    Raises TurnAlreadyRunning if the conversation already has one. There
    is no await here, so two requests can't both start a turn on one
    conversation, and a caller that calls control.events() straight away
    sees every event.
    """
    turns = _running_turns(datasette)
    if conversation_id in turns:
        raise TurnAlreadyRunning(ALREADY_RUNNING_MESSAGE)
    control = TurnControl()
    turns[conversation_id] = control

    async def turn():
        try:
            await run(control)
        finally:
            if turns.get(conversation_id) is control:
                del turns[conversation_id]
            control.close()

    # The registry holds the control, which keeps the task referenced.
    control.task = asyncio.create_task(turn())
    return control


def get_turn(datasette, conversation_id):
    return _running_turns(datasette).get(conversation_id)


def stop_turn(datasette, conversation_id):
    """Ask the running turn to stop. Returns False if none is running."""
    control = get_turn(datasette, conversation_id)
    if control is None:
        return False
    control.request_stop()
    return True


def running_conversation_ids(datasette):
    return set(_running_turns(datasette))

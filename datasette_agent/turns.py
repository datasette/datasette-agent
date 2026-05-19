from contextlib import asynccontextmanager
import threading


class ConversationTurnAlreadyRunning(Exception):
    pass


_active_conversation_ids = set()
_active_lock = threading.Lock()


@asynccontextmanager
async def conversation_turn(conversation_id):
    with _active_lock:
        if conversation_id in _active_conversation_ids:
            raise ConversationTurnAlreadyRunning(conversation_id)
        _active_conversation_ids.add(conversation_id)
    try:
        yield
    finally:
        with _active_lock:
            _active_conversation_ids.discard(conversation_id)

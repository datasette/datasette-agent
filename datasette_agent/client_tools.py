import asyncio
from dataclasses import dataclass
import json
import secrets


CLIENT_TOOL_RESULT_MAX_BYTES = 2 * 1024 * 1024


@dataclass
class PendingClientToolCall:
    future: asyncio.Future
    actor_id: str | None
    nonce: str
    tool_name: str


_pending_calls = {}


def actor_id(actor):
    if actor:
        value = actor.get("id")
        if value is not None:
            return str(value)
    return None


async def send_sse(writer, event, data):
    await writer.write(f"event: {event}\ndata: {json.dumps(data)}\n\n")


def _payload_to_tool_output(payload):
    if not payload.get("ok"):
        return json.dumps({"error": payload.get("error") or "Client tool failed"})
    result = payload.get("result")
    if isinstance(result, dict):
        return json.dumps(result)
    return json.dumps({"result": result})


class BrowserClientToolRunner:
    def __init__(self, writer, conversation_id, actor):
        self.writer = writer
        self.conversation_id = conversation_id
        self.actor_id = actor_id(actor)

    async def run(self, client_tool, arguments):
        loop = asyncio.get_running_loop()
        call_id = secrets.token_urlsafe(18)
        nonce = secrets.token_urlsafe(24)
        future = loop.create_future()
        key = (self.conversation_id, call_id)
        _pending_calls[key] = PendingClientToolCall(
            future=future,
            actor_id=self.actor_id,
            nonce=nonce,
            tool_name=client_tool.name,
        )
        try:
            await send_sse(
                self.writer,
                "client_tool_call",
                {
                    "id": call_id,
                    "nonce": nonce,
                    "name": client_tool.name,
                    "arguments": arguments,
                    "result_url": (
                        f"/-/agent/{self.conversation_id}"
                        f"/client-tool-result/{call_id}"
                    ),
                },
            )
            payload = await asyncio.wait_for(future, timeout=client_tool.timeout)
            return _payload_to_tool_output(payload)
        except asyncio.TimeoutError:
            return json.dumps(
                {"error": f"Timed out waiting for browser tool {client_tool.name}"}
            )
        finally:
            _pending_calls.pop(key, None)


def resolve_client_tool_call(conversation_id, call_id, actor, payload):
    pending = _pending_calls.get((conversation_id, call_id))
    if pending is None:
        return False
    if actor_id(actor) != pending.actor_id:
        raise PermissionError("Actor does not match pending client tool call")
    if payload.get("nonce") != pending.nonce:
        raise PermissionError("Invalid client tool nonce")
    if pending.future.done():
        return False
    pending.future.set_result(payload)
    return True

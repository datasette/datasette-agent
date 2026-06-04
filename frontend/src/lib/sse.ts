export interface TextChunkEvent {
  type: "text_chunk";
  content: string;
}

export interface ToolCallEvent {
  type: "tool_call";
  name: string;
  arguments: Record<string, unknown>;
}

export interface ToolResultEvent {
  type: "tool_result";
  name: string;
  output: string;
}

export interface DoneEvent {
  type: "done";
}

export interface ErrorEvent {
  type: "error";
  message: string;
}

export type SSEEvent =
  | TextChunkEvent
  | ToolCallEvent
  | ToolResultEvent
  | DoneEvent
  | ErrorEvent;

export async function* streamSSE(
  conversationId: string,
  message: string,
): AsyncGenerator<SSEEvent> {
  const response = await fetch(`/-/agent/${conversationId}/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });

  const reader = response.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop()!;

    let eventType: string | null = null;
    for (const line of lines) {
      if (line.startsWith("event: ")) {
        eventType = line.slice(7);
      } else if (line.startsWith("data: ") && eventType) {
        const data = JSON.parse(line.slice(6));

        if (eventType === "text_chunk") {
          yield { type: "text_chunk", content: data.content };
        } else if (eventType === "tool_call") {
          yield {
            type: "tool_call",
            name: data.name,
            arguments: data.arguments,
          };
        } else if (eventType === "tool_result") {
          yield { type: "tool_result", name: data.name, output: data.output };
        } else if (eventType === "done") {
          yield { type: "done" };
        } else if (eventType === "error") {
          yield { type: "error", message: data.message };
        }

        eventType = null;
      }
    }
  }
}

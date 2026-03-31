function appendMessage(role, content) {
  const messages = document.getElementById("messages");
  const div = document.createElement("div");
  div.className = "agent-message agent-message-" + role;
  const contentDiv = document.createElement("div");
  contentDiv.className = "agent-message-content";
  contentDiv.textContent = content;
  div.appendChild(contentDiv);
  messages.appendChild(div);
  messages.scrollTop = messages.scrollHeight;
  return contentDiv;
}

function appendToolCall(name, args) {
  const messages = document.getElementById("messages");
  const details = document.createElement("details");
  details.className = "agent-tool-call";
  const summary = document.createElement("summary");
  summary.textContent = "Tool: " + name;
  details.appendChild(summary);
  const pre = document.createElement("pre");
  pre.textContent = JSON.stringify(args, null, 2);
  details.appendChild(pre);
  messages.appendChild(details);
  messages.scrollTop = messages.scrollHeight;
}

function appendToolResult(name, output) {
  const messages = document.getElementById("messages");
  const details = document.createElement("details");
  details.className = "agent-tool-result";
  const summary = document.createElement("summary");
  summary.textContent = "Result: " + name;
  details.appendChild(summary);
  const pre = document.createElement("pre");
  pre.textContent = output;
  details.appendChild(pre);
  messages.appendChild(details);
  messages.scrollTop = messages.scrollHeight;
}

async function sendMessage(conversationId, message) {
  const sendBtn = document.getElementById("send-btn");
  const input = document.getElementById("message-input");
  sendBtn.disabled = true;
  input.disabled = true;

  // Show user message
  appendMessage("user", message);
  input.value = "";

  // Create assistant message placeholder
  let assistantContent = null;
  let currentText = "";

  try {
    const response = await fetch("/-/agent/" + conversationId + "/stream", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({message}),
    });

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const {done, value} = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, {stream: true});
      const lines = buffer.split("\n");
      buffer = lines.pop();

      let eventType = null;
      for (const line of lines) {
        if (line.startsWith("event: ")) {
          eventType = line.slice(7);
        } else if (line.startsWith("data: ") && eventType) {
          const data = JSON.parse(line.slice(6));

          if (eventType === "text_chunk") {
            if (!assistantContent) {
              assistantContent = appendMessage("assistant", "");
              currentText = "";
            }
            currentText += data.content;
            assistantContent.textContent = currentText;
            document.getElementById("messages").scrollTop = document.getElementById("messages").scrollHeight;
          } else if (eventType === "tool_call") {
            // Reset assistant content for text after tool calls
            assistantContent = null;
            currentText = "";
            appendToolCall(data.name, data.arguments);
          } else if (eventType === "tool_result") {
            appendToolResult(data.name, data.output);
          } else if (eventType === "error") {
            appendMessage("assistant", "Error: " + data.message);
          }

          eventType = null;
        }
      }
    }
  } catch (err) {
    appendMessage("assistant", "Connection error: " + err.message);
  } finally {
    sendBtn.disabled = false;
    input.disabled = false;
    input.focus();
  }
}

// Wire up form
document.getElementById("chat-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const input = document.getElementById("message-input");
  const message = input.value.trim();
  if (!message) return;
  const conversationId = document.querySelector(".agent-chat").dataset.conversationId;
  sendMessage(conversationId, message);
});

// Allow Ctrl+Enter / Cmd+Enter to send
document.getElementById("message-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
    e.preventDefault();
    document.getElementById("chat-form").dispatchEvent(new Event("submit"));
  }
});

import * as smd from "./smd.js";

function scrollToBottom() {
  const messages = document.getElementById("messages");
  messages.scrollTop = messages.scrollHeight;
}

function appendMessage(role, content) {
  const messages = document.getElementById("messages");
  const div = document.createElement("div");
  div.className = "agent-message agent-message-" + role;
  const contentDiv = document.createElement("div");
  contentDiv.className = "agent-message-content";
  if (role === "assistant" && content) {
    // Render existing content as markdown
    const renderer = smd.default_renderer(contentDiv);
    const parser = smd.parser(renderer);
    smd.parser_write(parser, content);
    smd.parser_end(parser);
  } else {
    contentDiv.textContent = content;
  }
  div.appendChild(contentDiv);
  messages.appendChild(div);
  scrollToBottom();
  return contentDiv;
}

function startAssistantMessage() {
  const messages = document.getElementById("messages");
  const div = document.createElement("div");
  div.className = "agent-message agent-message-assistant";
  const contentDiv = document.createElement("div");
  contentDiv.className = "agent-message-content";
  div.appendChild(contentDiv);
  messages.appendChild(div);

  const renderer = smd.default_renderer(contentDiv);
  const parser = smd.parser(renderer);
  return { contentDiv, parser };
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
  scrollToBottom();
}

function prettyPrintJson(text) {
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

function appendToolResult(name, output) {
  const messages = document.getElementById("messages");

  // Check for rich HTML content
  let parsed;
  try { parsed = JSON.parse(output); } catch {}
  if (parsed && parsed._html) {
    const container = document.createElement("div");
    container.className = "agent-rich-result";
    container.innerHTML = parsed._html;
    // Re-create script elements so they execute (innerHTML doesn't run scripts)
    container.querySelectorAll("script").forEach(oldScript => {
      const newScript = document.createElement("script");
      for (const attr of oldScript.attributes) {
        newScript.setAttribute(attr.name, attr.value);
      }
      newScript.textContent = oldScript.textContent;
      oldScript.replaceWith(newScript);
    });
    messages.appendChild(container);
  }

  const details = document.createElement("details");
  details.className = "agent-tool-result";
  const summary = document.createElement("summary");
  summary.textContent = "Result: " + name;
  details.appendChild(summary);
  const pre = document.createElement("pre");
  pre.textContent = prettyPrintJson(output);
  details.appendChild(pre);
  messages.appendChild(details);
  scrollToBottom();
}

async function sendMessage(conversationId, message) {
  const sendBtn = document.getElementById("send-btn");
  const input = document.getElementById("message-input");
  sendBtn.disabled = true;
  input.disabled = true;

  // Show user message
  appendMessage("user", message);
  input.value = "";

  let currentAssistant = null;

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
            if (!currentAssistant) {
              currentAssistant = startAssistantMessage();
            }
            smd.parser_write(currentAssistant.parser, data.content);
            scrollToBottom();
          } else if (eventType === "tool_call") {
            if (currentAssistant) {
              smd.parser_end(currentAssistant.parser);
              currentAssistant = null;
            }
            appendToolCall(data.name, data.arguments);
          } else if (eventType === "tool_result") {
            appendToolResult(data.name, data.output);
          } else if (eventType === "done") {
            if (currentAssistant) {
              smd.parser_end(currentAssistant.parser);
              currentAssistant = null;
            }
          } else if (eventType === "error") {
            if (currentAssistant) {
              smd.parser_end(currentAssistant.parser);
              currentAssistant = null;
            }
            appendMessage("assistant", "Error: " + data.message);
          }

          eventType = null;
        }
      }
    }

    // Ensure parser is ended if stream closes without done event
    if (currentAssistant) {
      smd.parser_end(currentAssistant.parser);
    }
  } catch (err) {
    if (currentAssistant) {
      smd.parser_end(currentAssistant.parser);
    }
    appendMessage("assistant", "Connection error: " + err.message);
  } finally {
    sendBtn.disabled = false;
    input.disabled = false;
    input.focus();
  }
}

// Make sendMessage available globally for the inline script
window.sendMessage = sendMessage;

// Render existing assistant messages as markdown on page load
document.querySelectorAll(".agent-message-assistant .agent-message-content").forEach(el => {
  const raw = el.textContent;
  if (!raw) return;
  el.textContent = "";
  const renderer = smd.default_renderer(el);
  const parser = smd.parser(renderer);
  smd.parser_write(parser, raw);
  smd.parser_end(parser);
});

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

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

function getOrCreateToolGroup() {
  const messages = document.getElementById("messages");
  const last = messages.lastElementChild;
  if (last && last.classList.contains("agent-tool-group")) {
    return last;
  }
  const group = document.createElement("div");
  group.className = "agent-tool-group";
  messages.appendChild(group);
  return group;
}

function appendToolCall(name, args) {
  const group = getOrCreateToolGroup();
  const details = document.createElement("details");
  details.className = "agent-tool-call pending";
  details.dataset.toolName = name;
  const summary = document.createElement("summary");
  summary.textContent = "Tool: " + name;
  details.appendChild(summary);
  const pre = document.createElement("pre");
  pre.textContent = JSON.stringify(args, null, 2);
  details.appendChild(pre);
  group.appendChild(details);
  scrollToBottom();
}

function getOrCreateTokenStream() {
  const messages = document.getElementById("messages");
  const existing = messages.querySelector(".agent-token-stream");
  if (existing) return existing;
  const box = document.createElement("div");
  box.className = "agent-token-stream";
  const pre = document.createElement("pre");
  box.appendChild(pre);
  messages.appendChild(box);
  return box;
}

function appendToTokenStream(box, text) {
  const pre = box.querySelector("pre");
  pre.textContent += text;
  // Keep only last ~3 lines worth of content
  const lines = pre.textContent.split("\n");
  if (lines.length > 3) {
    pre.textContent = lines.slice(-3).join("\n");
  }
}

function removeTokenStream() {
  const el = document.querySelector(".agent-token-stream");
  if (el) el.remove();
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

  // Remove pending indicator from the matching tool call
  const pendingCalls = messages.querySelectorAll('.agent-tool-call.pending[data-tool-name="' + name + '"]');
  if (pendingCalls.length > 0) {
    pendingCalls[pendingCalls.length - 1].classList.remove("pending");
  }

  // Check for rich HTML content
  let parsed;
  try { parsed = JSON.parse(output); } catch {}
  if (parsed && parsed._html) {
    const container = document.createElement("div");
    container.className = "agent-rich-result";
    container.innerHTML = parsed._html;
    // Append first so scripts can find sibling elements (e.g. iframes) in the DOM
    messages.appendChild(container);
    // Re-create script elements so they execute (innerHTML doesn't run scripts)
    container.querySelectorAll("script").forEach(oldScript => {
      const newScript = document.createElement("script");
      for (const attr of oldScript.attributes) {
        newScript.setAttribute(attr.name, attr.value);
      }
      newScript.textContent = oldScript.textContent;
      oldScript.replaceWith(newScript);
    });
    // Scroll again after the browser has laid out the iframe
    requestAnimationFrame(() => scrollToBottom());
  }

  const group = getOrCreateToolGroup();
  const details = document.createElement("details");
  details.className = "agent-tool-result";
  const summary = document.createElement("summary");
  summary.textContent = "Result: " + name;
  details.appendChild(summary);
  const pre = document.createElement("pre");
  pre.textContent = prettyPrintJson(output);
  details.appendChild(pre);
  group.appendChild(details);
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

    // Thinking indicator — shown while waiting for the LLM
    const messages = document.getElementById("messages");
    const thinkingEl = document.createElement("div");
    thinkingEl.className = "agent-thinking";
    thinkingEl.textContent = "Thinking\u2026";
    messages.appendChild(thinkingEl);
    scrollToBottom();
    let streamDone = false;

    function updateThinking() {
      // Show thinking indicator if the stream is still active and
      // we're not currently streaming text or tool call args
      const hasTokenStream = !!document.querySelector(".agent-token-stream");
      if (streamDone || currentAssistant || hasTokenStream) {
        if (thinkingEl.parentNode) thinkingEl.remove();
      } else {
        if (!thinkingEl.parentNode) {
          messages.appendChild(thinkingEl);
          scrollToBottom();
        }
      }
    }

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
          } else if (eventType === "tool_call_args_chunk") {
            if (thinkingEl.parentNode) thinkingEl.remove();
            const tokenBox = getOrCreateTokenStream();
            appendToTokenStream(tokenBox, data.content);
            scrollToBottom();
          } else if (eventType === "tool_call") {
            removeTokenStream();
            if (currentAssistant) {
              smd.parser_end(currentAssistant.parser);
              currentAssistant = null;
            }
            appendToolCall(data.name, data.arguments);
          } else if (eventType === "tool_result") {
            appendToolResult(data.name, data.output);
          } else if (eventType === "done") {
            streamDone = true;
            if (currentAssistant) {
              smd.parser_end(currentAssistant.parser);
              currentAssistant = null;
            }
          } else if (eventType === "error") {
            streamDone = true;
            if (currentAssistant) {
              smd.parser_end(currentAssistant.parser);
              currentAssistant = null;
            }
            appendMessage("assistant", "Error: " + data.message);
          }

          eventType = null;
        }
      }

      // Update thinking indicator after processing all events in this chunk
      updateThinking();
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
    const input = document.getElementById("message-input");
    const message = input.value.trim();
    if (!message) return;
    const conversationId = document.querySelector(".agent-chat").dataset.conversationId;
    sendMessage(conversationId, message);
  }
});

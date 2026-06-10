import * as smd from "./smd.js";

// Auto-follow: only programmatically scroll to bottom while the user is
// already pinned there. If they scroll up to read earlier content, stop
// yanking them back; resume once they scroll back down.
let autoFollow = true;
const FOLLOW_THRESHOLD_PX = 50;
const assistantCopyText = new WeakMap();

function createCopyIconSvg(size = 12) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("width", size);
  svg.setAttribute("height", size);
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");

  const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
  rect.setAttribute("x", "9");
  rect.setAttribute("y", "9");
  rect.setAttribute("width", "13");
  rect.setAttribute("height", "13");
  rect.setAttribute("rx", "2");
  svg.appendChild(rect);

  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute(
    "d",
    "M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"
  );
  svg.appendChild(path);

  return svg;
}

function addAssistantCopyButton(messageEl) {
  if (messageEl.querySelector(".agent-response-copy")) return;
  const button = document.createElement("button");
  button.type = "button";
  button.className = "agent-response-copy";
  button.setAttribute("aria-label", "Copy response");
  const copiedLabel = document.createElement("span");
  copiedLabel.className = "agent-response-copy-label";
  copiedLabel.textContent = "Copied";
  button.appendChild(copiedLabel);
  button.appendChild(createCopyIconSvg());
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(assistantCopyText.get(messageEl) || "");
      button.classList.add("copied");
      setTimeout(() => button.classList.remove("copied"), 1350);
    } catch (err) {
      console.error("Copy response failed:", err);
    }
  });
  messageEl.appendChild(button);
}

function setAssistantCopyText(messageEl, text) {
  assistantCopyText.set(messageEl, text || "");
  addAssistantCopyButton(messageEl);
}

(function initScrollFollow() {
  const messages = document.getElementById("messages");
  if (!messages) return;
  messages.addEventListener("scroll", () => {
    const distance =
      messages.scrollHeight - messages.scrollTop - messages.clientHeight;
    autoFollow = distance < FOLLOW_THRESHOLD_PX;
  });
})();

function scrollToBottom() {
  const messages = document.getElementById("messages");
  messages.scrollTop = messages.scrollHeight;
}

function scrollToBottomIfFollowing() {
  if (autoFollow) scrollToBottom();
}

function appendMessage(role, content) {
  const messages = document.getElementById("messages");
  const div = document.createElement("div");
  div.className = "agent-message agent-message-" + role;
  const contentDiv = document.createElement("div");
  contentDiv.className = "agent-message-content";
  div.appendChild(contentDiv);
  if (role === "assistant" && content) {
    // Render existing content as markdown
    const renderer = smd.default_renderer(contentDiv);
    const parser = smd.parser(renderer);
    smd.parser_write(parser, content);
    smd.parser_end(parser);
    setAssistantCopyText(div, content);
  } else {
    contentDiv.textContent = content;
  }
  messages.appendChild(div);
  scrollToBottom();
  return contentDiv;
}

// Reasoning preview: keep the summary to a single-line rolling tail of the
// most recent reasoning text — strips newlines and collapses whitespace.
const REASONING_PREVIEW_CHARS = 80;
const TOOL_ARGUMENT_PREVIEW_CHARS = 12000;

function reasoningPreview(text) {
  const cleaned = text.replace(/\s+/g, " ").trim();
  if (cleaned.length <= REASONING_PREVIEW_CHARS) return cleaned;
  return "…" + cleaned.slice(-REASONING_PREVIEW_CHARS);
}

function startReasoningBlock() {
  const messages = document.getElementById("messages");
  const details = document.createElement("details");
  details.className = "agent-reasoning streaming";
  details.open = true;
  const summary = document.createElement("summary");
  summary.textContent = "Thinking…";
  details.appendChild(summary);
  const contentDiv = document.createElement("div");
  contentDiv.className = "agent-reasoning-content";
  details.appendChild(contentDiv);
  messages.appendChild(details);
  const renderer = smd.default_renderer(contentDiv);
  const parser = smd.parser(renderer);
  // expectedOpen tracks the open state we set programmatically. A toggle
  // event where details.open !== expectedOpen came from the user clicking
  // the summary, so we should stop auto-collapsing on done.
  const state = {
    details,
    summary,
    contentDiv,
    parser,
    rawText: "",
    userToggled: false,
    expectedOpen: true,
  };
  details.addEventListener("toggle", () => {
    if (details.open !== state.expectedOpen) {
      state.userToggled = true;
    }
  });
  return state;
}

function appendReasoningChunk(state, chunk) {
  state.rawText += chunk;
  smd.parser_write(state.parser, chunk);
  // Only update the rolling preview while the details is collapsed —
  // if it's open, they're reading the full content and shouldn't see
  // the summary mutate under them.
  if (!state.details.open) {
    state.summary.textContent = "Thinking: " + reasoningPreview(state.rawText);
  }
}

function endReasoningBlock(state) {
  if (!state) return;
  smd.parser_end(state.parser);
  state.details.classList.remove("streaming");
  // Auto-collapse once reasoning is done, unless the user explicitly
  // toggled it during the stream — in which case respect their choice.
  if (!state.userToggled) {
    state.expectedOpen = false;
    state.details.open = false;
  }
  if (!state.details.open) {
    state.summary.textContent =
      "Thinking: " + reasoningPreview(state.rawText);
  }
}

function startAssistantMessage() {
  const messages = document.getElementById("messages");
  const div = document.createElement("div");
  div.className = "agent-message agent-message-assistant";
  const contentDiv = document.createElement("div");
  contentDiv.className = "agent-message-content";
  div.appendChild(contentDiv);
  setAssistantCopyText(div, "");
  messages.appendChild(div);

  const renderer = smd.default_renderer(contentDiv);
  const parser = smd.parser(renderer);
  return { messageEl: div, contentDiv, parser, rawText: "" };
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

function truncateMiddle(text, maxChars) {
  if (!text || text.length <= maxChars) return text || "";
  const marker = "\n\n... truncated ...\n\n";
  const keep = Math.max(0, maxChars - marker.length);
  const start = Math.ceil(keep / 2);
  const end = Math.floor(keep / 2);
  return text.slice(0, start) + marker + text.slice(text.length - end);
}

function formatToolArguments(args) {
  const text = typeof args === "string" ? args : JSON.stringify(args, null, 2);
  return truncateMiddle(text, TOOL_ARGUMENT_PREVIEW_CHARS);
}

function startToolCall(name, id, streaming = false) {
  const group = getOrCreateToolGroup();
  const details = document.createElement("details");
  details.className = "agent-tool-call pending" + (streaming ? " streaming" : "");
  details.dataset.toolName = name;
  if (id) details.dataset.toolCallId = id;
  if (streaming) details.open = true;
  const summary = document.createElement("summary");
  summary.textContent = "Tool: " + name + (streaming ? " - receiving arguments..." : "");
  details.appendChild(summary);
  const pre = document.createElement("pre");
  details.appendChild(pre);
  group.appendChild(details);
  scrollToBottomIfFollowing();
  const state = {
    details,
    summary,
    pre,
    name,
    rawArgs: "",
    userToggled: false,
    expectedOpen: details.open,
  };
  details.addEventListener("toggle", () => {
    if (details.open !== state.expectedOpen) {
      state.userToggled = true;
    }
  });
  return state;
}

function appendToolCall(name, args, id = null) {
  const state = startToolCall(name, id, false);
  state.pre.textContent = formatToolArguments(args);
  return state;
}

function appendToolCallArgsChunk(state, chunk) {
  state.rawArgs += chunk;
  state.pre.textContent = formatToolArguments(state.rawArgs);
  state.summary.textContent =
    "Tool: " + state.name + " - receiving arguments (" + state.rawArgs.length.toLocaleString() + " chars)";
}

function finishToolCall(state, name, args) {
  if (!state) return;
  state.name = name || state.name;
  state.details.dataset.toolName = state.name;
  state.details.classList.remove("streaming");
  state.details.classList.add("pending");
  state.pre.textContent = formatToolArguments(args);
  state.summary.textContent = "Tool: " + state.name;
  if (!state.userToggled) {
    state.expectedOpen = false;
    state.details.open = false;
  }
}

function prettyPrintJson(text) {
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

function createSqlEditLink(url) {
  const editLink = document.createElement("p");
  editLink.className = "agent-sql-edit-link";
  const link = document.createElement("a");
  link.href = url;
  link.target = "_blank";
  link.rel = "noopener";
  link.textContent = "View SQL query";
  editLink.appendChild(link);
  return editLink;
}

function appendToolResult(name, output, id = null) {
  const messages = document.getElementById("messages");

  // Remove pending indicator from the matching tool call
  let pendingCalls = [];
  if (id) {
    pendingCalls = Array.from(messages.querySelectorAll(".agent-tool-call.pending"))
      .filter(el => el.dataset.toolCallId === id);
  }
  if (pendingCalls.length === 0) {
    pendingCalls = messages.querySelectorAll('.agent-tool-call.pending[data-tool-name="' + name + '"]');
  }
  if (pendingCalls.length > 0) {
    pendingCalls[pendingCalls.length - 1].classList.remove("pending");
  }

  // Check for rich HTML content
  let parsed;
  try { parsed = JSON.parse(output); } catch {}
  if (parsed && parsed._html) {
    const container = document.createElement("div");
    container.className = "agent-rich-result";
    container.insertAdjacentHTML("beforeend", parsed._html);
    if (parsed._edit_sql_url) {
      container.appendChild(createSqlEditLink(parsed._edit_sql_url));
    }
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
    requestAnimationFrame(() => scrollToBottomIfFollowing());
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
  if (parsed && parsed._edit_sql_url && !parsed._html) {
    details.appendChild(createSqlEditLink(parsed._edit_sql_url));
  }
  group.appendChild(details);
  scrollToBottomIfFollowing();
}

// True while an ask_user() question is awaiting an answer — keeps the
// chat input disabled across the suspended turn.
let questionPending = false;

function setInputEnabled(enabled) {
  const sendBtn = document.getElementById("send-btn");
  const input = document.getElementById("message-input");
  if (sendBtn) sendBtn.disabled = !enabled;
  if (input) input.disabled = !enabled;
  if (enabled && input) input.focus();
}

async function sendMessage(conversationId, message) {
  appendMessage("user", message);
  const input = document.getElementById("message-input");
  if (input) input.value = "";
  await streamAgentEvents("/-/agent/" + conversationId + "/stream", {message});
}

async function answerQuestion(conversationId, questionId, answer) {
  questionPending = false;
  await streamAgentEvents(
    "/-/agent/" + conversationId + "/question/" + questionId,
    {answer}
  );
}

function renderQuestionForm(question) {
  const messages = document.getElementById("messages");
  const container = document.createElement("div");
  container.className = "agent-question";
  container.dataset.questionId = question.id;

  if (question.html) {
    const htmlEl = document.createElement("div");
    htmlEl.className = "agent-question-html";
    htmlEl.insertAdjacentHTML("beforeend", question.html);
    container.appendChild(htmlEl);
  }

  const promptEl = document.createElement("p");
  promptEl.className = "agent-question-prompt";
  promptEl.textContent = question.prompt;
  container.appendChild(promptEl);

  const toolEl = document.createElement("p");
  toolEl.className = "agent-question-tool";
  toolEl.textContent = "Asked by tool: " + question.tool_name;
  container.appendChild(toolEl);

  const controls = document.createElement("div");
  controls.className = "agent-question-controls";
  container.appendChild(controls);

  function submit(answer) {
    container.classList.add("answered");
    container.querySelectorAll("button, input, textarea").forEach(el => {
      el.disabled = true;
    });
    const conversationId =
      document.querySelector(".agent-chat").dataset.conversationId;
    answerQuestion(conversationId, question.id, answer);
  }

  if (question.question_type === "boolean") {
    const yes = document.createElement("button");
    yes.type = "button";
    yes.textContent = "Yes";
    yes.addEventListener("click", () => submit(true));
    const no = document.createElement("button");
    no.type = "button";
    no.className = "agent-question-no";
    no.textContent = "No";
    no.addEventListener("click", () => submit(false));
    controls.appendChild(yes);
    controls.appendChild(no);
  } else if (question.question_type === "choice") {
    const form = document.createElement("form");
    (question.options || []).forEach((option, i) => {
      const label = document.createElement("label");
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = "agent-question-" + question.id;
      radio.value = option;
      if (i === 0) radio.checked = true;
      label.appendChild(radio);
      label.appendChild(document.createTextNode(" " + option));
      form.appendChild(label);
    });
    const submitBtn = document.createElement("button");
    submitBtn.type = "submit";
    submitBtn.textContent = "Answer";
    form.appendChild(submitBtn);
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const selected = form.querySelector("input:checked");
      if (selected) submit(selected.value);
    });
    controls.appendChild(form);
  } else {
    const form = document.createElement("form");
    const textarea = document.createElement("textarea");
    textarea.rows = 3;
    form.appendChild(textarea);
    const submitBtn = document.createElement("button");
    submitBtn.type = "submit";
    submitBtn.textContent = "Answer";
    form.appendChild(submitBtn);
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      submit(textarea.value);
    });
    controls.appendChild(form);
    textarea.focus();
  }

  messages.appendChild(container);
  scrollToBottom();
}

async function streamAgentEvents(url, payload) {
  setInputEnabled(false);

  let currentAssistant = null;
  let currentReasoning = null;
  const streamingToolCalls = new Map();
  let hasToolActivity = false;
  let streamDone = false;

  function closeReasoning() {
    if (currentReasoning) {
      endReasoningBlock(currentReasoning);
      currentReasoning = null;
    }
  }

  function stopPendingToolCalls() {
    document.querySelectorAll(".agent-tool-call.pending").forEach(el => {
      el.classList.remove("pending");
      el.classList.remove("streaming");
    });
  }

  try {
    const response = await fetch(url, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
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
    scrollToBottomIfFollowing();

    function updateThinking() {
      // Show the "Thinking…" placeholder only while we're waiting for
      // the first signal — any text, reasoning, or tool activity from
      // the model supersedes it.
      const hasActivity =
        streamDone || currentAssistant || currentReasoning || hasToolActivity;
      if (hasActivity) {
        if (thinkingEl.parentNode) thinkingEl.remove();
      } else {
        if (!thinkingEl.parentNode) {
          messages.appendChild(thinkingEl);
          scrollToBottomIfFollowing();
        }
      }
    }

    let eventType = null;
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, {stream: true});
      const lines = buffer.split("\n");
      buffer = lines.pop();

      for (const line of lines) {
        if (line.startsWith("event: ")) {
          eventType = line.slice(7);
        } else if (line.startsWith("data: ") && eventType) {
          const data = JSON.parse(line.slice(6));

          if (eventType === "reasoning_chunk") {
            if (!data.content) {
              eventType = null;
              continue;
            }
            if (!currentReasoning) {
              currentReasoning = startReasoningBlock();
            }
            appendReasoningChunk(currentReasoning, data.content);
            scrollToBottomIfFollowing();
          } else if (eventType === "text_chunk") {
            closeReasoning();
            if (!currentAssistant) {
              currentAssistant = startAssistantMessage();
            }
            currentAssistant.rawText += data.content;
            setAssistantCopyText(currentAssistant.messageEl, currentAssistant.rawText);
            smd.parser_write(currentAssistant.parser, data.content);
            scrollToBottomIfFollowing();
          } else if (eventType === "tool_call_start") {
            closeReasoning();
            if (currentAssistant) {
              smd.parser_end(currentAssistant.parser);
              currentAssistant = null;
            }
            hasToolActivity = true;
            let state = data.id ? streamingToolCalls.get(data.id) : null;
            if (state) {
              state.name = data.name || state.name;
              state.details.dataset.toolName = state.name;
              state.summary.textContent = "Tool: " + state.name + " - receiving arguments...";
            } else {
              state = startToolCall(data.name || "tool", data.id, true);
              if (data.id) streamingToolCalls.set(data.id, state);
            }
          } else if (eventType === "tool_call_args_chunk") {
            hasToolActivity = true;
            let state = data.id ? streamingToolCalls.get(data.id) : null;
            if (!state) {
              state = startToolCall("tool", data.id, true);
              if (data.id) streamingToolCalls.set(data.id, state);
            }
            appendToolCallArgsChunk(state, data.chunk || "");
            scrollToBottomIfFollowing();
          } else if (eventType === "tool_call") {
            closeReasoning();
            if (currentAssistant) {
              smd.parser_end(currentAssistant.parser);
              currentAssistant = null;
            }
            hasToolActivity = true;
            const state = data.id ? streamingToolCalls.get(data.id) : null;
            if (state) {
              finishToolCall(state, data.name, data.arguments);
              streamingToolCalls.delete(data.id);
            } else {
              appendToolCall(data.name, data.arguments, data.id);
            }
          } else if (eventType === "tool_result") {
            hasToolActivity = true;
            appendToolResult(data.name, data.output, data.id);
          } else if (eventType === "question") {
            closeReasoning();
            stopPendingToolCalls();
            if (currentAssistant) {
              smd.parser_end(currentAssistant.parser);
              currentAssistant = null;
            }
            questionPending = true;
            renderQuestionForm(data);
          } else if (eventType === "done") {
            streamDone = true;
            closeReasoning();
            stopPendingToolCalls();
            if (currentAssistant) {
              smd.parser_end(currentAssistant.parser);
              currentAssistant = null;
            }
          } else if (eventType === "error") {
            streamDone = true;
            closeReasoning();
            stopPendingToolCalls();
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

    // Ensure parsers are ended if stream closes without done event
    streamDone = true;
    stopPendingToolCalls();
    updateThinking();
    closeReasoning();
    if (currentAssistant) {
      smd.parser_end(currentAssistant.parser);
    }
  } catch (err) {
    streamDone = true;
    closeReasoning();
    stopPendingToolCalls();
    document.querySelectorAll(".agent-thinking").forEach(el => el.remove());
    if (currentAssistant) {
      smd.parser_end(currentAssistant.parser);
    }
    appendMessage("assistant", "Connection error: " + err.message);
  } finally {
    if (!questionPending) {
      setInputEnabled(true);
    }
  }
}

// Make sendMessage available globally for the inline script
window.sendMessage = sendMessage;

// Render existing assistant messages as markdown on page load
document.querySelectorAll(".agent-message-assistant .agent-message-content").forEach(el => {
  const raw = el.textContent;
  if (!raw) return;
  const messageEl = el.closest(".agent-message-assistant");
  if (messageEl) setAssistantCopyText(messageEl, raw);
  el.textContent = "";
  const renderer = smd.default_renderer(el);
  const parser = smd.parser(renderer);
  smd.parser_write(parser, raw);
  smd.parser_end(parser);
});

// Render existing reasoning blocks as markdown + set the rolling summary preview
document.querySelectorAll(".agent-reasoning").forEach(details => {
  const contentEl = details.querySelector(".agent-reasoning-content");
  if (!contentEl) return;
  const raw = contentEl.textContent;
  if (!raw) return;
  contentEl.textContent = "";
  const renderer = smd.default_renderer(contentEl);
  const parser = smd.parser(renderer);
  smd.parser_write(parser, raw);
  smd.parser_end(parser);
  const summary = details.querySelector("summary");
  if (summary) {
    summary.textContent = "Thinking: " + reasoningPreview(raw);
  }
});

// Render a pending ask_user() question left over from a suspended turn
// (page reload, or the server restarted while the question was open).
(function initPendingQuestion() {
  const dataEl = document.getElementById("pending-question-data");
  if (!dataEl) return;
  try {
    const question = JSON.parse(dataEl.textContent);
    questionPending = true;
    setInputEnabled(false);
    renderQuestionForm(question);
  } catch (err) {
    console.error("Failed to render pending question:", err);
  }
})();

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

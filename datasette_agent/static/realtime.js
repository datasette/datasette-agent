// Realtime voice agent using OpenAI Realtime API over WebRTC
// Audio input only, text output + tool calls (no audio output)

let pc = null; // RTCPeerConnection
let dc = null; // DataChannel
let localStream = null;

const micBtn = document.getElementById("mic-btn");
const micLabel = document.getElementById("mic-label");
const statusText = document.getElementById("status-text");
const canvasContent = document.getElementById("canvas-content");
const transcriptContent = document.getElementById("transcript-content");
const historyContent = document.getElementById("history-content");

// Track pending function calls (arguments arrive in deltas)
const pendingFunctionCalls = {};

// ---- Session lifecycle ----

micBtn.addEventListener("click", async () => {
  if (pc) {
    stopSession();
  } else {
    await startSession();
  }
});

async function startSession() {
  setStatus("Requesting session token...");
  micBtn.disabled = true;

  try {
    // 1. Get ephemeral token from our server
    const tokenResp = await fetch("/-/agent-realtime/token", { method: "POST" });
    if (!tokenResp.ok) {
      const err = await tokenResp.json();
      throw new Error(err.error || "Failed to get token");
    }
    const tokenData = await tokenResp.json();
    const ephemeralKey = tokenData.client_secret?.value;
    if (!ephemeralKey) {
      throw new Error("No client_secret in token response");
    }

    setStatus("Connecting...");

    // 2. Create peer connection
    pc = new RTCPeerConnection();

    // We don't need audio output (text-only mode), but set up the handler
    // in case the API sends any track events
    pc.ontrack = () => {};

    // 3. Get microphone
    localStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    localStream.getTracks().forEach((track) => pc.addTrack(track, localStream));

    // 4. Create data channel
    dc = pc.createDataChannel("oai-events");
    dc.addEventListener("open", onDataChannelOpen);
    dc.addEventListener("message", onDataChannelMessage);
    dc.addEventListener("close", () => {
      setStatus("Disconnected");
      resetToMicButton();
    });

    // 5. SDP exchange
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);

    const sdpResp = await fetch("https://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview", {
      method: "POST",
      body: offer.sdp,
      headers: {
        Authorization: `Bearer ${ephemeralKey}`,
        "Content-Type": "application/sdp",
      },
    });

    if (!sdpResp.ok) {
      throw new Error(`WebRTC SDP exchange failed: ${sdpResp.status}`);
    }

    const answer = { type: "answer", sdp: await sdpResp.text() };
    await pc.setRemoteDescription(answer);

    micBtn.classList.add("active");
    micLabel.textContent = "Click to stop";
    micBtn.disabled = false;
    setStatus("Listening...");
  } catch (err) {
    console.error("Failed to start session:", err);
    setStatus(`Error: ${err.message}`);
    micBtn.disabled = false;
    stopSession();
  }
}

function stopSession() {
  if (localStream) {
    localStream.getTracks().forEach((t) => t.stop());
    localStream = null;
  }
  if (dc) {
    dc.close();
    dc = null;
  }
  if (pc) {
    pc.close();
    pc = null;
  }
  resetToMicButton();
  setStatus("Ready");
}

function resetToMicButton() {
  micBtn.classList.remove("active");
  micLabel.textContent = "Click to start";
  micBtn.disabled = false;
}

// ---- Data channel events ----

function onDataChannelOpen() {
  setStatus("Connected - listening...");
}

function onDataChannelMessage(ev) {
  let event;
  try {
    event = JSON.parse(ev.data);
  } catch {
    return;
  }

  switch (event.type) {
    case "session.created":
    case "session.updated":
      // Session is configured
      break;

    case "conversation.item.input_audio_transcription.completed":
      // User speech transcript
      addTranscriptEntry("user", event.transcript || "(inaudible)");
      break;

    case "response.text.delta":
      // Accumulate assistant text
      appendAssistantText(event.delta || "");
      break;

    case "response.text.done":
      finalizeAssistantText(event.text || "");
      break;

    case "response.function_call_arguments.delta":
      // Accumulate function call arguments
      if (!pendingFunctionCalls[event.call_id]) {
        pendingFunctionCalls[event.call_id] = {
          name: event.name || "",
          arguments: "",
          call_id: event.call_id,
        };
      }
      pendingFunctionCalls[event.call_id].arguments += event.delta || "";
      // Capture the name if it arrives here
      if (event.name) {
        pendingFunctionCalls[event.call_id].name = event.name;
      }
      break;

    case "response.function_call_arguments.done":
      handleFunctionCall(event);
      break;

    case "response.done":
      // Response complete
      break;

    case "input_audio_buffer.speech_started":
      setStatus("Hearing you...");
      break;

    case "input_audio_buffer.speech_stopped":
      setStatus("Processing...");
      break;

    case "error":
      console.error("Realtime API error:", event.error);
      setStatus(`Error: ${event.error?.message || "Unknown error"}`);
      break;

    default:
      // Ignore other events
      break;
  }
}

// ---- Function call handling ----

async function handleFunctionCall(event) {
  const callId = event.call_id;
  const name = event.name || pendingFunctionCalls[callId]?.name || "unknown";
  const argsStr = event.arguments || pendingFunctionCalls[callId]?.arguments || "{}";

  // Clean up pending state
  delete pendingFunctionCalls[callId];

  let args;
  try {
    args = JSON.parse(argsStr);
  } catch {
    args = {};
  }

  // Show loading in canvas
  showToolLoading(name);

  // Log to history panel
  const historyEntry = addHistoryEntry(name, args, "pending");

  setStatus(`Running tool: ${name}...`);

  try {
    // Execute tool server-side
    const resp = await fetch("/-/agent-realtime/tool-call", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, arguments: args }),
    });
    const data = await resp.json();
    const output = data.output || "{}";

    // Update history
    updateHistoryEntry(historyEntry, "done");

    // Render result in canvas
    renderToolResult(name, output, args);

    // Send result back to Realtime API
    dc.send(
      JSON.stringify({
        type: "conversation.item.create",
        item: {
          type: "function_call_output",
          call_id: callId,
          output: output,
        },
      })
    );
    dc.send(JSON.stringify({ type: "response.create" }));

    setStatus("Listening...");
  } catch (err) {
    console.error("Tool call failed:", err);
    updateHistoryEntry(historyEntry, "error");
    setStatus(`Tool error: ${err.message}`);

    // Send error back so the model knows
    dc.send(
      JSON.stringify({
        type: "conversation.item.create",
        item: {
          type: "function_call_output",
          call_id: callId,
          output: JSON.stringify({ error: err.message }),
        },
      })
    );
    dc.send(JSON.stringify({ type: "response.create" }));
  }
}

// ---- Canvas rendering ----

function showToolLoading(toolName) {
  canvasContent.innerHTML = `
    <div class="tool-loading">
      <div class="tool-loading-spinner"></div>
      <div class="tool-loading-text">Running ${escapeHtml(toolName)}...</div>
    </div>
  `;
  canvasContent.classList.add("has-result");
}

function renderToolResult(toolName, output, args) {
  let parsed;
  try {
    parsed = JSON.parse(output);
  } catch {
    parsed = null;
  }

  // Special case: sql_query tool - render as table
  if (toolName === "sql_query" && parsed && parsed.columns && parsed.rows) {
    renderSqlResult(parsed, args);
    return;
  }

  // Tools returning _html
  if (parsed && parsed._html) {
    renderHtmlResult(parsed._html);
    return;
  }

  // Fallback: show the mic button again (tool had no visual output)
  // The model will interpret the result and respond with text
  resetCanvas();
}

function renderSqlResult(data, args) {
  const { columns, rows, truncated } = data;
  const sql = args?.sql || "";

  let html = '<div class="realtime-sql-result">';
  html += "<table><thead><tr>";
  for (const col of columns) {
    html += `<th>${escapeHtml(col)}</th>`;
  }
  html += "</tr></thead><tbody>";
  for (const row of rows) {
    html += "<tr>";
    for (const col of columns) {
      const val = row[col];
      html += `<td>${val === null ? "<em>null</em>" : escapeHtml(String(val))}</td>`;
    }
    html += "</tr>";
  }
  html += "</tbody></table>";

  if (truncated) {
    html += '<div style="color: #fbbf24; margin-top: 0.5rem; font-size: 0.8rem;">Results truncated</div>';
  }

  if (sql) {
    html += `<div class="sql-display">${escapeHtml(sql)}</div>`;
  }

  html += "</div>";

  canvasContent.innerHTML = html;
  canvasContent.classList.add("has-result");
}

function renderHtmlResult(htmlStr) {
  canvasContent.innerHTML = "";
  canvasContent.classList.add("has-result");

  const container = document.createElement("div");
  container.className = "realtime-rich-result";
  container.innerHTML = htmlStr;

  // Re-create script elements so they execute
  container.querySelectorAll("script").forEach((oldScript) => {
    const newScript = document.createElement("script");
    for (const attr of oldScript.attributes) {
      newScript.setAttribute(attr.name, attr.value);
    }
    newScript.textContent = oldScript.textContent;
    oldScript.replaceWith(newScript);
  });

  canvasContent.appendChild(container);
}

function resetCanvas() {
  canvasContent.innerHTML = "";
  canvasContent.classList.remove("has-result");
  // Put mic button back
  canvasContent.appendChild(micBtn);
}

// ---- Transcript panel ----

let currentAssistantEntry = null;

function addTranscriptEntry(role, text) {
  const entry = document.createElement("div");
  entry.className = `transcript-entry ${role}`;
  const label = document.createElement("div");
  label.className = "transcript-label";
  label.textContent = role === "user" ? "You" : "Assistant";
  entry.appendChild(label);
  const body = document.createElement("div");
  body.textContent = text;
  entry.appendChild(body);
  transcriptContent.appendChild(entry);
  transcriptContent.scrollTop = transcriptContent.scrollHeight;
  return entry;
}

function appendAssistantText(delta) {
  if (!currentAssistantEntry) {
    currentAssistantEntry = addTranscriptEntry("assistant", "");
  }
  const body = currentAssistantEntry.querySelector("div:last-child");
  body.textContent += delta;
  transcriptContent.scrollTop = transcriptContent.scrollHeight;
}

function finalizeAssistantText(fullText) {
  if (currentAssistantEntry) {
    const body = currentAssistantEntry.querySelector("div:last-child");
    body.textContent = fullText;
  }
  currentAssistantEntry = null;
}

// ---- History panel ----

function addHistoryEntry(toolName, args, status) {
  const entry = document.createElement("div");
  entry.className = "history-entry";

  const nameEl = document.createElement("div");
  nameEl.className = "history-tool-name";
  nameEl.textContent = toolName;
  entry.appendChild(nameEl);

  const argsEl = document.createElement("div");
  argsEl.className = "history-tool-args";
  argsEl.textContent = JSON.stringify(args, null, 2);
  entry.appendChild(argsEl);

  const statusEl = document.createElement("div");
  statusEl.className = `history-tool-status ${status}`;
  statusEl.textContent = status === "pending" ? "Running..." : "Done";
  entry.appendChild(statusEl);

  historyContent.appendChild(entry);
  historyContent.scrollTop = historyContent.scrollHeight;
  return entry;
}

function updateHistoryEntry(entry, status) {
  const statusEl = entry.querySelector(".history-tool-status");
  if (statusEl) {
    statusEl.className = `history-tool-status ${status}`;
    statusEl.textContent = status === "done" ? "Done" : status === "error" ? "Error" : "Running...";
  }
}

// ---- Side panel toggles ----

document.getElementById("toggle-transcript").addEventListener("click", () => {
  togglePanel("transcript-panel", "toggle-transcript");
});

document.getElementById("toggle-history").addEventListener("click", () => {
  togglePanel("history-panel", "toggle-history");
});

document.querySelectorAll(".close-panel").forEach((btn) => {
  btn.addEventListener("click", () => {
    const panelId = btn.dataset.panel;
    document.getElementById(panelId).classList.add("hidden");
    // Deactivate corresponding toggle button
    if (panelId === "transcript-panel") {
      document.getElementById("toggle-transcript").classList.remove("active");
    } else if (panelId === "history-panel") {
      document.getElementById("toggle-history").classList.remove("active");
    }
  });
});

function togglePanel(panelId, toggleId) {
  const panel = document.getElementById(panelId);
  const toggle = document.getElementById(toggleId);
  const isHidden = panel.classList.contains("hidden");

  // Close other panels
  document.querySelectorAll(".side-panel").forEach((p) => p.classList.add("hidden"));
  document.querySelectorAll(".panel-toggle").forEach((t) => t.classList.remove("active"));

  if (isHidden) {
    panel.classList.remove("hidden");
    toggle.classList.add("active");
  }
}

// ---- Utilities ----

function setStatus(text) {
  statusText.textContent = text;
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

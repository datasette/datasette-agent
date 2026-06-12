(function () {
  let registered = false;

  function register(manager) {
    if (registered || !manager || typeof manager.registerPlugin !== "function") {
      return;
    }
    registered = true;
    const config = window.datasetteAgentJumpConfig || {};
    const createConversationUrl = config.createConversationUrl;
    const conversationBaseUrl = config.conversationBaseUrl;
    const pluginVersion = config.pluginVersion;
    manager.registerPlugin("datasette-agent", {
      version: pluginVersion,
      makeJumpSections() {
        return [
          {
            id: "datasette-agent-chat",
            render(node) {
              node.innerHTML = `
                <style>
                  .datasette-agent-jump-start {
                    display: flex;
                    flex-direction: column;
                    gap: 0.5rem;
                  }
                  .datasette-agent-jump-start label {
                    color: #111827;
                    font-size: 0.875rem;
                    font-weight: 600;
                  }
                  .datasette-agent-jump-start textarea {
                    border: 1px solid #d1d5db;
                    border-radius: 0.5rem;
                    box-sizing: border-box;
                    color: #111827;
                    font: inherit;
                    line-height: 1.4;
                    min-height: 4.75rem;
                    overflow-x: hidden;
                    padding: 0.625rem 0.75rem;
                    resize: vertical;
                    width: 100%;
                  }
                  .datasette-agent-jump-start textarea:focus {
                    border-color: #2563eb;
                    outline: 2px solid rgba(37, 99, 235, 0.2);
                    outline-offset: 0;
                  }
                  .datasette-agent-jump-actions {
                    align-items: center;
                    display: flex;
                    gap: 0.75rem;
                  }
                  .datasette-agent-jump-start button {
                    background: #2563eb;
                    border: 0;
                    border-radius: 0.375rem;
                    color: white;
                    cursor: pointer;
                    font: inherit;
                    font-weight: 600;
                    padding: 0.45rem 0.75rem;
                  }
                  .datasette-agent-jump-start button:disabled {
                    cursor: default;
                    opacity: 0.65;
                  }
                  .datasette-agent-jump-hint,
                  .datasette-agent-jump-error {
                    font-size: 0.75rem;
                    line-height: 1.35;
                    margin: 0;
                  }
                  .datasette-agent-jump-hint {
                    color: #6b7280;
                    margin-top: -0.25rem;
                  }
                  .datasette-agent-jump-error {
                    color: #b91c1c;
                  }
                </style>
                <form class="datasette-agent-jump-start">
                  <label for="datasette-agent-jump-message">Start a new agent chat</label>
                  <textarea id="datasette-agent-jump-message" name="message" placeholder="Ask a question about your data..." rows="3"></textarea>
                  <p class="datasette-agent-jump-hint">Press Enter to start. Shift+Enter adds a new line.</p>
                  <div class="datasette-agent-jump-actions">
                    <button type="submit">Start chat</button>
                  </div>
                  <p class="datasette-agent-jump-error" data-agent-jump-error role="alert"></p>
                </form>`;

              const form = node.querySelector("form");
              const textarea = node.querySelector("textarea");
              const button = node.querySelector("button");
              const error = node.querySelector("[data-agent-jump-error]");

              textarea.addEventListener("keydown", (event) => {
                if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
                  event.preventDefault();
                  form.requestSubmit();
                }
              });

              form.addEventListener("submit", async (event) => {
                event.preventDefault();
                const message = textarea.value.trim();
                if (!message) {
                  textarea.focus();
                  return;
                }

                error.textContent = "";
                button.disabled = true;
                button.textContent = "Starting...";

                try {
                  const response = await fetch(createConversationUrl, {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({message}),
                  });
                  if (!response.ok) {
                    throw new Error("HTTP " + response.status);
                  }
                  const data = await response.json();
                  if (!data.conversation_id) {
                    throw new Error("Missing conversation ID");
                  }
                  try {
                    sessionStorage.setItem("agent_pending_message", message);
                  } catch (e) {}
                  window.location.assign(conversationBaseUrl + data.conversation_id);
                } catch (e) {
                  button.disabled = false;
                  button.textContent = "Start chat";
                  error.textContent = "Could not start chat";
                }
              });
            },
          },
        ];
      },
    });
  }

  if (window.__DATASETTE__) {
    register(window.__DATASETTE__);
  } else {
    document.addEventListener("datasette_init", function (event) {
      register(event.detail);
    });
  }
})();

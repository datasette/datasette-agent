// "Datasette Agent" section on the Datasette homepage, using the
// makeHomepageSections() JavaScript plugin hook. Type # to tag a table,
// view, query or database, the same as on the /-/agent page.
import {attachReferencePicker} from "./references.js";

const MODEL_STORAGE_KEY = "datasette-agent-model";

function config() {
  return window.datasetteAgentJumpConfig || {};
}

function render(node) {
  const {createConversationUrl, modelsUrl, conversationBaseUrl, referencesUrl} =
    config();
  node.innerHTML = `
    <form class="datasette-agent-homepage">
      <div class="datasette-agent-homepage-row">
        <div class="datasette-agent-homepage-input">
          <div class="datasette-agent-homepage-anchor">
            <textarea rows="1" name="message"
              aria-label="Ask Datasette Agent a question about your data"
              placeholder="Ask a question about your data... (# to tag a table or query)"></textarea>
          </div>
        </div>
        <button type="submit">Ask</button>
      </div>
      <p class="datasette-agent-homepage-error" role="alert"></p>
    </form>
  `;
  const form = node.querySelector("form");
  const textarea = node.querySelector("textarea");
  const button = node.querySelector("button");
  const error = node.querySelector(".datasette-agent-homepage-error");
  if (referencesUrl) {
    textarea.dataset.referenceSearchUrl = referencesUrl;
  }
  const referencePicker = attachReferencePicker(textarea, {placement: "below"});

  // Grow with the text, starting at one row
  const autosize = () => {
    textarea.style.height = "auto";
    textarea.style.height = textarea.scrollHeight + "px";
  };
  textarea.addEventListener("input", autosize);

  // Use the model picked last time on /-/agent if it is still on offer,
  // otherwise the server's default
  let model = null;
  if (modelsUrl) {
    fetch(modelsUrl)
      .then((response) => (response.ok ? response.json() : null))
      .then((data) => {
        const models = (data && data.models) || [];
        try {
          const remembered = localStorage.getItem(MODEL_STORAGE_KEY);
          if (remembered && models.includes(remembered)) {
            model = remembered;
          }
        } catch (e) {}
      })
      .catch(() => {});
  }

  // Enter sends, Shift+Enter adds a line. The reference picker listens in
  // the capture phase, so Enter picks a # suggestion first when one is open.
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
    const body = {message};
    if (model) {
      body.model = model;
    }
    try {
      const response = await fetch(createConversationUrl, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body),
      });
      const data = await response.json();
      if (!response.ok || !data.conversation_id) {
        throw new Error(data.error || "HTTP " + response.status);
      }
      // The conversation page sends these as the first message
      try {
        sessionStorage.setItem("agent_pending_message", message);
        sessionStorage.setItem(
          "agent_pending_references",
          JSON.stringify(referencePicker.getReferences()),
        );
      } catch (e) {}
      window.location.assign(conversationBaseUrl + data.conversation_id);
    } catch (e) {
      button.disabled = false;
      button.textContent = "Ask";
      error.textContent = "Could not start chat: " + e.message;
    }
  });
}

function register(manager) {
  if (!manager || typeof manager.registerPlugin !== "function") {
    return;
  }
  manager.registerPlugin("datasette-agent-homepage", {
    version: config().pluginVersion,
    makeHomepageSections() {
      return [{id: "datasette-agent", title: "Datasette Agent", render}];
    },
  });
}

if (window.__DATASETTE__) {
  register(window.__DATASETTE__);
} else {
  document.addEventListener("datasette_init", (event) => register(event.detail));
}

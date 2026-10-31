// Canvas: a same-origin iframe next to the chat that shows Datasette
// pages. The agent opens pages with the show_in_canvas tool (agent.js
// calls window.datasetteAgentCanvas.open), and same-site links in the
// chat open there too. Wide screens show chat and canvas side by side;
// narrow screens show one at a time with a Chat | Canvas switch.

const chat = document.querySelector(".agent-chat");
const canvas = chat && chat.querySelector(".agent-canvas");

// Below this width the canvas and chat take turns; keep in sync with
// the media queries in agent.css.
const narrowQuery = window.matchMedia("(max-width: 899px)");

// Paths that must never load inside the canvas: the agent UI itself.
const AGENT_PATH = /\/-\/agent(\/|$)/;

function setupCanvas() {
  const frame = canvas.querySelector(".agent-canvas-frame");
  const urlEl = canvas.querySelector(".agent-canvas-url");
  const openLink = canvas.querySelector(".agent-canvas-open");
  const toggle = chat.querySelector(".agent-canvas-toggle");
  const storageKey = "datasette-agent-canvas:" + chat.dataset.conversationId;

  let currentUrl = null;
  let hidden = false;

  function load() {
    try {
      return JSON.parse(localStorage.getItem(storageKey) || "null");
    } catch (err) {
      return null;
    }
  }

  function save() {
    try {
      localStorage.setItem(storageKey, JSON.stringify({url: currentUrl, hidden}));
    } catch (err) {}
  }

  // Full-height layout: the chat fills the viewport below Datasette's
  // header, so the page itself never scrolls.
  function measure() {
    const top = chat.getBoundingClientRect().top + window.scrollY;
    chat.style.setProperty("--agent-chat-top", top + "px");
  }

  function setView(view) {
    chat.dataset.agentView = view;
    chat.querySelectorAll(".agent-view-switch button").forEach(button => {
      button.setAttribute("aria-pressed", String(button.dataset.agentView === view));
    });
    if (view === "canvas") chat.classList.remove("agent-canvas-unseen");
  }

  function setHidden(value) {
    hidden = value;
    chat.classList.toggle("agent-canvas-hidden", hidden);
    toggle.textContent = hidden ? "Show canvas" : "Hide canvas";
    save();
  }

  function activate() {
    if (chat.classList.contains("agent-has-canvas")) return;
    chat.classList.add("agent-has-canvas");
    document.body.classList.add("agent-workspace");
    window.scrollTo(0, 0);
    measure();
  }

  function showUrl(url) {
    currentUrl = url;
    urlEl.textContent = url;
    urlEl.title = url;
    openLink.href = url;
    save();
  }

  function flash() {
    canvas.classList.remove("flash");
    void canvas.offsetWidth;
    canvas.classList.add("flash");
  }

  // source is "agent" (show_in_canvas) or "user" (a clicked link).
  // The agent never pulls the user away from the chat on a narrow
  // screen - it flags the Canvas switch instead.
  function open(url, {source = "user"} = {}) {
    activate();
    showUrl(url);
    frame.src = url;
    if (source === "agent") {
      if (hidden) setHidden(false);
      if (narrowQuery.matches && chat.dataset.agentView !== "canvas") {
        chat.classList.add("agent-canvas-unseen");
      }
      flash();
    } else {
      if (hidden) setHidden(false);
      setView("canvas");
    }
  }

  frame.addEventListener("load", () => {
    let loc;
    try {
      loc = frame.contentWindow.location;
    } catch (err) {
      return; // Navigated off-site: keep showing the last known URL.
    }
    if (!loc || loc.href === "about:blank") return;
    if (loc.origin !== window.location.origin) return;
    const url = loc.pathname + loc.search + loc.hash;
    if (AGENT_PATH.test(loc.pathname)) {
      // A link inside the canvas led back to the agent UI (e.g. a new
      // chat): take the whole window there instead of nesting it.
      window.location.href = url;
      return;
    }
    showUrl(url);
    try {
      urlEl.title = frame.contentDocument.title || url;
    } catch (err) {}
  });

  canvas.querySelector(".agent-canvas-bar").addEventListener("click", e => {
    const button = e.target.closest("[data-canvas-action]");
    if (!button) return;
    try {
      const action = button.dataset.canvasAction;
      if (action === "back") frame.contentWindow.history.back();
      else if (action === "forward") frame.contentWindow.history.forward();
      else if (action === "reload") frame.contentWindow.location.reload();
    } catch (err) {}
  });

  chat.querySelector(".agent-view-switch").addEventListener("click", e => {
    const button = e.target.closest("[data-agent-view]");
    if (button) setView(button.dataset.agentView);
  });

  toggle.addEventListener("click", () => setHidden(!hidden));

  // Same-site links in the conversation open in the canvas. Modified
  // clicks keep their normal new-tab/new-window behaviour.
  document.getElementById("messages").addEventListener("click", e => {
    if (e.defaultPrevented || e.button !== 0) return;
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const link = e.target.closest("a[href]");
    if (!link || link.hasAttribute("download")) return;
    const url = new URL(link.href, window.location.href);
    if (url.origin !== window.location.origin) return;
    if (AGENT_PATH.test(url.pathname)) return;
    e.preventDefault();
    open(url.pathname + url.search + url.hash, {source: "user"});
  });

  window.addEventListener("resize", () => {
    if (chat.classList.contains("agent-has-canvas")) measure();
  });

  // Restore: this browser's last canvas page for the conversation, else
  // the last page the agent opened.
  const saved = load();
  const initial = (saved && saved.url) || chat.dataset.canvasUrl;
  if (initial && !AGENT_PATH.test(initial)) {
    activate();
    showUrl(initial);
    frame.src = initial;
    setHidden(Boolean(saved && saved.hidden));
  }

  window.datasetteAgentCanvas = {
    open,
    // What the user is looking at, sent along with each message. A
    // canvas they have hidden doesn't count.
    currentUrl: () => (hidden ? null : currentUrl),
  };
}

if (canvas) setupCanvas();

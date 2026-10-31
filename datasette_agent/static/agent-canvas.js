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
  const firstFrame = canvas.querySelector(".agent-canvas-frame");
  const backLink = chat.querySelector(".agent-header > a");
  const backHref = backLink.getAttribute("href");
  const toggle = chat.querySelector(".agent-canvas-toggle");
  const storageKey = "datasette-agent-canvas:" + chat.dataset.conversationId;

  let currentUrl = null;
  let hidden = false;
  let currentFrame = null;
  let pendingFrame = null;
  const observedFrames = new WeakSet();

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
    updateBackLink();
  }

  function updateBackLink() {
    const inCanvas = narrowQuery.matches && chat.dataset.agentView === "canvas";
    const label = inCanvas ? "Back to chat" : "Back to conversations";
    backLink.setAttribute("aria-label", label);
    backLink.setAttribute("title", label);
    backLink.setAttribute("href", inCanvas ? "#chat" : backHref);
  }

  function setHidden(value) {
    hidden = value;
    chat.classList.toggle("agent-canvas-hidden", hidden);
    toggle.textContent = hidden ? "Show canvas" : "Close canvas";
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
    save();
  }

  function loadFrame(url) {
    if (pendingFrame && pendingFrame !== firstFrame) pendingFrame.remove();
    const nextFrame = currentFrame ? document.createElement("iframe") : firstFrame;
    if (currentFrame) {
      nextFrame.className = "agent-canvas-frame";
      nextFrame.title = "Canvas";
      canvas.append(nextFrame);
    }
    pendingFrame = nextFrame;
    if (!observedFrames.has(nextFrame)) {
      nextFrame.addEventListener("load", () => frameLoaded(nextFrame));
      observedFrames.add(nextFrame);
    }
    nextFrame.src = url;
  }

  function promoteFrame(nextFrame) {
    if (nextFrame !== pendingFrame) return;
    const previousFrame = currentFrame;
    currentFrame = nextFrame;
    pendingFrame = null;
    if (previousFrame && previousFrame !== nextFrame) previousFrame.remove();
    nextFrame.name = "agent-canvas";
    nextFrame.classList.add("agent-canvas-frame-ready");
    canvas.classList.add("agent-canvas-ready");
    canvas.classList.remove("agent-canvas-loading");
  }

  // Uncover a frame once its page is ready: a fresh frame replaces the
  // visible one, the visible frame comes back from behind the loading
  // indicator.
  function revealFrame(frame) {
    if (frame === pendingFrame) promoteFrame(frame);
    else if (frame === currentFrame) canvas.classList.remove("agent-canvas-loading");
  }

  function frameLoaded(loadedFrame) {
    if (loadedFrame !== pendingFrame && loadedFrame !== currentFrame) return;
    let loc;
    try {
      loc = loadedFrame.contentWindow.location;
    } catch (err) {
      // An off-site navigation cannot be inspected, but should still be shown.
      revealFrame(loadedFrame);
      return;
    }
    if (!loc || loc.href === "about:blank") return;
    if (loc.origin !== window.location.origin) {
      revealFrame(loadedFrame);
      return;
    }
    const url = loc.pathname + loc.search + loc.hash;
    if (AGENT_PATH.test(loc.pathname)) {
      // A link inside the canvas led back to the agent UI (e.g. a new
      // chat): take the whole window there instead of nesting it.
      window.location.href = url;
      return;
    }
    // The chat header already sits above the canvas, so hide the page's
    // own Datasette header. The frame stays covered by the loading
    // indicator until this has happened, so the header never flashes.
    const header = loadedFrame.contentDocument.querySelector("header.hd");
    if (header) header.style.display = "none";
    // When this page starts navigating away (a link or form inside the
    // canvas), cover the frame until the next page is ready. Downloads
    // don't unload the page, so they never trigger this.
    loadedFrame.contentWindow.addEventListener("pagehide", () => {
      if (loadedFrame === currentFrame) canvas.classList.add("agent-canvas-loading");
    });
    showUrl(url);
    // Give nested app frames a chance to paint before uncovering this page.
    requestAnimationFrame(() => requestAnimationFrame(() => revealFrame(loadedFrame)));
  }

  // Navigate the visible frame so the visit joins the browser history
  // and Back returns to the previous canvas page.
  function navigateFrame(url) {
    if (!currentFrame) {
      loadFrame(url);
      return;
    }
    if (pendingFrame && pendingFrame !== firstFrame) pendingFrame.remove();
    pendingFrame = null;
    canvas.classList.add("agent-canvas-loading");
    try {
      currentFrame.contentWindow.location.assign(url);
    } catch (err) {
      currentFrame.src = url;
    }
  }

  // source is "agent" (show_in_canvas) or "user" (a clicked link).
  // Agent pages load in a fresh frame that replaces the visible one once
  // it has painted; links the user follows navigate the visible frame.
  // The agent never pulls the user away from the chat on a narrow
  // screen - it flags the Canvas switch instead.
  function open(url, {source = "user"} = {}) {
    activate();
    showUrl(url);
    if (source === "agent") loadFrame(url);
    else navigateFrame(url);
    if (source === "agent") {
      if (hidden) setHidden(false);
      if (narrowQuery.matches && chat.dataset.agentView !== "canvas") {
        chat.classList.add("agent-canvas-unseen");
      }
    } else {
      if (hidden) setHidden(false);
      setView("canvas");
    }
  }

  chat.querySelector(".agent-view-switch").addEventListener("click", e => {
    const button = e.target.closest("[data-agent-view]");
    if (button) setView(button.dataset.agentView);
  });

  backLink.addEventListener("click", e => {
    if (!narrowQuery.matches || chat.dataset.agentView !== "canvas") return;
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    setView("chat");
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
    updateBackLink();
  });

  // Restore: this browser's last canvas page for the conversation, else
  // the last page the agent opened.
  const saved = load();
  const initial = (saved && saved.url) || chat.dataset.canvasUrl;
  if (initial && !AGENT_PATH.test(initial)) {
    activate();
    showUrl(initial);
    loadFrame(initial);
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

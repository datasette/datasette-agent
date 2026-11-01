// Panel: a same-origin iframe next to the chat that shows Datasette
// pages. The agent opens pages with the show_in_panel tool (agent.js
// calls window.datasetteAgentPanel.open), and same-site links in the
// chat open there too. Wide screens show chat and panel side by side;
// narrow screens show one at a time with a Chat | Panel switch.

const chat = document.querySelector(".agent-chat");
const panel = chat && chat.querySelector(".agent-panel");

// Below this width the panel and chat take turns; keep in sync with
// the media queries in agent.css.
const narrowQuery = window.matchMedia("(max-width: 899px)");

// Paths that must never load inside the panel: the agent UI itself.
const AGENT_PATH = /\/-\/agent(\/|$)/;

function setupPanel() {
  const firstFrame = panel.querySelector(".agent-panel-frame");
  const backLink = chat.querySelector(".agent-header > a");
  const backHref = backLink.getAttribute("href");
  const toggle = chat.querySelector(".agent-panel-toggle");
  const storageKey = "datasette-agent-panel:" + chat.dataset.conversationId;

  let currentUrl = null;
  let hidden = false;
  let currentFrame = null;
  let pendingFrame = null;
  // The page on this site the panel is navigating to, if it is known.
  let expectedUrl = null;
  const errorBox = panel.querySelector(".agent-panel-error");
  const observedFrames = new WeakSet();

  // The path of a URL on this site, or null for anywhere else.
  function localPath(href) {
    const url = new URL(href, window.location.href);
    if (url.origin !== window.location.origin) return null;
    return url.pathname + url.search + url.hash;
  }

  function setError(url) {
    errorBox.hidden = !url;
    if (url) errorBox.querySelector("a").href = url;
  }

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
    if (view === "panel") chat.classList.remove("agent-panel-unseen");
    updateBackLink();
  }

  function updateBackLink() {
    const inPanel = narrowQuery.matches && chat.dataset.agentView === "panel";
    const label = inPanel ? "Back to chat" : "Back to conversations";
    backLink.setAttribute("aria-label", label);
    backLink.setAttribute("title", label);
    backLink.setAttribute("href", inPanel ? "#chat" : backHref);
  }

  function setHidden(value) {
    hidden = value;
    chat.classList.toggle("agent-panel-hidden", hidden);
    toggle.textContent = hidden ? "Show panel" : "Close panel";
    save();
  }

  function activate() {
    if (chat.classList.contains("agent-has-panel")) return;
    chat.classList.add("agent-has-panel");
    document.body.classList.add("agent-workspace");
    window.scrollTo(0, 0);
    measure();
  }

  function showUrl(url) {
    currentUrl = url;
    save();
  }

  function loadFrame(url) {
    expectedUrl = localPath(url);
    if (pendingFrame && pendingFrame !== firstFrame) pendingFrame.remove();
    const nextFrame = currentFrame ? document.createElement("iframe") : firstFrame;
    if (currentFrame) {
      nextFrame.className = "agent-panel-frame";
      nextFrame.title = "Panel";
      panel.append(nextFrame);
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
    nextFrame.name = "agent-panel";
    nextFrame.classList.add("agent-panel-frame-ready");
    panel.classList.add("agent-panel-ready");
    panel.classList.remove("agent-panel-loading");
  }

  // Uncover a frame once its page is ready: a fresh frame replaces the
  // visible one, the visible frame comes back from behind the loading
  // indicator.
  function revealFrame(frame) {
    if (frame === pendingFrame) promoteFrame(frame);
    else if (frame === currentFrame) panel.classList.remove("agent-panel-loading");
  }

  function frameLoaded(loadedFrame) {
    if (loadedFrame !== pendingFrame && loadedFrame !== currentFrame) return;
    let loc, href;
    try {
      loc = loadedFrame.contentWindow.location;
      href = loc.href;
    } catch (err) {
      // An off-site navigation, or a page that refuses to be framed, cannot
      // be inspected but should still be shown. Only reading from a
      // cross-origin Location throws, not getting hold of it. A page on this
      // site only looks cross-origin if the browser refused to frame it.
      setError(expectedUrl);
      expectedUrl = null;
      revealFrame(loadedFrame);
      return;
    }
    if (href === "about:blank") return;
    expectedUrl = null;
    setError(null);
    if (loc.origin !== window.location.origin) {
      revealFrame(loadedFrame);
      return;
    }
    const url = loc.pathname + loc.search + loc.hash;
    if (AGENT_PATH.test(loc.pathname)) {
      // A link inside the panel led back to the agent UI (e.g. a new
      // chat): take the whole window there instead of nesting it.
      window.location.href = url;
      return;
    }
    // The chat header already sits above the panel, so hide the page's
    // own Datasette header. The frame stays covered by the loading
    // indicator until this has happened, so the header never flashes.
    const header = loadedFrame.contentDocument.querySelector("header.hd");
    if (header) header.style.display = "none";
    // When this page starts navigating away (a link or form inside the
    // panel), cover the frame until the next page is ready. Downloads
    // don't unload the page, so they never trigger this.
    loadedFrame.contentWindow.addEventListener("pagehide", () => {
      if (loadedFrame === currentFrame) panel.classList.add("agent-panel-loading");
    });
    // Remember where links in this page lead, because a blocked page's URL
    // can't be read back from the frame.
    loadedFrame.contentDocument.addEventListener("click", e => {
      const link = e.target.closest("a[href]");
      if (link && !e.defaultPrevented) expectedUrl = localPath(link.href);
    });
    showUrl(url);
    // Give nested app frames a chance to paint before uncovering this page.
    requestAnimationFrame(() => requestAnimationFrame(() => revealFrame(loadedFrame)));
  }

  // Navigate the visible frame so the visit joins the browser history
  // and Back returns to the previous panel page.
  function navigateFrame(url) {
    if (!currentFrame) {
      loadFrame(url);
      return;
    }
    expectedUrl = localPath(url);
    setError(null);
    if (pendingFrame && pendingFrame !== firstFrame) pendingFrame.remove();
    pendingFrame = null;
    panel.classList.add("agent-panel-loading");
    try {
      currentFrame.contentWindow.location.assign(url);
    } catch (err) {
      currentFrame.src = url;
    }
  }

  // source is "agent" (show_in_panel) or "user" (a clicked link).
  // Agent pages load in a fresh frame that replaces the visible one once
  // it has painted; links the user follows navigate the visible frame.
  // The agent never pulls the user away from the chat on a narrow
  // screen - it flags the Panel switch instead.
  function open(url, {source = "user"} = {}) {
    activate();
    showUrl(url);
    if (source === "agent") loadFrame(url);
    else navigateFrame(url);
    if (source === "agent") {
      if (hidden) setHidden(false);
      if (narrowQuery.matches && chat.dataset.agentView !== "panel") {
        chat.classList.add("agent-panel-unseen");
      }
    } else {
      if (hidden) setHidden(false);
      setView("panel");
    }
  }

  chat.querySelector(".agent-view-switch").addEventListener("click", e => {
    const button = e.target.closest("[data-agent-view]");
    if (button) setView(button.dataset.agentView);
  });

  backLink.addEventListener("click", e => {
    if (!narrowQuery.matches || chat.dataset.agentView !== "panel") return;
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    setView("chat");
  });

  toggle.addEventListener("click", () => setHidden(!hidden));

  // Same-site links in the conversation open in the panel. Modified
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
    if (chat.classList.contains("agent-has-panel")) measure();
    updateBackLink();
  });

  // Restore: this browser's last panel page for the conversation, else
  // the last page the agent opened.
  const saved = load();
  const initial = (saved && saved.url) || chat.dataset.panelUrl;
  if (initial && !AGENT_PATH.test(initial)) {
    activate();
    showUrl(initial);
    loadFrame(initial);
    setHidden(Boolean(saved && saved.hidden));
  }

  window.datasetteAgentPanel = {
    open,
    // What the user is looking at, sent along with each message. A
    // panel they have hidden doesn't count.
    currentUrl: () => (hidden ? null : currentUrl),
  };
}

if (panel) setupPanel();

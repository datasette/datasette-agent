function parseWebUrl(value) {
  try {
    const url = new URL(value, document.baseURI);
    return ["http:", "https:"].includes(url.protocol) ? url : null;
  } catch {
    return null;
  }
}

export function setLinkDestination(link, value) {
  if (link.hasAttribute("data-agent-external-url")) {
    link.removeAttribute("data-agent-external-url");
    link.classList.remove("agent-external-link");
    link.removeAttribute("role");
    link.removeAttribute("tabindex");
  }
  const url = parseWebUrl(value);
  if (url && url.origin === window.location.origin) {
    link.setAttribute("href", value);
    return;
  }

  // Never give external destinations a navigable href: browser context menus
  // and modified clicks must not bypass the confirmation dialog.
  link.removeAttribute("href");
  if (!url) return;
  link.setAttribute("data-agent-external-url", url.href);
  link.classList.add("agent-external-link");
  link.setAttribute("role", "link");
  link.setAttribute("tabindex", "0");
}

export function prepareLinks(root) {
  root.querySelectorAll("a[href]").forEach(link => {
    setLinkDestination(link, link.getAttribute("href"));
  });
}

export function setupExternalLinks(root) {
  if (!root) return;
  prepareLinks(root);
  let dialog;
  let urlPreview;
  let cancelButton;
  let pendingUrl = "";
  let previousFocus;

  function appendText(parent, tag, className, text) {
    const element = document.createElement(tag);
    element.className = className;
    element.textContent = text;
    parent.appendChild(element);
    return element;
  }

  function ensureDialog() {
    if (dialog) return;
    dialog = document.createElement("dialog");
    dialog.className = "agent-link-dialog";
    dialog.setAttribute("aria-labelledby", "agent-link-title");
    dialog.setAttribute("aria-describedby", "agent-link-message");
    appendText(dialog, "h2", "", "Open external link").id = "agent-link-title";
    appendText(
      dialog, "p", "agent-link-message",
      "You're leaving Datasette to visit an external link:"
    ).id = "agent-link-message";
    urlPreview = appendText(dialog, "div", "agent-link-url", "");
    const actions = appendText(dialog, "div", "agent-link-actions", "");
    cancelButton = appendText(actions, "button", "agent-link-cancel", "Cancel");
    cancelButton.type = "button";
    const openButton = appendText(actions, "button", "agent-link-open", "Open link");
    openButton.type = "button";

    cancelButton.addEventListener("click", () => dialog.close());
    openButton.addEventListener("click", () => {
      const url = pendingUrl;
      dialog.close();
      if (url) {
        const opened = window.open(url, "_blank", "noopener,noreferrer");
        if (opened) opened.opener = null;
      }
    });
    dialog.addEventListener("close", () => {
      pendingUrl = "";
      if (previousFocus && previousFocus.isConnected) previousFocus.focus();
      previousFocus = null;
    });
    dialog.addEventListener("click", event => {
      if (event.target !== dialog) return;
      const box = dialog.getBoundingClientRect();
      if (event.clientX < box.left || event.clientX > box.right ||
          event.clientY < box.top || event.clientY > box.bottom) {
        dialog.close();
      }
    });
    document.body.appendChild(dialog);
  }

  function activate(event) {
    const link = event.target.closest("a[data-agent-external-url]");
    if (!link || !root.contains(link)) return;
    event.preventDefault();
    const url = parseWebUrl(link.getAttribute("data-agent-external-url"));
    if (!url || url.origin === window.location.origin) return;
    ensureDialog();
    previousFocus = link;
    pendingUrl = url.href;
    urlPreview.textContent = pendingUrl;
    if (!dialog.open) dialog.showModal();
    cancelButton.focus();
  }

  root.addEventListener("click", event => {
    if (event.button === 0) activate(event);
  });
  root.addEventListener("auxclick", event => {
    if (event.button === 1) activate(event);
  });
  root.addEventListener("contextmenu", event => {
    // On macOS, Control-click produces a context menu rather than a click.
    if (event.ctrlKey) activate(event);
  });
  root.addEventListener("keydown", event => {
    if (event.key === "Enter") activate(event);
  });
}

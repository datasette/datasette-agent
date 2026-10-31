document.querySelectorAll(".agent-patterns-preview").forEach((preview) => {
  const backLink = preview.querySelector(".agent-back-link");
  const toggle = preview.querySelector(".agent-canvas-toggle");
  const narrow = preview.classList.contains("agent-patterns-narrow");
  const backHref = backLink.getAttribute("href");

  function setView(view) {
    preview.dataset.agentView = view;
    preview.querySelectorAll(".agent-view-switch button").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.agentView === view));
    });
    if (narrow) {
      const label = view === "canvas" ? "Back to chat" : "Back to conversations";
      backLink.setAttribute("aria-label", label);
      backLink.setAttribute("title", label);
      backLink.setAttribute("href", view === "canvas" ? "#chat" : backHref);
    }
  }

  preview.querySelector(".agent-view-switch").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-agent-view]");
    if (button) setView(button.dataset.agentView);
  });

  toggle.addEventListener("click", () => {
    const hidden = preview.classList.toggle("agent-canvas-hidden");
    toggle.textContent = hidden ? "Show canvas" : "Close canvas";
  });

  backLink.addEventListener("click", (event) => {
    if (narrow && preview.dataset.agentView === "canvas") {
      event.preventDefault();
      setView("chat");
    }
  });
});

// Chat export dropdown menu
(function () {
  const containers = document.querySelectorAll(".chat-export-menu");
  const menus = [];

  containers.forEach((container) => {
    const markdownUrl = container.dataset.markdownUrl;
    const title = container.dataset.title || "chat";
    let menuOpen = container.dataset.open === "true";

    // Build menu HTML
    container.innerHTML = `
    <button class="export-tag" aria-label="Export options" aria-expanded="${menuOpen}">Export</button>
    <div class="export-menu${menuOpen ? "" : " hidden"}">
      <button class="export-menu-btn" data-action="copy">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
        Copy markdown
      </button>
      <button class="export-menu-btn" data-action="download">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
        Download markdown
      </button>
    </div>
  `;

    const tag = container.querySelector(".export-tag");
    const menu = container.querySelector(".export-menu");

    function setMenuOpen(open) {
      menuOpen = open;
      menu.classList.toggle("hidden", !open);
      tag.setAttribute("aria-expanded", String(open));
    }

    menus.push({container, close: () => setMenuOpen(false)});

    tag.addEventListener("click", () => {
      setMenuOpen(!menuOpen);
    });

    async function fetchMarkdown() {
      const resp = await fetch(markdownUrl);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      return await resp.text();
    }

    // Copy markdown
    container.querySelector('[data-action="copy"]').addEventListener("click", async () => {
      try {
        const md = await fetchMarkdown();
        await navigator.clipboard.writeText(md);
        const btn = container.querySelector('[data-action="copy"]');
        const orig = btn.innerHTML;
        btn.textContent = "Copied!";
        setTimeout(() => (btn.innerHTML = orig), 1200);
      } catch (err) {
        console.error("Copy failed:", err);
      }
    });

    // Download markdown
    container.querySelector('[data-action="download"]').addEventListener("click", async () => {
      try {
        const md = await fetchMarkdown();
        const blob = new Blob([md], { type: "text/markdown" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        const safeName = title.replace(/[^a-zA-Z0-9 _-]/g, "").trim().slice(0, 60) || "chat";
        a.href = url;
        a.download = safeName + ".md";
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
      } catch (err) {
        console.error("Download failed:", err);
      }
      setMenuOpen(false);
    });
  });

  // Clicking outside a menu closes it, including when opening another one.
  document.addEventListener("click", (event) => {
    menus.forEach(({container, close}) => {
      if (!container.contains(event.target)) close();
    });
  });
})();

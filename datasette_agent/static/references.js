// #-references: type # in a chat textarea to search databases, tables and
// stored queries, pick one with arrow keys + Enter (or a tap), and the
// pick becomes a pill. The message text keeps a readable "#name" token;
// the picked references travel alongside it so the server can attach
// their schemas for the model.

const SEARCH_URL = "/-/agent/api/references";
const SEARCH_DEBOUNCE_MS = 120;
const TYPE_LABELS = {table: "table", query: "query", database: "db"};

// Match "#query" ending at the caret, where # starts the text or follows
// whitespace/an opening bracket (so "a#b" or a URL fragment doesn't
// trigger it).
const TRIGGER_RE = /(^|[\s(\[])#([^\s#]*)$/;

export function referenceKey(ref) {
  return ref.type + "\u0000" + ref.database + "\u0000" + ref.name;
}

function referenceTitle(ref) {
  if (ref.type === "database") return "Database " + ref.name;
  const kind = ref.type === "query" ? "Query" : "Table";
  return kind + " " + ref.name + " in " + ref.database;
}

export function createReferencePill(ref) {
  const pill = document.createElement(ref.url ? "a" : "span");
  if (ref.url) pill.href = ref.url;
  pill.className = "agent-ref-pill agent-ref-" + ref.type;
  pill.title = referenceTitle(ref);
  const type = document.createElement("span");
  type.className = "agent-ref-pill-type";
  type.textContent = TYPE_LABELS[ref.type] || ref.type;
  const name = document.createElement("span");
  name.className = "agent-ref-pill-name";
  name.textContent = ref.name;
  pill.append(type, name);
  return pill;
}

// Render user message text into el, turning each "#name" token that
// matches a reference into an inline pill. References whose token is
// missing from the text still show, as pills after it.
export function renderTextWithReferences(el, text, references) {
  el.textContent = "";
  references = references || [];
  if (!references.length) {
    el.textContent = text;
    return;
  }
  // Longest names first so "#items_archive" wins over "#items".
  const ordered = [...references].sort((a, b) => b.name.length - a.name.length);
  const used = new Set();
  let pos = 0;
  let plainStart = 0;
  while (pos < text.length) {
    let matched = null;
    if (text[pos] === "#") {
      for (const ref of ordered) {
        const token = "#" + ref.name;
        if (!text.startsWith(token, pos)) continue;
        const next = text[pos + token.length];
        if (next !== undefined && /[\w-]/.test(next)) continue;
        matched = ref;
        break;
      }
    }
    if (matched) {
      if (pos > plainStart) {
        el.appendChild(document.createTextNode(text.slice(plainStart, pos)));
      }
      el.appendChild(createReferencePill(matched));
      used.add(referenceKey(matched));
      pos += matched.name.length + 1;
      plainStart = pos;
    } else {
      pos += 1;
    }
  }
  if (plainStart < text.length) {
    el.appendChild(document.createTextNode(text.slice(plainStart)));
  }
  const leftover = references.filter((ref) => !used.has(referenceKey(ref)));
  if (leftover.length) {
    const row = document.createElement("div");
    row.className = "agent-ref-message-extra";
    leftover.forEach((ref) => row.appendChild(createReferencePill(ref)));
    el.appendChild(row);
  }
}

// Attach the # autocomplete to a textarea. Returns a controller:
//   getReferences() - the picked references still present in the text
//   clear()         - forget all references (call after sending)
//   isOpen()        - true while the suggestion list is showing
// options.placement is "above" (default, for a composer pinned to the
// bottom of the screen) or "below".
export function attachReferencePicker(textarea, options = {}) {
  const placement = options.placement || "above";
  const anchor = textarea.parentElement;
  anchor.classList.add("agent-ref-anchor");

  const listId = "agent-ref-list-" + Math.random().toString(36).slice(2);
  const popup = document.createElement("div");
  popup.className = "agent-ref-popup agent-ref-popup-" + placement;
  popup.hidden = true;
  const list = document.createElement("ul");
  list.className = "agent-ref-list";
  list.id = listId;
  list.setAttribute("role", "listbox");
  list.setAttribute("aria-label", "Tag a table, query or database");
  popup.appendChild(list);
  const hint = document.createElement("div");
  hint.className = "agent-ref-hint";
  hint.textContent = "↑↓ to choose, Enter to tag, Esc to dismiss";
  popup.appendChild(hint);
  anchor.appendChild(popup);

  textarea.setAttribute("aria-autocomplete", "list");
  textarea.setAttribute("aria-controls", listId);
  textarea.setAttribute("aria-expanded", "false");

  // Chips for picked references, shown above the input row.
  const chips = document.createElement("div");
  chips.className = "agent-ref-chips";
  chips.hidden = true;
  anchor.parentElement.insertBefore(chips, anchor);

  let references = [];
  let results = [];
  let active = 0;
  let trigger = null; // {start, query} - start is the index of the "#"
  let debounceTimer = null;
  let controller = null;
  let requestSeq = 0;

  function currentTrigger() {
    if (textarea.selectionStart !== textarea.selectionEnd) return null;
    const before = textarea.value.slice(0, textarea.selectionStart);
    const m = TRIGGER_RE.exec(before);
    if (!m) return null;
    return {start: before.length - m[2].length - 1, query: m[2]};
  }

  function close() {
    trigger = null;
    results = [];
    popup.hidden = true;
    textarea.setAttribute("aria-expanded", "false");
    textarea.removeAttribute("aria-activedescendant");
    clearTimeout(debounceTimer);
    if (controller) controller.abort();
  }

  function setActive(index) {
    if (!results.length) return;
    active = (index + results.length) % results.length;
    list.querySelectorAll(".agent-ref-option").forEach((li, i) => {
      const selected = i === active;
      li.classList.toggle("active", selected);
      li.setAttribute("aria-selected", selected ? "true" : "false");
      if (selected) {
        textarea.setAttribute("aria-activedescendant", li.id);
        li.scrollIntoView({block: "nearest"});
      }
    });
  }

  function renderResults(query) {
    list.textContent = "";
    if (!results.length) {
      const empty = document.createElement("li");
      empty.className = "agent-ref-empty";
      empty.textContent = query
        ? "No tables, queries or databases match “" + query + "”"
        : "No tables, queries or databases available";
      list.appendChild(empty);
    }
    results.forEach((ref, i) => {
      const li = document.createElement("li");
      li.className = "agent-ref-option";
      li.id = listId + "-" + i;
      li.setAttribute("role", "option");
      const type = document.createElement("span");
      type.className = "agent-ref-option-type agent-ref-" + ref.type;
      type.textContent = TYPE_LABELS[ref.type] || ref.type;
      const text = document.createElement("span");
      text.className = "agent-ref-option-text";
      const name = document.createElement("span");
      name.className = "agent-ref-option-name";
      name.textContent = ref.name;
      const desc = document.createElement("span");
      desc.className = "agent-ref-option-desc";
      desc.textContent = ref.description || "";
      text.append(name, desc);
      li.append(type, text);
      // pointerdown + preventDefault keeps focus (and the on-screen
      // keyboard) on the textarea when the option is tapped.
      li.addEventListener("pointerdown", (e) => e.preventDefault());
      li.addEventListener("click", () => select(i));
      li.addEventListener("pointermove", () => {
        if (active !== i) setActive(i);
      });
      list.appendChild(li);
    });
    popup.hidden = false;
    textarea.setAttribute("aria-expanded", "true");
    active = 0;
    setActive(0);
  }

  async function search(query) {
    if (controller) controller.abort();
    controller = new AbortController();
    const seq = ++requestSeq;
    try {
      const resp = await fetch(
        (textarea.dataset.referenceSearchUrl || SEARCH_URL) + "?q=" + encodeURIComponent(query),
        {signal: controller.signal}
      );
      if (!resp.ok) return;
      const data = await resp.json();
      if (seq !== requestSeq || !trigger) return;
      results = data.results || [];
      renderResults(query);
    } catch (err) {
      if (err.name !== "AbortError") console.error("Reference search failed:", err);
    }
  }

  function update() {
    const next = currentTrigger();
    if (!next) {
      if (trigger) close();
      return;
    }
    const changed = !trigger || trigger.query !== next.query;
    trigger = next;
    if (!changed) return;
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => search(next.query), SEARCH_DEBOUNCE_MS);
  }

  function select(index) {
    const ref = results[index];
    if (!ref || !trigger) return;
    const value = textarea.value;
    const caret = textarea.selectionStart;
    const token = "#" + ref.name;
    const after = value.slice(caret);
    const spacer = after.startsWith(" ") ? "" : " ";
    textarea.value = value.slice(0, trigger.start) + token + spacer + after;
    const newCaret = trigger.start + token.length + 1;
    textarea.setSelectionRange(newCaret, newCaret);
    const picked = {type: ref.type, database: ref.database, name: ref.name, url: ref.url};
    if (!references.some((r) => referenceKey(r) === referenceKey(picked))) {
      references.push(picked);
    }
    close();
    renderChips();
    textarea.focus();
    textarea.dispatchEvent(new Event("input", {bubbles: true}));
  }

  function tokenPresent(ref) {
    const value = textarea.value;
    const token = "#" + ref.name;
    let i = value.indexOf(token);
    while (i !== -1) {
      const next = value[i + token.length];
      if (next === undefined || !/[\w-]/.test(next)) return true;
      i = value.indexOf(token, i + 1);
    }
    return false;
  }

  function removeReference(ref) {
    references = references.filter((r) => referenceKey(r) !== referenceKey(ref));
    // Strip the "#name" token too, so the text and pills stay in sync.
    const token = "#" + ref.name;
    textarea.value = textarea.value
      .split(token + " ")
      .join("")
      .split(token)
      .join("");
    renderChips();
    textarea.focus();
  }

  // Drop references whose "#name" was deleted from the text.
  function syncWithText() {
    const before = references.length;
    references = references.filter(tokenPresent);
    if (references.length !== before) renderChips();
  }

  function renderChips() {
    chips.textContent = "";
    references.forEach((ref) => {
      // No link on composer chips: they hold a remove button, and
      // following a link from here would lose the draft message.
      const pill = createReferencePill({...ref, url: null});
      pill.classList.add("agent-ref-chip");
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "agent-ref-chip-remove";
      remove.setAttribute("aria-label", "Remove " + ref.name);
      remove.textContent = "×";
      remove.addEventListener("click", () => removeReference(ref));
      pill.appendChild(remove);
      chips.appendChild(pill);
    });
    chips.hidden = references.length === 0;
  }

  textarea.addEventListener("input", () => {
    syncWithText();
    update();
  });
  textarea.addEventListener("click", update);
  textarea.addEventListener("keyup", (e) => {
    if (e.key === "ArrowLeft" || e.key === "ArrowRight" || e.key === "Home" || e.key === "End") {
      update();
    }
  });
  textarea.addEventListener("blur", () => setTimeout(close, 150));

  // Capture phase, so Enter picks a suggestion before any send-on-Enter
  // handler on the same textarea sees it.
  textarea.addEventListener(
    "keydown",
    (e) => {
      if (popup.hidden || e.isComposing) return;
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
        // Send shortcut: leave it to the page's own handler.
        close();
        return;
      }
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setActive(active + 1);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setActive(active - 1);
      } else if (e.key === "Enter" || e.key === "Tab") {
        if (!results.length) {
          if (e.key === "Enter") close();
          return;
        }
        e.preventDefault();
        e.stopImmediatePropagation();
        select(active);
      } else if (e.key === "Escape") {
        e.preventDefault();
        e.stopImmediatePropagation();
        close();
      }
    },
    true
  );

  return {
    getReferences() {
      syncWithText();
      return references.map((r) => ({...r}));
    },
    clear() {
      references = [];
      close();
      renderChips();
    },
    isOpen() {
      return !popup.hidden;
    },
  };
}

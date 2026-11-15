// File attachments for a chat composer: an attach button, pasting an
// image and dropping files onto the composer. Each file uploads straight
// away and shows as a removable chip; the message is then sent with the
// uploaded files' IDs. The server stores the files in datasette-files,
// private to the user.

import {setLinkDestination} from "./agent-links.js";

// A thumbnail linking to the file's page, for a message that has been sent.
export function createAttachmentThumbnail(file) {
  const link = document.createElement("a");
  link.className = "agent-attachment";
  setLinkDestination(link, file.url);
  link.title = file.filename;
  const img = document.createElement("img");
  img.src = file.thumbnail_url;
  img.alt = file.filename;
  link.appendChild(img);
  return link;
}

export function renderMessageAttachments(messageEl, attachments) {
  if (!attachments || !attachments.length) return;
  const row = document.createElement("div");
  row.className = "agent-message-attachments";
  attachments.forEach((file) => row.appendChild(createAttachmentThumbnail(file)));
  messageEl.appendChild(row);
}

// Attach file handling to a composer. options:
//   form       - the composer's form: holds the attach button and file
//                input, and is the drop target
//   textarea   - the message input, for pasted images
//   chips      - element the chips are rendered into
//   uploadUrl  - the attachments endpoint
//   getModelId - returns the model the message will be sent to
//   types      - MIME types that model accepts
// Returns a controller:
//   getAttachments() - the uploaded files, as the server described them
//   isUploading()    - true while any upload is still in flight
//   setTypes(types)  - the model changed: accept these types instead
//   restore(files)   - show already-uploaded files again
//   clear()          - forget every file (call after sending)
export function attachAttachmentPicker(options) {
  const {form, textarea, uploadUrl, getModelId} = options;
  const chips = options.chips || document.getElementById("attachment-chips");
  const button = form.querySelector(".agent-attach-btn");
  const input = form.querySelector('input[type="file"]');
  let types = [];
  let items = []; // {name, type, status, data, error, controller, previewUrl, el}

  function render() {
    chips.textContent = "";
    items.forEach((item) => chips.appendChild(item.el));
    chips.hidden = items.length === 0;
  }

  function remove(item) {
    if (item.controller) item.controller.abort();
    if (item.previewUrl) URL.revokeObjectURL(item.previewUrl);
    items = items.filter((other) => other !== item);
    render();
  }

  function buildChip(item) {
    const chip = document.createElement("span");
    chip.className = "agent-attachment-chip";
    const src = item.previewUrl || (item.data && item.data.thumbnail_url);
    if (src) {
      const img = document.createElement("img");
      img.src = src;
      img.alt = "";
      chip.appendChild(img);
    }
    const name = document.createElement("span");
    name.className = "agent-attachment-chip-name";
    name.textContent = item.name;
    name.title = item.name;
    chip.appendChild(name);
    const status = document.createElement("span");
    status.className = "agent-attachment-chip-status";
    chip.appendChild(status);
    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.className = "agent-attachment-chip-remove";
    removeBtn.setAttribute("aria-label", "Remove " + item.name);
    removeBtn.textContent = "×";
    removeBtn.addEventListener("click", () => remove(item));
    chip.appendChild(removeBtn);
    item.el = chip;
    item.statusEl = status;
    updateChip(item);
  }

  function updateChip(item) {
    item.el.classList.toggle("uploading", item.status === "uploading");
    item.el.classList.toggle("error", item.status === "error");
    item.el.dataset.status = item.status;
    if (item.status === "uploading") {
      item.statusEl.textContent = "Uploading…";
    } else if (item.status === "error") {
      item.statusEl.textContent = item.error;
    } else {
      item.statusEl.textContent = "";
    }
  }

  function fail(item, message) {
    item.status = "error";
    item.error = message;
    item.controller = null;
    updateChip(item);
  }

  async function upload(item, file) {
    item.controller = new AbortController();
    const body = new FormData();
    body.append("model_id", getModelId() || "");
    body.append("file", file, file.name);
    try {
      const response = await fetch(uploadUrl, {
        method: "POST",
        body,
        signal: item.controller.signal,
      });
      let data = null;
      try {
        data = await response.json();
      } catch (err) {}
      if (!response.ok || !data || !data.ok) {
        fail(item, (data && data.error) || "Upload failed (HTTP " + response.status + ")");
        return;
      }
      item.status = "done";
      item.data = data.file;
      item.controller = null;
      updateChip(item);
    } catch (err) {
      // Aborted because the chip was removed: nothing left to update.
      if (err.name !== "AbortError") fail(item, "Upload failed");
    }
  }

  function addFiles(fileList) {
    const files = Array.from(fileList || []);
    if (!files.length || !types.length) return;
    files.forEach((file) => {
      const item = {
        name: file.name || "attachment",
        type: file.type,
        status: "uploading",
        data: null,
        error: null,
        controller: null,
        previewUrl: file.type.startsWith("image/") ? URL.createObjectURL(file) : null,
      };
      buildChip(item);
      items.push(item);
      if (!types.includes(file.type)) {
        fail(item, "This model does not accept " + (file.type || "this type of file"));
      } else {
        upload(item, file);
      }
    });
    render();
  }

  function setTypes(newTypes) {
    types = newTypes || [];
    button.hidden = types.length === 0;
    input.accept = types.join(",");
    // Files the newly selected model cannot take are dropped.
    items
      .filter((item) => !types.includes(item.type))
      .forEach((item) => remove(item));
    render();
  }

  button.addEventListener("click", () => input.click());
  input.addEventListener("change", () => {
    addFiles(input.files);
    input.value = "";
  });

  // Pasting an image. A paste that also carries text (a spreadsheet
  // cell, say) is left alone, to paste as text.
  textarea.addEventListener("paste", (event) => {
    const clipboard = event.clipboardData;
    if (!clipboard || !clipboard.files.length || !types.length) return;
    if (clipboard.getData("text/plain")) return;
    event.preventDefault();
    addFiles(clipboard.files);
  });

  // Dropping files anywhere on the composer.
  function draggingFiles(event) {
    return (
      types.length > 0 &&
      event.dataTransfer &&
      Array.from(event.dataTransfer.types || []).includes("Files")
    );
  }
  let dragDepth = 0;
  form.addEventListener("dragenter", (event) => {
    if (!draggingFiles(event)) return;
    event.preventDefault();
    dragDepth += 1;
    form.classList.add("agent-attachment-dragover");
  });
  form.addEventListener("dragover", (event) => {
    if (!draggingFiles(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
  });
  form.addEventListener("dragleave", (event) => {
    if (!draggingFiles(event)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) form.classList.remove("agent-attachment-dragover");
  });
  form.addEventListener("drop", (event) => {
    if (!draggingFiles(event)) return;
    event.preventDefault();
    dragDepth = 0;
    form.classList.remove("agent-attachment-dragover");
    addFiles(event.dataTransfer.files);
  });

  setTypes(options.types || []);

  return {
    getAttachments() {
      return items.filter((item) => item.status === "done").map((item) => item.data);
    },
    isUploading() {
      return items.some((item) => item.status === "uploading");
    },
    setTypes,
    restore(files) {
      (files || []).forEach((file) => {
        if (!types.includes(file.content_type)) return;
        const item = {
          name: file.filename,
          type: file.content_type,
          status: "done",
          data: file,
          error: null,
          controller: null,
          previewUrl: null,
        };
        buildChip(item);
        items.push(item);
      });
      render();
    },
    clear() {
      items.slice().forEach((item) => remove(item));
    },
  };
}

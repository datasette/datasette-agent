const DEFAULT_READ_BYTES = 64 * 1024;
const MAX_READ_BYTES = 512 * 1024;
const DEFAULT_SCAN_BYTES = 128 * 1024;
const MAX_SCAN_BYTES = 256 * 1024;
const DEFAULT_MAX_RESULTS = 100;
const MAX_LIST_RESULTS = 1000;
const DEFAULT_MAX_MATCHES = 20;
const MAX_MATCHES = 100;

const filesByPath = new Map();
let selectedAt = null;

function clampInteger(value, fallback, min, max) {
  const parsed = Number.parseInt(value, 10);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.max(min, Math.min(max, parsed));
}

function normalizePrefix(prefix) {
  return String(prefix || "").replace(/^\/+/, "");
}

function filePath(file) {
  return file.webkitRelativePath || file.name;
}

function fileMetadata(path, file) {
  return {
    path,
    size: file.size,
    type: file.type || "",
    last_modified: file.lastModified || null,
  };
}

function selectedSummary() {
  let totalBytes = 0;
  for (const file of filesByPath.values()) {
    totalBytes += file.size;
  }
  return {
    folder_selected: filesByPath.size > 0,
    file_count: filesByPath.size,
    total_bytes: totalBytes,
    selected_at: selectedAt,
  };
}

function requireFiles() {
  if (!filesByPath.size) {
    return {
      error: (
        "No local folder selected. Use the local files control in the " +
        "chat page to select a folder first."
      ),
    };
  }
  return null;
}

function looksTextLike(path, file) {
  if (file.type && file.type.startsWith("text/")) return true;
  if (file.type === "application/json") return true;
  const lowered = path.toLowerCase();
  return /\.(csv|css|htm|html|js|json|jsonl|log|md|py|rst|sql|svg|ts|tsx|txt|xml|yaml|yml)$/.test(
    lowered,
  );
}

function snippetAround(text, index, queryLength) {
  const radius = 80;
  const start = Math.max(0, index - radius);
  const end = Math.min(text.length, index + queryLength + radius);
  let snippet = text.slice(start, end).replace(/\s+/g, " ").trim();
  if (start > 0) snippet = "..." + snippet;
  if (end < text.length) snippet += "...";
  return snippet;
}

async function localFilesStatus() {
  return selectedSummary();
}

async function localFilesList(args) {
  const missing = requireFiles();
  if (missing) return missing;

  const prefix = normalizePrefix(args.prefix);
  const maxResults = clampInteger(
    args.max_results,
    DEFAULT_MAX_RESULTS,
    1,
    MAX_LIST_RESULTS,
  );
  const files = [];
  let matched = 0;
  const paths = Array.from(filesByPath.keys()).sort();
  for (const path of paths) {
    if (prefix && !path.startsWith(prefix)) continue;
    matched += 1;
    if (files.length < maxResults) {
      files.push(fileMetadata(path, filesByPath.get(path)));
    }
  }
  return {
    ...selectedSummary(),
    prefix,
    matched_count: matched,
    returned_count: files.length,
    truncated: matched > files.length,
    files,
  };
}

async function localFilesReadText(args) {
  const missing = requireFiles();
  if (missing) return missing;

  const path = String(args.path || "");
  const file = filesByPath.get(path);
  if (!file) {
    return { error: "File not found", path };
  }

  const start = clampInteger(args.start, 0, 0, file.size);
  const maxBytes = clampInteger(
    args.max_bytes,
    DEFAULT_READ_BYTES,
    1,
    MAX_READ_BYTES,
  );
  const end = Math.min(file.size, start + maxBytes);
  const blob = file.slice(start, end);
  const text = await blob.text();
  return {
    path,
    start,
    bytes_read: blob.size,
    size: file.size,
    truncated: end < file.size,
    text,
  };
}

async function localFilesSearch(args) {
  const missing = requireFiles();
  if (missing) return missing;

  const query = String(args.query || "");
  if (!query) return { error: "query is required" };
  const needle = query.toLowerCase();
  const pathPrefix = normalizePrefix(args.path_prefix);
  const maxMatches = clampInteger(
    args.max_matches,
    DEFAULT_MAX_MATCHES,
    1,
    MAX_MATCHES,
  );
  const maxBytesPerFile = clampInteger(
    args.max_bytes_per_file,
    DEFAULT_SCAN_BYTES,
    1,
    MAX_SCAN_BYTES,
  );
  const matches = [];
  let scannedFiles = 0;
  let skippedBinary = 0;
  let skippedByPrefix = 0;

  const paths = Array.from(filesByPath.keys()).sort();
  for (const path of paths) {
    if (matches.length >= maxMatches) break;
    if (pathPrefix && !path.startsWith(pathPrefix)) {
      skippedByPrefix += 1;
      continue;
    }
    const file = filesByPath.get(path);
    const lowerPath = path.toLowerCase();
    if (lowerPath.includes(needle)) {
      matches.push({
        path,
        match_type: "path",
        size: file.size,
        snippet: path,
      });
      if (matches.length >= maxMatches) break;
    }
    if (!looksTextLike(path, file)) {
      skippedBinary += 1;
      continue;
    }
    scannedFiles += 1;
    const scanEnd = Math.min(file.size, maxBytesPerFile);
    const text = await file.slice(0, scanEnd).text();
    if (text.includes("\u0000")) {
      skippedBinary += 1;
      continue;
    }
    const index = text.toLowerCase().indexOf(needle);
    if (index !== -1) {
      matches.push({
        path,
        match_type: "content",
        size: file.size,
        scanned_bytes: scanEnd,
        truncated_scan: scanEnd < file.size,
        snippet: snippetAround(text, index, query.length),
      });
    }
  }

  return {
    ...selectedSummary(),
    query,
    path_prefix: pathPrefix,
    scanned_files: scannedFiles,
    skipped_binary: skippedBinary,
    skipped_by_prefix: skippedByPrefix,
    returned_count: matches.length,
    truncated: matches.length >= maxMatches,
    matches,
  };
}

function updateStatus(container) {
  const status = container.querySelector(".agent-local-files-status");
  const forget = container.querySelector(".agent-local-files-forget");
  if (!status || !forget) return;
  if (!filesByPath.size) {
    status.textContent = "No folder selected";
    forget.hidden = true;
    return;
  }
  const summary = selectedSummary();
  status.textContent =
    summary.file_count.toLocaleString() +
    " files, " +
    summary.total_bytes.toLocaleString() +
    " bytes";
  forget.hidden = false;
}

function installLocalFilesPanel() {
  const form = document.getElementById("chat-form");
  if (!form || document.querySelector(".agent-local-files")) return;

  const container = document.createElement("div");
  container.className = "agent-local-files";

  const input = document.createElement("input");
  input.type = "file";
  input.id = "agent-local-files-input";
  input.multiple = true;
  input.webkitdirectory = true;
  input.setAttribute("webkitdirectory", "");
  input.hidden = true;

  const button = document.createElement("button");
  button.type = "button";
  button.className = "agent-local-files-select";
  button.textContent = "Select local folder";
  button.addEventListener("click", () => input.click());

  const status = document.createElement("span");
  status.className = "agent-local-files-status";

  const forget = document.createElement("button");
  forget.type = "button";
  forget.className = "agent-local-files-forget";
  forget.textContent = "Forget";
  forget.addEventListener("click", () => {
    filesByPath.clear();
    selectedAt = null;
    input.value = "";
    updateStatus(container);
  });

  input.addEventListener("change", () => {
    filesByPath.clear();
    for (const file of input.files || []) {
      filesByPath.set(filePath(file), file);
    }
    selectedAt = filesByPath.size ? new Date().toISOString() : null;
    updateStatus(container);
  });

  container.append(input, button, status, forget);
  form.insertAdjacentElement("beforebegin", container);
  updateStatus(container);
}

if (!window.datasetteAgent || !window.datasetteAgent.registerTool) {
  throw new Error("datasetteAgent browser tool bridge is not available");
}

window.datasetteAgent.registerTool("local_files_status", localFilesStatus);
window.datasetteAgent.registerTool("local_files_list", localFilesList);
window.datasetteAgent.registerTool("local_files_read_text", localFilesReadText);
window.datasetteAgent.registerTool("local_files_search", localFilesSearch);

installLocalFilesPanel();

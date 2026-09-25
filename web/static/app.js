const state = {
  bookId: null,
  filename: null,
  structure: null,
  processed: new Set(),
  selected: new Set(), // subchapter numbers
};

const el = (id) => document.getElementById(id);

function showError(msg) {
  const banner = el("error-banner");
  banner.textContent = msg;
  banner.classList.remove("hidden");
}

function clearError() {
  el("error-banner").classList.add("hidden");
}

// ---------- Upload ----------

const dropzone = el("dropzone");
const fileInput = el("file-input");

dropzone.addEventListener("click", () => fileInput.click());
dropzone.addEventListener("dragover", (e) => {
  e.preventDefault();
  dropzone.classList.add("drag-over");
});
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("drag-over"));
dropzone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropzone.classList.remove("drag-over");
  const file = e.dataTransfer.files[0];
  if (file) handleFile(file);
});
fileInput.addEventListener("change", () => {
  if (fileInput.files[0]) handleFile(fileInput.files[0]);
});

async function handleFile(file) {
  clearError();
  if (!file.name.toLowerCase().endsWith(".pdf")) {
    showError("Please upload a PDF file.");
    return;
  }
  const form = new FormData();
  form.append("file", file);

  el("upload-section").classList.add("hidden");
  showLoading("Uploading…", file.name);

  try {
    const res = await fetch("/api/upload", { method: "POST", body: form });
    if (!res.ok) throw new Error(`Upload failed (${res.status})`);
    const data = await res.json();
    rememberBook(data.book_id, data.filename);
    await loadBook(data.book_id, data.filename);
  } catch (err) {
    el("upload-section").classList.remove("hidden");
    hideLoading();
    showError(err.message);
  }
}

// ---------- Recently used books (localStorage) ----------

function rememberBook(bookId, filename) {
  let books = [];
  try { books = JSON.parse(localStorage.getItem("recentBooks") || "[]"); } catch { books = []; }
  books = books.filter((b) => b.book_id !== bookId);
  books.unshift({ book_id: bookId, filename });
  localStorage.setItem("recentBooks", JSON.stringify(books.slice(0, 8)));
}

function renderRecentBooks() {
  let books = [];
  try { books = JSON.parse(localStorage.getItem("recentBooks") || "[]"); } catch { books = []; }
  if (!books.length) return;
  el("recent-books").classList.remove("hidden");
  const list = el("book-chip-list");
  list.innerHTML = "";
  for (const b of books) {
    const chip = document.createElement("div");
    chip.className = "book-chip";
    chip.textContent = b.filename;
    chip.onclick = () => {
      clearError();
      el("upload-section").classList.add("hidden");
      showLoading("Loading…", b.filename);
      loadBook(b.book_id, b.filename).catch((err) => {
        el("upload-section").classList.remove("hidden");
        hideLoading();
        showError(err.message);
      });
    };
    list.appendChild(chip);
  }
}

// ---------- Loading state ----------

function showLoading(label, sub) {
  el("loading-section").classList.remove("hidden");
  el("loading-label").textContent = label;
  el("loading-sub").textContent = sub || "This can take a while for scanned PDFs (OCR fallback).";
}
function hideLoading() {
  el("loading-section").classList.add("hidden");
}

// ---------- Parse flow ----------

async function loadBook(bookId, filename) {
  state.bookId = bookId;
  state.filename = filename;
  showLoading("Parsing structure…", filename);

  const manifestRes = await fetch(`/api/manifest/${bookId}`);
  const manifestData = await manifestRes.json();
  state.processed = new Set(manifestData.processed || []);

  const startRes = await fetch(`/api/parse/${bookId}`, { method: "POST" });
  if (!startRes.ok) throw new Error("Couldn't start parsing.");
  const { job_id } = await startRes.json();

  await pollParseJob(job_id);
}

function pollParseJob(jobId) {
  return new Promise((resolve, reject) => {
    const tick = async () => {
      const res = await fetch(`/api/parse/status/${jobId}`);
      const job = await res.json();
      if (job.status === "running") {
        setTimeout(tick, 1200);
      } else if (job.status === "done") {
        state.structure = job.structure;
        hideLoading();
        renderStructure();
        resolve();
      } else {
        reject(new Error(job.error || "Parsing failed."));
      }
    };
    tick();
  });
}

// ---------- Structure rendering ----------

function realChapterNumber(chapter) {
  const m = /^chapter\s+(\d+)/i.exec(chapter.title.trim()) || /^(\d{1,3})\.?\s+(\S.*)/.exec(chapter.title.trim());
  return m ? m[1] : chapter.number;
}

function renderStructure() {
  const s = state.structure;
  el("book-title").textContent = s.title;
  el("book-meta").innerHTML = `${s.total_pages} pages &nbsp;<span class="method-badge">${s.method}</span>`;

  const scannedNotice = el("scanned-notice");
  if (s.scanned_pages && s.scanned_pages.length) {
    scannedNotice.classList.remove("hidden");
    scannedNotice.textContent = `${s.scanned_pages.length} page(s) had no extractable text and were OCR'd.`;
  } else {
    scannedNotice.classList.add("hidden");
  }

  const ambiguousNotice = el("ambiguous-notice");
  if (s.ambiguous && s.ambiguous.length) {
    ambiguousNotice.classList.remove("hidden");
    ambiguousNotice.innerHTML = `<details class="notice warn"><summary>${s.ambiguous.length} item(s) flagged as ambiguous — click to review</summary>
      <ul>${s.ambiguous.map(a => `<li>p.${JSON.stringify(a.page)}: ${a.reason}${a.title ? ` (${a.title})` : ""}</li>`).join("")}</ul>
    </details>`;
  } else {
    ambiguousNotice.classList.add("hidden");
    ambiguousNotice.innerHTML = "";
  }

  const tree = el("chapter-tree");
  tree.innerHTML = "";

  for (const chapter of s.chapters) {
    const chapterEl = document.createElement("div");
    chapterEl.className = "chapter";
    chapterEl.dataset.numbered = chapter.is_numbered ? "1" : "0";

    const allSubNums = chapter.subchapters.map((sc) => sc.number);
    const allProcessed = allSubNums.length && allSubNums.every((n) => state.processed.has(n));

    const head = document.createElement("div");
    head.className = "chapter-head";
    const confFlag = chapter.confidence !== "high" ? `<span class="low-confidence">LOW CONFIDENCE</span>` : "";
    head.innerHTML = `
      <span class="chevron">▶</span>
      <input type="checkbox" class="chapter-checkbox" />
      <span class="title">${chapter.number}. ${escapeHtml(chapter.title)}${confFlag}</span>
      <span class="pages">p.${chapter.page_start}-${chapter.page_end}</span>
    `;
    const chapterCheckbox = head.querySelector(".chapter-checkbox");

    head.addEventListener("click", (e) => {
      if (e.target === chapterCheckbox) return;
      chapterEl.classList.toggle("expanded");
    });

    const subList = document.createElement("div");
    subList.className = "subchapter-list";

    for (const sub of chapter.subchapters) {
      const row = document.createElement("div");
      const isProcessed = state.processed.has(sub.number);
      row.className = "subchapter-row" + (isProcessed ? " processed" : "");
      const sconf = sub.confidence !== "high" ? `<span class="low-confidence">LOW CONF</span>` : "";
      row.innerHTML = `
        <input type="checkbox" class="sub-checkbox" data-num="${sub.number}" />
        <span class="title">${sub.number} ${escapeHtml(sub.title)}${sconf}</span>
        ${isProcessed ? '<span class="processed-badge">✓ noted</span>' : ""}
        <span class="pages">p.${sub.page_start}-${sub.page_end}</span>
      `;
      const cb = row.querySelector(".sub-checkbox");
      cb.addEventListener("change", () => {
        toggleSubchapter(sub.number, cb.checked);
        syncChapterCheckbox(chapterEl, allSubNums);
      });
      subList.appendChild(row);
    }

    chapterCheckbox.addEventListener("change", () => {
      const checked = chapterCheckbox.checked;
      for (const num of allSubNums) toggleSubchapter(num, checked);
      subList.querySelectorAll(".sub-checkbox").forEach((cb) => (cb.checked = checked));
      updateSelectionBar();
    });

    chapterEl.appendChild(head);
    chapterEl.appendChild(subList);
    tree.appendChild(chapterEl);
  }

  el("structure-section").classList.remove("hidden");
  el("selection-bar").classList.remove("hidden");
  updateSelectionBar();
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

function toggleSubchapter(num, on) {
  if (on) state.selected.add(num);
  else state.selected.delete(num);
  updateSelectionBar();
}

function syncChapterCheckbox(chapterEl, allSubNums) {
  const cb = chapterEl.querySelector(".chapter-checkbox");
  const selectedCount = allSubNums.filter((n) => state.selected.has(n)).length;
  cb.checked = selectedCount === allSubNums.length && allSubNums.length > 0;
  cb.indeterminate = selectedCount > 0 && selectedCount < allSubNums.length;
}

function updateSelectionBar() {
  el("selection-count").textContent = state.selected.size;
  el("continue-btn").disabled = state.selected.size === 0;
}

// ---------- Toolbar ----------

el("select-all").addEventListener("click", () => {
  document.querySelectorAll(".sub-checkbox").forEach((cb) => {
    cb.checked = true;
    state.selected.add(cb.dataset.num);
  });
  document.querySelectorAll(".chapter-checkbox").forEach((cb) => { cb.checked = true; cb.indeterminate = false; });
  updateSelectionBar();
});

el("select-real-chapters").addEventListener("click", () => {
  state.selected.clear();
  document.querySelectorAll(".chapter").forEach((chapterEl) => {
    const numbered = chapterEl.dataset.numbered === "1";
    chapterEl.querySelectorAll(".sub-checkbox").forEach((cb) => {
      cb.checked = numbered;
      if (numbered) state.selected.add(cb.dataset.num);
    });
    const nums = [...chapterEl.querySelectorAll(".sub-checkbox")].map((cb) => cb.dataset.num);
    syncChapterCheckbox(chapterEl, nums);
  });
  updateSelectionBar();
});

el("select-none").addEventListener("click", () => {
  state.selected.clear();
  document.querySelectorAll(".sub-checkbox").forEach((cb) => (cb.checked = false));
  document.querySelectorAll(".chapter-checkbox").forEach((cb) => { cb.checked = false; cb.indeterminate = false; });
  updateSelectionBar();
});

el("select-new").addEventListener("click", () => {
  state.selected.clear();
  document.querySelectorAll(".sub-checkbox").forEach((cb) => {
    const isNew = !state.processed.has(cb.dataset.num);
    cb.checked = isNew;
    if (isNew) state.selected.add(cb.dataset.num);
  });
  document.querySelectorAll(".chapter").forEach((chapterEl) => {
    const nums = [...chapterEl.querySelectorAll(".sub-checkbox")].map((cb) => cb.dataset.num);
    syncChapterCheckbox(chapterEl, nums);
  });
  updateSelectionBar();
});

el("continue-btn").addEventListener("click", () => {
  el("structure-section").classList.add("hidden");
  el("selection-bar").classList.add("hidden");
  el("generate-section").classList.remove("hidden");
  el("subject-input").focus();
});

el("back-to-picker").addEventListener("click", () => {
  el("generate-section").classList.add("hidden");
  el("structure-section").classList.remove("hidden");
  el("selection-bar").classList.remove("hidden");
});

el("back-to-library").addEventListener("click", () => {
  clearError();
  state.bookId = null;
  state.filename = null;
  state.structure = null;
  state.processed = new Set();
  state.selected = new Set();
  el("structure-section").classList.add("hidden");
  el("selection-bar").classList.add("hidden");
  el("generate-section").classList.add("hidden");
  el("progress-section").classList.add("hidden");
  el("upload-section").classList.remove("hidden");
  renderRecentBooks();
});

// ---------- Generation settings ----------

let genMode = "notes";
let config = { default_vault: "", default_model: "", default_lmstudio_url: "", default_timeout: 180 };

async function loadConfig() {
  try {
    const res = await fetch("/api/config");
    config = await res.json();
    el("vault-input").placeholder = config.default_vault;
    el("lmstudio-url-input").placeholder = config.default_lmstudio_url;
    el("model-input").placeholder = config.default_model;
    el("timeout-input").value = config.default_timeout;
  } catch { /* keep placeholders empty if server config isn't reachable yet */ }
}
loadConfig();

document.querySelectorAll(".mode-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".mode-tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    genMode = tab.dataset.mode;
    el("quiz-fields").classList.toggle("hidden", genMode !== "quiz");
    el("subject-field").classList.toggle("hidden", genMode === "flashcards");
    el("advanced-settings").classList.toggle("hidden", genMode === "flashcards");
    el("flashcards-notice").classList.toggle("hidden", genMode !== "flashcards");
    el("start-generate-btn").textContent =
      genMode === "quiz" ? "Generate Quiz →" : genMode === "flashcards" ? "Generate Flashcards →" : "Start Generating →";
  });
});

function gatherGenerationSettings() {
  return {
    book_id: state.bookId,
    selection: [...state.selected],
    subject: el("subject-input").value.trim(),
    vault: el("vault-input").value.trim() || undefined,
    lmstudio_url: el("lmstudio-url-input").value.trim() || undefined,
    model: el("model-input").value.trim() || undefined,
    timeout: el("timeout-input").value ? Number(el("timeout-input").value) : undefined,
    max_tokens: el("max-tokens-input").value ? Number(el("max-tokens-input").value) : undefined,
  };
}

el("start-generate-btn").addEventListener("click", async () => {
  clearError();
  const settings = gatherGenerationSettings();
  if (genMode !== "flashcards" && !settings.subject) {
    showError("Subject tag is required.");
    el("subject-input").focus();
    return;
  }

  el("generate-section").classList.add("hidden");
  el("progress-section").classList.remove("hidden");
  el("progress-title").textContent =
    genMode === "quiz" ? "Generating quiz…" : genMode === "flashcards" ? "Generating flashcards…" : "Generating notes…";
  el("progress-stats").textContent = `${settings.selection.length} subchapter(s)`;
  el("progress-log").innerHTML = "";
  el("progress-summary").classList.add("hidden");
  el("back-to-start").classList.add("hidden");

  if (genMode === "quiz") {
    settings.difficulty = el("difficulty-input").value;
    settings.num_questions = Number(el("num-questions-input").value) || 8;
    await runQuizGeneration(settings);
  } else if (genMode === "flashcards") {
    await runFlashcardsGeneration({ book_id: settings.book_id, selection: settings.selection, vault: settings.vault });
  } else {
    await runNotesGeneration(settings);
  }
});

// ---------- Cross-link vault (utility, not tied to a book's selection) ----------

function showToast(message, isError) {
  const existing = document.querySelector(".toast");
  if (existing) existing.remove();
  const toast = document.createElement("div");
  toast.className = "toast" + (isError ? " error" : "");
  toast.textContent = message;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 6000);
}

el("crosslink-btn").addEventListener("click", async () => {
  const btn = el("crosslink-btn");
  const vaultPath = el("vault-input").value.trim() || undefined;
  btn.disabled = true;
  btn.textContent = "Cross-linking…";
  try {
    const res = await fetch("/api/crosslink/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ vault: vaultPath }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
    showToast(`Scanned ${data.notes_scanned} note(s) — ${data.notes_linked} updated with cross-book links `
      + `(${data.terms_matched} shared term(s) across 2+ books).`);
  } catch (err) {
    showToast(err.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = "🔗 Cross-link Vault";
  }
});

// ---------- Active-job persistence (survives a page refresh) ----------

const ACTIVE_JOB_KEY = "activeGenerationJob";

function saveActiveJob(jobId, mode) {
  localStorage.setItem(ACTIVE_JOB_KEY, JSON.stringify({ jobId, mode, bookId: state.bookId, filename: state.filename }));
}

function clearActiveJob() {
  localStorage.removeItem(ACTIVE_JOB_KEY);
}

function loadActiveJob() {
  try { return JSON.parse(localStorage.getItem(ACTIVE_JOB_KEY) || "null"); } catch { return null; }
}

// ---------- Notes generation (SSE) ----------

async function runNotesGeneration(settings) {
  let res;
  try {
    res = await fetch("/api/notes/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(settings),
    });
  } catch (err) {
    showGenerationFatalError(err.message);
    return;
  }
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    showGenerationFatalError(data.error || `Request failed (${res.status})`);
    return;
  }
  const { job_id } = await res.json();
  saveActiveJob(job_id, "notes");
  el("progress-stats").textContent = `${settings.selection.length} subchapter(s)`;
  attachNotesStream(job_id);
}

function attachNotesStream(job_id) {
  const log = el("progress-log");
  let currentChapterEl = null;
  let currentRow = null;
  let generated = 0;
  let sawAnyEvent = false;
  const failedList = [];

  const src = new EventSource(`/api/notes/generate/stream/${job_id}`);
  src.onmessage = (e) => {
    sawAnyEvent = true;
    const event = JSON.parse(e.data);
    switch (event.type) {
      case "chapter_start": {
        currentChapterEl = document.createElement("div");
        currentChapterEl.className = "progress-chapter";
        currentChapterEl.textContent = event.label;
        log.appendChild(currentChapterEl);
        break;
      }
      case "subchapter_start": {
        currentRow = document.createElement("div");
        currentRow.className = "progress-row";
        currentRow.innerHTML = `<span class="status-icon working"></span><span class="title">${event.number} ${escapeHtml(event.title)}</span><span class="detail"></span>`;
        log.appendChild(currentRow);
        log.scrollTop = log.scrollHeight;
        break;
      }
      case "retry": {
        if (currentRow) {
          const icon = currentRow.querySelector(".status-icon");
          icon.className = "status-icon retry";
          icon.textContent = "↻";
          const detail = currentRow.querySelector(".detail");
          const label = event.reason === "token_limit" ? "hit token limit" : "timed out";
          detail.textContent = `${label}, retrying (${event.attempt}/3)…`;
        }
        break;
      }
      case "subchapter_done": {
        generated++;
        if (currentRow) {
          const icon = currentRow.querySelector(".status-icon");
          icon.className = "status-icon success";
          icon.textContent = "✓";
          currentRow.querySelector(".detail").textContent = "";
          currentRow.classList.add("linkable");
          currentRow.querySelector(".title").addEventListener("click", () => openPreview(event.path));
        }
        el("progress-stats").textContent = `${generated} generated`;
        break;
      }
      case "subchapter_failed": {
        failedList.push(event.number);
        if (currentRow) {
          const icon = currentRow.querySelector(".status-icon");
          icon.className = "status-icon failed";
          icon.textContent = "✕";
          currentRow.classList.add("failed-row");
          currentRow.querySelector(".detail").textContent = "failed after 3 attempts";
        }
        break;
      }
      case "connection_lost": {
        showGenerationFatalError(event.message);
        break;
      }
      case "done": {
        renderNotesSummary(event);
        break;
      }
      case "fatal_error": {
        showGenerationFatalError(event.error);
        break;
      }
      case "stream_end": {
        src.close();
        clearActiveJob();
        el("back-to-start").classList.remove("hidden");
        break;
      }
    }
  };
  src.onerror = () => {
    if (!sawAnyEvent) {
      // The job doesn't exist server-side (likely the server restarted) —
      // give up instead of letting the browser retry a dead job forever.
      src.close();
      clearActiveJob();
      showGenerationFatalError("Couldn't find that generation job (the server may have restarted). Start a new one below.");
    }
    // Otherwise leave it: EventSource auto-retries, and the server replays
    // everything published so far, so a transient drop recovers on its own.
  };
}

function renderNotesSummary(event) {
  const summary = el("progress-summary");
  summary.classList.remove("hidden");
  if (event.failed && event.failed.length) summary.classList.add("has-failures");
  summary.innerHTML = `
    <div><span class="big-stat">${event.generated}</span> note(s) generated</div>
    ${event.failed && event.failed.length ? `<div style="margin-top:6px;">${event.failed.length} failed: ${event.failed.join(", ")} — re-run to retry just these.</div>` : ""}
    ${event.connection_lost ? `<div style="margin-top:6px;color:var(--danger);">LM Studio became unreachable — stopped early.</div>` : ""}
  `;
  el("progress-title").textContent = event.failed && event.failed.length ? "Done (with some failures)" : "Done!";
  el("back-to-start").classList.remove("hidden");
}

// ---------- Quiz generation (SSE) ----------

async function runQuizGeneration(settings) {
  let res;
  try {
    res = await fetch("/api/quiz/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(settings),
    });
  } catch (err) {
    showGenerationFatalError(err.message);
    return;
  }
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    showGenerationFatalError(data.error || `Request failed (${res.status})`);
    return;
  }
  const { job_id } = await res.json();
  saveActiveJob(job_id, "quiz");
  attachQuizStream(job_id);
}

function attachQuizStream(job_id) {
  const log = el("progress-log");
  let row = null;
  let sawAnyEvent = false;

  const src = new EventSource(`/api/quiz/generate/stream/${job_id}`);
  src.onmessage = (e) => {
    sawAnyEvent = true;
    const event = JSON.parse(e.data);
    switch (event.type) {
      case "generating": {
        row = document.createElement("div");
        row.className = "progress-row";
        row.innerHTML = `<span class="status-icon working"></span><span class="title">${event.num_questions}-question ${event.difficulty} quiz</span><span class="detail"></span>`;
        log.appendChild(row);
        break;
      }
      case "retry": {
        if (!row) break;
        const icon = row.querySelector(".status-icon");
        icon.className = "status-icon retry";
        icon.textContent = "↻";
        const label = event.reason === "token_limit" ? "hit token limit" : "timed out";
        row.querySelector(".detail").textContent = `${label}, retrying (${event.attempt}/3)…`;
        break;
      }
      case "done": {
        const icon = row.querySelector(".status-icon");
        icon.className = "status-icon success";
        icon.textContent = "✓";
        row.querySelector(".detail").textContent = "";
        row.classList.add("linkable");
        row.querySelector(".title").addEventListener("click", () => openPreview(event.path));

        const summary = el("progress-summary");
        summary.classList.remove("hidden");
        if (event.delivered < event.requested) summary.classList.add("has-failures");
        summary.innerHTML = `<div><span class="big-stat">${event.delivered}</span> / ${event.requested} question(s) delivered</div>`;
        el("progress-title").textContent = "Done!";
        break;
      }
      case "fatal_error": {
        showGenerationFatalError(event.error);
        break;
      }
      case "stream_end": {
        src.close();
        clearActiveJob();
        el("back-to-start").classList.remove("hidden");
        break;
      }
    }
  };
  src.onerror = () => {
    if (!sawAnyEvent) {
      src.close();
      clearActiveJob();
      showGenerationFatalError("Couldn't find that generation job (the server may have restarted). Start a new one below.");
    }
  };
}

// ---------- Flashcards generation (SSE) ----------

async function runFlashcardsGeneration(settings) {
  let res;
  try {
    res = await fetch("/api/flashcards/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(settings),
    });
  } catch (err) {
    showGenerationFatalError(err.message);
    return;
  }
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    showGenerationFatalError(data.error || `Request failed (${res.status})`);
    return;
  }
  const { job_id } = await res.json();
  saveActiveJob(job_id, "flashcards");
  el("progress-stats").textContent = `${settings.selection.length} subchapter(s)`;
  attachFlashcardsStream(job_id);
}

function attachFlashcardsStream(job_id) {
  const log = el("progress-log");
  let currentChapterEl = null;
  let currentRow = null;
  let generated = 0;
  let sawAnyEvent = false;

  const src = new EventSource(`/api/flashcards/generate/stream/${job_id}`);
  src.onmessage = (e) => {
    sawAnyEvent = true;
    const event = JSON.parse(e.data);
    switch (event.type) {
      case "chapter_start": {
        currentChapterEl = document.createElement("div");
        currentChapterEl.className = "progress-chapter";
        currentChapterEl.textContent = event.label;
        log.appendChild(currentChapterEl);
        break;
      }
      case "subchapter_start": {
        currentRow = document.createElement("div");
        currentRow.className = "progress-row";
        currentRow.innerHTML = `<span class="status-icon working"></span><span class="title">${event.number} ${escapeHtml(event.title)}</span><span class="detail"></span>`;
        log.appendChild(currentRow);
        log.scrollTop = log.scrollHeight;
        break;
      }
      case "subchapter_done": {
        generated++;
        if (currentRow) {
          const icon = currentRow.querySelector(".status-icon");
          icon.className = "status-icon success";
          icon.textContent = "✓";
          currentRow.querySelector(".detail").textContent = "";
          currentRow.classList.add("linkable");
          currentRow.querySelector(".title").addEventListener("click", () => openPreview(event.path));
        }
        el("progress-stats").textContent = `${generated} generated`;
        break;
      }
      case "subchapter_skipped": {
        if (currentRow) {
          const icon = currentRow.querySelector(".status-icon");
          icon.className = "status-icon retry";
          icon.textContent = "–";
          currentRow.querySelector(".detail").textContent =
            event.reason === "no_note" ? "no note yet, skipped" : "no Key Concepts, skipped";
        }
        break;
      }
      case "done": {
        const summary = el("progress-summary");
        summary.classList.remove("hidden");
        const skippedNoNote = event.skipped_no_note || [];
        const skippedNoConcepts = event.skipped_no_concepts || [];
        if (skippedNoNote.length || skippedNoConcepts.length) summary.classList.add("has-failures");
        summary.innerHTML = `
          <div><span class="big-stat">${event.generated}</span> flashcard file(s) generated</div>
          ${skippedNoNote.length ? `<div style="margin-top:6px;">${skippedNoNote.length} skipped (no note yet): ${skippedNoNote.join(", ")} — run Notes generation first.</div>` : ""}
          ${skippedNoConcepts.length ? `<div style="margin-top:6px;">${skippedNoConcepts.length} skipped (no Key Concepts): ${skippedNoConcepts.join(", ")}</div>` : ""}
        `;
        el("progress-title").textContent = skippedNoNote.length || skippedNoConcepts.length ? "Done (some skipped)" : "Done!";
        el("back-to-start").classList.remove("hidden");
        break;
      }
      case "fatal_error": {
        showGenerationFatalError(event.error);
        break;
      }
      case "stream_end": {
        src.close();
        clearActiveJob();
        el("back-to-start").classList.remove("hidden");
        break;
      }
    }
  };
  src.onerror = () => {
    if (!sawAnyEvent) {
      src.close();
      clearActiveJob();
      showGenerationFatalError("Couldn't find that generation job (the server may have restarted). Start a new one below.");
    }
  };
}

function showGenerationFatalError(message) {
  const summary = el("progress-summary");
  summary.classList.remove("hidden");
  summary.classList.add("has-failures");
  summary.innerHTML = `<div style="color:var(--danger);">${escapeHtml(message)}</div>`;
  el("progress-title").textContent = "Failed";
  el("back-to-start").classList.remove("hidden");
}

el("back-to-start").addEventListener("click", () => {
  el("progress-section").classList.add("hidden");
  el("generate-section").classList.remove("hidden");
});

// ---------- Preview modal ----------

async function openPreview(path) {
  try {
    const res = await fetch(`/api/preview?path=${encodeURIComponent(path)}`);
    const data = await res.json();
    el("preview-title").textContent = path.split("/").pop();
    el("preview-content").textContent = data.content || data.error || "(empty)";
    el("preview-modal").classList.remove("hidden");
  } catch (err) {
    showError("Couldn't load preview: " + err.message);
  }
}

el("preview-close").addEventListener("click", () => el("preview-modal").classList.add("hidden"));
el("preview-modal").addEventListener("click", (e) => {
  if (e.target.id === "preview-modal") el("preview-modal").classList.add("hidden");
});

// ---------- Reconnect to a job still running after a page refresh ----------

function tryReconnectActiveJob() {
  const active = loadActiveJob();
  if (!active) return false;

  state.bookId = active.bookId;
  state.filename = active.filename;

  el("upload-section").classList.add("hidden");
  el("progress-section").classList.remove("hidden");
  el("progress-title").textContent =
    active.mode === "quiz" ? "Generating quiz…" : active.mode === "flashcards" ? "Generating flashcards…" : "Generating notes…";
  el("progress-stats").textContent = "reconnected";
  el("progress-log").innerHTML = "";
  el("progress-summary").classList.add("hidden");
  el("back-to-start").classList.add("hidden");

  if (active.mode === "quiz") {
    attachQuizStream(active.jobId);
  } else if (active.mode === "flashcards") {
    attachFlashcardsStream(active.jobId);
  } else {
    attachNotesStream(active.jobId);
  }
  return true;
}

if (!tryReconnectActiveJob()) {
  renderRecentBooks();
}

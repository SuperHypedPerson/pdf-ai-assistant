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

  if (s.scanned_pages && s.scanned_pages.length) {
    const notice = el("scanned-notice");
    notice.classList.remove("hidden");
    notice.textContent = `${s.scanned_pages.length} page(s) had no extractable text and were OCR'd.`;
  }

  if (s.ambiguous && s.ambiguous.length) {
    const wrap = el("ambiguous-notice");
    wrap.classList.remove("hidden");
    wrap.innerHTML = `<details class="notice warn"><summary>${s.ambiguous.length} item(s) flagged as ambiguous — click to review</summary>
      <ul>${s.ambiguous.map(a => `<li>p.${JSON.stringify(a.page)}: ${a.reason}${a.title ? ` (${a.title})` : ""}</li>`).join("")}</ul>
    </details>`;
  }

  const tree = el("chapter-tree");
  tree.innerHTML = "";

  for (const chapter of s.chapters) {
    const chapterEl = document.createElement("div");
    chapterEl.className = "chapter";

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
  // Stage W2 (note/quiz generation UI) plugs in here.
  alert(`${state.selected.size} subchapter(s) selected. Generation UI is the next stage — not built yet.`);
});

renderRecentBooks();

// LexiGo 前端交互

const TYPE_LABEL = { word: "单词", phrase: "词组", sentence: "句型" };
const DIR_LABEL = { zh2en: "中译英", en2zh: "英译中" };
const ORDER_LABEL = { sequence: "正序", shuffle: "乱序", reverse: "倒序" };
const ORDER_CYCLE = ["sequence", "shuffle", "reverse"];
const STATE_LABEL = {
  new: "未学",
  learning: "学习中",
  review: "复习中",
  mastered: "已掌握",
  suspended: "已搁置",
};

const views = {
  home: document.getElementById("view-home"),
  library: document.getElementById("view-library"),
  dashboard: document.getElementById("view-dashboard"),
  cram: document.getElementById("view-cram"),
  study: document.getElementById("view-study"),
  finish: document.getElementById("view-finish"),
};

let flipped = false;
let cardShownAt = Date.now();
let ratingPending = false;
let studyState = null;

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

// ---------- 导航 ----------

function switchView(name) {
  Object.entries(views).forEach(([k, el]) =>
    el.classList.toggle("hidden", k !== name)
  );
  document.querySelectorAll(".nav-link").forEach((b) =>
    b.classList.toggle("active", b.dataset.view === name)
  );
  if (name === "library") loadLibrary();
  if (name === "dashboard") loadDashboard();
  if (name === "home") loadStatus();
}

function enterStudy() {
  Object.entries(views).forEach(([k, el]) =>
    el.classList.toggle("hidden", k !== "study")
  );
}

document.querySelectorAll(".nav-link").forEach((btn) => {
  btn.addEventListener("click", () => switchView(btn.dataset.view));
});

// ---------- 首页 ----------

async function loadStatus() {
  const s = await (await fetch("/api/status")).json();

  document.getElementById("stat-total").textContent = s.overall.total;
  document.getElementById("stat-learned").textContent = s.overall.learned;
  document.getElementById("stat-streak").textContent = s.streak;
  document.getElementById("num-new").textContent = s.new_waiting;
  document.getElementById("num-review").textContent = s.review_due;

  renderBatchProgress(s.batches);

  const btn = document.getElementById("btn-start");
  const note = document.getElementById("home-note");
  btn.disabled = false;
  note.textContent = "";

  const pending = s.new_waiting + s.review_due;
  if (s.session) {
    btn.textContent = "继续学习";
  } else if (pending > 0) {
    btn.textContent = "开始学习";
  } else {
    btn.disabled = true;
    btn.textContent = "当前全部完成";
    note.textContent = "词库已全部安排，等待后续到期。";
  }
}

function renderBatchProgress(batches) {
  const el = document.getElementById("batch-progress");
  if (!batches || batches.length === 0) {
    el.innerHTML = "";
    return;
  }
  el.innerHTML = batches.map((b) => {
    const pct = b.total ? Math.round((b.done / b.total) * 100) : 0;
    const finished = b.new_waiting === 0;
    const cls = finished ? "batch-row is-finished" : "batch-row clickable";
    const hint = finished ? "没有待学新词；到期复习请点击开始学习" : "点击继续学习这个批次";
    const mode = b.order_mode || "sequence";
    return `
      <div class="${cls}" data-batch-id="${b.id}" title="${hint}">
        <div class="batch-head">
          <span class="batch-name">${escapeHtml(b.name)}</span>
          <button class="order-toggle" data-order-mode="${mode}"
            title="点击切换背诵顺序：正序 → 乱序 → 倒序">${ORDER_LABEL[mode] || "正序"}</button>
          <span class="batch-count">${b.done} / ${b.total}</span>
        </div>
        <div class="batch-bar"><div style="width:${pct}%"></div></div>
      </div>`;
  }).join("");
}

// 批次网格点击：切换顺序按钮 或 进入该批次学习（事件委托，兼容动态渲染）
document.getElementById("batch-progress").addEventListener("click", async (ev) => {
  const toggle = ev.target.closest(".order-toggle");
  if (toggle) {
    ev.stopPropagation();
    const row = toggle.closest(".batch-row");
    const batchId = parseInt(row.getAttribute("data-batch-id"), 10);
    const current = toggle.getAttribute("data-order-mode");
    const next = ORDER_CYCLE[(ORDER_CYCLE.indexOf(current) + 1) % ORDER_CYCLE.length];
    const res = await fetch(`/api/batches/${batchId}/order-mode`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ order_mode: next }),
    });
    if (res.ok) {
      toggle.setAttribute("data-order-mode", next);
      toggle.textContent = ORDER_LABEL[next];
    }
    return;
  }

  const row = ev.target.closest(".batch-row.clickable");
  if (!row) return;
  const batchId = row.getAttribute("data-batch-id");
  const res = await fetch("/api/session/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ batch_id: parseInt(batchId, 10) }),
  });
  const data = await res.json();
  enterStudy();
  renderState(data);
});

document.getElementById("btn-start").addEventListener("click", async () => {
  const res = await fetch("/api/session/start", { method: "POST" });
  const data = await res.json();
  enterStudy();
  renderState(data);
});

document.getElementById("btn-home").addEventListener("click", () => {
  switchView("home");
});

// 学习页左上角返回：评分已实时落库，直接回首页即可，进度不丢
document.getElementById("btn-exit-study").addEventListener("click", () => {
  switchView("home");
});

// ---------- 学习页 ----------

function renderState(data) {
  if (data.done) {
    studyState = null;
    renderFinish(data.summary);
    return;
  }

  const c = data.card;
  studyState = data;
  document.getElementById("study-error").textContent = "";
  flipped = false;
  cardShownAt = Date.now();

  // 顶部信息
  document.getElementById("stage-label").textContent = data.stage_label;
  document.getElementById("stage-count").textContent =
    `本轮剩余 ${data.queue_left}`;

  const base = data.base_total || 1;
  const pct = Math.min(100, Math.round((data.rated_count / base) * 100));
  document.getElementById("progress-fill").style.width = pct + "%";

  // 徽章
  const badgeType = document.getElementById("badge-type");
  badgeType.textContent = TYPE_LABEL[c.item_type] || "词组";
  const badgeTag = document.getElementById("badge-tag");
  if (c.tag) {
    badgeTag.textContent = c.tag;
    badgeTag.classList.remove("hidden");
  } else {
    badgeTag.classList.add("hidden");
  }
  document.getElementById("badge-dir").textContent =
    DIR_LABEL[c.direction] || c.direction;

  // 正面：只显示题目侧
  const frontHint = document.getElementById("front-hint");
  const frontText = document.getElementById("front-text");
  const frontPhonetic = document.getElementById("front-phonetic");
  if (c.direction === "zh2en") {
    frontHint.textContent = "看中文，回忆英文";
    frontText.textContent = c.zh;
    frontPhonetic.classList.add("hidden");
  } else {
    frontHint.textContent = "看英文，回忆中文";
    frontText.textContent = c.en;
    if (c.phonetic) {
      frontPhonetic.textContent = c.phonetic;
      frontPhonetic.classList.remove("hidden");
    } else {
      frontPhonetic.classList.add("hidden");
    }
  }

  // 背面：只显示答案侧
  document.getElementById("back-en").textContent = c.en;
  document.getElementById("back-zh").textContent = c.zh;
  document.getElementById("back-phonetic").textContent = c.phonetic || "";
  document.getElementById("back-line-en").classList.toggle(
    "hidden", c.direction !== "zh2en"
  );
  document.getElementById("back-line-zh").classList.toggle(
    "hidden", c.direction !== "en2zh"
  );

  // 词性只随英文答案出现
  const posEl = document.getElementById("back-pos");
  if (c.pos && c.direction === "zh2en") {
    posEl.textContent = c.pos;
    posEl.classList.remove("hidden");
  } else {
    posEl.classList.add("hidden");
  }

  const noteEl = document.getElementById("back-note");
  if (c.note) {
    noteEl.textContent = c.note;
    noteEl.classList.remove("hidden");
  } else {
    noteEl.classList.add("hidden");
  }

  // 翻面状态重置
  document.getElementById("card-front").classList.remove("hidden");
  document.getElementById("card-back").classList.add("hidden");
  document.getElementById("flip-hint").classList.remove("hidden");
  document.getElementById("rate-row").classList.add("hidden");
}

function flipCard() {
  if (flipped || ratingPending || !studyState) return;
  flipped = true;
  document.getElementById("card-front").classList.add("hidden");
  document.getElementById("card-back").classList.remove("hidden");
  document.getElementById("flip-hint").classList.add("hidden");
  document.getElementById("rate-row").classList.remove("hidden");
}

async function submitRating(rating) {
  if (!flipped || ratingPending || !studyState) return;
  ratingPending = true;
  const submitted = studyState;
  const errorEl = document.getElementById("study-error");
  errorEl.textContent = "";
  document.querySelectorAll(".rate-btn").forEach((btn) => { btn.disabled = true; });
  const elapsedMs = Date.now() - cardShownAt;
  try {
    const res = await fetch("/api/session/rate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rating, elapsed_ms: elapsedMs,
        card_id: submitted.card.card_id, version: submitted.version }),
    });
    const data = await res.json();
    if (studyState !== submitted || views.study.classList.contains("hidden")) return;
    if (res.status === 409) {
      if (data.state) renderState(data.state);
      errorEl.textContent = data.error;
      return;
    }
    if (!res.ok) throw new Error(data.error || "评分失败，请重试");
    renderState(data);
  } catch (err) {
    if (studyState === submitted && !views.study.classList.contains("hidden")) {
      errorEl.textContent = err.message || "未能确认评分结果，请重试";
    }
  } finally {
    ratingPending = false;
    document.querySelectorAll(".rate-btn").forEach((btn) => { btn.disabled = false; });
  }
}

document.querySelectorAll(".rate-btn").forEach((btn) => {
  btn.addEventListener("click", () =>
    submitRating(parseInt(btn.dataset.rating, 10))
  );
});

document.addEventListener("keydown", (e) => {
  if (views.study.classList.contains("hidden")) return;
  if (e.repeat || e.target.closest("input, textarea, select, [contenteditable]")) return;
  if (e.code === "Space" || e.code === "Enter") {
    e.preventDefault();
    flipCard();
  } else if (e.key >= "1" && e.key <= "4") {
    e.preventDefault();
    submitRating(parseInt(e.key, 10));
  }
});

// ---------- 结束页 ----------

function renderFinish(s) {
  const isCram = s.session_type === "cram";
  document.getElementById("finish-title").textContent =
    isCram ? "突击完成" : "任务完成";
  document.getElementById("f-new").textContent = s.new_learned;
  document.getElementById("f-review").textContent = s.review_done;
  document.getElementById("f-lapse").textContent = s.lapses;
  document.getElementById("f-time").textContent = Math.round(
    s.elapsed_sec / 60
  );
  document.getElementById("f-streak").textContent = s.streak;
  document.getElementById("f-tomorrow").textContent = s.tomorrow_due;
  Object.entries(views).forEach(([k, el]) =>
    el.classList.toggle("hidden", k !== "finish")
  );
}

// ---------- 词库管理 ----------

const lib = { page: 1, q: "", tag: "", type: "", state: "", batch: "", sort: "added" };
let lastItems = [];

async function loadTagOptions() {
  const r = await (await fetch("/api/tags")).json();
  const keep = lib.tag;
  document.getElementById("lib-tag").innerHTML =
    '<option value="">全部标签</option>' +
    r.tags.map((t) => `<option value="${escapeHtml(t)}">${escapeHtml(t)}</option>`).join("");
  document.getElementById("lib-tag").value = keep;
  document.getElementById("tag-list").innerHTML =
    r.tags.map((t) => `<option value="${escapeHtml(t)}">`).join("");
}

async function loadLibrary() {
  await loadTagOptions();
  const batches = await (await fetch("/api/batches")).json();
  document.getElementById("lib-batch").innerHTML = '<option value="">全部批次</option>' +
    batches.batches.map((b) => `<option value="${b.id}">${escapeHtml(b.name)}（${b.total}）</option>`).join("");
  document.getElementById("lib-batch").value = lib.batch;
  document.getElementById("btn-rename-batch").disabled = !lib.batch;
  const params = new URLSearchParams({
    page: lib.page,
    per_page: 15,
    q: lib.q,
    tag: lib.tag,
    type: lib.type,
    state: lib.state,
    batch_id: lib.batch,
    sort: lib.sort,
  });
  const r = await (await fetch("/api/phrases?" + params)).json();
  lastItems = r.items;
  renderLibrary(r);
}

function renderLibrary(r) {
  const tbody = document.getElementById("lib-tbody");
  if (r.items.length === 0) {
    tbody.innerHTML =
      '<tr><td colspan="7" style="text-align:center;color:var(--ink-soft)">没有符合条件的词</td></tr>';
  } else {
    tbody.innerHTML = r.items.map((p) => `
      <tr>
        <td>${TYPE_LABEL[p.item_type] || p.item_type}</td>
        <td class="en-cell">${escapeHtml(p.en)}</td>
        <td class="zh-cell">${escapeHtml(p.zh)}</td>
        <td>${escapeHtml(p.batch_name || "未分组")}</td>
        <td class="tag-cell">${escapeHtml(p.tag || "")}</td>
        <td><span class="state-pill state-${p.agg_state}">${
          STATE_LABEL[p.agg_state] || p.agg_state
        }</span></td>
        <td class="row-actions">
          <button class="icon-btn" data-act="edit" data-id="${p.id}">编辑</button>
          ${p.agg_state === "suspended"
            ? `<button class="icon-btn" data-act="resume" data-id="${p.id}">恢复</button>`
            : `<button class="icon-btn" data-act="suspend" data-id="${p.id}">搁置</button>`}
          <button class="icon-btn danger" data-act="delete" data-id="${p.id}">删除</button>
        </td>
      </tr>
    `).join("");
  }

  const pages = Math.max(1, Math.ceil(r.total / r.per_page));
  document.getElementById("page-info").textContent = `${r.page} / ${pages}`;
  document.getElementById("page-prev").disabled = r.page <= 1;
  document.getElementById("page-next").disabled = r.page >= pages;
}

document.getElementById("lib-tbody").addEventListener("click", async (e) => {
  const btn = e.target.closest("button.icon-btn");
  if (!btn) return;
  const id = btn.dataset.id;
  const act = btn.dataset.act;
  if (act === "edit") {
    openEditModal(id);
  } else if (act === "delete") {
    const p = lastItems.find((x) => String(x.id) === id);
    if (confirm(`确定删除「${p.en}」？其卡片和复习记录将一并删除。`)) {
      await fetch(`/api/phrases/${id}`, { method: "DELETE" });
      loadLibrary();
    }
  } else if (act === "suspend") {
    await fetch(`/api/phrases/${id}/suspend`, { method: "POST" });
    loadLibrary();
  } else if (act === "resume") {
    await fetch(`/api/phrases/${id}/resume`, { method: "POST" });
    loadLibrary();
  }
});

document.getElementById("page-prev").addEventListener("click", () => {
  if (lib.page > 1) { lib.page -= 1; loadLibrary(); }
});
document.getElementById("page-next").addEventListener("click", () => {
  lib.page += 1;
  loadLibrary();
});

// 搜索防抖
let searchTimer = null;
document.getElementById("lib-search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    lib.q = e.target.value.trim();
    lib.page = 1;
    loadLibrary();
  }, 300);
});
["lib-tag", "lib-type", "lib-state", "lib-batch", "lib-sort"].forEach((id) => {
  document.getElementById(id).addEventListener("change", (e) => {
    lib[id.split("-")[1]] = e.target.value;
    lib.page = 1;
    loadLibrary();
  });
});

// ---------- 弹窗 ----------

document.getElementById("btn-rename-batch").addEventListener("click", async () => {
  if (!lib.batch) return;
  const name = prompt("输入新的批次名称：");
  if (!name || !name.trim()) return;
  const res = await fetch(`/api/batches/${lib.batch}`, {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: name.trim() }),
  });
  if (!res.ok) {
    const data = await res.json();
    alert(data.error || "重命名失败");
    return;
  }
  await loadLibrary();
});

const modal = document.getElementById("modal");
let editingId = null;

function showModal() { modal.classList.remove("hidden"); }
function hideModal() { modal.classList.add("hidden"); }

function toggleWordFields() {
  const isWord = document.getElementById("f-type").value === "word";
  document.querySelectorAll(".word-only").forEach((el) =>
    el.classList.toggle("hidden", !isWord)
  );
}

async function loadBatchOptions(selectedId) {
  const r = await (await fetch("/api/batches")).json();
  const sel = document.getElementById("f-batch");
  sel.innerHTML = `<option value="">${editingId ? "（未分组）" : "（沿用最近批次）"}</option>` +
    r.batches.map((b) =>
      `<option value="${b.id}">${escapeHtml(b.name)}（${b.done}/${b.total}）</option>`
    ).join("") + (editingId ? "" : '<option value="new">＋ 新建批次…</option>');
  sel.value = selectedId == null ? "" : String(selectedId);
  document.getElementById("f-batch-name").value = "";
  document.getElementById("new-batch-row").classList.add("hidden");
}

document.getElementById("f-batch").addEventListener("change", (e) => {
  document.getElementById("new-batch-row").classList.toggle("hidden", e.target.value !== "new");
});

async function openAddModal() {
  editingId = null;
  document.getElementById("modal-title").textContent = "加词";
  document.getElementById("f-type").value = "phrase";
  ["f-en", "f-zh", "f-phonetic", "f-pos", "f-tag", "f-note"].forEach((id) => {
    document.getElementById(id).value = "";
  });
  await loadBatchOptions(lib.batch);
  toggleWordFields();
  showModal();
}

async function openEditModal(id) {
  const p = lastItems.find((x) => String(x.id) === String(id));
  if (!p) return;
  editingId = id;
  document.getElementById("modal-title").textContent = "编辑词条";
  document.getElementById("f-type").value = p.item_type;
  document.getElementById("f-en").value = p.en;
  document.getElementById("f-zh").value = p.zh;
  document.getElementById("f-phonetic").value = p.phonetic || "";
  document.getElementById("f-pos").value = p.pos || "";
  document.getElementById("f-tag").value = p.tag || "";
  document.getElementById("f-note").value = p.note || "";
  await loadBatchOptions(p.batch_id);
  toggleWordFields();
  showModal();
}

document.getElementById("f-type").addEventListener("change", toggleWordFields);
document.getElementById("btn-add").addEventListener("click", openAddModal);
document.getElementById("modal-cancel").addEventListener("click", hideModal);
modal.addEventListener("click", (e) => {
  if (e.target === modal) hideModal();
});

document.getElementById("modal-save").addEventListener("click", async () => {
  const batchVal = document.getElementById("f-batch").value;
  const payload = {
    item_type: document.getElementById("f-type").value,
    en: document.getElementById("f-en").value.trim(),
    zh: document.getElementById("f-zh").value.trim(),
    phonetic: document.getElementById("f-phonetic").value.trim(),
    pos: document.getElementById("f-pos").value.trim(),
    tag: document.getElementById("f-tag").value.trim(),
    note: document.getElementById("f-note").value.trim(),
  };
  if (batchVal === "new") {
    payload.batch_name = document.getElementById("f-batch-name").value.trim();
    if (!payload.batch_name) { alert("请填写新批次名称"); return; }
  } else if (batchVal) {
    payload.batch_id = parseInt(batchVal, 10);
  } else if (editingId) {
    payload.batch_id = null;
  }
  const url = editingId ? `/api/phrases/${editingId}` : "/api/phrases";
  const method = editingId ? "PUT" : "POST";
  const res = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (res.ok) {
    const data = await res.json();
    if (data.existed) { alert("这个词已存在，未重复添加。"); return; }
    hideModal();
    loadLibrary();
  } else {
    const e = await res.json().catch(() => ({}));
    alert(e.error || "保存失败");
  }
});

// ---------- 导出 ----------

document.getElementById("btn-export-csv").addEventListener("click", () => {
  window.location.href = "/api/export/csv";
});
document.getElementById("btn-export-db").addEventListener("click", () => {
  window.location.href = "/api/export/db";
});

// ---------- 仪表盘 ----------

function localIso(dt) {
  const y = dt.getFullYear();
  const m = String(dt.getMonth() + 1).padStart(2, "0");
  const d = String(dt.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

async function loadDashboard() {
  const d = await (await fetch("/api/dashboard")).json();

  document.getElementById("d-total").textContent = d.overall.total;
  document.getElementById("d-learned").textContent = d.overall.learned;
  document.getElementById("d-mastered").textContent = d.overall.mastered;
  document.getElementById("d-forget").textContent = d.forever.forget_rate + "%";
  document.getElementById("d-streak").textContent = d.streak;
  document.getElementById("d-ratings").textContent = d.forever.total_ratings;

  // 30 天堆叠柱状图
  const maxVal = Math.max(
    1, ...d.series.map((x) => x.new + x.review + x.lapse)
  );
  document.getElementById("chart-bars").innerHTML = d.series.map((x) => {
    const seg = (n, cls) =>
      n ? `<div class="bar ${cls}" style="height:${(n / maxVal) * 100}%"></div>` : "";
    return `<div class="bar-group" title="${x.date} 新学${x.new} 复习${x.review} 遗忘${x.lapse}">`
      + seg(x.lapse, "bar-lapse") + seg(x.review, "bar-review")
      + seg(x.new, "bar-new") + "</div>";
  }).join("");

  // 90 天热力图
  const today = new Date();
  const maxAct = Math.max(1, ...Object.values(d.activity));
  const cells = [];
  for (let i = 89; i >= 0; i--) {
    const dt = new Date(today);
    dt.setDate(dt.getDate() - i);
    const iso = localIso(dt);
    const n = d.activity[iso] || 0;
    const lvl = n === 0 ? 0 : Math.min(4, Math.ceil((n / maxAct) * 4));
    cells.push(
      `<div class="heat-cell ${lvl ? "heat-" + lvl : ""}" title="${iso} 活动 ${n}"></div>`
    );
  }
  document.getElementById("heatmap").innerHTML = cells.join("");

  // 未来 7 天
  document.getElementById("due-week").innerHTML = d.due_next_week.map((x) =>
    `<div class="due-day"><div class="due-num">${x.count}</div>`
    + `<div class="due-date">${x.date.slice(5)}</div></div>`
  ).join("");
}

// ---------- 突击 ----------

document.getElementById("btn-cram-start").addEventListener("click", async () => {
  const limit = parseInt(document.getElementById("cram-limit").value, 10) || 30;
  const warn = document.getElementById("cram-warn");
  warn.textContent = "";

  const status = await (await fetch("/api/status")).json();
  if (status.session && !status.session.done) {
    if (!confirm("当前常规学习未完成，开始突击会中断当前队列（已评分记录不会丢）。继续？")) {
      return;
    }
  }

  const res = await fetch("/api/cram/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ limit }),
  });
  const data = await res.json();
  if (data.done) {
    warn.textContent = "暂无可突击的词——需要先学过一些词，且它们处于复习/学习中。";
    return;
  }
  enterStudy();
  renderState(data);
});

// ---------- 主题切换 ----------

document.getElementById("theme-toggle").addEventListener("click", () => {
  const current = document.documentElement.getAttribute("data-theme");
  const next = current === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  localStorage.setItem("theme", next);
});

// 初始化
loadStatus();

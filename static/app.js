const $ = (sel) => document.querySelector(sel);

const SAMPLES = [
  "周四晚上7点参加社团例会，在二教401",
  "数学作业下周一上午9点前提交",
  "明天上午9点去图书馆写论文，重要",
  "周六下午3点到体育馆参加篮球比赛",
  "9月30日前交入党申请书",
];

const CAT = {
  exam: { label: "考试", color: "#e5484d", icon: "📝" },
  homework: { label: "作业", color: "#8e4ec6", icon: "📚" },
  class: { label: "上课", color: "#3b82f6", icon: "🎒" },
  deadline: { label: "截止", color: "#f76808", icon: "⏰" },
  meeting: { label: "会议", color: "#12a150", icon: "🤝" },
  activity: { label: "活动", color: "#f59e0b", icon: "🎯" },
  social: { label: "社交", color: "#ec4899", icon: "🍻" },
  health: { label: "健康", color: "#14b8a6", icon: "💪" },
  other: { label: "其他", color: "#64748b", icon: "📌" },
};

const KIND = {
  schedule: { label: "日程", color: "#4f6ef7", icon: "🗓️", cls: "kind-schedule" },
  todo: { label: "待办", color: "#b45309", icon: "✅", cls: "kind-todo" },
};

const PAGES = ["input", "schedule", "todos", "mine"];
const state = {
  data: null,
  page: "input",
  day: null,
  todoFilter: "open",
  suggestionsById: {},
  editId: null,
  editMode: null, // edit | new | plan
  chat: { messages: [], sending: false },
  aiPlan: [],
};

/* ---------------- 基础工具 ---------------- */
async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: { "Content-Type": "application/json" },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json();
  if (!res.ok || data.ok === false) throw new Error(data.error || "请求失败");
  return data;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function catOf(t) { return CAT[t.category] || CAT.other; }
function kindOf(t) { return KIND[t.kind] || KIND[t.is_schedule ? "schedule" : "todo"] || KIND.schedule; }

function fmtDay(iso) {
  if (!iso) return "";
  const d = new Date(iso + "T00:00:00");
  return `${d.getMonth() + 1}月${d.getDate()}日`;
}

function fmtWeekday(iso) {
  if (!iso) return "";
  return "周" + "日一二三四五六"[new Date(iso + "T00:00:00").getDay()];
}

function todayISO() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

function addDays(iso, n) {
  const d = new Date(iso + "T00:00:00");
  d.setDate(d.getDate() + n);
  const p = (x) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

function badge(text, color) {
  return `<span class="badge" style="--c:${color}">${esc(text)}</span>`;
}

function timeRange(t) {
  if (!t.time) return "";
  return t.end_time ? `${t.time}–${t.end_time}` : t.time;
}

function sourceBadge(t) {
  if (t.source === "wechat_ai") return badge("🤖 微信AI", "#7c3aed");
  if (t.source === "wechat") return badge("📲 微信", "#0ea5e9");
  if (t.source === "chat_ai") return badge("💬 AI 对话", "#6366f1");
  if (t.source === "chat_rule") return badge("🧭 对话识别", "#0891b2");
  return "";
}

function kindBadge(t) {
  const k = kindOf(t);
  return `<span class="badge ${k.cls}">${k.icon} ${k.label}</span>`;
}

function deadlineText(t) {
  if (!t.deadline) return "";
  return `⏰ 截止 ${fmtDay(t.deadline)}${t.deadline_time ? " " + t.deadline_time : ""}`;
}

/* ---------------- 顶栏统计 ---------------- */
function renderHeaderStats() {
  const s = state.data ? state.data.stats : {};
  if (!s) return;
  $("#stats").innerHTML = [
    `今日日程 <b>${s.today_count || 0}</b>`,
    `待办 <b>${s.todo_count || 0}</b>`,
    s.overdue_count ? `<span class="warn">逾期 <b>${s.overdue_count}</b></span>`
      : `逾期 <b>0</b>`,
    `冲突 <b>${s.conflict_count || 0}</b>`,
  ].map((h) => `<span class="stat">${h}</span>`).join("");
}

/* ---------------- 页面路由 ---------------- */
function currentPage() {
  const h = (location.hash || "#/input").replace(/^#\/?/, "").split("?")[0];
  return PAGES.includes(h) ? h : "input";
}

function showPage() {
  state.page = currentPage();
  document.querySelectorAll(".page").forEach((p) => {
    p.classList.toggle("hidden", p.dataset.page !== state.page);
  });
  document.querySelectorAll(".nav-item").forEach((a) => {
    a.classList.toggle("active", a.dataset.page === state.page);
  });
  if (state.page === "schedule") renderSchedulePage();
  if (state.page === "todos") renderTodosPage();
  if (state.page === "mine") { refreshWx(); refreshAi(); }
}

window.addEventListener("hashchange", showPage);

/* ---------------- 状态刷新 ---------------- */
async function refresh() {
  try {
    const res = await api("/api/state");
    state.data = res;
    state.suggestionsById = {};
    (res.suggestions || []).forEach((s) => { state.suggestionsById[s.id] = s; });
    renderHeaderStats();
    if (state.page === "schedule") renderSchedulePage();
    if (state.page === "todos") renderTodosPage();
  } catch (err) {
    console.error("refresh failed", err);
  }
}

/* ================= AI 对话（第一页主入口） ================= */
function chatScrollBottom() {
  const log = $("#chatLog");
  log.scrollTop = log.scrollHeight;
}

function chatPendingIds() {
  return new Set((state.pending || []).map((p) => p.id));
}

function chatItemStatus(it) {
  const done = (label) => ({ done: true, label });
  if (it.outcome === "rejected") return done("已忽略");
  if (it.outcome === "accepted:schedule") return done("✅ 已采纳到日程");
  if (it.outcome === "accepted:todo") return done("✅ 已采纳到待办");
  if (!chatPendingIds().has(it.id)) return done("已处理");
  return null;
}

function chatItemHTML(it) {
  const f = it.fields || {};
  const kind = f.kind === "todo" ? "todo"
    : (f.deadline || f.deadline_time) && f.kind !== "schedule" ? "todo"
      : "schedule";
  const k = KIND[kind];
  const method = it.method === "rule-fallback" ? "🧭 规则识别" : "🤖 AI 识别";
  const when = fmtAiWhen(f) || "未识别到明确时间";
  const status = chatItemStatus(it);
  const conflicts = (it.conflicts || []).map((c) =>
    `<div class="chat-conflict"><span class="dot conflict"></span><span>${esc(c)}</span></div>`).join("");
  const actions = status
    ? `<div class="chat-item-done">${esc(status.label)}</div>`
    : `<div class="ai-actions">
        <button class="small ok-schedule" data-chat-id="${esc(it.id)}" data-chat-act="accept" data-chat-kind="schedule">🗓️ 采纳到日程</button>
        <button class="small ok-todo" data-chat-id="${esc(it.id)}" data-chat-act="accept" data-chat-kind="todo">✅ 采纳到待办</button>
        <button class="ghost small" data-chat-id="${esc(it.id)}" data-chat-act="reject">忽略</button>
      </div>`;
  return `
    <div class="chat-item">
      <div class="ai-item-head">
        <span class="ai-title">${esc(f.title || "未命名事项")}</span>
        <span class="badge ${k.cls}">${k.icon} 建议${k.label}</span>
        <span class="badge" style="--c:#7c3aed">${method}</span>
      </div>
      <div class="ai-when">${esc(when)}</div>
      ${f.location ? `<div class="ai-when">📍 ${esc(f.location)}</div>` : ""}
      ${it.reason ? `<div class="ai-reason">${esc(it.reason)}</div>` : ""}
      ${conflicts}
      ${actions}
    </div>`;
}

function chatMsgHTML(m) {
  if (m.role === "user") {
    return `<div class="chat-msg user"><div class="chat-bubble">${esc(m.content)}</div></div>`;
  }
  const meta = m.meta || {};
  const cards = (m.items || []).map(chatItemHTML).join("");
  const metaLine = meta.label
    ? `<div class="chat-meta">${esc(meta.label)}</div>`
    : "";
  return `
    <div class="chat-msg ai">
      <div class="chat-bubble">${esc(m.content)}</div>
      ${cards ? `<div class="chat-cards">${cards}</div>` : ""}
      ${metaLine}
    </div>`;
}

function renderChatLog() {
  const log = $("#chatLog");
  const stickToBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
  const msgs = state.chat.messages || [];
  if (!msgs.length) {
    log.innerHTML = '<div class="chat-empty">把安排直接说给我，我会一边回应一边记下来，放进“待你采纳”等你确认。</div>';
    return;
  }
  log.innerHTML = msgs.map(chatMsgHTML).join("");
  if (stickToBottom) chatScrollBottom();
}

function autosizeChatInput() {
  const el = $("#chatInput");
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, 150) + "px";
}

async function sendChatText(text) {
  text = (text || "").trim();
  if (!text || state.chat.sending) return;
  state.chat.sending = true;
  $("#chatSendBtn").disabled = true;
  state.chat.messages.push({ role: "user", content: text, ts: new Date().toISOString() });
  const log = $("#chatLog");
  log.insertAdjacentHTML("beforeend",
    '<div class="chat-msg ai"><div class="chat-bubble chat-thinking">正在思考…</div></div>');
  chatScrollBottom();
  try {
    const res = await api("/api/chat", { method: "POST", body: { text } });
    state.chat.messages.push({
      role: "assistant",
      content: res.reply,
      items: res.items || [],
      meta: res.meta || {},
    });
  } catch (err) {
    state.chat.messages.push({
      role: "assistant",
      content: "出错了：" + err.message,
      items: [],
      meta: { label: "本地错误" },
    });
  }
  state.chat.sending = false;
  $("#chatSendBtn").disabled = false;
  renderChatLog();
  await refreshAi().catch(() => {});
  await refresh().catch(() => {});
  await loadAiYou().catch(() => {});
}

async function loadChatHistory() {
  try {
    const res = await api("/api/chat/history");
    state.chat.messages = res.messages || [];
    renderChatLog();
  } catch (_) { /* 服务未就绪时静默 */ }
}

$("#chatSendBtn").addEventListener("click", () => {
  const el = $("#chatInput");
  const text = el.value;
  el.value = "";
  autosizeChatInput();
  sendChatText(text);
});
$("#chatInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    const el = $("#chatInput");
    const text = el.value;
    el.value = "";
    autosizeChatInput();
    sendChatText(text);
  }
});
$("#chatInput").addEventListener("input", autosizeChatInput);
$("#chatResetBtn").addEventListener("click", async () => {
  if (!confirm("清空当前对话记录？不会影响待采纳与已保存的事项。")) return;
  try {
    await api("/api/chat/reset", { method: "POST", body: {} });
    state.chat.messages = [];
    renderChatLog();
  } catch (err) { alert(err.message); }
});
$("#chatLog").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-chat-id]");
  if (!btn) return;
  const id = btn.dataset.chatId;
  const act = btn.dataset.chatAct;
  const kind = btn.dataset.chatKind;
  if (act === "accept") {
    const label = kind === "todo" ? "待办" : "日程";
    if (!confirm(`确认把这条识别结果采纳到“${label}”？`)) return;
  }
  await aiAction(act, id, kind);
});

/* ---- 微信自动提取 ---- */
const WX_ACT_LABEL = {
  added: { cls: "wx-added", icon: "＋" },
  ignored: { cls: "wx-ignored", icon: "–" },
  error: { cls: "wx-error", icon: "!" },
  info: { cls: "wx-info", icon: "·" },
};

function renderWx(partial) {
  const s = { ...(state.wx || {}), ...partial };
  if (!s.running) s.connected = false;
  state.wx = s;
  const act0 = (s.activity && s.activity[0]) || {};
  const sig = JSON.stringify([
    s.connected, s.connecting, s.running, s.added_total, s.ignored_total,
    s.last_error, s.targets, act0.ts,
  ]);
  const changed = state.wxSig !== sig;
  state.wxSig = sig;

  const el = $("#wxStatus");
  if (s.connected) {
    el.className = "wx-status wx-on";
    const who = (s.account && (s.account.nick_name || s.account.username)) || "";
    const n = (s.targets || []).length;
    el.innerHTML = `● 已连接 ${who ? "：" + esc(who) : ""}，监听 ${n} 个会话`;
  } else if (s.running) {
    el.className = "wx-status wx-wait";
    el.innerHTML = s.connecting
      ? "… 正在连接微信（首次需读取密钥，约几秒）"
      : "● 等待微信登录后自动连接…";
    if (s.last_error) el.innerHTML += `<span class="wx-err">${esc(String(s.last_error).slice(0, 90))}</span>`;
  } else {
    el.className = "wx-status wx-off";
    el.innerHTML = "○ 未运行";
  }

  const input = $("#wxChats");
  if (document.activeElement !== input) input.value = (s.watch || []).join("、");
  $("#wxAll").checked = !!s.watch_all;
  $("#wxStartBtn").disabled = !!s.running;
  $("#wxScanBtn").disabled = !s.connected;
  $("#wxStopBtn").disabled = !s.running;
  $("#wxMeta").textContent =
    `每 ${s.interval || 1.5} 秒轮询新消息；「立即扫描」回读每个监听会话最近 ${s.backfill || 30} 条消息`;

  const list = s.activity || [];
  $("#wxLog").innerHTML = list.length
    ? list.map((a) => {
        const meta = WX_ACT_LABEL[a.action] || WX_ACT_LABEL.info;
        const time = String(a.ts || "").slice(11, 19);
        const body = a.detail || a.content || "";
        return `<div class="wx-line ${meta.cls}"><span class="wx-tag">${meta.icon}</span><span class="wx-time">${time}</span><span class="wx-txt">${esc(body)}</span></div>`;
      }).join("")
    : '<div class="wx-empty">暂无记录 — 把消息发到监听会话即可自动提取（区分日程 / 待办）</div>';
  return changed;
}

async function refreshWx() {
  try {
    const s = await api("/api/wechat/status");
    if (renderWx(s) && s.connected) refresh();
  } catch (_) { /* 服务未就绪时静默 */ }
}

async function wxAction(path, body = {}) {
  try {
    const res = await api(path, { method: "POST", body });
    const st = res.status || res;
    const changed = renderWx(st);
    if ((res.status ? res.status.connected : res.connected)) {
      if (changed) refresh();
    }
    return res;
  } catch (err) {
    alert(err.message);
    return null;
  }
}

/* ---- AI 设置 ---- */
const AI_PRESETS = {
  openai: { base: "https://api.openai.com/v1", model: "gpt-4o-mini" },
  deepseek: { base: "https://api.deepseek.com/v1", model: "deepseek-chat" },
  moonshot: { base: "https://api.moonshot.cn/v1", model: "moonshot-v1-8k" },
};

function aiCfgPayload() {
  const provider = $("#aiProvider").value;
  const minLen = parseInt($("#aiMinLen").value, 10);
  const payload = {
    enabled: $("#aiEnabled").checked,
    provider,
    model: $("#aiModel").value.trim(),
    api_key: $("#aiKey").value.trim(),
    min_len: Number.isFinite(minLen) && minLen >= 0 ? minLen : 10,
    require_time_word: $("#aiRequireTimeWord").checked,
  };
  if (provider === "custom") payload.base_url = $("#aiBaseUrl").value.trim();
  return payload;
}

async function refreshAi() {
  try {
    const [cfgRes, pendingRes] = await Promise.all([
      api("/api/ai/config"),
      api("/api/ai/pending"),
    ]);
    renderAi(cfgRes.config);
    renderAiPending(pendingRes.pending);
  } catch (_) { /* 静默 */ }
}

function syncAiBaseRow() {
  $("#aiBaseRow").classList.toggle("hidden", $("#aiProvider").value !== "custom");
}

let aiSaveTimer = null;
function scheduleAiSave() {
  state.aiDirty = true;
  $("#aiMeta").textContent = "修改会自动保存（进行中…）";
  if (aiSaveTimer) clearTimeout(aiSaveTimer);
  aiSaveTimer = setTimeout(() => saveAiConfig(true), 700);
}

async function saveAiConfig(silent) {
  try {
    const res = await api("/api/ai/config", { method: "POST", body: aiCfgPayload() });
    state.aiDirty = false;
    renderAi(res.config);
    if (!silent) {
      alert(res.config.enabled
        ? "已保存并启用 AI 分析（消息先进入待采纳，会区分日程/待办）"
        : "已保存 AI 配置（未启用）");
    }
  } catch (err) {
    state.aiDirty = true;
    $("#aiMeta").textContent = "自动保存失败：" + err.message;
    if (!silent) alert(err.message);
  }
}

function renderAi(cfg) {
  state.aiCfg = cfg;
  const ready = cfg.enabled && cfg.has_key && !!cfg.model;
  const el = $("#aiStatus");
  el.className = "ai-status " + (ready ? "ai-on" : cfg.enabled ? "ai-wait" : "ai-off");
  el.innerHTML = ready
    ? `● 已启用：${esc(cfg.model)} · 自动区分日程 / 待办`
    : cfg.enabled
      ? "○ 已启用，但配置不完整（需要模型 + API Key）"
      : "○ 未启用（启用后微信消息将先进入「对话」页的待采纳区）";
  $("#aiKey").placeholder = cfg.has_key ? "已保存 Key，留空表示不修改" : "粘贴 API Key（仅保存在本机）";
  if (!state.aiDirty) {
    if (document.activeElement !== $("#aiModel")) $("#aiModel").value = cfg.model || "";
    if (document.activeElement !== $("#aiKey")) $("#aiKey").value = "";
    if (document.activeElement !== $("#aiMinLen")) $("#aiMinLen").value = cfg.min_len;
    if (document.activeElement !== $("#aiRequireTimeWord")) {
      $("#aiRequireTimeWord").checked = !!cfg.require_time_word;
    }
    if (document.activeElement !== $("#aiProvider")) {
      $("#aiProvider").value = cfg.provider || "openai";
      syncAiBaseRow();
      const preset = AI_PRESETS[$("#aiProvider").value];
      if (document.activeElement !== $("#aiBaseUrl")) {
        $("#aiBaseUrl").value = (preset && preset.base) || cfg.base_url || "";
      }
    }
    $("#aiEnabled").checked = !!cfg.enabled;
  }
  if (!state.aiDirty) {
    $("#aiMeta").textContent =
      `预筛：少于 ${cfg.min_len} 字${cfg.require_time_word ? "或无时间 / 地点 / 安排线索" : ""}的消息不送 AI；AI 会区分“日程”和“待办”。`;
  }
}

$("#aiProvider").addEventListener("change", () => {
  syncAiBaseRow();
  const preset = AI_PRESETS[$("#aiProvider").value];
  if (preset) {
    $("#aiBaseUrl").value = preset.base;
    if (!$("#aiModel").value.trim()) $("#aiModel").value = preset.model;
  }
  scheduleAiSave();
});
["aiEnabled", "aiModel", "aiBaseUrl", "aiKey", "aiMinLen"].forEach((id) => {
  const el = document.getElementById(id);
  if (el) el.addEventListener("input", scheduleAiSave);
});
document.getElementById("aiEnabled").addEventListener("change", scheduleAiSave);
document.getElementById("aiRequireTimeWord").addEventListener("change", scheduleAiSave);

function fmtAiWhen(f) {
  const parts = [];
  if (f.date) parts.push(fmtDay(f.date));
  if (f.time) parts.push(f.time + (f.end_time ? "–" + f.end_time : ""));
  if (f.deadline) parts.push("截止 " + fmtDay(f.deadline) + (f.deadline_time ? " " + f.deadline_time : ""));
  if (f.duration_min) parts.push(f.duration_min + " 分钟");
  return parts.join(" · ");
}

function renderAiPending(list) {
  const items = list || [];
  const sig = JSON.stringify(items.map((p) => p.id + ":" + JSON.stringify(p.fields)));
  if (state.aiPendingSig === sig) return;
  state.aiPendingSig = sig;
  state.pending = items;
  $("#aiPendingHint").textContent = items.length
    ? `${items.length} 条待你确认`
    : "暂无待采纳";
  const box = $("#aiPendingList");
  if (!items.length) {
    box.innerHTML = '<div class="wx-empty">暂无待采纳事项。启用 AI 后，微信消息通过预筛会出现在这里，你可选择“采纳到日程”或“采纳到待办”。</div>';
    if ((state.chat.messages || []).length) renderChatLog();
    return;
  }
  box.innerHTML = items.map((p) => {
    const f = p.fields || {};
    const kind = f.kind === "todo" ? "todo" : "schedule";
    const k = KIND[kind];
    const method = p.method === "ai" ? "🤖 AI" : "🧭 规则兜底";
    const conf = p.confidence != null ? ` · 置信度 ${Math.round(p.confidence * 100)}%` : "";
    const src = p.raw ? "原文：" + esc(String(p.raw).slice(0, 120)) : "";
    return `
    <div class="ai-item" data-id="${esc(p.id)}">
      <div class="ai-item-head">
        <span class="ai-title">${esc(f.title || "未命名事项")}</span>
        <span class="badge ${k.cls}">${k.icon} 建议${k.label}</span>
        <span class="badge" style="--c:#7c3aed">${method}${conf}</span>
      </div>
      <div class="ai-when">${fmtAiWhen(f) || "未识别到明确时间"}</div>
      ${f.location ? `<div class="ai-when">📍 ${esc(f.location)}</div>` : ""}
      ${src ? `<div class="ai-raw">${src}</div>` : ""}
      ${p.chat_display ? `<div class="ai-reason">来源：${esc(p.chat_display)}${p.sender && p.sender !== "我" ? " · " + esc(p.sender) : ""}</div>` : ""}
      ${p.reason ? `<div class="ai-reason">${esc(p.reason)}</div>` : ""}
      ${p.error ? `<div class="ai-err">AI 调用失败：${esc(p.error)}（本次是临时规则兜底）</div>` : ""}
      <div class="ai-actions">
        <button class="small ok-schedule" data-ai-action="accept" data-kind="schedule">🗓️ 采纳到日程</button>
        <button class="small ok-todo" data-ai-action="accept" data-kind="todo">✅ 采纳到待办</button>
        <button class="ghost small" data-ai-action="reject">忽略</button>
      </div>
    </div>`;
  }).join("");
  if ((state.chat.messages || []).length) renderChatLog();
}

async function aiAction(act, id, kind) {
  try {
    const res = await api(`/api/ai/pending/${id}/${act}`, {
      method: "POST",
      body: kind ? { kind } : {},
    });
    if (res.state) { state.data = res.state; renderHeaderStats(); }
    await refreshAi();
    await refresh();
    await loadChatHistory();
  } catch (err) { alert(err.message); }
}

$("#aiPendingList").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-ai-action]");
  if (!btn) return;
  const item = btn.closest("[data-id]");
  if (!item) return;
  const act = btn.dataset.aiAction;
  const kind = btn.dataset.kind;
  if (act === "accept") {
    const label = kind === "todo" ? "待办" : "日程";
    if (!confirm(`确认采纳这条 AI 识别结果到“${label}”？`)) return;
  }
  aiAction(act, item.dataset.id, kind);
});

/* ================= AI 规划 ================= */
function planItemHTML(it) {
  const when = it.date
    ? `${fmtDay(it.date)}（${fmtWeekday(it.date)}）${it.time}–${it.end_time}`
    : "暂无可排空档";
  return `
    <div class="plan-item" data-id="${esc(it.id)}">
      <div class="plan-item-main">
        <div class="plan-title">${esc(it.title || "未命名待办")}</div>
        <div class="plan-when">🕐 ${esc(when)}</div>
        ${it.reason ? `<div class="plan-reason">💡 ${esc(it.reason)}</div>` : ""}
      </div>
      ${it.date
        ? `<button class="primary small" data-plan-act="apply" data-plan-id="${esc(it.id)}">采纳</button>`
        : `<span class="badge">需手动安排</span>`}
    </div>`;
}

function renderAiPlan() {
  const items = state.aiPlan || [];
  const box = $("#planList");
  if (!items.length) {
    box.innerHTML = '<div class="plan-empty">暂无建议，先添加未排期的待办再试试。</div>';
    $("#planApplyAllBtn").disabled = true;
    return;
  }
  box.innerHTML = items.map(planItemHTML).join("");
  $("#planApplyAllBtn").disabled = !items.some((x) => x.date);
}

async function loadAiPlan() {
  const res = await api("/api/ai/plan", { method: "POST", body: {} });
  state.aiPlan = res.items || [];
  const method = res.method === "ai" ? "由 AI 生成" : "规则规划";
  $("#planHint").textContent = `${res.summary || ""}（${method}，采纳后进入日程）`;
  if (!state.aiPlan.length) {
    $("#planCard").classList.add("hidden");
    return;
  }
  $("#planCard").classList.remove("hidden");
  renderAiPlan();
}

async function applyPlanItem(id) {
  const it = (state.aiPlan || []).find((x) => x.id === id);
  if (!it || !it.date) return;
  try {
    await api(`/api/todos/${id}`, {
      method: "PATCH",
      body: { kind: "schedule", date: it.date, time: it.time, end_time: it.end_time },
    });
    state.aiPlan = state.aiPlan.filter((x) => x.id !== id);
    renderAiPlan();
    await refresh().catch(() => {});
    if (!state.aiPlan.length) $("#planCard").classList.add("hidden");
  } catch (err) {
    alert(err.message);
  }
}

$("#aiPlanBtn").addEventListener("click", async () => {
  $("#aiPlanBtn").disabled = true;
  try {
    await loadAiPlan();
  } catch (err) {
    alert(err.message);
  } finally {
    $("#aiPlanBtn").disabled = false;
  }
});
$("#planList").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-plan-act]");
  if (!btn || btn.dataset.planAct !== "apply") return;
  applyPlanItem(btn.dataset.planId);
});
$("#planApplyAllBtn").addEventListener("click", async () => {
  const items = (state.aiPlan || []).filter((x) => x.date);
  if (!items.length) return;
  if (!confirm(`确认把 ${items.length} 条建议全部采纳进日程？`)) return;
  let ok = 0;
  for (const it of items) {
    try {
      await api(`/api/todos/${it.id}`, {
        method: "PATCH",
        body: { kind: "schedule", date: it.date, time: it.time, end_time: it.end_time },
      });
      ok++;
    } catch (_) { /* 单条失败不中断 */ }
  }
  state.aiPlan = [];
  $("#planCard").classList.add("hidden");
  await refresh().catch(() => {});
  alert(`已采纳 ${ok}/${items.length} 条排期建议`);
});
$("#planCloseBtn").addEventListener("click", () => {
  state.aiPlan = [];
  $("#planCard").classList.add("hidden");
});

/* ================= 日程页 ================= */
function dayInfo(iso) {
  const d = new Date(iso + "T00:00:00");
  const today = state.data ? state.data.today : todayISO();
  const tag = iso === today ? " · 今天" : "";
  return { d, label: `${d.getMonth() + 1}月${d.getDate()}日（${fmtWeekday(iso)}）${tag}`, isToday: iso === today };
}

function eventHTML(t) {
  const c = catOf(t);
  const meta = [
    t.time ? `🕐 ${timeRange(t)}` : "",
    t.location ? `📍 ${esc(t.location)}` : "",
    t.done ? "" : deadlineText(t),
  ].filter(Boolean).join(" · ");
  const ops = t.course ? "" : `
    <span class="ev-ops">
      <button class="mini" data-action="edit" title="编辑">✎</button>
      <button class="mini" data-action="toggle" data-to="${t.done ? "pending" : "done"}" title="${t.done ? "恢复" : "完成"}">${t.done ? "↩" : "✓"}</button>
      <button class="mini del" data-action="del" title="删除">✕</button>
    </span>`;
  return `
  <div class="ev ${t.done ? "done" : ""} ${t.time ? "" : "all-day"}" data-id="${esc(t.id)}">
    <span class="ev-title">${esc(t.title)}</span>
    ${meta ? `<span class="ev-meta">${meta}</span>` : ""}
    <span class="badge" style="--c:${c.color}">${c.icon} ${c.label}</span>
    ${t.conflict ? badge("冲突", "#e5484d") : ""}
    ${ops}
  </div>`;
}

function renderSchedulePage() {
  if (!state.data) return;
  if (!state.day) state.day = state.data.today || todayISO();
  const day = state.day;
  const info = dayInfo(day);
  $("#dayPick").value = day;
  $("#dayTitle").textContent = info.label;

  const dayData = (state.data.timeline || []).find((d) => d.date === day) || { date: day, items: [], ddl_items: [] };
  const items = dayData.items || [];
  const due = dayData.ddl_items || [];
  if (due.length) {
    $("#dueCard").classList.remove("hidden");
    $("#dueList").innerHTML = due.map((t) =>
      `<div class="due-list-item">⏰ <b>${esc(t.title)}</b> 今天截止${t.deadline_time ? " " + esc(t.deadline_time) : ""}</div>`).join("");
  } else {
    $("#dueCard").classList.add("hidden");
  }

  const allDay = items.filter((t) => !t.time);
  if (!items.length) {
    $("#scheduleTimeline").innerHTML = `
      <div class="empty-day">
        <div class="empty-icon">🗓️</div>
        <p>${info.label} 还没有安排</p>
        <button class="primary" data-action="add-day">＋ 添加安排</button>
      </div>`;
    return;
  }
  const periodDef = [
    { key: "am", label: "☀️ 上午", min: "00:00", max: "12:00", def: "09:00" },
    { key: "pm", label: "🌤️ 下午", min: "12:00", max: "18:00", def: "14:00" },
    { key: "ev", label: "🌙 晚上", min: "18:00", max: "24:00", def: "19:30" },
  ];
  const inPeriod = (t, p) => t.time && t.time >= p.min && t.time < p.max;
  const blocks = periodDef.map((p) => {
    const evs = items.filter((t) => inPeriod(t, p)).sort((a, b) => (a.time < b.time ? -1 : 1));
    if (!evs.length) return "";
    return `
      <div class="period-block">
        <div class="period-head">
          <span>${p.label}</span>
          <span class="period-count">${evs.length} 项</span>
          <button class="mini period-add" data-action="add-at" data-time="${p.def}"
                  title="在 ${p.label} 添加安排">＋</button>
        </div>
        <div class="period-list">${evs.map(eventHTML).join("")}</div>
      </div>`;
  }).join("");
  const allDayBlock = allDay.length
    ? `
      <div class="period-block">
        <div class="period-head"><span>📌 全天</span><span class="period-count">${allDay.length} 项</span></div>
        <div class="period-list">${allDay.map(eventHTML).join("")}</div>
      </div>`
    : "";
  $("#scheduleTimeline").innerHTML = allDayBlock + blocks;
}

$("#dayPrev").addEventListener("click", () => {
  state.day = addDays(state.day, -1);
  renderSchedulePage();
});
$("#dayNext").addEventListener("click", () => {
  state.day = addDays(state.day, 1);
  renderSchedulePage();
});
$("#dayToday").addEventListener("click", () => {
  state.day = state.data ? state.data.today : todayISO();
  renderSchedulePage();
});
$("#dayPick").addEventListener("change", (e) => {
  if (e.target.value) { state.day = e.target.value; renderSchedulePage(); }
});
$("#addEventBtn").addEventListener("click", () => {
  openItemModal({ mode: "new", defaults: { kind: "schedule", date: state.day } });
});
$("#addTodoBtn").addEventListener("click", () => {
  openItemModal({ mode: "new", defaults: { kind: "todo" } });
});

/* ================= 待办页 ================= */
function todoItemHTML(t) {
  const c = catOf(t);
  const badges = [
    kindBadge(t),
    badge(`${c.icon} ${c.label}`, c.color),
    t.deadline ? badge(deadlineText(t), "#f76808") : "",
    t.priority === "high" ? badge("高优先级", "#e5484d") : "",
    t.overdue ? badge("已逾期", "#e5484d") : "",
    sourceBadge(t),
  ].filter(Boolean).join("");
  const meta = [
    t.deadline ? `⏰ 截止 ${fmtDay(t.deadline)}${t.deadline_time ? " " + t.deadline_time : ""}` : "",
    t.deadline && t.deadline < (state.data ? state.data.today : "") && !t.done ? "（已逾期）" : "",
  ].filter(Boolean).join("　");
  return `
  <div class="todo-item ${t.done ? "done" : ""} ${t.overdue ? "overdue" : ""}" data-id="${esc(t.id)}">
    <div class="td-main">
      <div class="td-title">${esc(t.title)} <span class="tl-badges">${badges}</span></div>
      ${meta ? `<div class="td-meta">${meta}</div>` : ""}
      ${t.location ? `<div class="td-meta">📍 ${esc(t.location)}</div>` : ""}
    </div>
    <div class="td-ops">
      ${!t.done ? `<button class="mini plan" data-action="plan" title="规划到日程">规划到日程</button>` : ""}
      <button class="mini" data-action="edit" title="编辑">✎</button>
      <button class="mini" data-action="toggle" data-to="${t.done ? "pending" : "done"}" title="${t.done ? "恢复待办" : "标记完成"}">${t.done ? "↩" : "✓"}</button>
      <button class="mini del" data-action="del" title="删除">✕</button>
    </div>
  </div>`;
}

function renderTodosPage() {
  if (!state.data) return;
  const filter = state.todoFilter;
  let list = [];
  if (filter === "done") list = state.data.todo_done || [];
  else if (filter === "overdue") list = (state.data.todo_items || []).filter((t) => t.overdue);
  else list = state.data.todo_items || [];
  document.querySelectorAll(".todo-filter .chip").forEach((c) => {
    c.classList.toggle("active", c.dataset.todoFilter === filter);
  });
  const counts = { open: (state.data.todo_items || []).length, done: (state.data.todo_done || []).length };
  $("#todoFilterInfo").textContent = `未完成 ${counts.open} · 已完成 ${counts.done}`;
  const box = $("#todoList");
  if (!list.length) {
    const addBtn = filter === "done"
      ? ""
      : `<div class="empty-actions"><button class="primary small" data-action="add-todo">＋ 添加待办</button></div>`;
    box.innerHTML = `<div class="empty-list">
      <div>${filter === "done" ? "还没有已完成的事项" : "暂无待办。微信里带截止时间的任务会自动进这里，也可以直接手动添加。"}</div>
      ${addBtn}
    </div>`;
    return;
  }
  box.innerHTML = list.map(todoItemHTML).join("");
}

$(".todo-filter").addEventListener("click", (e) => {
  const chip = e.target.closest("[data-todo-filter]");
  if (!chip) return;
  state.todoFilter = chip.dataset.todoFilter;
  renderTodosPage();
});

/* ================= 通用操作（编辑/删除/完成/规划/新增） ================= */
function findTodo(id) {
  return (state.data && state.data.todos || []).find((t) => t.id === id) || null;
}

function syncItemKindFields() {
  const isSchedule = $("#edKind").value === "schedule";
  document.querySelectorAll("#itemModal .sched-field").forEach((el) => {
    el.classList.toggle("hidden", !isSchedule);
  });
}

function openItemModal({ mode, id, defaults = {} }) {
  const t = id ? findTodo(id) : null;
  state.editMode = mode;
  state.editId = id || null;
  const kind = (defaults.kind)
    || (mode === "plan" ? "schedule"
      : (t && (t.kind || (t.is_schedule ? "schedule" : "todo"))) || "schedule");
  const isTodoNew = mode === "new" && kind === "todo";
  $("#modalTitle").textContent = mode === "new"
    ? (isTodoNew ? "＋ 新建待办" : "＋ 新建安排")
    : mode === "plan" ? "📅 规划到日程" : "✏️ 编辑事项";
  $("#modalHint").textContent = mode === "plan"
    ? "为这条待办选择日期和时间，保存后进入日程时间轴"
    : isTodoNew
      ? "填标题和截止时间即可；不填截止时间也能先记下"
      : mode === "new"
        ? "留空字段表示不设置"
      : "可修改类型与时间，日程进时间轴，待办留在待办页";
  $("#edKind").value = kind;
  syncItemKindFields();
  $("#edTitle").value = (t ? t.title : defaults.title) || "";
  $("#edDate").value = (defaults.date != null ? defaults.date : t && t.date) || "";
  $("#edTime").value = (defaults.time != null ? defaults.time : t && t.time) || "";
  $("#edEndTime").value = (defaults.end_time != null ? defaults.end_time : t && t.end_time) || "";
  $("#edDuration").value = (t && t.duration_min != null ? t.duration_min : defaults.duration_min != null ? defaults.duration_min : "");
  $("#edLocation").value = (t ? t.location : defaults.location) || "";
  $("#edDeadline").value = (t ? t.deadline : defaults.deadline) || "";
  $("#edDeadlineTime").value = (t ? t.deadline_time : defaults.deadline_time) || "";
  $("#edPriority").value = (t ? t.priority : defaults.priority) || "medium";
  const tip = $("#planTip");
  if (mode === "plan") {
    const sug = state.suggestionsById[id];
    tip.classList.remove("hidden");
    tip.innerHTML = sug
      ? `💡 建议空档：${fmtDay(sug.date)}（${fmtWeekday(sug.date)}）${sug.time}–${sug.end_time}（已帮你预填，可修改）`
      : "暂时没有自动建议，你可以手动选择任意日期和时段。";
    if (sug && !defaults.override) {
      $("#edDate").value = sug.date;
      $("#edTime").value = sug.time;
      $("#edEndTime").value = sug.end_time || "";
    }
  } else {
    tip.classList.add("hidden");
  }
  $("#itemModal").classList.remove("hidden");
  $("#edTitle").focus();
}

function closeItemModal() {
  state.editId = null;
  state.editMode = null;
  $("#itemModal").classList.add("hidden");
}

async function saveItemModal() {
  const durRaw = $("#edDuration").value.trim();
  let duration = null;
  if (durRaw !== "") {
    const n = parseInt(durRaw, 10);
    duration = Number.isFinite(n) && n >= 0 ? n : null;
  }
  const body = {
    title: $("#edTitle").value.trim() || "未命名事项",
    kind: $("#edKind").value,
    date: $("#edDate").value || null,
    time: $("#edTime").value || null,
    end_time: $("#edEndTime").value || null,
    duration_min: duration,
    location: $("#edLocation").value.trim() || null,
    deadline: $("#edDeadline").value || null,
    deadline_time: $("#edDeadlineTime").value || null,
    priority: $("#edPriority").value,
  };
  try {
    let res;
    if (state.editMode === "plan") {
      // 待办 → 日程：把规划信息写回原事项，并切换类型
      res = await api(`/api/todos/${state.editId}`, {
        method: "PATCH",
        body: { ...body, kind: "schedule" },
      });
    } else if (state.editId) {
      res = await api(`/api/todos/${state.editId}`, { method: "PATCH", body });
    } else {
      res = await api("/api/items", { method: "POST", body });
    }
    state.data = res.state;
    closeItemModal();
    await refresh();
  } catch (err) {
    alert(err.message);
  }
}

$("#edSaveBtn").addEventListener("click", saveItemModal);
$("#edCancelBtn").addEventListener("click", closeItemModal);
$("#edKind").addEventListener("change", syncItemKindFields);
$("#itemModal").addEventListener("click", (e) => {
  if (e.target === $("#itemModal")) closeItemModal();
});

/* 页面内操作委托 */
document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-action]");
  if (!btn) return;
  const act = btn.dataset.action;
  if (act === "add-todo") {
    openItemModal({ mode: "new", defaults: { kind: "todo" } });
    return;
  }
  if (act === "add-day") {
    const iso = state.day || todayISO();
    openItemModal({ mode: "new", defaults: { kind: "schedule", date: iso } });
    return;
  }
  if (act === "add-at") {
    const iso = state.day || todayISO();
    openItemModal({ mode: "new", defaults: { kind: "schedule", date: iso, time: btn.dataset.time } });
    return;
  }
  const item = btn.closest("[data-id]");
  if (!item) return;
  const id = item.dataset.id;
  if (act === "edit") { openItemModal({ mode: "edit", id }); return; }
  if (act === "plan") { openItemModal({ mode: "plan", id }); return; }
  try {
    let res;
    if (act === "toggle") {
      res = await api(`/api/todos/${id}`, { method: "PATCH", body: { status: btn.dataset.to } });
    } else if (act === "del") {
      if (!confirm("删除这条事项？")) return;
      res = await api(`/api/todos/${id}`, { method: "DELETE" });
    }
    if (res) { state.data = res.state; await refresh(); }
  } catch (err) { alert(err.message); }
});

/* 样例、示例与清空 */
$("#samples").innerHTML = SAMPLES.map((s) => `<button class="chip">${esc(s)}</button>`).join("");
$("#samples").addEventListener("click", (e) => {
  const chip = e.target.closest(".chip");
  if (chip) sendChatText(chip.textContent);
});

$("#seedBtn").addEventListener("click", async () => {
  try {
    const res = await api("/api/seed", { method: "POST", body: {} });
    state.data = res.state;
    await refresh();
  } catch (err) { alert(err.message); }
});

async function clearScope(scope, label) {
  if (!confirm(`确认清空全部${label}？此操作不可恢复。`)) return;
  try {
    const res = await api("/api/clear", { method: "POST", body: { scope } });
    state.data = res.state;
    await refresh();
  } catch (err) {
    alert(err.message);
  }
}

$("#clearScheduleBtn").addEventListener("click", () => clearScope("schedule", "手动日程（不含课程表）"));
$("#clearTodoBtn").addEventListener("click", () => clearScope("todo", "待办"));

/* 微信控制 */
$("#wxApplyBtn").addEventListener("click", async () => {
  const watch = $("#wxChats").value
    .split(/[,，、;；\n]+/).map((x) => x.trim()).filter(Boolean);
  const res = await wxAction("/api/wechat/config", { watch, watch_all: $("#wxAll").checked });
  if (res) refreshWx();
});
$("#wxStartBtn").addEventListener("click", async () => {
  await wxAction("/api/wechat/start");
  refreshWx();
});
$("#wxScanBtn").addEventListener("click", async () => {
  const res = await wxAction("/api/wechat/scan");
  if (res && !res.ok) alert(res.error || "扫描失败");
  refreshWx();
});
$("#wxStopBtn").addEventListener("click", async () => {
  if (!confirm("确认断开微信监听？")) return;
  await wxAction("/api/wechat/stop");
  refreshWx();
});

$("#aiSaveBtn").addEventListener("click", async () => {
  if (aiSaveTimer) clearTimeout(aiSaveTimer);
  await saveAiConfig(false);
});
$("#aiTestBtn").addEventListener("click", async () => {
  try {
    const res = await api("/api/ai/test", { method: "POST", body: aiCfgPayload() });
    alert(res.ok ? "连接成功，AI 回复：" + (res.reply || "(空)") : "失败：" + (res.error || "未知错误"));
    refreshAi();
  } catch (err) { alert(err.message); }
});

/* ================= 课程表导入 ================= */
function courseResult(text, isErr) {
  const el = $("#courseResult");
  el.classList.remove("hidden", "err");
  if (isErr) el.classList.add("err");
  el.textContent = text;
}

async function loadCourseSummary() {
  try {
    const s = await api("/api/courses");
    const meta = s.meta || {};
    const status = $("#courseStatus");
    if (s.course_count) {
      status.textContent =
        `当前已导入：${meta.term || ""} · ${meta.class_name || ""} · ${s.course_count} 门课` +
        `（第 1–${s.weeks_total || "?"} 周，学期起始 ${s.term_start || "未设置"}）`;
      if (s.term_start && !$("#courseTermStart").value) {
        $("#courseTermStart").value = s.term_start;
      }
    } else {
      status.textContent = "尚未导入课程表";
    }
  } catch (_) { /* 静默 */ }
}

function uploadCourseFile(file) {
  if (!file) return;
  if (!/\.xlsx$/i.test(file.name)) {
    courseResult("请选择 .xlsx 格式的课表文件", true);
    return;
  }
  $("#courseFileName").textContent = file.name;
  const reader = new FileReader();
  reader.onerror = () => courseResult("文件读取失败，请重试", true);
  reader.onload = async () => {
    try {
      const data = String(reader.result).split(",")[1] || "";
      courseResult("正在解析课表…");
      const res = await api("/api/courses/import", {
        method: "POST",
        body: {
          name: file.name,
          data,
          term_start: $("#courseTermStart").value || null,
        },
      });
      courseResult(
        `导入成功：${res.class_name || ""} · ${res.course_count} 门课，` +
        `已按 ${res.term_start}（第 1 周）展开进“日程”页。`
      );
      $("#courseFileName").textContent = "";
      $("#courseFile").value = "";
      await refresh().catch(() => {});
      await loadCourseSummary();
    } catch (err) {
      courseResult("导入失败：" + err.message, true);
      $("#courseFile").value = "";
    }
  };
  reader.readAsDataURL(file);
}

$("#courseFile").addEventListener("change", (e) => {
  const file = e.target.files && e.target.files[0];
  if (file) uploadCourseFile(file);
});

/* ================= AI 画像展示（Phase 1） ================= */
const AI_YOU_DIMS = [
  { key: "energy", label: "精力" },
  { key: "task_load", label: "事务负载" },
  { key: "external_pressure", label: "外部压力" },
];
const AI_YOU_LEVEL = { low: "低", medium: "中", high: "高", unknown: "了解中" };

function aiYouDimText(state) {
  return AI_YOU_DIMS.map((d) =>
    `${d.label} ${AI_YOU_LEVEL[state[d.key]] || "—"}`).join(" · ");
}

function renderAiYou(s) {
  const state = s.state || {};
  const bar = $("#aiYouBar");
  const hasProfile = s.updated_at || s.evidence_count || s.memory_count;
  if (hasProfile) {
    bar.classList.remove("hidden");
    bar.innerHTML =
      `🤖 当前画像：${esc(aiYouDimText(state))}` +
      ` ｜ 对话证据 ${s.evidence_count || 0} 条 · 长期记忆 ${s.memory_count || 0} 条`;
  } else {
    bar.classList.add("hidden");
  }

  const card = $("#aiYouCard");
  card.classList.remove("hidden");
  const body = $("#aiYouBody");
  const known = AI_YOU_DIMS.filter((d) => state[d.key] && state[d.key] !== "unknown");
  body.innerHTML = `
    <div class="ai-you-chips">
      ${AI_YOU_DIMS.map((d) => `
        <span class="ai-you-chip">${d.label}：<b>${esc(AI_YOU_LEVEL[state[d.key]] || "—")}</b></span>`).join("")}
      <span class="ai-you-chip">置信度：<b>${Math.round((state.confidence || 0) * 100)}%</b></span>
    </div>
    <div class="ai-you-note">${
      known.length
        ? `AI 对你的当前状态有初步理解（最近更新 ${s.updated_at ? esc(s.updated_at.slice(0, 16).replace("T", " ")) : "—"}）；画像将随对话与反馈持续演化。`
        : `AI 还在通过对话慢慢了解你：已收集 ${s.evidence_count || 0} 条观察、${s.memory_count || 0} 条长期记忆。所有数据只保存在本机，可随时重置。`
    }</div>`;
}

async function loadAiYou() {
  try {
    const s = await api("/api/ai/profile");
    renderAiYou(s);
  } catch (_) { /* 静默 */ }
}

$("#profileResetBtn").addEventListener("click", async () => {
  if (!confirm("重置 AI 画像将清空状态、长期记忆和已收集的观察证据；不会影响你的日程、待办与聊天记录。确认重置？")) return;
  try {
    await api("/api/ai/profile/reset", { method: "POST", body: {} });
    await loadAiYou();
    alert("已重置 AI 画像");
  } catch (err) { alert(err.message); }
});

/* 初始化 */
async function init() {
  if (!location.hash) history.replaceState(null, "", "#/input");
  showPage();
  await refresh().catch(() => {});
  refreshWx();
  await refreshAi();
  await loadChatHistory();
  await loadCourseSummary();
  await loadAiYou();
  setInterval(() => { refreshWx(); refreshAi(); }, 4000);
  setInterval(() => { if (!document.hidden) refresh().catch(() => {}); }, 8000);
}

init();

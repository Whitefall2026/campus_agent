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

const PAGES = ["input", "schedule", "todos", "planner", "mine"];
const state = {
  data: null,
  page: "input",
  day: null,
  todoFilter: "open",
  suggestionsById: {},
  editId: null,
  editMode: null, // edit | new | plan
  chat: { messages: [], sending: false },
  plan: null,       // /api/plan 载荷 {plan, load}
  planDay: null,
  planSig: "",
  energy: null,     // /api/energy {hours, available_points}
  ratedIds: new Set(),
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
  if (state.page === "planner") loadPlanner();
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
    if (state.page === "planner") loadPlanner();
  } catch (err) {
    console.error("refresh failed", err);
  }
}

/* ---------------- 今日主动权：只提示，不替用户占满时间 ---------------- */
function formatFreeTime(minutes) {
  const h = Math.floor(minutes / 60), m = minutes % 60;
  return h ? `${h} 小时${m ? " " + m + " 分" : ""}` : `${m} 分钟`;
}

function renderTodayCompass(data) {
  $("#todayMessage").textContent = data.message || "今天的节奏，由你决定。";
  $("#freeTime").textContent = formatFreeTime(Number(data.free_minutes || 0));
  const items = data.priority_items || [];
  $("#todayPriority").innerHTML = items.length
    ? items.map((x) => `<div class="today-priority-item"><b>${esc(x.title)}</b><span>${esc(x.reason)}</span></div>`).join("")
    : '<div class="today-priority-empty">没有必须立刻处理的事。</div>';
}

async function loadTodayCompass() {
  try { renderTodayCompass(await api("/api/ai/today")); } catch (_) { /* 静默 */ }
}

document.querySelectorAll("[data-energy]").forEach((btn) => btn.addEventListener("click", async () => {
  try {
    const res = await api("/api/ai/profile/state", { method: "POST", body: { energy: btn.dataset.energy } });
    document.querySelectorAll("[data-energy]").forEach((b) => b.classList.toggle("selected", b === btn));
    await loadAiYou();
    await loadTodayCompass();
    state.planSig = null;
    if (state.page === "planner") await loadPlanner(true);
  } catch (err) { alert(err.message); }
}));

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
  <div class="ev ${t.done ? "done" : ""} ${t.time ? "" : "all-day"}" data-id="${esc(t.id)}" ${t.course ? "" : `draggable="true" title="按住可拖到其他时段"`}>
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
  const periodDef = [
    { key: "am", label: "☀️ 上午", min: "00:00", max: "12:00", def: "09:00" },
    { key: "pm", label: "🌤️ 下午", min: "12:00", max: "18:00", def: "14:00" },
    { key: "ev", label: "🌙 晚上", min: "18:00", max: "24:00", def: "19:30" },
  ];
  const inPeriod = (t, p) => t.time && t.time >= p.min && t.time < p.max;
  const emptyDayMsg = !items.length
    ? `<div class="empty-day">
         <div class="empty-icon">🗓️</div>
         <p>${info.label} 还没有安排 — 把待办卡片拖进下面的时段，即可排进这天</p>
         <button class="primary" data-action="add-day">＋ 添加安排</button>
       </div>`
    : "";
  // 三个时段块始终渲染：空时段也能作为拖拽落点
  const blocks = periodDef.map((p) => {
    const evs = items.filter((t) => inPeriod(t, p)).sort((a, b) => (a.time < b.time ? -1 : 1));
    const body = evs.length
      ? evs.map(eventHTML).join("")
      : `<div class="drop-hint">空闲 · 可把卡片拖到这里</div>`;
    return `
      <div class="period-block">
        <div class="period-head">
          <span>${p.label}</span>
          <span class="period-count">${evs.length} 项</span>
          <button class="mini period-add" data-action="add-at" data-time="${p.def}"
                  title="在 ${p.label} 添加安排">＋</button>
        </div>
        <div class="period-list" data-def="${p.def}">${body}</div>
      </div>`;
  }).join("");
  const allDayBlock = allDay.length
    ? `
      <div class="period-block">
        <div class="period-head"><span>📌 全天</span><span class="period-count">${allDay.length} 项</span></div>
        <div class="period-list" data-def="09:00">${allDay.map(eventHTML).join("")}</div>
      </div>`
    : "";
  $("#scheduleTimeline").innerHTML = emptyDayMsg + allDayBlock + blocks;
}

/* ================= 拖拽调整（拖到其他时段 / 拖入某一天） ================= */
function addMinTo(hm, mins) {
  const [h, m] = String(hm).split(":").map(Number);
  const total = ((h * 60 + m + mins) % (24 * 60) + 24 * 60) % (24 * 60);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(Math.floor(total / 60))}:${p(total % 60)}`;
}

document.addEventListener("dragstart", (e) => {
  const card = e.target.closest(".ev, .todo-item");
  if (!card || !card.dataset.id) return;
  const t = findTodo(card.dataset.id);
  if (!t || t.course || t.done) { e.preventDefault(); return; }
  e.dataTransfer.setData("text/plain", card.dataset.id);
  e.dataTransfer.effectAllowed = "move";
});

function dragZone(e) {
  return e.target.closest(".period-list, #scheduleTimeline");
}
document.addEventListener("dragover", (e) => {
  const zone = dragZone(e);
  if (!zone) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = "move";
  zone.classList.add("drag-over");
});
document.addEventListener("dragleave", (e) => {
  const zone = dragZone(e);
  if (zone) zone.classList.remove("drag-over");
});
document.addEventListener("drop", async (e) => {
  const zone = dragZone(e);
  if (!zone) return;
  e.preventDefault();
  zone.classList.remove("drag-over");
  const id = e.dataTransfer.getData("text/plain");
  if (!id) return;
  const t = findTodo(id);
  if (!t) return;
  const dateISO = state.day || todayISO();
  const newTime = zone.dataset.def || t.time || "09:00";
  const dur = t.duration_min || 60;
  const endTime = addMinTo(newTime, dur);
  try {
    const res = await api("/api/plan/move", {
      method: "POST",
      body: { id, date: dateISO, time: newTime, end_time: endTime },
    });
    await refresh();
  } catch (err) { alert(err.message); }
});

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
  <div class="todo-item ${t.done ? "done" : ""} ${t.overdue ? "overdue" : ""}" data-id="${esc(t.id)}" ${t.done ? "" : `draggable="true" title="按住可拖到日程页的某个时段"`}>
    <div class="td-main">
      <div class="td-title">${esc(t.title)} <span class="tl-badges">${badges}</span></div>
      ${meta ? `<div class="td-meta">${meta}</div>` : ""}
      ${t.location ? `<div class="td-meta">📍 ${esc(t.location)}</div>` : ""}
      ${t.done ? rateRowHTML(t.id) : ""}
    </div>
    <div class="td-ops">
      ${!t.done && (t.energy_cost || 0) >= 3
        ? `<button class="mini" data-action="decompose" title="AI 拆解为里程碑子任务">✂️ 拆解</button>` : ""}
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
  $("#edDdlType").value = (t ? t.deadline_type : defaults.deadline_type) || "";
  $("#edEnergy").value = t && t.energy_cost != null ? String(t.energy_cost) : (defaults.energy_cost != null ? String(defaults.energy_cost) : "");
  $("#edDeliverable").value = (t ? t.deliverable : defaults.deliverable) || "";
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
    deadline_type: $("#edDdlType").value || null,
    energy_cost: $("#edEnergy").value ? parseInt($("#edEnergy").value, 10) : null,
    deliverable: $("#edDeliverable").value.trim() || null,
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
  // 拆解面板操作（面板不在 data-id 容器内，需在取 item 之前处理）
  if (act === "accept-subs") {
    await acceptSubtasks(btn.dataset.id);
    return;
  }
  if (act === "sub-dismiss") {
    const panel = btn.closest(".sub-panel");
    if (panel) panel.remove();
    return;
  }
  const item = btn.closest("[data-id]");
  if (!item) return;
  const id = item.dataset.id;
  if (act === "edit") { openItemModal({ mode: "edit", id }); return; }
  if (act === "plan") { openItemModal({ mode: "plan", id }); return; }
  if (act === "decompose") { runDecompose(id, item); return; }
  if (act === "rate") {
    try {
      await api("/api/feedback", {
        method: "POST",
        body: { id, rating: btn.dataset.rate },
      });
      state.ratedIds.add(id);
      renderTodosPage();
    } catch (err) { alert(err.message); }
    return;
  }
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

/* ================= 计划页（能量看板 + 挡箭牌） ================= */
const RING_CIRC = 2 * Math.PI * 52;

const DDL_LABEL = {
  hard: { text: "硬线", cls: "dl-hard", icon: "🔴" },
  soft: { text: "软线", cls: "dl-soft", icon: "🟡" },
};

async function loadPlanner(force) {
  const day = state.planDay || todayISO();
  state.planDay = day;
  const sig = day;
  if (!force && state.planSig === sig && state.plan) {
    renderPlannerPage();
    return;
  }
  try {
    const [planRes, energyRes] = await Promise.all([
      api(`/api/plan?date=${day}`),
      api("/api/energy"),
    ]);
    state.plan = planRes;
    state.energy = energyRes.energy || null;
    state.planSig = sig;
    renderPlannerPage();
  } catch (err) {
    console.error("loadPlanner failed", err);
    $("#planHint").textContent = "加载失败：" + err.message;
  }
}

function planWarnById() {
  const map = {};
  ((state.plan && state.plan.plan.warnings) || []).forEach((w) => {
    if (!map[w.task_id]) map[w.task_id] = w;
  });
  return map;
}

function renderPlannerPage() {
  if (!state.plan) return;
  const day = state.planDay || state.plan.date || todayISO();
  $("#planPick").value = day;
  $("#planTitle").textContent =
    dayInfo(day).label + (state.plan.plan.date === day ? "" : "（本地日期）");

  const plan = state.plan.plan;
  const load = state.plan.load || {};
  const b = plan.budget || {};

  // ---- 能量环（只显示剩余比例颜色，不显示数值占位） ----
  const avail = b.available_points || 0;
  const used = b.planned_points || 0;
  const remain = Math.max(0, avail - used);
  const ratio = avail > 0 ? remain / avail : 0;   // 剩余能量占比
  const color = ratio >= 0.6 ? "#22c55e" : ratio >= 0.35 ? "#f59e0b" : "#e5484d";
  const fg = $("#ringFg");
  fg.style.stroke = color;
  fg.style.strokeDasharray = RING_CIRC;
  fg.style.strokeDashoffset = RING_CIRC * (1 - ratio);
  $("#energyHint").textContent = b.free_runs
    ? `${b.free_runs} 段空闲 · ${plan.meta.focus_cap_min ? `今天建议主动推进不超过 ${plan.meta.focus_cap_min} 分钟` : "按精力曲线安排"}`
    : "这一天没有空闲时段";
  $("#energyMeta").innerHTML =
    `可用 <b>${avail}</b> 点 · 已排 <b>${used}</b> 点` +
    `<br>占用 <b>${Math.round((b.planned_ratio || 0) * 100)}%</b>` +
    `<br>保留 <b>${Math.floor((b.protected_free_minutes || 0) / 60)} 小时${(b.protected_free_minutes || 0) % 60 ? " " + ((b.protected_free_minutes || 0) % 60) + " 分" : ""}</b>自由空白` +
    `<br><span style="font-size:11px">1 点 ≈ 状态好时的 30 分钟专注</span>`;
  const levelLabel = { low: "低", medium: "中", high: "高" }[plan.meta.state_energy];
  if (levelLabel) {
    $("#energyMeta").innerHTML += `<br><span style="font-size:11px">已按你刚记录的${levelLabel}精力调整今日建议</span>`;
  }

  // ---- 精力曲线 ----
  const hours = (state.energy && state.energy.hours) || [];
  const cells = [];
  for (let h = 7; h <= 22; h++) {
    const c = hours[h] || 0;
    const pct = Math.max(6, Math.min(100, (c / 1.2) * 100));
    cells.push(
      `<div class="cell ${c < 0.25 ? "z" : ""}" title="${h} 点 · 系数 ${c.toFixed(1)}">
         <i style="height:${pct}%"></i></div>`);
  }
  $("#curveStrip").innerHTML = cells.join("");

  // ---- 排程入口 & 空态 ----
  const entries = plan.entries || [];
  const empty = $("#planEmpty");
  const box = $("#planEntries");
  if (!entries.length) {
    box.innerHTML = "";
    empty.innerHTML = plan.candidates === 0
      ? `<div class="plan-empty">这一天没有等待规划的任务 🎉
          <br><span style="font-size:12px">把带截止日期的任务留在「待办」页，规划日当天这里会自动给出排程。</span></div>`
      : `<div class="plan-empty">任务没排进去：先看看下方预警/明日优先说明。</div>`;
  } else {
    empty.innerHTML = "";
    const warns = planWarnById();
    box.innerHTML = entries.map((e) => entryHTML(e, day, warns)).join("");
  }
  $("#planHint").innerHTML = entries.length
    ? `${entries.length} 项待采纳 · 另有 ${(plan.tomorrow || []).length} 项明日优先`
    : "暂无排程建议";

  // ---- 预警（阻塞）与明日优先 ----
  const warnBox = $("#planWarnings");
  const blocked = (plan.warnings || []).filter((w) => w.level === "blocked");
  const toleranceCount = (plan.warnings || []).filter((w) => w.level === "tolerance").length;
  const tomorrow = plan.tomorrow || [];
  let html = "";
  if (blocked.length) {
    html += blocked.map((w) =>
      `<div class="warn-box">⚠️ ${esc(w.copy)}</div>`).join("");
  }
  if (tomorrow.length) {
    html += `<div class="tomorrow-title">明日优先（今日未能启动）</div>` +
      tomorrow.map((t) => {
        const dd = DDL_LABEL[t.deadline_type] || DDL_LABEL.hard;
        const isToday = t.deadline === day;
        return `<span class="tomorrow-chip" title="${dd.text}截止${isToday ? "（今日截止）" : ""}">
          ${esc(t.title)}${isToday ? ` <span class="t-risk">${dd.icon} 今日截止</span>` : ""}</span>`;
      }).join("");
  }
  if (!html && toleranceCount) {
    html = `<div style="font-size:12px;color:var(--ink-2)">${toleranceCount} 项属容差适配（⚡），卡片内已给出温和说明。</div>`;
  }
  warnBox.innerHTML = html;

  // ---- 软线顺延建议 ----
  const deferrals = plan.deferrals || [];
  const deferBox = $("#planDeferrals");
  deferBox.innerHTML = deferrals.map((d) => `
    <div class="defer-box">
      <span class="defer-copy">🟡 ${esc(d.copy)}</span>
      <span class="defer-copy" style="font-size:11px">原截止 ${esc(d.deadline)} → 顺延至 ${esc(d.to_date)}（${fmtWeekday(d.to_date)}）</span>
      <div class="defer-actions">
        <button class="mini ok-todo" data-plan="defer-yes" data-id="${esc(d.task_id)}">✅ 同意顺延</button>
        <button class="ghost mini" data-plan="defer-no">暂不处理</button>
      </div>
    </div>`).join("");
}

function entryHTML(e, day, warns) {
  const warn = warns[e.task_id] || null;
  const dd = DDL_LABEL[e.deadline_type] || null;
  const hardToday = dd && dd.cls === "dl-hard" && e.deadline === day;
  const prob = e.probability != null ? Math.round(e.probability * 100) : null;
  const pcls = prob == null ? "" : prob >= 75 ? "p-hi" : prob >= 60 ? "p-mid" : "p-low";
  const risk = !!e.risk;
  const adapt = !!e.tolerance;
  const adaptNote = warn && warn.level === "tolerance"
    ? esc(warn.copy)
    : "这项任务原计划需稍多精力，但系统评估你今天的状态可以拿下（≤15% 容差）。";
  const deadlineLine = e.deadline
    ? ` · ${dd ? dd.icon + " " + dd.text : ""}${hardToday ? " 今日截止" : ""}${e.deadline_time ? " " + esc(e.deadline_time) : ""}`
    : "";
  const deliverable = e.deliverable ? ` 📦 ${esc(e.deliverable)}` : "";
  return `
  <div class="plan-entry ${hardToday || (dd && dd.cls === "dl-hard") ? "ddl-hard" : (dd ? "ddl-soft" : "")} ${risk ? "risk-shake" : ""}">
    <div class="entry-time">${esc(e.start)}–${esc(e.end)}</div>
    <div class="entry-body">
      <div class="entry-title">${esc(e.title)}${adapt ? `<span class="tag-adapt">⚡适配</span>` : ""}
        <span class="badge" style="--c:${hardToday ? "#e5484d" : "#f59e0b"}">${hardToday ? "硬线·今日截止" : (dd ? dd.text + "截止" : "待办")}</span>
      </div>
      <div class="entry-meta">
        ${esc(deadlineLine || "无截止")}${deliverable}
        ${adapt ? `<br>${adaptNote}` : ""}
      </div>
      ${prob != null ? `
      <div class="prob-bar"><i class="fill ${pcls}" style="width:${prob}%"></i></div>
      <div class="prob-note ${risk ? "risk" : ""}">
        ${risk ? `⚠️ 完成概率 ${prob}%，系统不太放心` : `完成概率约 ${prob}%`}
      </div>
      ${risk && e.risk_copy ? `<div class="entry-meta" style="margin-top:4px">💡 ${esc(e.risk_copy)}</div>` : ""}`
      : ""}
    </div>
      <div class="entry-ops">
      <button class="mini ok-todo" data-plan="adopt" data-id="${esc(e.task_id)}" title="采纳进日程">采纳</button>
      <button class="mini" data-plan="skip" data-id="${esc(e.task_id)}" title="不采纳这条建议">跳过</button>
    </div>
  </div>`;
}

/* ---- 计划页控件 ---- */
function planPreferenceFeedback(action, entries) {
  const items = (entries || []).map((e) => ({
    id: e.task_id,
    title: e.title,
    date: state.planDay,
    time: e.start || e.time || "",
    end_time: e.end || e.end_time || "",
  }));
  if (!items.length) return Promise.resolve();
  return api("/api/ai/plan/feedback", {
    method: "POST",
    body: { action, items },
  }).catch(() => {});
}

$("#planPrev").addEventListener("click", () => { state.planDay = addDays(state.planDay, -1); loadPlanner(true); });
$("#planNext").addEventListener("click", () => { state.planDay = addDays(state.planDay, 1); loadPlanner(true); });
$("#planToday").addEventListener("click", () => { state.planDay = todayISO(); loadPlanner(true); });
$("#planPick").addEventListener("change", (e) => {
  if (e.target.value) { state.planDay = e.target.value; loadPlanner(true); }
});
$("#planRecomputeBtn").addEventListener("click", () => loadPlanner(true));

$("#planApplyAllBtn").addEventListener("click", async () => {
  const plan = state.plan && state.plan.plan;
  if (!plan || !(plan.entries || []).length) { alert("没有可采纳的排程"); return; }
  if (!confirm(`一键把今天 ${plan.entries.length} 项建议全部写入日程？（软线顺延需单独点「同意顺延」）`)) return;
  try {
    await api("/api/plan/apply", {
      method: "POST",
      body: { date: state.planDay, placements: (plan.entries || []).map((x) => x.task_id) },
    });
    await planPreferenceFeedback("accept", plan.entries || []);
    await refresh();
    await loadPlanner(true);
  } catch (err) { alert(err.message); }
});

$("#planEntries").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-plan]");
  if (!btn) return;
  if (btn.dataset.plan === "adopt") {
    try {
      const res = await api("/api/plan/apply", {
        method: "POST",
        body: { date: state.planDay, placements: [btn.dataset.id] },
      });
      const entry = (state.plan && state.plan.plan.entries || [])
        .find((x) => x.task_id === btn.dataset.id);
      if (entry) await planPreferenceFeedback("accept", [entry]);
      await refresh();
      await loadPlanner(true);
    } catch (err) { alert(err.message); }
  } else if (btn.dataset.plan === "skip") {
    const entry = (state.plan && state.plan.plan.entries || [])
      .find((x) => x.task_id === btn.dataset.id);
    if (entry) await planPreferenceFeedback("reject", [entry]);
    const card = btn.closest(".plan-entry");
    if (card) card.remove();
  }
});
$("#planDeferrals").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-plan]");
  if (!btn) return;
  const box = btn.closest(".defer-box");
  if (btn.dataset.plan === "defer-no") { if (box) box.remove(); return; }
  if (btn.dataset.plan === "defer-yes") {
    try {
      const res = await api("/api/plan/apply", {
        method: "POST",
        body: { date: state.planDay, deferrals: [btn.dataset.id] },
      });
      await refresh();
      await loadPlanner(true);
    } catch (err) { alert(err.message); }
  }
});

/* ---- 完成反馈：待办标记完成后给出精力反馈（校准曲线） ---- */
function rateRowHTML(id) {
  if (state.ratedIds.has(id)) {
    return `<div class="rate-row"><span>✅ 反馈已记录，感谢校准精力曲线</span></div>`;
  }
  return `<div class="rate-row">
    <span>这项做完的感觉？</span>
    <button class="mini" data-action="rate" data-rate="easy">轻松</button>
    <button class="mini" data-action="rate" data-rate="ok">正常</button>
    <button class="mini" data-action="rate" data-rate="tough">吃力</button>
  </div>`;
}

/* ================= LLM 里程碑拆解（待办卡片 → 面板） ================= */
async function runDecompose(id, item) {
  // 移除同一任务的旧面板
  const host = item.closest(".todo-item") || document.body;
  const old = host.parentNode.querySelector(`.sub-panel[data-parent="${id}"]`);
  if (old) old.remove();
  const panel = document.createElement("div");
  panel.className = "sub-panel";
  panel.dataset.parent = id;
  panel.innerHTML = '<div class="sub-loading">正在拆解为里程碑子任务…（需要 AI 已启用并配置）</div>';
  host.after(panel);
  try {
    const res = await api("/api/decompose", { method: "POST", body: { id } });
    if (!res.decomposed) {
      panel.innerHTML = `<div class="sub-msg">${esc(res.reason || "暂不需要拆解")}</div>
        <div class="sub-actions"><button class="ghost mini" data-action="sub-dismiss">收起</button></div>`;
      return;
    }
    panel.innerHTML = `
      <div class="sub-title">🧩 建议拆成 ${res.subtasks.length} 个里程碑</div>
      ${res.subtasks.map((s, i) => `
        <div class="sub-row">
          <span class="sub-idx">${i + 1}</span>
          <b>${esc(s.name)}</b>
          ${s.deliverable ? `<span class="sub-deliverable">📦 ${esc(s.deliverable)}</span>` : ""}
          <span class="badge" style="--c:#7c3aed">⚡${s.energy_cost}</span>
          ${s.deadline ? `<span class="sub-when">→ ${esc(s.deadline)}</span>` : ""}
        </div>`).join("")}
      <div class="sub-actions">
        <button class="mini ok-todo" data-action="accept-subs" data-id="${esc(id)}">采纳全部为子任务</button>
        <button class="ghost mini" data-action="sub-dismiss">收起</button>
      </div>`;
  } catch (err) {
    panel.innerHTML = `<div class="sub-msg">拆解失败：${esc(err.message)}</div>
      <div class="sub-actions"><button class="ghost mini" data-action="sub-dismiss">收起</button></div>`;
  }
}

async function acceptSubtasks(id) {
  try {
    const res = await api("/api/decompose/accept", { method: "POST", body: { id } });
    const panel = document.querySelector(`.sub-panel[data-parent="${id}"]`);
    if (panel) panel.remove();
    await refresh();
    await renderTodosPage();
    alert(`已把 ${res.created} 个子任务加入待办池（可在计划页排期）`);
  } catch (err) { alert(err.message); }
}


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
  await loadTodayCompass();
  setInterval(() => { refreshWx(); refreshAi(); }, 4000);
  setInterval(() => { if (!document.hidden) refresh().catch(() => {}); }, 8000);
}

init();

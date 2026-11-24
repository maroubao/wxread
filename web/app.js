// 原生控制台，所有管理指令在浏览器加密后直连GitHub。
import * as gh from "./gh.js?v=3";
import * as c from "./crypto.js?v=3";
import * as vm from "./view-model.js?v=3";

const $ = id => document.getElementById(id);
const state = {
  cfg: gh.readConfig(), key: null, publicKey: null, keyVersion: 1,
  accounts: [], runs: [], settings: {}, status: "idle", epoch: 0,
  controller: null, refreshTimer: null, refreshing: false, syncedAt: 0, revision: null,
  cache: new Map(), sessions: new Map(), tracks: new Map(),
  accountActions: new Set(),
  qrSession: null, qrPolling: null, qrUrl: null, askingQr: false,
  editId: null, deleteId: null, expiresAt: 0,
};
const accountRows = new Map();
const recordRows = new Map();
const chartColumns = new Map();
const QR_POLL_MS = 4000;
const QR_BUDGET_MS = 15 * 60 * 1000;
const RECEIPT_POLL_MS = 3000;
const RECEIPT_BUDGET_MS = 15 * 60 * 1000;
const RUN_POLL_MS = 5000;
const RUN_BUDGET_MS = 7 * 60 * 60 * 1000;
const CHANNEL_NAMES = { email: "邮件", pushplus: "PushPlus", feishu: "飞书", telegram: "Telegram" };
let toastTimer = null;

function putText(element, value) {
  const text = String(value == null ? "" : value);
  if (element.textContent !== text) element.textContent = text;
}
function say(message, kind = "") {
  const box = $("status-message");
  putText(box, message);
  box.dataset.kind = kind;
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.hidden = true; }, 3000);
}
function aborted() { return new DOMException("连接已结束", "AbortError"); }
function current(epoch) { return epoch === state.epoch && !state.controller?.signal.aborted; }
function check(epoch) { if (!current(epoch)) throw aborted(); }
function delay(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) { reject(aborted()); return; }
    const abort = () => { clearTimeout(timer); reject(aborted()); };
    const timer = setTimeout(() => { signal?.removeEventListener("abort", abort); resolve(); }, ms);
    signal?.addEventListener("abort", abort, { once: true });
  });
}
function showDialog(id) {
  const dialog = $(id);
  if (!dialog.open) dialog.showModal();
}
function closeDialog(id) { if ($(id).open) $(id).close(); }
function releaseQrImage() {
  if (state.qrUrl) URL.revokeObjectURL(state.qrUrl);
  state.qrUrl = null;
}
function endSession(id) {
  const session = state.sessions.get(id);
  if (session) session.secret.fill(0);
  state.sessions.delete(id);
}
function stopConnection(preserveQr = false) {
  state.epoch += 1;
  state.controller?.abort();
  clearInterval(state.refreshTimer);
  state.refreshTimer = null;
  for (const id of state.sessions.keys()) endSession(id);
  state.tracks.clear();
  state.accountActions.clear();
  state.cache.clear();
  state.qrSession = null;
  state.qrPolling = null;
  state.askingQr = false;
  state.refreshing = false;
  releaseQrImage();
  if (!preserveQr) try { sessionStorage.removeItem("wxread.qr.v2"); } catch { /* 私密模式 */ }
}
function forget() {
  stopConnection();
  state.key?.fill(0);
  state.key = null;
  state.publicKey = null;
  state.cfg = { repo: "", token: "", branch: "wxread-state" };
  state.accounts = [];
  state.runs = [];
  state.settings = {};
  state.status = "idle";
  state.syncedAt = 0;
  state.revision = null;
  state.expiresAt = 0;
  gh.clearConfig();
  for (const input of document.querySelectorAll('input[type="password"]')) input.value = "";
  $("inp-remember").checked = false;
  showQrPanel(false);
  renderAll();
  say("已断开并清除本机凭据。");
}
function updateConnection() {
  let kind = "", text = "未连接";
  if (state.status === "connecting") { kind = "warn"; text = "连接中"; }
  if (state.status === "error") { kind = "bad"; text = "连接失败"; }
  if (state.status === "connected") {
    const age = Date.now() - state.syncedAt;
    kind = age > 120000 ? "bad" : age > 30000 ? "warn" : "ok";
    text = age > 120000 ? "连接中断" : age > 30000 ? "连接中断，重试中" : state.cfg.repo;
  }
  $("conn-dot").className = "dot " + kind;
  putText($("conn-text"), text);
}
function updateButtons() {
  const connected = state.status === "connected";
  $("btn-refresh").disabled = !connected || state.refreshing;
  $("btn-qr").disabled = !connected || !state.publicKey || state.askingQr;
  $("btn-run").disabled = connected && state.tracks.has("read.yml");
  putText($("btn-run"), connected ? state.tracks.has("read.yml") ? "运行中" : "运行全部" : "连接仓库");
  putText($("btn-config"), connected ? "连接设置" : "连接仓库");
  $("btn-stop").disabled = !connected || (!state.tracks.has("read.yml") && !state.accounts.some(account => account.progress?.status === "running"));
  $("btn-connect").disabled = state.status === "connecting";
}
async function connect(restoring = false) {
  const repo = $("inp-repo").value.trim();
  const token = $("inp-token").value.trim();
  const stateKeyText = $("inp-statekey").value.trim();
  const branch = $("inp-branch").value.trim() || "wxread-state";
  if (!/^[\w.-]+\/[\w.-]+$/.test(repo)) throw new Error("仓库填写 owner/repo。");
  if (!token || !stateKeyText) throw new Error("填入token和状态密钥后连接。");
  const key = c.stateKeyFromBase64(stateKeyText);
  const previousExpiry = restoring ? Number(state.cfg.expiresAt) || 0 : 0;
  stopConnection(true);
  state.key?.fill(0);
  gh.resetTokenExpiry();
  state.controller = new AbortController();
  state.cfg = { repo, token, branch, signal: state.controller.signal };
  state.key = key;
  state.publicKey = null;
  state.accounts = [];
  state.runs = [];
  state.settings = {};
  state.revision = null;
  state.status = "connecting";
  $("connect-error").hidden = true;
  renderAll();
  const epoch = state.epoch;
  try {
    const control = await gh.getJson(state.cfg, "control.json");
    check(epoch);
    if (!control?.public_key) throw new Error("状态分支尚未初始化，先运行control.yml的init。");
    state.publicKey = c.b64d(control.public_key);
    if (state.publicKey.length !== 32) throw new Error("指令公钥无效。");
    state.keyVersion = Number(control.key_version || 1);
    await refreshAll(true);
    check(epoch);
    state.status = "connected";
    try {
      if (!restoring) gh.saveConfig({ repo, token, branch, stateKeyText }, $("inp-remember").checked);
      state.expiresAt = restoring ? previousExpiry : $("inp-remember").checked ? Date.now() + gh.REMEMBER_MS : 0;
    } catch {
      $("inp-remember").checked = false;
      state.expiresAt = 0;
      say("已连接，浏览器存储不可用，本次页面有效。", "warn");
    }
    closeDialog("connect-dialog");
    state.refreshTimer = setInterval(() => refreshAll().catch(reportError), 20000);
    restoreQrSession();
    renderAll();
    say("已连接，账户状态已同步。", "ok");
  } catch (error) {
    if (!current(epoch)) return;
    state.status = "error";
    putText($("connect-error"), error.message);
    $("connect-error").hidden = false;
    showDialog("connect-dialog");
    renderAll();
    throw error;
  }
}
async function encryptedFile(cfg, key, path) {
  const epoch = state.epoch;
  const file = await gh.getFile(cfg, path);
  check(epoch);
  if (!file) return null;
  const cacheKey = cfg.repo + "|" + cfg.branch + "|" + path;
  const cached = state.cache.get(cacheKey);
  if (cached?.sha === file.sha) return cached.value;
  const envelope = JSON.parse(new TextDecoder().decode(file.bytes));
  const value = c.decryptState(envelope, key, c.CONTEXT_TEMPLATE(cfg.repo, Number(envelope.kv || state.keyVersion)));
  state.cache.set(cacheKey, { sha: file.sha, value });
  return value;
}
async function loadAccounts(cfg, key) {
  const files = await gh.listDir(cfg, "accounts");
  const accounts = await Promise.all(files.filter(file => file.name.endsWith(".enc.json")).map(async file => {
    const id = file.name.slice(0, -9);
    try {
      const value = await encryptedFile(cfg, key, "accounts/" + file.name);
      if (!value || Array.isArray(value) || typeof value !== "object" || !value.cookies || (value.id && value.id !== id)) throw new Error("账户数据不完整");
      return { ...value, id };
    } catch (error) {
      if (error.name === "AbortError") throw error;
      return { id, label: "无法读取", status: "bad", note: error.message, enabled: false };
    }
  }));
  return accounts.sort((left, right) => String(left.id).localeCompare(String(right.id)));
}
async function loadRuns(cfg, key) {
  const months = vm.stateMonths();
  const parts = await Promise.all(months.map(month => encryptedFile(cfg, key, "runs/" + month + ".enc.json")));
  return vm.sortRuns(parts.flatMap(part => Array.isArray(part?.runs) ? part.runs : []));
}
async function refreshAll(force = false) {
  if (!state.key || !state.cfg.token || state.refreshing) return;
  const epoch = state.epoch, cfg = state.cfg, key = state.key;
  state.refreshing = true;
  updateButtons();
  try {
    const revision = await gh.latestBranchCommit(cfg);
    check(epoch);
    if (!force && revision && revision === state.revision) {
      state.syncedAt = Date.now();
      updateConnection();
      return;
    }
    const [accounts, runs, settings] = await Promise.all([
      loadAccounts(cfg, key), loadRuns(cfg, key), encryptedFile(cfg, key, "settings.enc.json"),
    ]);
    check(epoch);
    if (accounts.length && accounts.every(account => account.status === "bad")) throw new Error("账户无法解密，检查状态密钥和密钥版本。");
    state.accounts = accounts;
    state.runs = runs;
    state.settings = settings || {};
    state.syncedAt = Date.now();
    state.revision = revision;
    if (state.status !== "error") state.status = "connected";
    renderAll();
  } finally {
    if (epoch === state.epoch) {
      state.refreshing = false;
      updateButtons();
    }
  }
}
function rowStatus(account) {
  if (account.status === "bad") return ["bad", "无法读取", "none"];
  if (account.enabled === false) return ["neutral", "已停用", "none"];
  if (!vm.payloadReady(account) || ["payload_invalid", "no_synckey"].includes(account.note)) return ["warn", "参数待准备", "capture"];
  if (account.status === "needs_login") return ["warn", "需重新绑定", "rebind"];
  if (account.progress?.status === "running") {
    return ["run", "运行中 " + Math.min(100, Math.round(100 * vm.count(account.progress.read_count) / vm.targetCount(account))) + "%", "stop"];
  }
  return ["ok", "正常", "run"];
}
function renderAccounts(data) {
  const tbody = $("acc-body");
  const ids = new Set(state.accounts.map(account => String(account.id)));
  for (const [id, row] of accountRows) {
    if (!ids.has(id)) {
      const focused = row.contains(document.activeElement);
      row.remove();
      accountRows.delete(id);
      if (focused) $("btn-qr").focus();
    }
  }
  for (const account of state.accounts) {
    const id = String(account.id);
    let row = accountRows.get(id);
    if (!row) {
      row = $("account-row-template").content.firstElementChild.cloneNode(true);
      row.dataset.id = id;
      tbody.append(row);
      accountRows.set(id, row);
    }
    const name = String(account.label || id);
    putText(row.querySelector(".avatar"), [...name][0] || "账");
    putText(row.querySelector(".account-name"), name);
    row.querySelector(".account-name").title = name;
    putText(row.querySelector(".account-id"), id.slice(0, 12));
    const [kind, status, action] = rowStatus(account);
    const badge = row.querySelector(".account-status");
    badge.className = "badge account-status " + kind;
    putText(badge, status);
    badge.title = account.note || status;
    const done = data.byAccount.get(id) || 0;
    putText(row.querySelector(".read-text"), vm.minutes(done) + " / " + vm.minutes(vm.targetCount(account)));
    const currentCount = action === "stop" ? vm.count(account.progress.read_count) : done;
    const percent = Math.min(100, Math.round(100 * currentCount / vm.targetCount(account)));
    const meter = row.querySelector(".progress");
    meter.classList.toggle("complete", percent === 100 && action !== "stop");
    meter.setAttribute("aria-label", name + "阅读进度");
    meter.setAttribute("aria-valuenow", String(percent));
    row.querySelector(".progress i").style.width = percent + "%";
    putText(row.querySelector(".last-ok"), vm.formatTime(account.last_ok_at));
    row.querySelector(".last-ok").title = account.last_ok_at || "";
    putText(row.querySelector(".last-renew"), relativeTime(account.renewed_at));
    row.querySelector(".last-renew").title = account.renewed_at || "";
    const run = row.querySelector(".row-run");
    run.dataset.act = action;
    run.disabled = action === "none";
    run.classList.toggle("rebind", action === "rebind" || action === "capture");
    putText(run, action === "rebind" ? "重绑" : action === "capture" ? "取参" : action === "stop" ? "■" : "▶");
    run.setAttribute("aria-label", (action === "rebind" ? "重新绑定" : action === "capture" ? "自动获取参数" : action === "stop" ? "停止" : "运行") + name);
    for (const button of row.querySelectorAll("button")) {
      button.dataset.id = id;
      if (button.dataset.act === "edit" || button.dataset.act === "delete") {
        button.disabled = account.status === "bad";
        button.setAttribute("aria-label", (button.dataset.act === "edit" ? "编辑" : "删除") + name);
      }
      if (state.accountActions.has(id)) button.disabled = true;
    }
  }
  $("account-table").hidden = !state.accounts.length;
  $("account-empty").hidden = state.accounts.length > 0;
  putText($("account-empty-text"), state.status === "connected" ? "还没有账户，扫码添加后自动获取阅读参数。" : "连接仓库后查看账户。");
  putText($("btn-empty-action"), state.status === "connected" ? "添加账户" : "连接仓库");
  putText($("acc-count"), state.accounts.length ? state.accounts.length + " 个" : "");
}
function relativeTime(value) {
  if (!value) return "—";
  const seconds = (Date.now() - new Date(value).getTime()) / 1000;
  if (!Number.isFinite(seconds)) return "—";
  if (seconds < 60) return "刚刚";
  if (seconds < 3600) return Math.floor(seconds / 60) + " 分前";
  if (seconds < 86400) return Math.floor(seconds / 3600) + " 小时前";
  return Math.floor(seconds / 86400) + " 天前";
}
function renderRuns() {
  const records = vm.sortRuns(state.runs).slice(0, 100);
  const keys = new Set(records.map(vm.runKey));
  for (const [key, row] of recordRows) if (!keys.has(key)) { row.remove(); recordRows.delete(key); }
  let previous = null;
  for (const record of records) {
    const key = vm.runKey(record);
    let row = recordRows.get(key);
    if (!row) { row = $("record-row-template").content.firstElementChild.cloneNode(true); recordRows.set(key, row); }
    const anchor = previous ? previous.nextSibling : $("run-list").firstChild;
    if (anchor !== row) $("run-list").insertBefore(row, anchor);
    previous = row;
    putText(row.querySelector("time"), vm.formatTime(vm.runTime(record)));
    row.querySelector("time").title = vm.runTime(record);
    putText(row.querySelector(".record-name"), record.label || record.account_id || "账户");
    let kind = "bad", text = "未完成";
    if (vm.successful(record)) { kind = "ok"; text = "成功"; }
    else if (record.reason === "cancelled") { kind = "neutral"; text = "已停止"; }
    else if (record.state_error) text = "保存失败";
    else if (record.level === "L3") { kind = "warn"; text = "需处理"; }
    const badge = row.querySelector(".badge");
    badge.className = "badge " + kind;
    putText(badge, text);
    badge.title = record.reason || text;
    putText(row.querySelector(".record-count"), vm.minutes(record.read_count) + " / " + vm.minutes(vm.targetCount(record)));
    const seconds = Math.max(0, Math.round((Date.parse(record.finished_at) - Date.parse(record.started_at)) / 1000));
    putText(row.querySelector(".record-duration"), Number.isFinite(seconds) ? Math.floor(seconds / 60) + " 分 " + String(seconds % 60).padStart(2, "0") + " 秒" : "—");
  }
  $("record-table").hidden = !records.length;
  $("record-empty").hidden = records.length > 0;
  putText($("run-hint"), "最近 " + records.length + " 条 · 每次成功读取按 30 秒折算");
}
function renderChart() {
  const days = vm.chartData(state.accounts, state.runs);
  const keys = new Set(days.map(day => day.day));
  for (const [key, column] of chartColumns) if (!keys.has(key)) { column.remove(); chartColumns.delete(key); }
  const goal = Math.max(1, ...days.map(day => day.target));
  const max = Math.max(goal, Math.ceil(Math.max(0, ...days.map(day => day.value || 0)) / goal) * goal);
  for (const day of days) {
    let column = chartColumns.get(day.day);
    if (!column) {
      column = $("chart-column-template").content.firstElementChild.cloneNode(true);
      $("run-bars").append(column);
      chartColumns.set(day.day, column);
    }
    const percent = day.value == null ? 100 : Math.min(100, 100 * day.value / max);
    column.classList.toggle("full", day.full);
    column.classList.toggle("missing", day.value == null);
    column.classList.toggle("today", day.day === vm.dayKey(Date.now()));
    column.style.setProperty("--height", percent + "%");
    putText(column.querySelector(".bar-value"), day.value == null ? "—" : String(day.value));
    putText(column.querySelector(".bar-date"), day.day.slice(-2));
    const description = day.day + (day.value == null ? " 无记录" : " " + day.value + " 分钟");
    column.setAttribute("aria-label", description);
    putText(column.querySelector(".bar-tooltip"), description);
  }
  const empty = days.every(day => day.value == null);
  $("chart").hidden = empty;
  $("chart-empty").hidden = !empty;
}
function renderAll() {
  const connected = state.status === "connected";
  const data = vm.summary(state.accounts, state.runs);
  putText($("m-read"), connected && data.hasToday ? vm.minutes(data.readCount) : "—");
  putText($("m-read-hint"), connected ? "可运行目标 " + vm.minutes(data.target) + " 分 · 还差 " + vm.minutes(data.remaining) + " 分" : "连接后显示阅读进度");
  putText($("m-accounts"), connected ? state.accounts.length : "—");
  putText($("m-accounts-hint"), connected ? data.activeCount + " 可运行 · " + data.completed + " 已达标" : "连接后显示账户状态");
  putText($("m-last"), connected && data.successRate != null ? data.successRate : "—");
  putText($("m-last-hint"), connected && data.totalRuns ? data.wins + " 成功 / " + data.totalRuns + " 次运行" : "暂无运行记录");
  putText($("m-needs"), connected ? data.needsLogin.length : "—");
  putText($("m-needs-hint"), data.needsPayload.length ? data.needsPayload.length + " 个账户待自动取参" : "扫码后自动恢复");
  $("needs-card").classList.toggle("warning", data.needsLogin.length > 0);
  const problem = data.needsLogin[0] || data.needsPayload[0];
  $("account-alert").hidden = !connected || !problem;
  if (problem) {
    putText($("account-alert-title"), (problem.label || problem.id) + (data.needsLogin.includes(problem) ? " 需要重新绑定" : " 阅读参数待准备"));
    putText($("account-alert-detail"), data.needsLogin.includes(problem) ? "扫码确认后自动获取阅读参数并恢复运行。" : "使用已有登录态重新自动获取阅读参数。");
    $("btn-alert-action").dataset.id = problem.id;
    $("btn-alert-action").dataset.action = data.needsLogin.includes(problem) ? "rebind" : "capture";
    putText($("btn-alert-action"), data.needsLogin.includes(problem) ? "重新绑定" : "自动获取");
  }
  const schedule = state.settings.schedule || {};
  putText($("plan-warm"), schedule.warm_hours === 0 || schedule.enabled === false ? "已关闭" : "每 6 小时");
  const nowClock = new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(Date.now());
  putText($("plan-next"), schedule.enabled === false ? "已暂停" : (nowClock < "01:07" ? "今天" : "明天") + " 01:07");
  const channels = state.settings.notifications?.enabled ? state.settings.notifications.channels || [] : [];
  putText($("plan-notify"), channels.map(channel => CHANNEL_NAMES[channel] || channel).join(" · ") || "未配置");
  putText($("sync-time"), state.syncedAt ? "上次同步 " + relativeTime(state.syncedAt) : "尚未同步");
  putText($("overview-hint"), vm.dayKey(Date.now()) + " · 北京时间");
  renderAccounts(data);
  renderRuns();
  renderChart();
  document.body.classList.toggle("loading", state.status === "connecting");
  updateConnection();
  updateButtons();
}
function requireConnection() {
  if (state.status !== "connected" || !state.publicKey) throw new Error("先连接仓库。");
}
async function dispatchInstruction(action, payload) {
  requireConnection();
  const id = crypto.randomUUID().replaceAll("-", "").slice(0, 24);
  const reply = c.newKeypair();
  const session = { id, action, secret: reply.secretKey, startedAt: Date.now(), epoch: state.epoch, cfg: state.cfg };
  state.sessions.set(id, session);
  const envelope = c.seal({
    action, session_id: id, issued_at: Math.floor(Date.now() / 1000),
    nonce: c.b64e(c.randomBytes(12)), payload, reply_key: c.b64e(reply.publicKey),
  }, state.publicKey);
  try {
    await gh.dispatchWorkflow(session.cfg, "control.yml", { control: "instruction", instruction: JSON.stringify(envelope), session_id: id });
  } catch (error) {
    if ([401, 403, 404, 422].includes(error.status) || error.name === "AbortError") { endSession(id); throw error; }
    say("提交状态待确认，正在检查同一条指令回执。", "warn");
  }
  check(session.epoch);
  return session;
}
function decryptReceipt(session, file) {
  check(session.epoch);
  const receipt = c.unseal(JSON.parse(new TextDecoder().decode(file.bytes)), session.secret);
  if (receipt.session_id !== session.id || receipt.action !== session.action || (!receipt.pending && typeof receipt.ok !== "boolean")) throw new Error("回执字段无法验证。");
  return receipt;
}
async function pollReceipt(sessionId, action) {
  const session = state.sessions.get(sessionId);
  if (!session || session.action !== action) throw aborted();
  while (Date.now() - session.startedAt < RECEIPT_BUDGET_MS) {
    check(session.epoch);
    try {
      const file = await gh.getFile(session.cfg, "responses/" + sessionId + ".json");
      if (file) {
        const receipt = decryptReceipt(session, file);
        if (!receipt.pending) return receipt;
      }
    } catch (error) {
      if (error.name === "AbortError" || [401, 403].includes(error.status) || error instanceof c.CryptoFailure || error instanceof SyntaxError || error.message === "回执字段无法验证。") throw error;
    }
    await delay(RECEIPT_POLL_MS, session.cfg.signal);
  }
  throw new Error("尚未收到操作确认，查看Actions里的control运行。");
}
async function sendInstruction(action, payload) {
  const session = await dispatchInstruction(action, payload);
  say("操作已提交，正在等待结果。");
  try {
    const receipt = await pollReceipt(session.id, action);
    check(session.epoch);
    if (receipt.ok !== true) throw new Error(receipt.error || receipt.reason || "操作未成功。");
    await refreshAll(true);
    return receipt;
  } finally { endSession(session.id); }
}
function showQrPanel(live) {
  $("qr-idle").hidden = live;
  $("qr-live").hidden = !live;
  if (!live) { releaseQrImage(); $("qr-slot").replaceChildren(); $("qr-slot").classList.remove("capturing"); }
}
function clearQrSession() {
  const session = state.qrSession;
  state.qrSession = null;
  try { sessionStorage.removeItem("wxread.qr.v2"); } catch { /* 私密模式 */ }
  if (session) endSession(session.id);
  releaseQrImage();
  return session;
}
async function finishQr(sessionId, receipt, ok) {
  if (state.qrSession?.id !== sessionId) return;
  const epoch = state.epoch;
  clearQrSession();
  showQrPanel(false);
  closeDialog("qr-dialog");
  await refreshAll(true);
  check(epoch);
  if (!ok) throw new Error(receipt.error || receipt.reason || "绑定未成功，可重新扫码。");
  if (!receipt.payload_captured) {
    say("登录已保存，正在使用登录态自动重试取参。", "warn");
    await sendInstruction("capture", { account_id: receipt.account_id });
  }
  check(epoch);
  say("账户已绑定，阅读参数已自动获取，可以直接运行。", "ok");
}
async function qrReceipt(sessionId) {
  const session = state.sessions.get(sessionId);
  if (!session) throw new Error("本次回执密钥已失效，无法确认绑定结果。");
  const file = await gh.getFile(session.cfg, "responses/" + sessionId + ".json");
  if (!file || state.qrSession?.id !== sessionId) return null;
  return decryptReceipt(session, file);
}
async function findSessionQr(sessionId) {
  const session = state.sessions.get(sessionId);
  if (!session) throw aborted();
  const files = await gh.listDir(session.cfg, "responses");
  const prefix = "qr-" + sessionId + "-";
  const names = files.filter(file => file.name.startsWith(prefix) && file.name.endsWith(".png")).map(file => file.name).sort();
  return names.length ? "responses/" + names[names.length - 1] : null;
}
async function pollQr(startedAt = Date.now(), shownPath = null) {
  const session = state.qrSession;
  if (!session || state.qrPolling === session.id) return;
  const sessionId = session.id;
  state.qrPolling = sessionId;
  try {
    while (state.qrSession?.id === sessionId && Date.now() - startedAt < QR_BUDGET_MS) {
      check(session.epoch);
      try {
        const receipt = await qrReceipt(sessionId);
        if (receipt && !receipt.pending) { await finishQr(sessionId, receipt, receipt.ok === true); return; }
        if (receipt?.pending) {
          releaseQrImage();
          $("qr-slot").classList.add("capturing");
          putText($("qr-slot"), receipt.message || "登录已确认，正在自动获取阅读参数。");
          putText($("qr-hint"), "自动获取中 · 已等 " + Math.round((Date.now() - startedAt) / 1000) + " 秒");
        } else {
          const path = await findSessionQr(sessionId);
          if (path && path !== shownPath) {
            const file = await gh.getFile(session.cfg, path);
            if (file && state.qrSession?.id === sessionId) {
              releaseQrImage();
              state.qrUrl = URL.createObjectURL(new Blob([file.bytes], { type: "image/png" }));
              const image = document.createElement("img");
              image.src = state.qrUrl;
              image.alt = "微信读书登录二维码，用微信扫描";
              $("qr-slot").classList.remove("capturing");
              $("qr-slot").replaceChildren(image);
              shownPath = path;
              putText($("qr-hint"), "用微信扫码并确认登录，过期后自动换新码。");
            }
          } else if (!shownPath) putText($("qr-slot"), "正在等待Actions生成二维码…");
        }
      } catch (error) {
        if (state.qrSession?.id !== sessionId) throw error;
        if (error.name === "AbortError" || [401, 403].includes(error.status)) throw error;
        putText($("qr-hint"), "暂未确认绑定结果：" + error.message);
      }
      await delay(QR_POLL_MS, session.cfg.signal);
    }
    if (state.qrSession?.id === sessionId) {
      clearQrSession();
      showQrPanel(false);
      say("本轮扫码等待已结束，可重新发起。", "warn");
    }
  } finally {
    if (state.qrPolling === sessionId) state.qrPolling = null;
  }
}
async function askQr() {
  if (state.qrSession || state.askingQr) return;
  state.askingQr = true;
  updateButtons();
  try {
    const session = await dispatchInstruction("qr", { label: $("inp-qr-label").value.trim(), timeout: 600 });
    state.qrSession = session;
    showQrPanel(true);
    putText($("qr-slot"), "正在生成二维码…");
    try {
      sessionStorage.setItem("wxread.qr.v2", JSON.stringify({ id: session.id, repo: state.cfg.repo, branch: state.cfg.branch, secret: c.b64e(session.secret), startedAt: session.startedAt }));
    } catch { /* 私密模式仅使用内存 */ }
    await pollQr(session.startedAt);
  } finally { state.askingQr = false; updateButtons(); }
}
function restoreQrSession() {
  try {
    const saved = JSON.parse(sessionStorage.getItem("wxread.qr.v2") || "null");
    if (!saved || saved.repo !== state.cfg.repo || saved.branch !== state.cfg.branch || Date.now() - saved.startedAt >= QR_BUDGET_MS) {
      sessionStorage.removeItem("wxread.qr.v2"); return;
    }
    const session = { ...saved, action: "qr", secret: c.b64d(saved.secret), epoch: state.epoch, cfg: state.cfg };
    state.sessions.set(session.id, session);
    state.qrSession = session;
    showDialog("qr-dialog");
    showQrPanel(true);
    pollQr(session.startedAt).catch(reportError);
  } catch { /* 无可恢复扫码会话 */ }
}
async function cancelQr() {
  const session = clearQrSession();
  showQrPanel(false);
  closeDialog("qr-dialog");
  if (!session) return;
  const runs = await gh.listRuns(session.cfg, "control.yml");
  const mine = runs.find(run => String(run.display_title || "").includes(session.id) && run.status !== "completed");
  if (mine) await gh.cancelRun(session.cfg, mine.id);
  say("本次绑定已取消。");
}
function openQr(label = "") {
  requireConnection();
  $("inp-qr-label").value = label;
  showDialog("qr-dialog");
  if (!state.qrSession) showQrPanel(false);
}
async function startRun(workflow, accountId = "") {
  requireConnection();
  if (state.tracks.has(workflow)) throw new Error("当前任务仍在运行，先查看结果。");
  const requestId = crypto.randomUUID().replaceAll("-", "").slice(0, 24);
  const inputs = { request_id: requestId };
  if (workflow === "read.yml") {
    if (accountId) inputs.account_ids = accountId;
    const pace = vm.count($("inp-pace").value);
    const deadline = vm.count($("inp-deadline").value);
    if (pace) inputs.pace = String(Math.round(pace));
    if (deadline) inputs.deadline = String(Math.round(deadline));
  }
  const track = { requestId, epoch: state.epoch, cfg: state.cfg, startedAt: Date.now(), id: null };
  state.tracks.set(workflow, track);
  updateButtons();
  try {
    await gh.dispatchWorkflow(track.cfg, workflow, inputs);
    check(track.epoch);
    say("任务已提交，正在等待Actions运行。", "ok");
    trackRun(workflow, track).catch(reportError);
  } catch (error) { state.tracks.delete(workflow); updateButtons(); throw error; }
}
async function trackRun(workflow, track) {
  try {
    while (current(track.epoch) && state.tracks.get(workflow) === track && Date.now() - track.startedAt < RUN_BUDGET_MS) {
      try {
        const runs = await gh.listRuns(track.cfg, workflow);
        check(track.epoch);
        const mine = track.id ? runs.find(run => run.id === track.id) : runs.find(run => String(run.display_title || "").includes(track.requestId));
        if (mine) {
          track.id = mine.id;
          if (mine.status === "completed") {
            await refreshAll(true);
            check(track.epoch);
            say("任务" + (mine.conclusion === "success" ? "完成" : "结束：" + mine.conclusion) + "，运行结果已刷新。", mine.conclusion === "success" ? "ok" : "warn");
            return;
          }
        }
      } catch (error) {
        if (error.name === "AbortError" || [401, 403].includes(error.status)) throw error;
      }
      await delay(RUN_POLL_MS, track.cfg.signal);
    }
    if (current(track.epoch)) say("任务尚未确认结束，查看Actions运行状态。", "warn");
  } finally {
    if (state.tracks.get(workflow) === track) state.tracks.delete(workflow);
    updateButtons();
  }
}
async function stopAll() {
  requireConnection();
  const runs = await gh.listRuns(state.cfg, "read.yml");
  const active = runs.filter(run => ["queued", "in_progress", "waiting", "requested"].includes(run.status));
  if (!active.length) { say("当前没有运行中的阅读任务。"); return; }
  await Promise.all(active.map(run => gh.cancelRun(state.cfg, run.id)));
  say("已请求停止阅读任务，正在等待进度回写。", "warn");
}
function openSettings() {
  requireConnection();
  const schedule = state.settings.schedule || {};
  $("inp-auto-enabled").checked = schedule.enabled !== false;
  $("inp-warm-enabled").checked = schedule.warm_hours !== 0;
  const block = state.settings.notifications || {};
  for (const input of document.querySelectorAll('input[name="channel"]')) input.checked = block.enabled !== false && (block.channels || ["feishu"]).includes(input.value);
  for (const [channel, title] of Object.entries(CHANNEL_NAMES)) {
    const cfg = block[channel] || {};
    putText($(channel + "-saved"), Object.keys(cfg).length ? "已保存" : "");
    const secretFields = channel === "email" ? ["password"] : channel === "pushplus" ? ["token"] : channel === "feishu" ? ["webhook", "secret"] : ["bot-token"];
    for (const field of secretFields) {
      const input = $(channel + "-" + field);
      input.value = "";
      input.placeholder = Object.keys(cfg).length ? "已保存，留空沿用" : title + "口令";
    }
  }
  const email = block.email || {};
  for (const name of ["host", "port", "user", "to"]) $("email-" + name).value = email[name] || "";
  $("telegram-chat-id").value = block.telegram?.chat_id || "";
  showDialog("settings-dialog");
}
async function saveNotifications() {
  const channels = [...document.querySelectorAll('input[name="channel"]:checked')].map(input => input.value);
  const payload = { enabled: channels.length > 0, channels };
  const fields = {
    email: { host: "email-host", port: "email-port", user: "email-user", password: "email-password", to: "email-to" },
    pushplus: { token: "pushplus-token" }, feishu: { webhook: "feishu-webhook", secret: "feishu-secret" },
    telegram: { bot_token: "telegram-bot-token", chat_id: "telegram-chat-id" },
  };
  for (const [channel, inputs] of Object.entries(fields)) {
    const cfg = {};
    for (const [name, id] of Object.entries(inputs)) {
      const value = $(id).value.trim();
      if (value) cfg[name] = name === "port" ? Number(value) : value;
    }
    if (Object.keys(cfg).length) payload[channel] = cfg;
  }
  await sendInstruction("notifications", payload);
  for (const input of $("notify-form").querySelectorAll('input[type="password"]')) input.value = "";
  say("通知配置已保存。", "ok");
}
function editAccount(id) {
  const account = state.accounts.find(item => item.id === id);
  if (!account) return;
  state.editId = id;
  $("edit-label").value = account.label || "";
  $("edit-target").value = vm.minutes(vm.targetCount(account));
  $("edit-enabled").checked = account.enabled !== false;
  showDialog("edit-dialog");
  $("edit-label").focus();
}
async function saveAccount() {
  await sendInstruction("update", { account_id: state.editId, label: $("edit-label").value.trim(), target_count: Math.round(Number($("edit-target").value) * 2), enabled: $("edit-enabled").checked });
  closeDialog("edit-dialog");
  say("账户设置已保存。", "ok");
}
function requestDelete(id) {
  const account = state.accounts.find(item => item.id === id);
  if (!account) return;
  state.deleteId = id;
  putText($("delete-title"), "删除 " + (account.label || id));
  putText($("delete-description"), "该账户的登录凭据会删除，运行历史继续保留。");
  showDialog("delete-dialog");
}
async function deleteAccount() {
  await sendInstruction("delete", { account_id: state.deleteId });
  closeDialog("delete-dialog");
  state.deleteId = null;
  $("btn-qr").focus();
  say("账户已删除。");
}
async function accountAction(button) {
  const account = state.accounts.find(item => item.id === button.dataset.id);
  if (!account) return;
  const action = button.dataset.act;
  if (action === "edit") { editAccount(account.id); return; }
  if (action === "delete") { requestDelete(account.id); return; }
  if (action === "rebind") { openQr(account.label); return; }
  if (action === "run") { await startRun("read.yml", account.id); return; }
  if (action === "stop") { await sendInstruction("stop", { account_id: account.id, run_id: account.progress.run_id }); say("已请求停止该账户。"); return; }
  if (action === "capture") { await sendInstruction("capture", { account_id: account.id }); say("阅读参数已自动更新。", "ok"); }
}
function reportError(error) {
  if (error?.name !== "AbortError") say(error.message || "操作暂未确认。", "bad");
}
function click(id, handler) {
  $(id).addEventListener("click", async event => {
    const button = $(id);
    if (button.getAttribute("aria-busy") === "true") return;
    button.setAttribute("aria-busy", "true");
    button.disabled = true;
    try { await handler(event); } catch (error) { reportError(error); }
    finally { button.removeAttribute("aria-busy"); button.disabled = false; updateButtons(); }
  });
}
function submit(id, buttonId, handler) {
  $(id).addEventListener("submit", async event => {
    event.preventDefault();
    const button = $(buttonId);
    if (button.getAttribute("aria-busy") === "true") return;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    try { await handler(); } catch (error) { reportError(error); }
    finally { button.disabled = false; button.removeAttribute("aria-busy"); updateButtons(); }
  });
}
function bind() {
  for (const button of document.querySelectorAll("[data-close]")) button.addEventListener("click", () => button.closest("dialog").close());
  for (const dialog of document.querySelectorAll("dialog")) dialog.addEventListener("close", () => {
    for (const input of dialog.querySelectorAll('input[type="password"]')) if (dialog.id !== "connect-dialog") input.value = "";
  });
  $("qr-dialog").addEventListener("cancel", event => { event.preventDefault(); cancelQr().catch(reportError); });
  click("btn-config", () => showDialog("connect-dialog"));
  click("btn-refresh", () => refreshAll(true));
  click("btn-forget", forget);
  click("btn-qr", () => openQr());
  click("btn-qr-start", askQr);
  click("btn-qr-close", cancelQr);
  click("btn-qr-cancel", cancelQr);
  click("btn-qr-poll", async () => {
    if (!state.qrSession) return;
    const receipt = await qrReceipt(state.qrSession.id);
    if (receipt && !receipt.pending) await finishQr(state.qrSession.id, receipt, receipt.ok === true);
  });
  click("btn-run", () => state.status === "connected" ? startRun("read.yml") : showDialog("connect-dialog"));
  click("btn-run-now", () => startRun("read.yml"));
  click("btn-warm-now", () => startRun("warm.yml"));
  click("btn-stop", stopAll);
  click("btn-empty-action", () => state.status === "connected" ? openQr() : showDialog("connect-dialog"));
  for (const id of ["btn-settings", "btn-mobile-settings", "btn-plan-settings"]) click(id, () => state.status === "connected" ? openSettings() : showDialog("connect-dialog"));
  click("btn-alert-action", async () => {
    const button = $("btn-alert-action");
    const account = state.accounts.find(item => item.id === button.dataset.id);
    if (button.dataset.action === "capture") { await sendInstruction("capture", { account_id: button.dataset.id }); say("阅读参数已自动更新。", "ok"); }
    else openQr(account?.label || "");
  });
  click("btn-delete-confirm", deleteAccount);
  click("btn-test-mail", async () => { await sendInstruction("test-mail", {}); say("测试邮件已发送。", "ok"); });
  click("btn-test-push", async () => {
    const channel = $("inp-test-channel").value;
    if (!channel) throw new Error("选一个推送渠道并保存后测试。");
    await sendInstruction("test-push", { channel }); say("测试推送已发送。", "ok");
  });
  submit("connect-form", "btn-connect", connect);
  submit("edit-form", "btn-edit-save", saveAccount);
  submit("schedule-form", "btn-schedule", async () => { await sendInstruction("schedule", { enabled: $("inp-auto-enabled").checked, warm_hours: $("inp-warm-enabled").checked ? 6 : 0 }); say("自动计划已保存。", "ok"); });
  submit("notify-form", "btn-notify", saveNotifications);
  $("acc-body").addEventListener("click", async event => {
    const button = event.target.closest("button[data-act]");
    if (!button || button.disabled || state.accountActions.has(button.dataset.id)) return;
    const id = button.dataset.id, epoch = state.epoch;
    state.accountActions.add(id);
    button.disabled = true;
    try { await accountAction(button); } catch (error) { reportError(error); }
    finally {
      if (current(epoch)) { state.accountActions.delete(id); renderAll(); }
    }
  });
  for (const link of document.querySelectorAll(".tabs a")) link.addEventListener("click", () => {
    for (const tab of document.querySelectorAll(".tabs a")) tab.removeAttribute("aria-current");
    link.setAttribute("aria-current", "location");
  });
}
function restore() {
  $("inp-repo").value = state.cfg.repo || "";
  $("inp-token").value = state.cfg.token || "";
  $("inp-statekey").value = state.cfg.stateKeyText || "";
  $("inp-branch").value = state.cfg.branch || "wxread-state";
  $("inp-remember").checked = Boolean(state.cfg.remember);
  renderAll();
  if (state.cfg.token && state.cfg.stateKeyText) connect(true).catch(reportError);
}
bind();
restore();
setInterval(() => {
  if (state.expiresAt && Date.now() >= state.expiresAt) { forget(); say("记住连接已到24小时，重新连接后继续。", "warn"); }
  updateConnection();
}, 1000);

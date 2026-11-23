// 控制台前端：零构建，直连 GitHub REST。
// 明文（cookie、口令）只在浏览器与 worker 之间以密文流动；localStorage 只放 token 与本机状态密钥。

import * as gh from "./gh.js";
import * as c from "./crypto.js";

const $ = (id) => document.getElementById(id);

const state = {
  cfg: gh.readConfig(),
  stateKey: null,
  publicKey: null,
  keyVersion: 1,
  accounts: [],
  runs: [],
  sessions: new Map(), // session_id -> 回执私钥
  pollTimer: null,
  qrShots: 0,
  qrSession: null,
};

// 二维码轮询预算：control.yml 冷启动要装依赖、装浏览器，1–2 分钟才出第一张码；
// 码本身 ~85 秒换一张，所以按时间预算（而不是次数）轮询，够撑到扫完。
const QR_POLL_MS = 4000;
const QR_BUDGET_MS = 15 * 60 * 1000;
const RECEIPT_POLL_MS = 3000;
const RECEIPT_TRIES = 100; // 5 分钟：够 control.yml 启动 + 二维码登录窗口
const RUN_POLL_MS = 5000;

// ---------------------------------------------------------------- 小工具

function say(text, kind = "") {
  const box = $("status-message");
  box.textContent = text;
  if (kind) box.dataset.kind = kind;
  else delete box.dataset.kind;
}

function fmtTime(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString("zh-CN", { hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function esc(text) {
  return String(text == null ? "" : text).replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]);
}

function contextFor() {
  return c.CONTEXT_TEMPLATE(state.cfg.repo, state.keyVersion);
}

function newSessionId() {
  return (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`).replace(/-/g, "").slice(0, 24);
}

// ---------------------------------------------------------------- 连接

async function connect() {
  const repo = $("inp-repo").value.trim();
  const token = $("inp-token").value.trim();
  const keyText = $("inp-statekey").value.trim();
  const branch = $("inp-branch").value.trim() || "wxread-state";
  if (!repo || !token) {
    say("仓库与 token 都要填。", "bad");
    return;
  }
  gh.saveConfig({ repo, token, branch });
  state.cfg = { repo, token, branch };
  try {
    state.qrSession = sessionStorage.getItem("wxread.qrSession") || null;
  } catch (error) {
    state.qrSession = null;
  }
  if (keyText) {
    try {
      state.stateKey = c.stateKeyFromBase64(keyText);
      localStorage.setItem("wxread.statekey", keyText);
    } catch (error) {
      say(`状态密钥不对：${error.message}`, "bad");
      return;
    }
  }
  say("连接中…");
  try {
    const control = await gh.getJson(state.cfg, "control.json");
    if (control && control.public_key) {
      state.publicKey = c.b64d(control.public_key);
      state.keyVersion = Number(control.key_version || 1);
    }
    $("conn-dot").className = "dot ok";
    $("conn-text").textContent = repo;
    await refreshAll();
    const base = state.publicKey ? "已连接。" : "已连接，但还没有指令公钥：先跑一次 control.yml（control=init）。";
    say(base + expiryNote(), state.publicKey ? "ok" : "warn");
  } catch (error) {
    $("conn-dot").className = "dot bad";
    $("conn-text").textContent = "连接失败";
    const hint =
      error.status === 401
        ? "（token 过期或权限不足：去 GitHub 重新生成 fine-grained token，只需 contents + actions 写）"
        : error.status === 404
          ? "（仓库名写错，或状态分支上还没有 control.json）"
          : "";
    say(`连接失败：${error.message}${hint}`, "bad");
  }
}

// token 到期提醒：GitHub 在响应头里给了到期时间就用它，没给就不显示。
function expiryNote() {
  const raw = typeof gh.getTokenExpiry === "function" ? gh.getTokenExpiry() : null;
  if (!raw) return "";
  const at = new Date(raw);
  if (Number.isNaN(at.getTime())) return "";
  const days = Math.floor((at.getTime() - Date.now()) / 86400000);
  const text = at.toISOString().slice(0, 10);
  if (days < 0) return ` token 已于 ${text} 过期，自动任务不受影响，网页需要重新填一个新 token。`;
  if (days <= 14) return ` token 还有 ${days} 天到期（${text}）。`;
  return ` token 有效至 ${text}。`;
}

function forget() {
  for (const key of ["wxread.repo", "wxread.token", "wxread.branch", "wxread.statekey"]) localStorage.removeItem(key);
  state.cfg = { repo: "", token: "", branch: "wxread-state" };
  state.stateKey = null;
  state.publicKey = null;
  $("inp-token").value = "";
  $("inp-statekey").value = "";
  $("conn-dot").className = "dot";
  $("conn-text").textContent = "未连接";
  say("已清除本机凭据（状态分支上的密文不受影响）。");
}

// ---------------------------------------------------------------- 读数据

async function loadAccounts() {
  const dir = await gh.listDir(state.cfg, "accounts");
  const out = [];
  for (const entry of dir) {
    if (!entry.name.endsWith(".enc.json")) continue;
    const envelope = await gh.getFile(state.cfg, `accounts/${entry.name}`);
    if (!envelope) continue;
    try {
      const payload = c.decryptState(JSON.parse(new TextDecoder().decode(envelope.bytes)), state.stateKey, contextFor());
      out.push(payload);
    } catch (error) {
      out.push({ id: entry.name.replace(".enc.json", ""), label: "（解不开）", status: "bad", note: error.message });
    }
  }
  return out.sort((a, b) => String(a.id).localeCompare(String(b.id)));
}

async function loadRuns() {
  const month = new Date().toISOString().slice(0, 7);
  const file = await gh.getFile(state.cfg, `runs/${month}.enc.json`);
  if (!file) return [];
  try {
    const payload = c.decryptState(JSON.parse(new TextDecoder().decode(file.bytes)), state.stateKey, contextFor());
    return Array.isArray(payload.runs) ? payload.runs : [];
  } catch (error) {
    say(`运行记录解不开：${error.message}`, "warn");
    return [];
  }
}

// ---------------------------------------------------------------- 渲染

function renderMetrics(runs) {
  const today = new Date().toISOString().slice(0, 10);
  const todays = runs.filter((run) => String(run.at || run.started_at || "").startsWith(today));
  const reads = todays.reduce((sum, run) => sum + Number(run.read_count || 0), 0);
  const ok = state.accounts.filter((a) => a.enabled !== false && a.status === "ok");
  const needs = state.accounts.filter((a) => a.status === "needs_login");
  const last = runs.length ? runs[runs.length - 1] : null;

  $("m-read").textContent = String(reads);
  $("m-read-hint").textContent = `目标 ${ok.reduce((sum, a) => sum + Number(a.target_count || 40), 0)} 次`;
  $("m-accounts").textContent = String(ok.length);
  $("m-accounts-hint").textContent = `共 ${state.accounts.length} 个账户`;
  $("m-last").textContent = last ? String(last.read_count || 0) : "—";
  $("m-last-hint").textContent = last ? `${fmtTime(last.at || last.started_at)} · ${last.reason || "ok"}` : "还没有运行记录";
  $("m-needs").textContent = String(needs.length);
}

function badge(status) {
  const map = { ok: ["ok", "正常"], needs_login: ["warn", "需要登录"], stopped: ["bad", "已停用"] };
  const [kind, text] = map[status] || ["run", status || "未知"];
  return `<span class="badge ${kind}">${esc(text)}</span>`;
}

function renderAccounts() {
  const body = $("acc-body");
  $("acc-count").textContent = state.accounts.length ? `${state.accounts.length} 个` : "";
  if (!state.accounts.length) {
    body.innerHTML = '<tr><td colspan="5" class="sub">还没有账户：先扫码或导入 curl。</td></tr>';
    return;
  }
  body.innerHTML = state.accounts
    .map(
      (a) => `<tr>
        <td><div>${esc(a.label || a.id)}</div><div class="sub">${esc(a.id)}${a.note ? ` · ${esc(a.note)}` : ""}</div></td>
        <td>${badge(a.status)}</td>
        <td class="num">${esc(a.target_count || 40)}</td>
        <td class="num">${esc(fmtTime(a.renewed_at))}</td>
        <td>
          <div class="actions">
            <button type="button" data-act="edit" data-id="${esc(a.id)}">编辑</button>
            <button type="button" data-act="toggle" data-id="${esc(a.id)}" data-enabled="${a.enabled === false ? "0" : "1"}">${a.enabled === false ? "启用" : "停用"}</button>
            <button type="button" class="danger" data-act="delete" data-id="${esc(a.id)}">删除</button>
          </div>
        </td>
      </tr>`,
    )
    .join("");
}

function renderRuns(runs) {
  const byDay = new Map();
  for (const run of runs) {
    const day = String(run.at || run.started_at || "").slice(0, 10);
    if (!day) continue;
    byDay.set(day, (byDay.get(day) || 0) + Number(run.read_count || 0));
  }
  const days = [...byDay.keys()].sort().slice(-14);
  const max = Math.max(1, ...days.map((day) => byDay.get(day)));
  const today = new Date().toISOString().slice(0, 10);
  $("run-bars").innerHTML = days
    .map((day) => {
      const value = byDay.get(day);
      const pct = Math.round((value / max) * 100);
      return `<div class="bar-col" title="${esc(day)}：${value} 次"><div class="fill${day === today ? " accent" : ""}" style="height:${pct}%"></div></div>`;
    })
    .join("") || '<div class="sub">暂无数据</div>';

  const last = [...runs].slice(-8).reverse();
  $("run-list").innerHTML =
    last
      .map(
        (run) => `<li><time>${esc(fmtTime(run.at || run.started_at))}</time>
          <span>${esc(run.label || run.account_id || "")}</span>
          <span class="num">${esc(run.read_count || 0)} 次</span>
          <span class="sub">${esc(run.reason || "ok")}</span></li>`,
      )
      .join("") || '<li class="sub">暂无运行记录</li>';
}

async function refreshAll() {
  if (!state.cfg.repo || !state.cfg.token) return;
  if (!state.stateKey) {
    say("还没有填状态密钥，账户与记录都解不开。", "warn");
    return;
  }
  try {
    state.accounts = await loadAccounts();
    state.runs = await loadRuns();
    renderAccounts();
    renderRuns(state.runs);
    renderMetrics(state.runs);
  } catch (error) {
    say(`读取状态失败：${error.message}`, "bad");
  }
}

// ---------------------------------------------------------------- 下发指令

async function sendInstruction(action, payload, { quiet = false } = {}) {
  if (!state.publicKey) {
    say("还没有指令公钥：先跑一次 control.yml（control=init）拿到 public_key。", "bad");
    return null;
  }
  const sessionId = newSessionId();
  const reply = c.newKeypair();
  state.sessions.set(sessionId, reply.secretKey);
  const instruction = {
    action,
    session_id: sessionId,
    issued_at: Math.floor(Date.now() / 1000),
    nonce: c.b64e(c.randomBytes(12)),
    payload: payload || {},
    reply_key: c.b64e(reply.publicKey),
  };
  const envelope = c.seal(instruction, state.publicKey, c.newKeypair());
  await gh.dispatchWorkflow(state.cfg, "control.yml", {
    control: "instruction",
    instruction: JSON.stringify(envelope),
    session_id: sessionId,
  });
  if (!quiet) say(`已下发 ${action}，等待 control.yml 回执…`);
  pollReceipt(sessionId, action);
  return sessionId;
}

async function pollReceipt(sessionId, action, tries = 0) {
  const file = await gh.getFile(state.cfg, `responses/${sessionId}.json`);
  if (!file) {
    if (tries >= RECEIPT_TRIES) {
      say(`${action} 还没有回执：去 Actions 里看 control 运行日志。`, "warn");
      return;
    }
    setTimeout(() => pollReceipt(sessionId, action, tries + 1).catch(() => {}), RECEIPT_POLL_MS);
    return;
  }
  try {
    const envelope = JSON.parse(new TextDecoder().decode(file.bytes));
    const receipt = c.unseal(envelope, state.sessions.get(sessionId));
    const ok = receipt.ok !== false;
    say(`${action} ${ok ? "完成" : "失败"}：${JSON.stringify(receipt).slice(0, 400)}`, ok ? "ok" : "bad");
    await refreshAll();
  } catch (error) {
    say(`回执解不开：${error.message}`, "bad");
  }
}

// ---------------------------------------------------------------- 二维码

async function findSessionQr(sessionId) {
  // 帧名 = qr-<session>-<seq>-<fp>.png，序号保证按名字排序就是时间顺序。
  // 只认本次会话的帧：上一轮留下的（可能带过期浮层的）旧图永远不会被显示。
  const dir = await gh.listDir(state.cfg, "responses");
  const prefix = `qr-${sessionId}-`;
  const names = dir
    .filter((item) => item.name.startsWith(prefix) && item.name.endsWith(".png"))
    .map((item) => item.name)
    .sort();
  return names.length ? `responses/${names[names.length - 1]}` : null;
}

async function pollQr(startedAt = Date.now(), shownPath = null) {
  const elapsed = Math.round((Date.now() - startedAt) / 1000);
  if (Date.now() - startedAt > QR_BUDGET_MS) {
    say("二维码轮询超时：去 Actions 看 control 运行，或改用本机 scripts/qr_local.py。", "warn");
    return;
  }
  if (!state.qrSession) {
    say('还没有扫码会话：先点"生成二维码"。', "warn");
    return;
  }
  let path = null;
  try {
    path = await findSessionQr(state.qrSession);
  } catch (error) {
    // 网络抖动不算失败，下一轮再看
  }
  if (path && path !== shownPath) {
    const file = await gh.getFile(state.cfg, path);
    if (file) {
      const url = URL.createObjectURL(new Blob([file.bytes], { type: "image/png" }));
      $("qr-slot").innerHTML = `<img alt="微信读书登录二维码，用微信扫描" src="${url}" />`;
      shownPath = path;
      state.qrShots += 1;
      say(`二维码已就绪（第 ${state.qrShots} 张，等了 ${elapsed} 秒），用微信扫一扫；过期会自动换新码。`, "ok");
    }
  } else if (!shownPath) {
    $("qr-slot").innerHTML =
      `<div class="empty">control.yml 启动中…（已等 ${elapsed} 秒）` +
      `<br />首次要装浏览器依赖，通常 1–2 分钟；码一出来就显示在这里。</div>`;
  }
  setTimeout(() => pollQr(startedAt, shownPath).catch(() => {}), QR_POLL_MS);
}

async function askQr() {
  state.qrShots = 0;
  state.qrSession = null;
  $("qr-slot").innerHTML = '<div class="empty">已申请，等待 control.yml 生成二维码…</div>';
  const sessionId = await sendInstruction("qr", { label: $("inp-qr-label").value.trim(), timeout: 600 });
  state.qrSession = sessionId;
  try {
    sessionStorage.setItem("wxread.qrSession", sessionId); // 刷新页面还能接着看这批帧
  } catch (error) {
    // 隐私模式下 sessionStorage 可能不可用，忽略
  }
  pollQr().catch(() => {});
}

// ---------------------------------------------------------------- 手动运行

const RUN_STATE = { queued: "排队中", in_progress: "进行中", waiting: "等待中", completed: "结束" };
const RUN_VERDICT = {
  success: "成功",
  failure: "失败",
  cancelled: "已取消",
  timed_out: "超时",
  skipped: "跳过",
};

function numberOrZero(id) {
  const value = Number($(id).value);
  return Number.isFinite(value) && value > 0 ? Math.round(value) : 0;
}

async function startRun(workflow, inputs, noun) {
  const startedAt = Date.now();
  try {
    await gh.dispatchWorkflow(state.cfg, workflow, inputs);
  } catch (error) {
    say(`${noun}触发失败：${error.message}`, "bad");
    return;
  }
  say(`已触发 ${workflow}，正在等它出现在运行列表里…`, "ok");
  trackRun(workflow, startedAt, noun).catch((error) => say(`跟踪${noun}状态失败：${error.message}`, "warn"));
}

async function trackRun(workflow, startedAt, noun, tries = 0) {
  // dispatch 之后 GitHub 要几秒才把这次 run 放进列表，所以按 created_at 找，不按 id 猜。
  if (tries > 120) {
    say(`${noun}十分钟还没结束：去 Actions 页面看日志。`, "warn");
    return;
  }
  let runs = [];
  try {
    runs = await gh.listRuns(state.cfg, workflow);
  } catch (error) {
    say(`查询${noun}状态失败：${error.message}`, "warn");
    return;
  }
  const mine = runs.find((run) => {
    const created = Date.parse(run.created_at || "");
    return Number.isFinite(created) && created >= startedAt - 15_000;
  });
  if (!mine) {
    if (tries === 4) say("还没出现在运行列表里：可能还在排队，或 token 少了 actions 权限。", "warn");
    setTimeout(() => trackRun(workflow, startedAt, noun, tries + 1).catch(() => {}), RUN_POLL_MS);
    return;
  }
  if (mine.status !== "completed") {
    if (tries % 3 === 0) say(`${noun} #${mine.run_number} ${RUN_STATE[mine.status] || mine.status}…`);
    setTimeout(() => trackRun(workflow, startedAt, noun, tries + 1).catch(() => {}), RUN_POLL_MS);
    return;
  }
  const verdict = RUN_VERDICT[mine.conclusion] || mine.conclusion || "结束";
  const ok = mine.conclusion === "success";
  say(`${noun} #${mine.run_number} ${verdict}。日志：${mine.html_url}`, ok ? "ok" : "bad");
  if (ok) await refreshAll().catch(() => {});
}

// ---------------------------------------------------------------- 事件

function bind() {
  $("btn-connect").addEventListener("click", () => connect().catch((error) => say(error.message, "bad")));
  $("btn-forget").addEventListener("click", forget);
  $("btn-refresh").addEventListener("click", () => refreshAll().catch((error) => say(error.message, "bad")));
  $("btn-qr").addEventListener("click", () => askQr().catch((error) => say(error.message, "bad")));
  $("btn-qr-poll").addEventListener("click", () => pollQr().catch(() => {}));

  const runRead = () => {
    const inputs = {};
    const pace = numberOrZero("inp-pace");
    const deadline = numberOrZero("inp-deadline");
    if (pace) inputs.pace = String(pace);
    if (deadline) inputs.deadline = String(deadline);
    startRun("read.yml", inputs, "阅读任务");
  };
  $("btn-run").addEventListener("click", runRead);
  $("btn-run-now").addEventListener("click", runRead);
  $("btn-warm-now").addEventListener("click", () => startRun("warm.yml", {}, "保活任务"));

  $("btn-import").addEventListener("click", async () => {
    const curl = $("inp-curl").value.trim();
    if (!curl) {
      say("先把 cURL 贴进上面的框里。", "warn");
      return;
    }
    const payload = { curl };
    const label = $("inp-curl-label").value.trim();
    if (label) payload.label = label.slice(0, 24);
    const target = numberOrZero("inp-curl-target");
    if (target) payload.target_count = target;
    await sendInstruction("import", payload);
  });

  $("btn-stop").addEventListener("click", async () => {
    try {
      const runs = await gh.listRuns(state.cfg, "read.yml");
      const running = runs.find((run) => run.status === "in_progress" || run.status === "queued");
      if (!running) {
        say("当前没有在跑的阅读任务。", "warn");
        return;
      }
      await gh.cancelRun(state.cfg, running.id);
      say(`已请求停止 #${running.id}。`, "ok");
    } catch (error) {
      say(`停止失败：${error.message}`, "bad");
    }
  });

  $("btn-schedule").addEventListener("click", async () => {
    const daily = $("inp-daily").value || "01:07";
    const warm = Number($("inp-warm").value);
    await sendInstruction("schedule", { enabled: true, daily, warm_hours: warm });
  });

  $("btn-notify").addEventListener("click", async () => {
    const channels = $("inp-channels")
      .value.split(/[,，\s]+/)
      .filter(Boolean);
    await sendInstruction("notifications", { enabled: channels.length > 0, channels });
  });

  $("btn-test-mail").addEventListener("click", () => sendInstruction("test-mail", {}));
  $("btn-test-push").addEventListener("click", () => {
    const channel = ($("inp-channels").value.split(/[,，\s]+/).filter(Boolean)[0] || "feishu");
    sendInstruction("test-push", { channel });
  });

  $("acc-body").addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-act]");
    if (!button) return;
    const id = button.dataset.id;
    const act = button.dataset.act;
    if (act === "delete") {
      if (!confirm(`删除账户 ${id}？状态分支上的密文会被移除。`)) return;
      await sendInstruction("delete", { account_id: id });
      return;
    }
    if (act === "toggle") {
      const enabled = button.dataset.enabled !== "1";
      await sendInstruction("update", { account_id: id, enabled });
      return;
    }
    if (act === "edit") {
      const account = state.accounts.find((item) => item.id === id) || {};
      const label = prompt("账户备注（≤24 字）", account.label || "");
      if (label === null) return;
      const target = prompt("每日目标次数（1–200）", String(account.target_count || 40));
      if (target === null) return;
      await sendInstruction("update", { account_id: id, label: label.slice(0, 24), target_count: Number(target) });
    }
  });
}

function restore() {
  $("inp-repo").value = state.cfg.repo || "";
  $("inp-token").value = state.cfg.token || "";
  $("inp-branch").value = state.cfg.branch || "wxread-state";
  const keyText = localStorage.getItem("wxread.statekey");
  if (keyText) {
    $("inp-statekey").value = keyText;
    try {
      state.stateKey = c.stateKeyFromBase64(keyText);
    } catch {
      say("本机保存的状态密钥无法解析，请重新填。", "warn");
    }
  }
  if (state.cfg.repo && state.cfg.token && state.stateKey) connect().catch(() => {});
}

bind();
restore();

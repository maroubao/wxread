// GitHub API 客户端：所有请求都带 Bearer token（token 只存在浏览器 localStorage，不进仓库）。
// 只用 REST，无第三方库，Pages 静态托管即可运行。

const API = "https://api.github.com";

// fine-grained token 的到期时间由 GitHub 在响应头里回传；CORS 允许时才有值。
let tokenExpiry = null;
export const REMEMBER_MS = 24 * 60 * 60 * 1000;
const CONFIG_KEY = "wxread.connection";

export function getTokenExpiry() {
  return tokenExpiry;
}

export function resetTokenExpiry() {
  tokenExpiry = null;
}

export class GhError extends Error {
  constructor(message, status, body) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

export function readConfig() {
  const empty = { repo: "", token: "", branch: "wxread-state", stateKeyText: "", remember: false };
  try {
    const saved = JSON.parse(localStorage.getItem(CONFIG_KEY) || "null");
    if (saved && Number(saved.expiresAt) > Date.now() && Number(saved.expiresAt) <= Date.now() + REMEMBER_MS) {
      return { ...empty, ...saved, remember: true };
    }
    localStorage.removeItem(CONFIG_KEY);
    localStorage.removeItem("wxread.token");
    localStorage.removeItem("wxread.statekey");
    return { ...empty, repo: localStorage.getItem("wxread.repo") || "", branch: localStorage.getItem("wxread.branch") || "wxread-state" };
  } catch {
    return empty;
  }
}

export function saveConfig({ repo, token, branch, stateKeyText = "" }, remember = false) {
  localStorage.removeItem(CONFIG_KEY);
  localStorage.removeItem("wxread.token");
  localStorage.removeItem("wxread.statekey");
  localStorage.setItem("wxread.repo", repo.trim());
  localStorage.setItem("wxread.branch", (branch || "wxread-state").trim());
  if (remember) {
    localStorage.setItem(CONFIG_KEY, JSON.stringify({ repo: repo.trim(), token: token.trim(), branch: branch || "wxread-state", stateKeyText, expiresAt: Date.now() + REMEMBER_MS }));
  }
}

export function clearConfig() {
  for (const key of [CONFIG_KEY, "wxread.repo", "wxread.token", "wxread.branch", "wxread.statekey"]) {
    try { localStorage.removeItem(key); } catch { /* 浏览器禁止存储时仍可断开内存会话 */ }
  }
  resetTokenExpiry();
}

function headers(token) {
  return {
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
}

async function request(path, { token, method = "GET", body, accept, signal } = {}) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (signal?.aborted) controller.abort();
  signal?.addEventListener("abort", abort, { once: true });
  const timeout = setTimeout(abort, 15000);
  try {
    const response = await fetch(path.startsWith("http") ? path : API + path, {
      method,
      signal: controller.signal,
      cache: "no-store",
      headers: { ...headers(token), ...(accept ? { Accept: accept } : {}), ...(body ? { "Content-Type": "application/json" } : {}) },
      body: body ? JSON.stringify(body) : undefined,
    });
    const expiry = response.headers.get("github-authentication-token-expiration");
    if (expiry) tokenExpiry = expiry;
    if (response.status === 204) return null;
    const text = await response.text();
    let parsed;
    try { parsed = text ? JSON.parse(text) : null; } catch { parsed = text; }
    if (!response.ok) {
      throw new GhError((parsed && parsed.message) || "GitHub API " + response.status, response.status, parsed);
    }
    return parsed;
  } catch (error) {
    if (controller.signal.aborted && !signal?.aborted) throw new GhError("GitHub 请求超时，稍后重试", 0, null);
    throw error;
  } finally {
    clearTimeout(timeout);
    signal?.removeEventListener("abort", abort);
  }
}

/** 读状态分支上的一个文件；不存在返回 null。 */
export async function getFile({ repo, token, branch, signal }, path) {
  try {
    const data = await request(`/repos/${repo}/contents/${encodeURI(path)}?ref=${encodeURIComponent(branch)}`, { token, signal });
    if (!data || Array.isArray(data)) return null;
    const binary = atob((data.content || "").replace(/\n/g, ""));
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    return { bytes, sha: data.sha, path };
  } catch (error) {
    if (error instanceof GhError && error.status === 404) return null;
    throw error;
  }
}

export async function getJson(cfg, path) {
  const file = await getFile(cfg, path);
  if (!file) return null;
  return JSON.parse(new TextDecoder().decode(file.bytes));
}

export async function listDir({ repo, token, branch, signal }, path) {
  try {
    const data = await request(`/repos/${repo}/contents/${encodeURI(path)}?ref=${encodeURIComponent(branch)}`, { token, signal });
    return Array.isArray(data) ? data : [];
  } catch (error) {
    if (error instanceof GhError && error.status === 404) return [];
    throw error;
  }
}

export async function dispatchWorkflow({ repo, token, signal }, workflow, inputs, ref = "main") {
  await request(`/repos/${repo}/actions/workflows/${workflow}/dispatches`, {
    token,
    signal,
    method: "POST",
    body: { ref, inputs },
  });
}

export async function listRuns({ repo, token, signal }, workflow) {
  const data = await request(`/repos/${repo}/actions/workflows/${workflow}/runs?per_page=30`, { token, signal });
  return (data && data.workflow_runs) || [];
}

export async function cancelRun({ repo, token, signal }, runId) {
  await request(`/repos/${repo}/actions/runs/${runId}/cancel`, { token, signal, method: "POST" });
}

export async function latestBranchCommit({ repo, token, branch, signal }) {
  try {
    const data = await request(`/repos/${repo}/branches/${encodeURIComponent(branch)}`, { token, signal });
    return data && data.commit ? data.commit.sha : null;
  } catch (error) {
    if (error instanceof GhError && error.status === 404) return null;
    throw error;
  }
}

export async function ensureStateBranch({ repo, token, branch }) {
  const existing = await latestBranchCommit({ repo, token, branch });
  if (existing) return false;
  const main = await latestBranchCommit({ repo, token, branch: "main" });
  await request(`/repos/${repo}/git/refs`, {
    token,
    method: "POST",
    body: { ref: `refs/heads/${branch}`, sha: main },
  });
  return true;
}

/** 某个路径上最后一次提交（用于"这个目录下最新写入的文件是哪个"）。 */
export async function latestCommitForPath({ repo, token, branch }, path) {
  const data = await request(`/repos/${repo}/commits?sha=${encodeURIComponent(branch)}&path=${encodeURIComponent(path)}&per_page=1`, { token });
  return data && data.length ? data[0] : null;
}

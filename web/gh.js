// GitHub API 客户端：所有请求都带 Bearer token（token 只存在浏览器 localStorage，不进仓库）。
// 只用 REST，无第三方库，Pages 静态托管即可运行。

const API = "https://api.github.com";

// fine-grained token 的到期时间由 GitHub 在响应头里回传；CORS 允许时才有值。
let tokenExpiry = null;

export function getTokenExpiry() {
  return tokenExpiry;
}

export class GhError extends Error {
  constructor(message, status, body) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

export function readConfig() {
  const repo = localStorage.getItem("wxread.repo") || "";
  const token = localStorage.getItem("wxread.token") || "";
  const branch = localStorage.getItem("wxread.branch") || "wxread-state";
  return { repo, token, branch };
}

export function saveConfig({ repo, token, branch }) {
  if (repo !== undefined) localStorage.setItem("wxread.repo", repo.trim());
  if (token !== undefined) localStorage.setItem("wxread.token", token.trim());
  if (branch !== undefined && branch.trim()) localStorage.setItem("wxread.branch", branch.trim());
}

function headers(token) {
  return {
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
}

async function request(path, { token, method = "GET", body, accept } = {}) {
  const response = await fetch(path.startsWith("http") ? path : `${API}${path}`, {
    method,
    headers: { ...headers(token), ...(accept ? { Accept: accept } : {}), ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (response.status === 204) return null;
  const expiry = response.headers.get("github-authentication-token-expiration");
  if (expiry) tokenExpiry = expiry;
  const text = await response.text();
  let parsed = null;
  try {
    parsed = text ? JSON.parse(text) : null;
  } catch {
    parsed = text;
  }
  if (!response.ok) {
    const message = (parsed && parsed.message) || `GitHub API ${response.status}`;
    throw new GhError(message, response.status, parsed);
  }
  return parsed;
}

/** 读状态分支上的一个文件；不存在返回 null。 */
export async function getFile({ repo, token, branch }, path) {
  try {
    const data = await request(`/repos/${repo}/contents/${encodeURI(path)}?ref=${encodeURIComponent(branch)}`, { token });
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

export async function getJson({ repo, token, branch }, path) {
  const file = await getFile({ repo, token, branch }, path);
  if (!file) return null;
  return JSON.parse(new TextDecoder().decode(file.bytes));
}

export async function listDir({ repo, token, branch }, path) {
  try {
    const data = await request(`/repos/${repo}/contents/${encodeURI(path)}?ref=${encodeURIComponent(branch)}`, { token });
    return Array.isArray(data) ? data : [];
  } catch (error) {
    if (error instanceof GhError && error.status === 404) return [];
    throw error;
  }
}

export async function dispatchWorkflow({ repo, token }, workflow, inputs, ref = "main") {
  await request(`/repos/${repo}/actions/workflows/${workflow}/dispatches`, {
    token,
    method: "POST",
    body: { ref, inputs },
  });
}

export async function listRuns({ repo, token }, workflow) {
  const data = await request(`/repos/${repo}/actions/workflows/${workflow}/runs?per_page=5`, { token });
  return (data && data.workflow_runs) || [];
}

export async function cancelRun({ repo, token }, runId) {
  await request(`/repos/${repo}/actions/runs/${runId}/cancel`, { token, method: "POST" });
}

export async function latestBranchCommit({ repo, token, branch }) {
  try {
    const data = await request(`/repos/${repo}/branches/${encodeURIComponent(branch)}`, { token });
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

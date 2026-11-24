import assert from "node:assert/strict";
import test from "node:test";
import * as gh from "../../web/gh.js";

function storage() {
  const values = new Map();
  globalThis.localStorage = { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, String(value)), removeItem: key => values.delete(key) };
  return values;
}

test("默认连接不会持久化token和状态密钥", () => {
  const values = storage();
  gh.saveConfig({ repo: "owner/repo", token: "fake-token", branch: "state", stateKeyText: "fake-key" });
  assert.equal(gh.readConfig().token, "");
  assert.ok(![...values.values()].some(value => value.includes("fake-token") || value.includes("fake-key")));
});

test("显式记住后24小时到期清除凭据", () => {
  const values = storage();
  gh.saveConfig({ repo: "owner/repo", token: "fake-token", branch: "state", stateKeyText: "fake-key" }, true);
  assert.equal(gh.readConfig().token, "fake-token");
  const saved = JSON.parse(values.get("wxread.connection"));
  saved.expiresAt = Date.now() - 1;
  values.set("wxread.connection", JSON.stringify(saved));
  assert.equal(gh.readConfig().token, "");
  assert.equal(values.has("wxread.connection"), false);
});

test("旧版没有到期时间的凭据不会继续自动连接", () => {
  const values = storage();
  values.set("wxread.token", "old-token");
  values.set("wxread.statekey", "old-key");
  assert.equal(gh.readConfig().token, "");
  assert.equal(values.has("wxread.statekey"), false);
});

test("断开会话会中止响应体读取", { timeout: 1000 }, async () => {
  const previous = globalThis.fetch;
  const controller = new AbortController();
  globalThis.fetch = async (_url, options) => ({
    status: 200, ok: true, headers: new Headers(),
    text: () => new Promise((_resolve, reject) => options.signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")))),
  });
  try {
    const pending = gh.getFile({ repo: "owner/repo", token: "fake", branch: "state", signal: controller.signal }, "account.json");
    setTimeout(() => controller.abort(), 10);
    await assert.rejects(pending, /Aborted/);
  } finally {
    globalThis.fetch = previous;
  }
});

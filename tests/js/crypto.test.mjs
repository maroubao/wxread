// 跨语言加密互通测试：node --test tests/js/
// 1) JS 能解开 Python 写的状态密文；2) Python 能解开 JS 写的状态密文与指令封套。

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import path from "node:path";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "..", "..");

// nacl-fast.min.js 是 UMD，用 require 加载后挂到 globalThis
const require = createRequire(import.meta.url);
globalThis.nacl = require(path.join(root, "web/assets/vendor/nacl-fast.min.js").replace(/\\/g, "/"));
const cryptoJs = await import(pathToFileURL(path.join(root, "web/crypto.js")).href);

const python = process.env.PYTHON || "python";

function runBridge(args, input) {
  return execFileSync(python, [path.join(here, "bridge.py"), ...args], {
    cwd: root,
    input,
    encoding: "utf-8",
    maxBuffer: 8 * 1024 * 1024,
  });
}

const fixtures = JSON.parse(runBridge(["mkfixtures"]));

test("JS 解开 Python 写的状态密文", () => {
  const key = cryptoJs.b64d(fixtures.key_b64);
  const payload = cryptoJs.decryptState(fixtures.envelope, key, fixtures.ctx);
  assert.deepEqual(payload, fixtures.payload);
});

test("上下文不符时 JS 必须拒绝", () => {
  const key = cryptoJs.b64d(fixtures.key_b64);
  assert.throws(() => cryptoJs.decryptState(fixtures.envelope, key, "wxread-state|other/repo|v1"), /上下文不符/);
});

test("Python 能解开 JS 写的状态密文与指令封套", () => {
  const key = cryptoJs.b64d(fixtures.key_b64);
  const payload = { text: "反向中文", k: 2 };
  const jsState = cryptoJs.encryptState(payload, key, fixtures.ctx);
  const jsSeal = cryptoJs.seal(
    { action: "schedule", session_id: "abc123", issued_at: 1700000000, nonce: "nonce-1234", payload: { daily: "01:07" } },
    cryptoJs.b64d(fixtures.public_key_b64),
  );
  const out = JSON.parse(runBridge(["verify"], JSON.stringify({ ...fixtures, js_state_env: jsState, js_seal_env: jsSeal })));
  assert.deepEqual(out.state, payload);
  assert.equal(out.sealed.action, "schedule");
  assert.equal(out.sealed.payload.daily, "01:07");
});

test("canonicalJson 与 Python sort_keys/紧凑分隔符一致", () => {
  const value = { b: [1, { d: null, c: "x" }], a: 2, "另": "值" };
  assert.equal(cryptoJs.canonicalJson(value), '{"a":2,"b":[1,{"c":"x","d":null}],"另":"值"}');
});

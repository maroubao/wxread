// 浏览器端加密：与 wxread/crypto.py 逐字节对齐（见 docs/THREAT-MODEL.md §5）。
// 依赖 web/assets/vendor/nacl-fast.min.js（tweetnacl 1.0.3，本地 vendored，无 CDN）。

const nacl = globalThis.nacl;

export const CONTEXT_TEMPLATE = (ownerRepo, keyVersion) => `wxread-state|${ownerRepo}|v${keyVersion}`;
export const STATE_KEY_LEN = 32;
export const NONCE_LEN = 24;

export class CryptoFailure extends Error {}

// ---------------------------------------------------------------- 编解码

export function b64e(bytes) {
  let text = "";
  for (const byte of bytes) text += String.fromCharCode(byte);
  return btoa(text);
}

export function b64d(text) {
  if (typeof text !== "string") throw new CryptoFailure("期望 base64 字符串");
  let raw;
  try {
    raw = atob(text);
  } catch {
    throw new CryptoFailure("base64 解码失败");
  }
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i += 1) out[i] = raw.charCodeAt(i);
  return out;
}

const enc = new TextEncoder();
const dec = new TextDecoder();

export function utf8(text) {
  return enc.encode(text);
}

export function fromUtf8(bytes) {
  return dec.decode(bytes);
}

/** 与 Python `json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",",":"))` 等价。 */
export function canonicalJson(value) {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  const keys = Object.keys(value).sort();
  const body = keys
    .filter((key) => value[key] !== undefined)
    .map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`)
    .join(",");
  return `{${body}}`;
}

// ---------------------------------------------------------------- 密钥

export function randomBytes(length) {
  return nacl.randomBytes(length);
}

export function newStateKey() {
  return nacl.randomBytes(STATE_KEY_LEN);
}

/** 由 base64 状态密钥派生（不做 KDF，密钥本身就该是 32 字节随机数）。 */
export function stateKeyFromBase64(text) {
  const raw = b64d(text.trim());
  if (raw.length !== STATE_KEY_LEN) throw new CryptoFailure("状态密钥必须是 32 字节");
  return raw;
}

export function keypairFromSecretBase64(text) {
  const secret = b64d(text.trim());
  if (secret.length !== 32) throw new CryptoFailure("私钥长度必须是 32 字节");
  return nacl.box.keyPair.fromSecretKey(secret);
}

export function newKeypair() {
  return nacl.box.keyPair();
}

// ---------------------------------------------------------------- 指令封套

export function seal(payload, recipientPk, senderKeypair) {
  if (!(recipientPk instanceof Uint8Array) || recipientPk.length !== 32) {
    throw new CryptoFailure("接收方公钥长度必须是 32 字节");
  }
  const sender = senderKeypair || nacl.box.keyPair();
  const nonce = nacl.randomBytes(NONCE_LEN);
  const raw = typeof payload === "string" ? utf8(payload) : utf8(canonicalJson(payload));
  const ciphertext = nacl.box(raw, nonce, recipientPk, sender.secretKey);
  return { v: 1, epk: b64e(sender.publicKey), n: b64e(nonce), c: b64e(ciphertext) };
}

export function unseal(envelope, recipientSecret) {
  if (!envelope || typeof envelope !== "object" || envelope.v !== 1) {
    throw new CryptoFailure(`不支持的封套版本：${envelope && envelope.v}`);
  }
  const epk = b64d(envelope.epk || "");
  const nonce = b64d(envelope.n || "");
  const ciphertext = b64d(envelope.c || "");
  if (epk.length !== 32 || nonce.length !== NONCE_LEN || ciphertext.length < 16) {
    throw new CryptoFailure("封套字段长度异常");
  }
  const plaintext = nacl.box.open(ciphertext, nonce, epk, recipientSecret);
  if (!plaintext) throw new CryptoFailure("封套解密失败（错钥或已被篡改）");
  try {
    return JSON.parse(fromUtf8(plaintext));
  } catch {
    throw new CryptoFailure("封套明文不是合法 JSON");
  }
}

// ---------------------------------------------------------------- 状态密文

export function encryptState(payload, key, context, keyVersion = 1) {
  if (key.length !== STATE_KEY_LEN) throw new CryptoFailure("状态密钥必须是 32 字节");
  const nonce = nacl.randomBytes(NONCE_LEN);
  const prefix = utf8(context);
  const body = utf8(canonicalJson(payload));
  const plaintext = new Uint8Array(prefix.length + 1 + body.length);
  plaintext.set(prefix, 0);
  plaintext[prefix.length] = 0x0a;
  plaintext.set(body, prefix.length + 1);
  return {
    v: 1,
    kv: Number(keyVersion),
    n: b64e(nonce),
    c: b64e(nacl.secretbox(plaintext, nonce, key)),
  };
}

export function decryptState(envelope, key, context) {
  if (!envelope || typeof envelope !== "object" || envelope.v !== 1) {
    throw new CryptoFailure(`不支持的密文版本：${envelope && envelope.v}`);
  }
  const nonce = b64d(envelope.n || "");
  const ciphertext = b64d(envelope.c || "");
  if (nonce.length !== NONCE_LEN || ciphertext.length < 16) throw new CryptoFailure("密文字段长度异常");
  if (key.length !== STATE_KEY_LEN) throw new CryptoFailure("状态密钥必须是 32 字节");
  const plaintext = nacl.secretbox.open(ciphertext, nonce, key);
  if (!plaintext) throw new CryptoFailure("状态解密失败（错钥或已被篡改）");
  const text = fromUtf8(plaintext);
  const split = text.indexOf("\n");
  if (split < 0) throw new CryptoFailure("状态明文缺少上下文前缀");
  if (text.slice(0, split) !== context) throw new CryptoFailure("状态密文上下文不符（跨仓库/跨版本搬运）");
  try {
    return JSON.parse(text.slice(split + 1));
  } catch {
    throw new CryptoFailure("状态明文不是合法 JSON");
  }
}

"""加密原语：指令封套、回执、状态密文。

实现约定（与 THREAT-MODEL §5 同步，浏览器端用 vendored tweetnacl 做同样运算）：

* 指令封套  = X25519 + XSalsa20-Poly1305（`nacl.box`），发送方每次新建临时密钥对，
  nonce 24 字节随机，随封套一起传输。等价于 libsodium sealed box 的"匿名发送"语义，
  但 nonce 显式携带，便于浏览器侧（tweetnacl）复算。
* 状态密文  = XSalsa20-Poly1305（`nacl.secretbox`），明文首行是上下文前缀
  `wxread-state|<owner>/<repo>|v<key_version>`。前缀在 MAC 覆盖范围之内，因此
  改仓库、改版本号、跨仓库搬运都会解密失败——等价于 AEAD 的 AAD 绑定。
* 密钥版本  = 封套里的 `kv` 字段；旧版本密钥可解旧密文（轮换期并存一轮）。

任何解密失败一律抛 `CryptoFailure`，调用方不得吞掉。
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any, Mapping

from nacl.bindings import (
    crypto_box,
    crypto_box_keypair,
    crypto_box_open,
    crypto_secretbox,
    crypto_secretbox_open,
)
from nacl.exceptions import CryptoError as NaclCryptoError

STATE_KEY_LEN = 32
NONCE_LEN = 24
PUBLIC_KEY_LEN = 32
SECRET_KEY_LEN = 32
ENVELOPE_VERSION = 1
MAC_LEN = 16  # Poly1305 认证标签长度

CONTEXT_TEMPLATE = "wxread-state|{owner_repo}|v{key_version}"


class CryptoFailure(RuntimeError):
    """加解密失败：错钥、篡改、上下文不符。"""


# ---------------------------------------------------------------- 编解码


def b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def b64d(text: str) -> bytes:
    if not isinstance(text, str):
        raise CryptoFailure("期望 base64 字符串")
    try:
        return base64.b64decode(text.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise CryptoFailure("base64 解码失败") from exc


def new_state_key() -> bytes:
    return os.urandom(STATE_KEY_LEN)


def new_keypair() -> tuple[bytes, bytes]:
    """返回 (public_key, secret_key)。"""
    public_key, secret_key = crypto_box_keypair()
    return bytes(public_key), bytes(secret_key)


def keypair_from_secret(secret_key: bytes) -> tuple[bytes, bytes]:
    from nacl.bindings import crypto_scalarmult_base

    if len(secret_key) != SECRET_KEY_LEN:
        raise CryptoFailure("私钥长度必须是 32 字节")
    return bytes(crypto_scalarmult_base(secret_key)), bytes(secret_key)


# ---------------------------------------------------------------- 指令封套


def seal(payload: Mapping[str, Any] | bytes, recipient_pk: bytes, sender: tuple[bytes, bytes] | None = None) -> dict:
    """用接收方公钥封一条指令/回执。返回可直接 JSON 序列化的封套。"""
    if len(recipient_pk) != PUBLIC_KEY_LEN:
        raise CryptoFailure("接收方公钥长度必须是 32 字节")
    raw = payload if isinstance(payload, bytes) else _dumps(payload)
    ephemeral_pk, ephemeral_sk = sender if sender is not None else new_keypair()
    nonce = os.urandom(NONCE_LEN)
    try:
        ciphertext = crypto_box(raw, nonce, recipient_pk, ephemeral_sk)
    except (NaclCryptoError, ValueError) as exc:  # pragma: no cover - 参数正确时不会发生
        raise CryptoFailure("封套加密失败") from exc
    return {
        "v": ENVELOPE_VERSION,
        "epk": b64e(ephemeral_pk),
        "n": b64e(nonce),
        "c": b64e(ciphertext),
    }


def unseal(envelope: Mapping[str, Any], recipient_sk: bytes) -> dict:
    """解开指令/回执封套，返回 dict。"""
    if not isinstance(envelope, Mapping):
        raise CryptoFailure("封套格式错误")
    if envelope.get("v") != ENVELOPE_VERSION:
        raise CryptoFailure(f"不支持的封套版本：{envelope.get('v')!r}")
    ephemeral_pk = b64d(envelope.get("epk", ""))
    nonce = b64d(envelope.get("n", ""))
    ciphertext = b64d(envelope.get("c", ""))
    if len(ephemeral_pk) != PUBLIC_KEY_LEN or len(nonce) != NONCE_LEN or len(ciphertext) < MAC_LEN:
        raise CryptoFailure("封套字段长度异常")
    try:
        plaintext = crypto_box_open(ciphertext, nonce, ephemeral_pk, recipient_sk)
    except (NaclCryptoError, ValueError) as exc:
        raise CryptoFailure("封套解密失败（错钥或已被篡改）") from exc
    return _loads(plaintext)


# ---------------------------------------------------------------- 状态密文


def context(owner_repo: str, key_version: int) -> bytes:
    if not owner_repo or "/" not in owner_repo:
        raise CryptoFailure("owner/repo 格式错误")
    return CONTEXT_TEMPLATE.format(owner_repo=owner_repo, key_version=int(key_version)).encode("utf-8")


def encrypt_state(payload: Mapping[str, Any], key: bytes, ctx: bytes, key_version: int = 1) -> dict:
    if len(key) != STATE_KEY_LEN:
        raise CryptoFailure("状态密钥必须是 32 字节")
    nonce = os.urandom(NONCE_LEN)
    plaintext = ctx + b"\n" + _dumps(payload)
    return {
        "v": ENVELOPE_VERSION,
        "kv": int(key_version),
        "n": b64e(nonce),
        "c": b64e(crypto_secretbox(plaintext, nonce, key)),
    }


def decrypt_state(envelope: Mapping[str, Any], key: bytes, ctx: bytes) -> dict:
    if not isinstance(envelope, Mapping):
        raise CryptoFailure("密文格式错误")
    if envelope.get("v") != ENVELOPE_VERSION:
        raise CryptoFailure(f"不支持的密文版本：{envelope.get('v')!r}")
    nonce = b64d(envelope.get("n", ""))
    ciphertext = b64d(envelope.get("c", ""))
    if len(nonce) != NONCE_LEN or len(ciphertext) < MAC_LEN:
        raise CryptoFailure("密文字段长度异常")
    if len(key) != STATE_KEY_LEN:
        raise CryptoFailure("状态密钥必须是 32 字节")
    try:
        plaintext = crypto_secretbox_open(ciphertext, nonce, key)
    except (NaclCryptoError, ValueError) as exc:
        raise CryptoFailure("状态解密失败（错钥或已被篡改）") from exc
    prefix, _, body = plaintext.partition(b"\n")
    if prefix != ctx:
        raise CryptoFailure("状态密文上下文不符（跨仓库/跨版本搬运）")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CryptoFailure("状态明文不是合法 JSON") from exc


# ---------------------------------------------------------------- 内部


def _dumps(payload: Any) -> bytes:
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CryptoFailure("明文不可序列化") from exc


def _loads(raw: bytes) -> dict:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CryptoFailure("封套明文不是合法 JSON") from exc
    if not isinstance(value, dict):
        raise CryptoFailure("封套明文必须是 JSON 对象")
    return value

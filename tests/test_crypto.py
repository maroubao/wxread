"""P1 加密原语测试：封套与状态密文。"""

from __future__ import annotations

import pytest

from wxread import crypto


def test_seal_unseal_roundtrip():
    pk, sk = crypto.new_keypair()
    envelope = crypto.seal({"action": "qr", "session_id": "abc"}, pk)
    assert set(envelope) == {"v", "epk", "n", "c"}
    assert crypto.unseal(envelope, sk) == {"action": "qr", "session_id": "abc"}


def test_seal_rejects_wrong_key():
    pk, _sk = crypto.new_keypair()
    _other_pk, other_sk = crypto.new_keypair()
    envelope = crypto.seal({"a": 1}, pk)
    with pytest.raises(crypto.CryptoFailure):
        crypto.unseal(envelope, other_sk)


def test_seal_rejects_tampered_ciphertext():
    pk, sk = crypto.new_keypair()
    envelope = crypto.seal({"a": 1}, pk)
    raw = bytearray(crypto.b64d(envelope["c"]))
    raw[-1] ^= 0x01
    envelope["c"] = crypto.b64e(bytes(raw))
    with pytest.raises(crypto.CryptoFailure):
        crypto.unseal(envelope, sk)


def test_seal_rejects_unknown_version():
    pk, sk = crypto.new_keypair()
    envelope = crypto.seal({"a": 1}, pk)
    envelope["v"] = 99
    with pytest.raises(crypto.CryptoFailure):
        crypto.unseal(envelope, sk)


def test_state_roundtrip():
    key = crypto.new_state_key()
    ctx = crypto.context("owner/repo", 1)
    envelope = crypto.encrypt_state({"id": "abc", "status": "ok"}, key, ctx, 1)
    assert envelope["kv"] == 1
    assert crypto.decrypt_state(envelope, key, ctx) == {"id": "abc", "status": "ok"}


def test_state_context_is_authenticated():
    key = crypto.new_state_key()
    envelope = crypto.encrypt_state({"id": "abc"}, key, crypto.context("owner/repo", 1), 1)
    with pytest.raises(crypto.CryptoFailure):
        crypto.decrypt_state(envelope, key, crypto.context("owner/other", 1))
    with pytest.raises(crypto.CryptoFailure):
        crypto.decrypt_state(envelope, key, crypto.context("owner/repo", 2))


def test_state_detects_tampering():
    key = crypto.new_state_key()
    ctx = crypto.context("owner/repo", 1)
    envelope = crypto.encrypt_state({"id": "abc"}, key, ctx, 1)
    raw = bytearray(crypto.b64d(envelope["c"]))
    raw[0] ^= 0xFF
    envelope["c"] = crypto.b64e(bytes(raw))
    with pytest.raises(crypto.CryptoFailure):
        crypto.decrypt_state(envelope, key, ctx)


def test_state_rejects_short_key():
    with pytest.raises(crypto.CryptoFailure):
        crypto.encrypt_state({}, b"short", crypto.context("o/r", 1), 1)


def test_keypair_from_secret_matches_generated():
    pk, sk = crypto.new_keypair()
    assert crypto.keypair_from_secret(sk)[0] == pk

"""P1 状态层测试：CAS、响应丢失、篡改、上下文、runs 裁剪、防重放。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from wxread import crypto
from wxread.state import (
    RUNS_KEEP,
    Conflict,
    StateReadError,
    StateStore,
    StateWriteError,
)

from support import MemoryContents, digest


def make_store(backend=None, owner_repo="owner/repo", key=None):
    backend = backend or MemoryContents()
    key = key or crypto.new_state_key()
    return backend, key, StateStore(backend, key, owner_repo)


def account_payload(account_id="a1b2c3d4e5f6", label="主账户"):
    return {
        "schema_version": 1,
        "id": account_id,
        "label": label,
        "enabled": True,
        "target_count": 40,
        "cookies": {"wr_vid": "1234567890", "wr_skey": "abcdefghij"},
        "headers": {"user-agent": "UA"},
        "read_payload": {"appId": "wb1", "ps": "ps1", "pc": "pc1"},
        "status": "ok",
    }


def test_account_roundtrip_is_encrypted():
    backend, _key, store = make_store()
    store.save_account(account_payload())
    path = "accounts/a1b2c3d4e5f6.enc.json"
    raw = backend.files[path]
    assert b"abcdefghij" not in raw and b"\xe4\xb8\xbb" not in raw  # 密文里没有明文 cookie/中文标签
    loaded = store.load_account("a1b2c3d4e5f6")
    assert loaded["cookies"]["wr_skey"] == "abcdefghij"
    assert loaded["label"] == "主账户"
    assert store.list_account_ids() == ["a1b2c3d4e5f6"]


def test_cas_retries_after_conflict():
    backend, _key, store = make_store()
    store.save_account(account_payload(), "chore(state): first")
    backend.conflicts = 1
    payload = account_payload()
    payload["label"] = "改名"
    store.save_account(payload, "chore(state): rename")
    assert store.load_account("a1b2c3d4e5f6")["label"] == "改名"
    assert sum(1 for path, _ in backend.commits if path.endswith("a1b2c3d4e5f6.enc.json")) == 2


def test_lost_response_does_not_duplicate_write():
    backend, _key, store = make_store()
    store.save_account(account_payload(), "chore(state): first")
    backend.lose_response = 1
    payload = account_payload()
    payload["target_count"] = 60
    store.save_account(payload, "chore(state): retarget")
    assert store.load_account("a1b2c3d4e5f6")["target_count"] == 60
    writes = [msg for path, msg in backend.commits if path.endswith("a1b2c3d4e5f6.enc.json")]
    assert writes.count("chore(state): retarget") == 1


def test_persistent_conflict_raises_write_error():
    backend, _key, store = make_store()
    store.save_account(account_payload(), "chore(state): first")
    backend.conflicts = 99
    with pytest.raises(StateWriteError):
        store.save_account(account_payload(), "chore(state): second")


def test_tampered_ciphertext_is_rejected():
    backend, _key, store = make_store()
    store.save_account(account_payload())
    path = "accounts/a1b2c3d4e5f6.enc.json"
    envelope = json.loads(backend.files[path])
    cipher = envelope["c"]
    flipped = ("B" if cipher[-1] != "B" else "C")
    envelope["c"] = cipher[:-1] + flipped
    backend.files[path] = json.dumps(envelope).encode("utf-8")
    backend.revs[path] = digest(backend.files[path])
    with pytest.raises(StateReadError):
        store.load_account("a1b2c3d4e5f6")


def test_context_binds_repo():
    backend, key, store = make_store(owner_repo="owner/repo")
    store.save_account(account_payload())
    other = StateStore(backend, key, "owner/other")
    with pytest.raises(StateReadError):
        other.load_account("a1b2c3d4e5f6")


def test_unknown_key_version_is_rejected():
    backend, key, store = make_store()
    store.write_encrypted("settings.enc.json", {"a": 1}, "chore(state): settings")
    stranger = StateStore(backend, crypto.new_state_key(), "owner/repo")
    with pytest.raises(StateReadError):
        stranger.load_settings()


def test_runs_are_pruned_to_keep():
    backend, _key, store = make_store()
    base = datetime(2026, 11, 1, tzinfo=timezone.utc)
    total = RUNS_KEEP + 5
    for index in range(total):
        started = (base + timedelta(minutes=index)).strftime("%Y-%m-%dT%H:%M:%SZ")
        store.append_run({"started_at": started, "level": "L0", "read_count": index})
    records = store.load_runs("2026-11")
    assert len(records) == RUNS_KEEP
    assert records[0]["read_count"] == total - 1
    raw = backend.files["runs/2026-11.enc.json"]
    assert b"read_count" not in raw


def test_recent_runs_sorted_desc_across_months():
    _backend, _key, store = make_store()
    store.append_run({"started_at": "2026-10-31T23:00:00Z", "level": "L0"})
    store.append_run({"started_at": "2026-11-01T01:07:00Z", "level": "L3"})
    recent = store.recent_runs()
    assert [item["started_at"] for item in recent] == ["2026-11-01T01:07:00Z", "2026-10-31T23:00:00Z"]


def test_session_cannot_be_replayed():
    _backend, _key, store = make_store()
    store.claim_session("sess-1", {"action": "qr"})
    with pytest.raises(StateWriteError):
        store.claim_session("sess-1", {"action": "qr"})


def test_control_key_roundtrip():
    _backend, _key, store = make_store()
    pk, _sk = crypto.new_keypair()
    store.save_control(pk, 1)
    loaded, version = store.public_key()
    assert loaded == pk and version == 1


def test_response_roundtrip_is_container_only():
    backend, _key, store = make_store()
    pk, sk = crypto.new_keypair()
    store.write_response("sess-1", crypto.seal({"ready": True}, pk))
    envelope = store.read_response("sess-1")
    assert crypto.unseal(envelope, sk) == {"ready": True}
    assert b"ready" not in backend.files["responses/sess-1.json"]


def test_create_only_rejects_existing():
    _backend, _key, store = make_store()
    store.claim_session("sess-2", {"a": 1})
    with pytest.raises((StateWriteError, Conflict)):
        store.claim_session("sess-2", {"a": 2})

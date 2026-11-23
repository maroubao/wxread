"""状态分支不存在时的首次写入（GitHub 上首次跑 control init 的真实故障）。"""

from __future__ import annotations

import pytest

from support import MemoryContents
from wxread.state import StateStore, StateWriteError


class MissingBranchContents:
    """模拟 GitHub：分支不存在时写入返回 404（这里抛 StateWriteError）。"""

    def __init__(self, inner: MemoryContents) -> None:
        self.inner = inner
        self.branch_exists = False
        self.ensure_calls = 0
        self.put_calls: list[dict] = []

    # -- 分支
    def ensure_branch(self, source: str = "main") -> None:
        self.ensure_calls += 1
        self.branch_exists = True

    # -- ContentsBackend
    def get(self, path):
        return self.inner.get(path) if self.branch_exists else (None, None)

    def put(self, path, data, message, sha=None, create_only=False):
        if not self.branch_exists:
            raise StateWriteError(f"写入 {path} 失败：HTTP 404")
        self.put_calls.append({"path": path, "sha": sha, "create_only": create_only})
        return self.inner.put(path, data, message, sha=sha, create_only=create_only)

    def delete(self, path, message, sha=None):
        if not self.branch_exists:
            raise StateWriteError(f"删除 {path} 失败：HTTP 404")
        return self.inner.delete(path, message, sha=sha)

    def list(self, directory):
        return self.inner.list(directory) if self.branch_exists else []


@pytest.fixture()
def store():
    backend = MissingBranchContents(MemoryContents())
    return StateStore(backend, b"k" * 32, owner_repo="owner/repo"), backend


def test_first_write_creates_branch(store):
    """control init 在空仓库上第一次写 control.json 必须自己建分支。"""
    s, backend = store
    assert backend.branch_exists is False

    s.save_control(b"p" * 32)

    assert backend.branch_exists is True
    assert backend.ensure_calls == 1
    assert s.load_control()["public_key"]


def test_ensure_runs_once_per_store(store):
    s, backend = store
    s.save_control(b"p" * 32)
    s.write_plain("control.json", {"public_key": "x", "key_version": 1, "updated_at": "t"}, "m")
    s.write_encrypted("settings.enc.json", {"a": 1}, "m")

    assert backend.ensure_calls == 1


def test_put_raw_uses_sha_keyword(store):
    """回归：put_raw 曾用 existing_sha= 传参，与后端签名不符会 TypeError。"""
    s, backend = store
    s.put_raw("responses/qr-abc.png", b"\x89PNG\r\n", "chore(state): qr deadbeefdeadbeef")

    assert backend.put_calls
    assert backend.put_calls[-1]["path"] == "responses/qr-abc.png"
    assert backend.put_calls[-1]["sha"] is None
    raw, _ = backend.get("responses/qr-abc.png")
    assert raw == b"\x89PNG\r\n"


def test_put_raw_overwrites_existing(store):
    s, backend = store
    s.put_raw("responses/qr-abc.png", b"first", "m")
    s.put_raw("responses/qr-abc.png", b"second", "m")
    raw, _ = backend.get("responses/qr-abc.png")
    assert raw == b"second"


def test_delete_account_creates_branch_too(store):
    s, backend = store
    s.delete_account("abc123")
    assert backend.ensure_calls == 1

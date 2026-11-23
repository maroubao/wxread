"""GitHub Contents 后端的 HTTP 装配：分支自建 + 404 诊断文案。"""

from __future__ import annotations

import pytest

from wxread.state import GitHubContents, StateWriteError


class FakeResponse:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = str(self._payload)

    def json(self):
        return self._payload


class FakeSession:
    """记录请求；按顺序返回预置响应。"""

    def __init__(self, ref_status=404, create_status=201, put_status=201):
        self.ref_status = ref_status
        self.create_status = create_status
        self.put_status = put_status
        self.calls: list[tuple[str, str, dict]] = []
        self.headers: dict[str, str] = {}

    def get(self, url, params=None, timeout=None):
        self.calls.append(("GET", url, params or {}))
        if url.endswith("/git/ref/heads/wxread-state"):
            return FakeResponse(self.ref_status)
        if url.endswith("/git/ref/heads/main"):
            return FakeResponse(200, {"object": {"sha": "deadbeef"}})
        return FakeResponse(404)

    def post(self, url, json=None, timeout=None):
        self.calls.append(("POST", url, json or {}))
        return FakeResponse(self.create_status, {"ref": "refs/heads/wxread-state"})

    def put(self, url, json=None, headers=None, timeout=None):
        self.calls.append(("PUT", url, json or {}))
        return FakeResponse(self.put_status, {"content": {"sha": "abc123"}})

    def request(self, method, url, json=None, timeout=None):
        self.calls.append((method, url, json or {}))
        return FakeResponse(200)


def test_ensure_branch_creates_from_main():
    session = FakeSession()
    GitHubContents("owner/repo", "t", session=session).ensure_branch()
    posts = [c for c in session.calls if c[0] == "POST"]
    assert len(posts) == 1
    assert posts[0][2] == {"ref": "refs/heads/wxread-state", "sha": "deadbeef"}


def test_ensure_branch_noop_when_exists():
    session = FakeSession(ref_status=200)
    GitHubContents("owner/repo", "t", session=session).ensure_branch()
    assert [c for c in session.calls if c[0] == "POST"] == []


def test_put_404_mentions_branch_and_permission():
    session = FakeSession(put_status=404)
    client = GitHubContents("owner/repo", "t", session=session)
    with pytest.raises(StateWriteError) as exc:
        client.put("control.json", b"{}", "m")
    message = str(exc.value)
    assert "HTTP 404" in message
    assert "分支不存在" in message

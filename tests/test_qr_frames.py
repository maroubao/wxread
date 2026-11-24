"""二维码帧命名与旧帧清理：网页只该看到本次会话的码。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from support import MemoryContents
from wxread.state import StateStore
from wxread.worker import purge_stale_qr, qr_frame_path

STATE_KEY = b"k" * 32


def make_store(backend: MemoryContents) -> StateStore:
    return StateStore(backend, STATE_KEY, owner_repo="owner/repo", key_version=1)


def test_frame_path_carries_session_and_padded_seq() -> None:
    path = qr_frame_path("abc123", 7, "0011223344556677")
    assert path == "responses/qr-abc123-007-0011223344556677.png"


def test_frame_names_sort_in_capture_order() -> None:
    names = [qr_frame_path("s1", n, "f" * 16) for n in (10, 2, 1)]
    assert sorted(names) == [
        qr_frame_path("s1", 1, "f" * 16),
        qr_frame_path("s1", 2, "f" * 16),
        qr_frame_path("s1", 10, "f" * 16),
    ]


def test_purge_drops_frames_from_other_sessions_only() -> None:
    backend = MemoryContents()
    backend.files["responses/qr-old-001-aaaaaaaaaaaaaaaa.png"] = b"old"
    backend.files["responses/qr-old-002-bbbbbbbbbbbbbbbb.png"] = b"old"
    backend.files["responses/qr-keep-001-cccccccccccccccc.png"] = b"new"
    backend.files["responses/last-response.json"] = b"{}"
    store = make_store(backend)

    removed = purge_stale_qr(store, "keep")

    assert removed == 2
    assert backend.list("responses") == [
        "last-response.json",
        "qr-keep-001-cccccccccccccccc.png",
    ]


def test_purge_ignores_non_frame_files() -> None:
    backend = MemoryContents()
    backend.files["responses/note.png"] = b"x"
    backend.files["accounts.json"] = b"[]"
    store = make_store(backend)

    assert purge_stale_qr(store, "keep") == 0
    assert backend.list("responses") == ["note.png"]


def test_purge_survives_backends_without_listing() -> None:
    class NoList(MemoryContents):
        def list(self, directory: str) -> list[str]:
            raise NotImplementedError

    assert purge_stale_qr(make_store(NoList()), "keep") == 0


def test_purge_tolerates_delete_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = MemoryContents()
    backend.files["responses/qr-old-001-aaaaaaaaaaaaaaaa.png"] = b"old"
    store = make_store(backend)

    def boom(path: str, message: str, sha: str | None = None) -> None:
        from wxread.state import StateWriteError

        raise StateWriteError("删除失败")

    monkeypatch.setattr(backend, "delete", boom)

    assert purge_stale_qr(store, "keep") == 0


def test_purge_preserves_recent_claimed_scan_and_drops_expired_scan() -> None:
    backend = MemoryContents()
    store = make_store(backend)
    for session, age in (("active", 0), ("expired", 45)):
        store.claim_session(session, {"action": "qr", "claimed_at": (datetime.now(timezone.utc) - timedelta(minutes=age)).isoformat()})
        store.put_raw(qr_frame_path(session, 1, "f" * 16), b"png", "fixture")
    assert purge_stale_qr(store, "new") == 1
    assert backend.list("responses") == ["qr-active-001-" + "f" * 16 + ".png"]

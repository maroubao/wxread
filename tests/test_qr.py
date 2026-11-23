from wxread import qr


class FakePage:
    """脚本化页面：每次 `qr_png()` 取下一帧；第 `login_at` 次轮询后返回登录 cookie。"""

    def __init__(self, frames: list[bytes], login_at: int | None, ua: str = "UA"):
        self.frames = frames
        self.login_at = login_at
        self.calls = 0
        self._ua = ua
        self.closed = False
        self.opened = False

    def open(self) -> None:
        self.opened = True

    def qr_png(self) -> bytes | None:
        if not self.frames:
            return None
        index = min(self.calls, len(self.frames) - 1)
        self.calls += 1
        return self.frames[index]

    def cookies(self) -> dict[str, str]:
        if self.login_at is not None and self.calls > self.login_at:
            return {"wr_vid": "1234567890", "wr_skey": "abcdefghij"}
        return {"wr_vid": ""}

    def user_agent(self) -> str:
        return self._ua

    def close(self) -> None:
        self.closed = True


def make_clock(start: float = 0.0, step: float = 1.0):
    state = {"t": start}

    def now() -> float:
        state["t"] += step
        return state["t"]

    return now


def test_qr_frames_are_deduplicated_by_fingerprint():
    page = FakePage([b"frame-a" * 100, b"frame-a" * 100, b"frame-b" * 100], login_at=3)
    seen: list[str] = []
    result = qr.wait_for_login(
        page, timeout=20, poll=1, now=make_clock(), sleep=lambda _s: None, on_qr=lambda shot: seen.append(shot.fingerprint)
    )
    assert result.ok and result.reason == "logged_in"
    assert seen == [qr.fingerprint(b"frame-a" * 100), qr.fingerprint(b"frame-b" * 100)]
    assert [shot.fingerprint for shot in result.shots] == seen


def test_qr_timeout_reports_refresh_count():
    page = FakePage([b"a" * 600, b"b" * 600], login_at=None)
    result = qr.wait_for_login(page, timeout=5, poll=1, now=make_clock(step=1), sleep=lambda _s: None)
    assert result.ok is False and result.reason == "timeout"
    assert len(result.shots) == 2


class ReopeningPage(FakePage):
    """记录整页重开次数；重开即推出新一帧（模拟真机上换码）。"""

    def __init__(self, frames: list[bytes], login_at: int | None):
        super().__init__(frames, login_at)
        self.reopens = 0

    def reopen(self) -> None:
        self.reopens += 1
        self.frames.append(b"reopened-%d" % self.reopens * 80)


def test_qr_reopens_page_before_code_expires():
    page = ReopeningPage([b"a" * 600], login_at=None)
    result = qr.wait_for_login(
        page, timeout=30, poll=1, now=make_clock(step=1), sleep=lambda _s: None, refresh_after=5
    )
    assert result.reason == "timeout"
    assert page.reopens >= 3
    assert len({shot.fingerprint for shot in result.shots}) == len(result.shots) > page.reopens


def test_qr_can_be_cancelled():
    page = FakePage([b"a" * 600], login_at=None)
    result = qr.wait_for_login(page, timeout=100, poll=1, now=make_clock(), sleep=lambda _s: None, should_stop=lambda: True)
    assert result.reason == "cancelled"


def test_account_from_cookies_flags_missing_payload():
    account = qr.account_from_cookies({"wr_vid": "1", "wr_skey": "s" * 10, "junk": "x"}, label="扫码号", now_iso="2026-11-23T10:00:00Z")
    assert account.id and account.label == "扫码号"
    assert "junk" not in account.cookies
    assert account.note == qr.PAYLOAD_HINT

    filled = qr.account_from_cookies(
        {"wr_vid": "1", "wr_skey": "s" * 10}, payload={"appId": "wb1", "ps": "p", "pc": "c"}, now_iso="2026-11-23T10:00:00Z"
    )
    assert filled.note == "" and filled.read_payload["ps"] == "p"


def test_describe_mentions_failure_reason():
    result = qr.QrResult(ok=False, reason="timeout", shots=[qr.QrShot("f", b"x", 0.0)], waited=600)
    assert "超时" in qr.describe(result)

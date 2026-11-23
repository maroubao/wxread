"""自动抓取阅读参数：`web/book/read` 的 POST body → 账户 read_payload。"""

import json

from wxread import capture, control
from wxread.state import StateStore

READ_BODY = json.dumps(
    {
        "appId": "wb182564874603h266381671",
        "b": "ce032b305a9bc1ce0b0dd2a",
        "c": "7f632b502707f6ffaa6bf2e",
        "ci": 27,
        "co": 389,
        "sm": "19聚会《三体》",
        "pr": 74,
        "ps": "4ee326507a65a465g015fae",
        "pc": "aab32e207a65a466g010615",
        "ts": 1761000000000,
    }
)

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 Chrome/141",
    "Content-Type": "application/json",
    "Cookie": "wr_vid=1234567890; wr_skey=abcdefghij",
    "Origin": "https://weread.qq.com",
    "Referer": "https://weread.qq.com/web/book/read",
    "accept": "application/json",
}


def test_parse_read_body_accepts_the_sites_payload():
    parsed = capture.parse_read_body(READ_BODY)
    assert parsed is not None
    assert parsed["ps"] == "4ee326507a65a465g015fae"


def test_parse_read_body_rejects_form_encoding_and_missing_fields():
    assert capture.parse_read_body("appId=1&ps=2&pc=3") is None
    assert capture.parse_read_body("{not json") is None
    assert capture.parse_read_body(json.dumps({"appId": "x", "ps": ""})) is None
    assert capture.parse_read_body(None) is None


def test_sniffer_takes_the_first_qualified_read_request():
    sniffer = capture.ReadSniffer()
    assert sniffer.feed("https://weread.qq.com/web/book/read", "GET", None) is False
    assert sniffer.feed("https://weread.qq.com/web/shelf", "POST", READ_BODY) is False
    assert sniffer.feed("https://weread.qq.com/web/book/read", "POST", READ_BODY, BROWSER_HEADERS) is True
    assert sniffer.ready is True

    second = json.loads(READ_BODY) | {"ps": "another"}
    sniffer.feed("https://weread.qq.com/web/book/read", "POST", json.dumps(second))
    assert sniffer.read_payload["ps"] == "4ee326507a65a465g015fae"  # 不覆盖第一条


def test_sniffer_promotes_only_account_fields_and_drops_transport_headers():
    sniffer = capture.ReadSniffer()
    sniffer.feed("https://weread.qq.com/web/book/readingProgress", "POST", READ_BODY, BROWSER_HEADERS)
    payload = sniffer.read_payload
    assert set(payload) == {"appId", "ps", "pc", "ci", "co", "sm", "pr", "b", "c"}
    assert all(isinstance(value, str) for value in payload.values())
    assert "ts" not in payload

    headers = sniffer.headers
    assert headers["User-Agent"] == "Mozilla/5.0 Chrome/141"
    assert headers["accept"] == "application/json"
    for dropped in ("Cookie", "Content-Type", "Origin", "Referer"):
        assert dropped not in headers


class QrOnlyPage:
    """只有二维码流程所需方法的页面：没有 capture_read_payload。"""

    def __init__(self) -> None:
        self.calls = 0
        self.closed = False

    def open(self) -> None:
        pass

    def qr_png(self) -> bytes | None:
        self.calls += 1
        return b"\x89PNG" + bytes([self.calls])

    def cookies(self) -> dict[str, str]:
        return {"wr_vid": "1234567890", "wr_skey": "abcdefghij"}

    def user_agent(self) -> str:
        return "UA"

    def close(self) -> None:
        self.closed = True


class CapturingPage(QrOnlyPage):
    def capture_read_payload(self):
        return capture.promoted(json.loads(READ_BODY)), {"accept": "application/json"}


class BrokenCapturePage(QrOnlyPage):
    def capture_read_payload(self):
        raise RuntimeError("页面炸了")


def make_ctx(store: StateStore, page) -> control.ControlContext:
    return control.ControlContext(
        store=store,
        now_iso=lambda: "2026-11-23T10:00:00Z",
        qr_page_factory=lambda: page,
    )


def test_qr_binds_payload_captured_from_the_browser(store: StateStore):
    page = CapturingPage()
    receipt = control.handle_qr(make_ctx(store, page), {"label": "主号"})
    assert receipt["ok"] is True
    assert receipt["payload_captured"] is True

    stored = store.load_account(receipt["account_id"])
    assert stored is not None
    assert stored["read_payload"]["ps"] == "4ee326507a65a465g015fae"
    assert stored["read_payload"]["pc"] == "aab32e207a65a466g010615"
    assert stored["note"] == ""
    assert page.closed is True


def test_qr_without_capture_still_binds_and_flags_the_gap(store: StateStore):
    receipt = control.handle_qr(make_ctx(store, QrOnlyPage()), {})
    assert receipt["ok"] is True
    assert receipt["payload_captured"] is False
    stored = store.load_account(receipt["account_id"])
    assert stored["read_payload"] == {}
    assert "ps/pc" in stored["note"]


def test_capture_failure_does_not_break_the_binding(store: StateStore):
    receipt = control.handle_qr(make_ctx(store, BrokenCapturePage()), {})
    assert receipt["ok"] is True
    assert receipt["payload_captured"] is False


def test_rebind_clears_the_stale_missing_payload_note(store: StateStore):
    first = control.handle_qr(make_ctx(store, QrOnlyPage()), {})
    assert "ps/pc" in store.load_account(first["account_id"])["note"]

    again = control.handle_qr(make_ctx(store, CapturingPage()), {})
    assert again["account_id"] == first["account_id"]
    stored = store.load_account(again["account_id"])
    assert stored["note"] == ""
    assert stored["read_payload"]["ps"] == "4ee326507a65a465g015fae"

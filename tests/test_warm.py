from wxread import warm
from wxread.accounts import Account
from wxread.reader import RenewalFailed, TransportError


def make_account(aid="acc1", status="ok", enabled=True):
    return Account.from_dict(
        {
            "schema_version": 1,
            "id": aid,
            "label": "号" + aid[-1],
            "cookies": {"wr_vid": "1", "wr_skey": "s" * 8},
            "read_payload": {"appId": "wb1", "ps": "p", "pc": "c"},
            "target_count": 40,
            "enabled": enabled,
            "status": status,
        }
    )


class FakeTransport:
    def __init__(self, skey="newkey00", fail=False):
        self.skey = skey
        self.fail = fail
        self.calls = 0

    def post(self, url, payload, cookies, headers):
        from wxread.reader import TransportResponse

        self.calls += 1
        if self.fail:
            raise TransportError("boom")
        return TransportResponse(status_code=200, payload={"succ": 1}, set_cookies={"wr_skey": self.skey})


def test_warm_renews_and_writes_skey_with_timestamp(store):
    account = make_account()
    store.save_account(account)
    summary = warm.warm_all(store, transport_factory=lambda: FakeTransport(), now_iso=lambda: "2026-11-23T10:00:00Z")
    assert summary.warmed == 1 and summary.failed == 0
    saved = Account.from_dict(store.load_account("acc1"), "acc1")
    assert saved.cookies["wr_skey"] == "newkey00"
    assert saved.renewed_at == "2026-11-23T10:00:00Z"


def test_warm_marks_needs_login_on_renewal_failure(store):
    account = make_account()
    store.save_account(account)
    summary = warm.warm_all(store, transport_factory=lambda: FakeTransport(fail=True), now_iso=lambda: "2026-11-23T10:00:00Z")
    assert summary.failed == 1
    saved = Account.from_dict(store.load_account("acc1"), "acc1")
    assert saved.status == "needs_login"
    assert saved.cookies["wr_skey"] == "s" * 8  # 失败不得覆盖旧 skey


def test_warm_skips_disabled_and_needs_login(store):
    store.save_account(make_account(aid="acc1", enabled=False))
    store.save_account(make_account(aid="acc2", status="needs_login"))
    summary = warm.warm_all(store, transport_factory=lambda: FakeTransport(), now_iso=lambda: "2026-11-23T10:00:00Z")
    assert summary.skipped == 2 and summary.warmed == 0


def test_warm_summary_render(store):
    store.save_account(make_account())
    summary = warm.warm_all(store, transport_factory=lambda: FakeTransport(fail=True), now_iso=lambda: "2026-11-23T10:00:00Z")
    text = warm.render_summary(summary)
    assert "失败 1" in text and "renew_failed" in text or "RenewalFailed" in text


def test_renewal_failed_is_read_error():
    assert issubclass(RenewalFailed, Exception)

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


def test_warm_keeps_network_failures_retryable(store):
    account = make_account()
    store.save_account(account)
    summary = warm.warm_all(store, transport_factory=lambda: FakeTransport(fail=True), now_iso=lambda: "2026-11-23T10:00:00Z")
    assert summary.failed == 1
    saved = Account.from_dict(store.load_account("acc1"), "acc1")
    assert saved.status == "ok"
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
    assert "失败 1" in text


def test_renewal_failed_is_read_error():
    assert issubclass(RenewalFailed, Exception)


class RejectedTransport(FakeTransport):
    def post(self, *args):
        from wxread.reader import TransportResponse
        return TransportResponse(200, {"errcode": -2012})


def test_two_confirmed_login_failures_are_required(store):
    store.save_account(make_account())
    warm.warm_all(store, transport_factory=RejectedTransport, now_iso=lambda: "2026-11-24T01:00:00Z")
    assert store.load_account("acc1")["status"] == "ok"
    warm.warm_all(store, transport_factory=RejectedTransport, now_iso=lambda: "2026-11-24T07:00:00Z")
    assert store.load_account("acc1")["status"] == "needs_login"
    assert store.load_account("acc1")["warm_failures"] == 2


def test_success_clears_the_previous_login_failure(store):
    store.save_account(make_account())
    warm.warm_all(store, transport_factory=RejectedTransport, now_iso=lambda: "2026-11-24T01:00:00Z")
    warm.warm_all(store, transport_factory=FakeTransport, now_iso=lambda: "2026-11-24T07:00:00Z")
    assert store.load_account("acc1")["warm_failures"] == 0


def test_worker_notifies_once_after_two_login_failures(store, monkeypatch):
    from wxread import notify, worker

    store.save_account(make_account())
    store.save_settings({"notifications": {"enabled": True, "channels": ["email"], "email": {}}})
    sent = []
    monkeypatch.setattr(notify, "send", lambda channel, cfg, title, text: sent.append((title, text)) or notify.NotifyResult(channel, True))
    for _attempt in range(3):
        worker.cmd_warm(store, {}, transport_factory=RejectedTransport)
    assert len(sent) == 1
    assert "重新扫码" in sent[0][0]


def test_save_failure_does_not_interrupt_the_other_accounts(store, monkeypatch):
    from wxread.state import StateWriteError

    store.save_account(make_account("acc1"))
    store.save_account(make_account("acc2"))
    original = store.save_account_runtime

    def save(account, baseline, message):
        if account["id"] == "acc1":
            raise StateWriteError("temporary write failure")
        return original(account, baseline, message)

    monkeypatch.setattr(store, "save_account_runtime", save)
    summary = warm.warm_all(store, transport_factory=FakeTransport, now_iso=lambda: "2026-11-24T01:00:00Z")
    assert summary.failed == 1 and summary.warmed == 1


def test_legacy_warm_network_failure_can_recover(store):
    account = make_account(status="needs_login")
    account.note = "renewal 未返回 wr_skey"
    store.save_account(account)
    summary = warm.warm_all(store, transport_factory=FakeTransport, now_iso=lambda: "2026-11-24T01:00:00Z")
    assert summary.warmed == 1
    assert store.load_account(account.id)["status"] == "ok"

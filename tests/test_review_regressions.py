import pytest

from wxread import control, notify, warm, worker
from wxread.accounts import account_from_curl
from wxread.jobs import JobRunner
from wxread.reader import READ_URL, RENEW_URL, TransportResponse
from support import CURL_SAMPLE
from test_jobs import env_base
from test_reader import FakeTransport, err, ok, renew_ok


def test_queued_read_uses_latest_session_and_preserves_planned_target(store):
    queued = account_from_curl(CURL_SAMPLE)
    queued.target_count = 1
    store.save_account(queued)
    fresh = queued.to_dict()
    fresh["cookies"]["wr_skey"] = "new-login"
    fresh["headers"]["User-Agent"] = "new-agent"
    fresh["read_payload"]["pc"] = "new-pc"
    fresh["target_count"] = 100
    store.save_account(fresh)
    seen = []

    class LatestTransport(FakeTransport):
        def post(self, url, payload, cookies, headers):
            seen.append((cookies["wr_skey"], payload.get("pc"), headers["User-Agent"]))
            return ok()

    record = JobRunner(store, transport_factory=LatestTransport, pace=0).run([queued])[0]
    assert seen == [("new-login", "new-pc", "new-agent")]
    assert record["read_count"] == record["target_count"] == 1
    assert store.load_account(queued.id)["target_count"] == 100
    assert store.load_account(queued.id)["cookies"]["wr_skey"] == "new-login"


def test_queued_warm_uses_rebound_session(store):
    queued = account_from_curl(CURL_SAMPLE)
    store.save_account(queued)
    fresh = queued.to_dict()
    fresh["cookies"]["wr_skey"] = "new-login"
    store.save_account(fresh)
    seen = []

    class LatestTransport(FakeTransport):
        def post(self, url, payload, cookies, headers):
            seen.append(cookies["wr_skey"])
            return TransportResponse(200, {"succ": 1})

    summary = warm.warm_all(store, accounts=[queued], transport_factory=LatestTransport, now_iso=lambda: "2026-11-24T12:00:00Z")
    assert summary.warmed == 1 and seen == ["new-login"]
    assert store.load_account(queued.id)["cookies"]["wr_skey"] == "new-login"


def test_queued_warm_rechecks_enabled_state(store):
    queued = account_from_curl(CURL_SAMPLE)
    store.save_account(queued)
    fresh = queued.to_dict()
    fresh["enabled"] = False
    store.save_account(fresh)
    made = []
    summary = warm.warm_all(store, accounts=[queued], transport_factory=lambda: made.append(True), now_iso=lambda: "now")
    assert summary.skipped == 1 and not made


@pytest.mark.parametrize("change_payload", [True, False])
def test_successful_capture_supersedes_failed_old_reader_even_without_cookie_change(store, change_payload):
    account = account_from_curl(CURL_SAMPLE)
    account.headers["User-Agent"] = "fixture-agent"
    account.target_count = 2
    store.save_account(account)
    initial = account.to_dict()

    class Page:
        def open(self): pass
        def close(self): pass
        def cookies(self): return dict(initial["cookies"])
        def user_agent(self): return "fixture-agent"
        def capture_read_payload(self):
            payload = dict(initial["read_payload"])
            if change_payload:
                payload["pc"] = "new-pc"
            return payload, dict(initial["headers"])

    class FailedOldTransport(FakeTransport):
        captured = False
        def post(self, url, payload, cookies, headers):
            if url == READ_URL and not self.captured:
                self.captured = True
                receipt = control.handle_capture(control.ControlContext(store, qr_page_factory=Page), {"account_id": account.id})
                assert receipt["ok"]
            return err(-2012)

    record = JobRunner(store, transport_factory=FailedOldTransport, pace=0).run([account])[0]
    saved = store.load_account(account.id)
    assert saved["cookies"] == initial["cookies"]
    assert saved["status"] == "ok"
    assert saved["read_payload"]["pc"] == ("new-pc" if change_payload else initial["read_payload"]["pc"])
    assert record["reason"] == "cancelled" and saved["progress"]["status"] == "cancelled"


def test_l2_then_stop_records_cancellation_without_exception_alert(store, monkeypatch):
    account = account_from_curl(CURL_SAMPLE)
    store.save_account(account)
    store.save_settings({"notifications": {"enabled": True, "channels": ["feishu", "pushplus"]}})
    messages = []
    monkeypatch.setattr(notify, "send", lambda channel, cfg, title, text: messages.append((channel, text)) or notify.NotifyResult(channel, True))

    class StopAfterRecovery(FakeTransport):
        def __init__(self):
            super().__init__([err(-2012), renew_ok(), ok()])
        def post(self, *args):
            response = super().post(*args)
            if args[0] != RENEW_URL and response.payload.get("synckey"):
                run = store.load_account(account.id)["progress"]["run_id"]
                control.handle_stop(control.ControlContext(store), {"account_id": account.id, "run_id": run})
            return response

    worker.cmd_read(store, env_base(), transport_factory=StopAfterRecovery, sleep=lambda _s: None)
    record = store.recent_runs()[0]
    assert record["read_count"] == 1 and record["reason"] == "cancelled"
    assert store.load_account(account.id)["progress"]["status"] == "cancelled"
    assert len(messages) == 1 and messages[0][0] == "feishu" and "已停止" in messages[0][1]


def test_late_old_stop_during_new_progress_cas_does_not_cancel_new_run(store, monkeypatch):
    account = account_from_curl(CURL_SAMPLE)
    account.target_count = 1
    old = account.to_dict()
    old["progress"] = {"run_id": "old", "status": "running", "cancel_requested": False}
    store.save_account(old)
    original = store.save_account_runtime
    first = True

    def stop_then_save(fresh, baseline, message):
        nonlocal first
        if first:
            first = False
            control.handle_stop(control.ControlContext(store), {"account_id": account.id, "run_id": "old"})
        return original(fresh, baseline, message)

    monkeypatch.setattr(store, "save_account_runtime", stop_then_save)
    record = JobRunner(store, transport_factory=lambda: FakeTransport([ok()]), pace=0).run([account])[0]
    assert record["run_id"] != "old" and record["read_count"] == 1 and record["reason"] == "ok"


def test_new_run_stop_during_cas_is_preserved(store):
    account = account_from_curl(CURL_SAMPLE)
    old = account.to_dict()
    old["progress"] = {"run_id": "old", "cancel_requested": False}
    store.save_account(old)
    fresh = account.to_dict()
    fresh["progress"] = {"run_id": "new", "cancel_requested": False, "status": "running"}
    concurrent = dict(old, progress={"run_id": "new", "cancel_requested": True, "status": "running"})
    store.save_account(concurrent)
    saved = store.save_account_runtime(fresh, old, "fixture")
    assert saved["progress"]["run_id"] == "new" and saved["progress"]["cancel_requested"] is True

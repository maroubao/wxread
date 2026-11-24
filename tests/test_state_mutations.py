"""用正常管理入口复现 CAS 重试覆盖并发修改及重复运行记录。"""

from wxread import control
from wxread.accounts import account_from_curl
from wxread.state import Conflict

from support import CURL_SAMPLE


def race_next_write(store, monkeypatch, path, change):
    original = store.backend.put
    fired = False

    def put(target, data, message, sha=None, create_only=False):
        nonlocal fired
        if target == path and not fired:
            fired = True
            change()
            raise Conflict("另一个任务已提交")
        return original(target, data, message, sha=sha, create_only=create_only)

    monkeypatch.setattr(store.backend, "put", put)


def test_schedule_edit_preserves_concurrent_notifications(store, monkeypatch):
    store.save_settings({"schedule": {"daily": "01:07"}, "notifications": {"enabled": False}})
    race_next_write(
        store, monkeypatch, "settings.enc.json",
        lambda: store.save_settings({"schedule": {"daily": "01:07"}, "notifications": {"enabled": True}}),
    )
    control.handle_schedule(control.ControlContext(store), {"enabled": False})
    saved = store.load_settings()
    assert saved["schedule"]["enabled"] is False
    assert saved["notifications"]["enabled"] is True


def test_account_edit_preserves_concurrent_cookie_rotation(store, monkeypatch):
    account = account_from_curl(CURL_SAMPLE, label="旧备注")
    store.save_account(account)
    rotated = account.to_dict()
    rotated["cookies"]["wr_skey"] = "new-session-key"
    race_next_write(store, monkeypatch, store.account_path(account.id), lambda: store.save_account(rotated))
    control.handle_update(control.ControlContext(store), {"account_id": account.id, "label": "新备注"})
    saved = store.load_account(account.id)
    assert saved["label"] == "新备注"
    assert saved["cookies"]["wr_skey"] == "new-session-key"


def test_concurrent_run_appends_preserve_both_records(store, monkeypatch):
    first = {"run_id": "first", "started_at": "2026-11-23T01:00:00Z", "read_count": 1}
    second = {"run_id": "second", "started_at": "2026-11-23T02:00:00Z", "read_count": 2}
    store.append_run(first)
    race_next_write(store, monkeypatch, "runs/2026-11.enc.json", lambda: store.append_run(second))
    store.append_run({"run_id": "third", "started_at": "2026-11-23T03:00:00Z", "read_count": 3})
    assert {record["run_id"] for record in store.load_runs("2026-11")} == {"first", "second", "third"}


def test_retrying_the_same_run_is_idempotent(store):
    record = {"run_id": "same-run", "started_at": "2026-11-23T01:00:00Z", "read_count": 2}
    store.append_run(record)
    store.append_run(record)
    assert len(store.load_runs("2026-11")) == 1

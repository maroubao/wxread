from wxread import control, worker
from wxread.accounts import account_from_curl
from support import CURL_SAMPLE


def test_deleted_last_account_is_not_reimported_from_legacy_secret(store):
    account = account_from_curl(CURL_SAMPLE)
    store.save_account(account)
    store.save_settings({"notifications": {"enabled": True, "channels": ["feishu"]}})
    control.handle_delete(control.ControlContext(store), {"account_id": account.id})
    assert worker.load_accounts(store, {"WXREAD_CURL_BASH": CURL_SAMPLE}) == []
    assert store.list_account_ids() == []
    assert store.load_settings()["notifications"]["channels"] == ["feishu"]


def test_first_legacy_import_is_still_supported_but_only_once(store):
    env = {"WXREAD_CURL_BASH": CURL_SAMPLE}
    accounts = worker.load_accounts(store, env)
    assert len(accounts) == 1
    store.delete_account(accounts[0].id)
    assert worker.load_accounts(store, env) == []


def test_worker_does_not_recreate_account_deleted_after_loading(store, monkeypatch):
    from test_jobs import env_base
    from test_reader import FakeTransport

    account = account_from_curl(CURL_SAMPLE)
    store.save_account(account)

    def loaded_then_deleted(_store, _env):
        store.delete_account(account.id)
        return [account]

    monkeypatch.setattr(worker, "load_accounts", loaded_then_deleted)
    worker.cmd_read(store, env_base(), transport_factory=lambda: FakeTransport([]), sleep=lambda _s: None)
    assert store.load_account(account.id) is None
    assert store.recent_runs()[0]["read_count"] == 0

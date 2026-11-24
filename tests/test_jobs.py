"""P2 jobs / worker 测试：串行队列、跳过、写回、旧配置迁移、退出码。"""

from __future__ import annotations

import base64

import pytest

from wxread import worker
from wxread.accounts import account_from_curl
from wxread.jobs import JobRunner

from support import CURL_SAMPLE, MemoryContents
from test_reader import FakeTransport, ok
from wxread.reader import TransportResponse

STATE_KEY = base64.b64encode(b"k" * 32).decode("ascii")


def env_base(**extra):
    env = {
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_TOKEN": "gh-token",
        "WXREAD_STATE_KEY": STATE_KEY,
        "WXREAD_PACE": "0",
        "WXREAD_DEADLINE": "60",
        "WXREAD_TARGET_COUNT": "2",
    }
    env.update(extra)
    return env


def make_store(env):
    backend = MemoryContents()
    return backend, worker.build_store(env, backend=backend)


def test_runner_skips_disabled_and_needs_login():
    _backend, store = make_store(env_base())
    active = account_from_curl(CURL_SAMPLE)
    active.target_count = 1
    pending = account_from_curl(CURL_SAMPLE.replace("wr_vid=1234567890", "wr_vid=1111111111"))
    pending.mark_needs_login("cookie 失效")
    runner = JobRunner(store, transport_factory=lambda: FakeTransport([ok()]), pace=0, sleep=lambda _s: None)
    records = runner.run([active, pending])
    assert len(records) == 1
    assert records[0]["account_id"] == active.id
    assert records[0]["read_count"] == 1


def test_runner_persists_account_and_run():
    backend, store = make_store(env_base())
    account = account_from_curl(CURL_SAMPLE)
    account.target_count = 2
    runner = JobRunner(store, transport_factory=lambda: FakeTransport([ok(), ok()]), pace=0, sleep=lambda _s: None)
    records = runner.run([account])
    saved = store.load_account(account.id)
    assert saved["last_ok_at"] is not None
    assert store.load_runs(records[0]["started_at"][:7])[0]["read_count"] == 2
    assert any(path.startswith("accounts/") for path in backend.files)


def test_runner_marks_state_error_as_exit_4():
    backend, store = make_store(env_base())
    account = account_from_curl(CURL_SAMPLE)
    account.target_count = 1
    runner = JobRunner(store, transport_factory=lambda: FakeTransport([ok()]), pace=0, sleep=lambda _s: None)
    runner.run([account])
    backend.conflicts = 99
    records = runner.run([account])
    assert records[0]["exit_code"] == 4
    assert "state_error" in records[0]


def test_worker_read_migrates_then_reads():
    env = env_base(WXREAD_CURL_BASH=CURL_SAMPLE)
    backend, store = make_store(env)
    made: list[FakeTransport] = []

    def factory():
        transport = FakeTransport([ok(), ok()])
        made.append(transport)
        return transport

    code = worker.cmd_read(store, env, transport_factory=factory)
    assert code == worker.EXIT_OK
    assert store.list_account_ids()
    assert len(made[0].calls) == 2


def test_worker_read_skips_needs_login_accounts():
    env = env_base()
    _backend, store = make_store(env)
    account = account_from_curl(CURL_SAMPLE)
    account.mark_needs_login("过期")
    store.save_account(account.to_dict())
    used: list[int] = []

    def factory():
        used.append(1)
        return FakeTransport([ok()])

    assert worker.cmd_read(store, env, transport_factory=factory) == worker.EXIT_OK
    assert used == []


def test_worker_without_accounts_is_config_error():
    env = env_base()
    _backend, store = make_store(env)
    assert worker.cmd_read(store, env, transport_factory=lambda: FakeTransport()) == worker.EXIT_CONFIG


def test_worker_missing_key_is_config_error():
    env = {"GITHUB_REPOSITORY": "owner/repo", "GITHUB_TOKEN": "t"}
    with pytest.raises(worker.ConfigError):
        worker.build_store(env, backend=MemoryContents())


def test_worker_main_reports_config_error(capsys):
    code = worker.main(["read"], env={"GITHUB_REPOSITORY": "owner/repo"})
    assert code == worker.EXIT_CONFIG


def test_state_key_accepts_hex_and_base64():
    raw = bytes(range(32))
    assert worker.decode_state_key(base64.b64encode(raw).decode()) == raw
    assert worker.decode_state_key(raw.hex()) == raw
    with pytest.raises(worker.ConfigError):
        worker.decode_state_key("short")


def test_all_accounts_failed_returns_1():
    env = env_base()
    _backend, store = make_store(env)
    account = account_from_curl(CURL_SAMPLE)
    account.target_count = 1
    store.save_account(account.to_dict())
    from test_reader import TransportError

    factory = lambda: FakeTransport([TransportError("x")] * 100)
    assert worker.cmd_read(store, env, transport_factory=factory, sleep=lambda _s: None) == worker.EXIT_ALL_FAILED


def test_unexpected_account_failure_persists_cookies_and_continues_batch():
    _backend, store = make_store(env_base())
    first = account_from_curl(CURL_SAMPLE)
    first.target_count = 2
    second = account_from_curl(CURL_SAMPLE.replace("wr_vid=1234567890", "wr_vid=1111111111"))
    second.target_count = 1
    transports = iter([
        FakeTransport([TransportResponse(200, {"succ": 1, "synckey": "sync"}, {"wr_skey": "rotated"}), RuntimeError("unexpected")]),
        FakeTransport([ok()]),
    ])
    records = JobRunner(store, transport_factory=lambda: next(transports), pace=0).run([first, second])
    assert len(records) == 2 and records[1]["read_count"] == 1
    assert store.load_account(first.id)["cookies"]["wr_skey"] == "rotated"
    assert records[0]["read_count"] == 1 and records[0]["reason"] == "internal_error"


def test_interrupt_saves_progress_and_reraises():
    _backend, store = make_store(env_base())
    account = account_from_curl(CURL_SAMPLE)
    account.target_count = 2
    transport = FakeTransport([TransportResponse(200, {"succ": 1, "synckey": "sync"}, {"wr_skey": "rotated"}), KeyboardInterrupt()])
    with pytest.raises(KeyboardInterrupt):
        JobRunner(store, transport_factory=lambda: transport, pace=0).run([account])
    assert store.load_account(account.id)["cookies"]["wr_skey"] == "rotated"
    record = store.recent_runs()[0]
    assert record["read_count"] == 1 and record["reason"] == "cancelled"


def test_running_account_does_not_overwrite_a_concurrent_edit():
    from wxread import control

    _backend, store = make_store(env_base())
    account = account_from_curl(CURL_SAMPLE, label="旧备注")
    account.target_count = 1
    store.save_account(account)

    class EditingTransport(FakeTransport):
        def post(self, *args):
            control.handle_update(control.ControlContext(store), {"account_id": account.id, "label": "新备注", "enabled": False})
            return ok()

    JobRunner(store, transport_factory=EditingTransport, pace=0).run([account])
    saved = store.load_account(account.id)
    assert saved["label"] == "新备注" and saved["enabled"] is False


def test_deleted_account_is_not_recreated_by_final_write():
    _backend, store = make_store(env_base())
    account = account_from_curl(CURL_SAMPLE)
    account.target_count = 1
    store.save_account(account)

    class DeletingTransport(FakeTransport):
        def post(self, *args):
            store.delete_account(account.id)
            return ok()

    JobRunner(store, transport_factory=DeletingTransport, pace=0).run([account])
    assert store.load_account(account.id) is None
    assert store.recent_runs()[0]["read_count"] == 1


def test_rebinding_during_a_run_preserves_the_new_login():
    from wxread import control

    _backend, store = make_store(env_base())
    account = account_from_curl(CURL_SAMPLE)
    account.target_count = 2
    store.save_account(account)

    class RebindingTransport(FakeTransport):
        def post(self, *args):
            control.handle_import(control.ControlContext(store), {"curl": CURL_SAMPLE.replace("wr_skey=abcdefghij", "wr_skey=rebound-session")})
            return TransportResponse(200, {"succ": 1, "synckey": "sync"}, {"wr_skey": "old-session-rotation"})

    record = JobRunner(store, transport_factory=RebindingTransport, pace=0).run([account])[0]
    assert store.load_account(account.id)["cookies"]["wr_skey"] == "rebound-session"
    assert record["read_count"] == 1 and record["reason"] == "cancelled"


def test_one_corrupt_account_does_not_prevent_loading_the_others():
    env = env_base()
    _backend, store = make_store(env)
    account = account_from_curl(CURL_SAMPLE)
    store.save_account(account)
    store.save_account({"id": "broken", "cookies": "not-an-object"})
    assert [item.id for item in worker.load_accounts(store, env)] == [account.id]

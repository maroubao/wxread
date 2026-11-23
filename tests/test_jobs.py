"""P2 jobs / worker 测试：串行队列、跳过、写回、旧配置迁移、退出码。"""

from __future__ import annotations

import base64

import pytest

from wxread import worker
from wxread.accounts import account_from_curl
from wxread.jobs import JobRunner

from support import CURL_SAMPLE, MemoryContents
from test_reader import FakeTransport, ok

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

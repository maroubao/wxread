from wxread import worker, notify
from wxread.accounts import account_from_curl
from support import CURL_SAMPLE
from test_jobs import env_base
from test_reader import FakeTransport, ok


def test_shard_defers_and_final_report_sends_one_summary(store, monkeypatch):
    account = account_from_curl(CURL_SAMPLE, label="主号")
    store.save_account(account)
    store.save_settings({"notifications": {"enabled": True, "channels": ["feishu"], "feishu": {"webhook": "fixture"}}})
    messages = []
    monkeypatch.setattr(notify, "send", lambda channel, cfg, title, text: messages.append((title, text)) or notify.NotifyResult(channel, True))
    env = env_base(WXREAD_BATCH_ID="batch-one", WXREAD_REPORT_MODE="defer")
    worker.cmd_read(store, env, transport_factory=lambda: FakeTransport([ok(), ok()]), sleep=lambda _s: None)
    assert messages == []
    assert worker.cmd_report(store, env) == 0
    assert len(messages) == 1 and "1/1" in messages[0][0]


def test_selected_account_does_not_run_the_others(store):
    first = account_from_curl(CURL_SAMPLE)
    second = account_from_curl(CURL_SAMPLE.replace("wr_vid=1234567890", "wr_vid=1111111111"))
    store.save_account(first)
    store.save_account(second)
    env = env_base(WXREAD_ACCOUNT_IDS=second.id)
    worker.cmd_read(store, env, transport_factory=lambda: FakeTransport([ok(), ok()]), sleep=lambda _s: None)
    assert [record["account_id"] for record in store.recent_runs()] == [second.id]


def test_selected_account_report_excludes_other_account_targets(store, monkeypatch):
    first = account_from_curl(CURL_SAMPLE, label="主号")
    second = account_from_curl(CURL_SAMPLE.replace("wr_vid=1234567890", "wr_vid=1111111111"), label="其它")
    store.save_account(first)
    store.save_account(second)
    store.save_settings({"notifications": {"enabled": True, "channels": ["feishu"], "feishu": {"webhook": "fixture"}}})
    messages = []
    monkeypatch.setattr(notify, "send", lambda channel, cfg, title, text: messages.append((title, text)) or notify.NotifyResult(channel, True))
    env = env_base(WXREAD_ACCOUNT_IDS=first.id, WXREAD_BATCH_ID="selected", WXREAD_REPORT_MODE="defer")
    worker.cmd_read(store, env, transport_factory=lambda: FakeTransport([ok(), ok()]), sleep=lambda _s: None)
    assert worker.cmd_report(store, env) == 0
    assert "1/1" in messages[0][0] and "其它" not in messages[0][1]

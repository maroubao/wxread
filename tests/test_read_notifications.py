"""从worker入口验证阅读通知策略，外部发送使用替身。"""

import pytest

from wxread import notify, worker
from wxread.accounts import account_from_curl
from support import CURL_SAMPLE
from test_jobs import env_base
from test_reader import FakeTransport, TransportError, err, ok, renew_ok


def run_with_notifications(store, monkeypatch, script):
    account = account_from_curl(CURL_SAMPLE, label="主号")
    store.save_account(account)
    store.save_settings({"notifications": {"enabled": True, "channels": ["feishu"], "feishu": {}}})
    messages = []
    monkeypatch.setattr(notify, "send", lambda channel, cfg, title, text: messages.append((title, text)) or notify.NotifyResult(channel, True))
    worker.cmd_read(store, env_base(), transport_factory=lambda: FakeTransport(script), sleep=lambda _s: None)
    return messages


@pytest.mark.parametrize("script", [[ok(), ok()], [TransportError("temporary"), ok(), ok()]])
def test_l0_and_l1_do_not_notify(store, monkeypatch, script):
    assert run_with_notifications(store, monkeypatch, script) == []


def test_l2_recovery_sends_a_recovery_message(store, monkeypatch):
    messages = run_with_notifications(store, monkeypatch, [err(-2012), renew_ok(), ok(), ok()])
    assert len(messages) == 1
    assert "恢复" in messages[0][0]
    assert "重新扫码" not in messages[0][0]


def test_l3_failure_sends_a_rebind_message(store, monkeypatch):
    messages = run_with_notifications(store, monkeypatch, [err(-2012), renew_ok(), err(-2012)])
    assert len(messages) == 1
    assert "重新扫码" in messages[0][0]


def test_a_bad_notification_channel_does_not_break_the_batch(monkeypatch):
    def sender(channel, _cfg, _title, _text):
        if channel == "email":
            raise ValueError("bad port")
        return notify.NotifyResult(channel, True)

    monkeypatch.setattr(notify, "send", sender)
    results = notify.dispatch({"notifications": {"enabled": True, "channels": ["email", "feishu"]}}, "title", "body")
    assert len(results) == 2
    assert results[0].ok is False and results[1].ok is True

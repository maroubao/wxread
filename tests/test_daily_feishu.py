import json
import base64
import hashlib
import hmac
from wxread import notify, worker
from wxread.accounts import account_from_curl
from support import CURL_SAMPLE
from test_jobs import env_base
from test_reader import FakeTransport, ok


def test_successful_read_sends_one_daily_feishu_summary(store, monkeypatch):
    account = account_from_curl(CURL_SAMPLE, label="主号")
    store.save_account(account)
    store.save_settings({"notifications": {"enabled": True, "channels": ["feishu"], "feishu": {"webhook": "fixture"}}})
    messages = []
    monkeypatch.setattr(notify, "send", lambda channel, cfg, title, text: messages.append((channel, title, text)) or notify.NotifyResult(channel, True))
    worker.cmd_read(store, env_base(), transport_factory=lambda: FakeTransport([ok(), ok()]), sleep=lambda _s: None)
    assert len(messages) == 1 and messages[0][0] == "feishu"
    assert "1/1" in messages[0][1] and "完成" in messages[0][2]


def test_legacy_feishu_secret_is_encrypted_and_migrated(store):
    settings = worker.notification_settings(store, {"FEISHU_WEBHOOK": "fixture-webhook"})
    assert settings["notifications"]["channels"] == ["feishu"]
    assert b"fixture-webhook" not in store.backend.files["settings.enc.json"]
    store.save_settings({"notifications": {"enabled": False, "channels": []}})
    assert worker.notification_settings(store, {"FEISHU_WEBHOOK": "fixture-webhook"})["notifications"]["enabled"] is False


def test_feishu_signature_and_response_are_validated(monkeypatch):
    seen = []
    monkeypatch.setattr(notify.time, "time", lambda: 1000)
    monkeypatch.setattr(notify, "_post", lambda url, body, headers: seen.append(json.loads(body)) or '{"code":0}')
    assert notify.send_feishu({"webhook": "fixture", "secret": "signing-key"}, "title", "text") == ""
    expected = base64.b64encode(hmac.new(b"1000\nsigning-key", b"", hashlib.sha256).digest()).decode()
    assert seen[0]["sign"] == expected

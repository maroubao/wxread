import pytest

from wxread import notify


def test_render_run_report_has_no_secrets():
    title, text = notify.render_run_report(
        [
            {"account_id": "aaaaaaaaaaaa", "label": "主号", "level": "L0", "read_count": 40},
            {"account_id": "bbbbbbbbbbbb", "label": "小号", "level": "L3", "read_count": 0, "reason": "renew_failed"},
        ]
    )
    assert "1/2" in title and "+40" in title
    assert "主号：L0 +40" in text
    assert "小号：L3 +0（renew_failed）" in text


def test_failure_render_and_scrub():
    title, text = notify.render_failure("主号", "renew_failed", "wr_skey=abcdefghij 已失效")
    assert "需要重新扫码" in title
    assert "renew_failed" in text
    assert "abcdefghij" not in text  # scrub 兜底


def test_send_reports_missing_config():
    result = notify.send("email", {}, "t", "x")
    assert result.ok is False and "缺少" in result.detail


def test_dispatch_skips_when_disabled():
    assert notify.dispatch({"notifications": {"enabled": False, "channels": ["email"]}}, "t", "x") == []


def test_dispatch_only_known_channels():
    settings = {"notifications": {"enabled": True, "channels": ["sms"], "email": {}}}
    results = notify.dispatch(settings, "t", "x")
    assert len(results) == 1 and results[0].ok is False


def test_dispatch_uses_channel_config():
    class FakeResult:
        ok = True
        detail = ""

        def as_dict(self):
            return {"channel": "x", "ok": True}

    seen: dict = {}

    def fake_send(cfg, title, text):
        seen["feishu"] = cfg
        return FakeResult()

    original = notify._SENDERS["feishu"]
    notify._SENDERS["feishu"] = fake_send
    try:
        results = notify.dispatch({"notifications": {"enabled": True, "channels": ["feishu"], "feishu": {"webhook": "https://x"}}}, "t", "x")
    finally:
        notify._SENDERS["feishu"] = original
    assert results and seen["feishu"] == {"webhook": "https://x"}

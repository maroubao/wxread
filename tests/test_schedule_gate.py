"""定时开关：`settings.schedule` 决定 read/warm 是否真的干活（cron 本身写死在 workflow）。"""

from wxread import worker


def test_read_runs_when_schedule_absent(store):
    store.save_settings({"schedule": {}})
    assert worker.schedule_gate(store, "read") == ""


def test_read_skipped_when_disabled(store):
    store.save_settings({"schedule": {"enabled": False}})
    assert "关闭" in worker.schedule_gate(store, "read")


def test_warm_skipped_when_warm_hours_zero(store):
    store.save_settings({"schedule": {"enabled": True, "warm_hours": 0}})
    assert "0" in worker.schedule_gate(store, "warm")
    assert worker.schedule_gate(store, "read") == ""


def test_cmd_read_exits_zero_when_disabled(store):
    store.save_settings({"schedule": {"enabled": False}})
    assert worker.cmd_read(store, {"WXREAD_TARGET_COUNT": ""}) == worker.EXIT_OK


def test_cmd_warm_exits_zero_when_warm_hours_zero(store):
    store.save_settings({"schedule": {"warm_hours": 0}})
    assert worker.cmd_warm(store, {}) == worker.EXIT_OK


def test_manual_read_and_warm_bypass_the_automatic_switch(store):
    store.save_settings({"schedule": {"enabled": False, "warm_hours": 0}})
    assert worker.schedule_gate(store, "read", automatic=False) == ""
    assert worker.schedule_gate(store, "warm", automatic=False) == ""


def test_unsupported_time_is_rejected_instead_of_falsely_saved(store):
    import pytest
    from wxread import control

    with pytest.raises(control.ControlError, match="cron"):
        control.handle_schedule(control.ControlContext(store), {"daily": "02:30"})
    assert "schedule" not in store.load_settings()

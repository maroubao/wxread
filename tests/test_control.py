import time

import pytest

from support import CURL_SAMPLE
from wxread import control
from wxread.crypto import b64e, new_keypair, seal
from wxread.state import StateStore


def make_store(store: StateStore) -> control.ControlContext:
    return control.ControlContext(store=store, now_iso=lambda: "2026-11-23T10:00:00Z")


def instr(action: str, payload: dict | None = None, *, sid: str = "s" * 32, at: int | None = None) -> control.Instruction:
    return control.Instruction(
        action=action,
        session_id=sid,
        issued_at=int(at if at is not None else time.time()),
        nonce="0123456789abcdef",
        payload=payload or {},
        reply_key="",
    )


def test_decode_roundtrip_and_wrong_key(store: StateStore):
    public, secret = new_keypair()
    other_public, other_secret = new_keypair()
    envelope = seal({"action": "init", "session_id": "a" * 32, "issued_at": int(time.time()), "nonce": "0123456789ab"}, public)

    decoded = control.decode(envelope, secret)
    assert decoded.action == "init"
    with pytest.raises(control.ControlError):
        control.decode(envelope, other_secret)


def test_expired_and_future_instructions_rejected(store: StateStore):
    now = 1_800_000_000
    with pytest.raises(control.ControlError, match="过期"):
        control.check_fresh(instr("init", at=now - 61), now=now)
    with pytest.raises(control.ControlError, match="超前"):
        control.check_fresh(instr("init", at=now + 61), now=now)
    control.check_fresh(instr("init", at=now - 5), now=now)  # 窗口内通过


def test_unknown_action_rejected(store: StateStore):
    with pytest.raises(control.ControlError, match="未知动作"):
        control.execute(make_store(store), instr("drop-everything"))


def test_replay_is_blocked(store: StateStore):
    ctx = make_store(store)
    first = control.execute(ctx, instr("init", sid="b" * 32))
    assert first["ok"] is True
    with pytest.raises(control.ControlError, match="重放"):
        control.execute(ctx, instr("init", sid="b" * 32))


def test_failed_action_still_writes_receipt(store: StateStore):
    ctx = make_store(store)
    receipt = control.execute(ctx, instr("delete", {"account_id": "nope"}, sid="c" * 32))
    assert receipt["ok"] is False and "不存在" in receipt["error"]
    assert store.read_response("c" * 32)["plain"]["ok"] is False


def test_init_is_idempotent_and_imports_account(store: StateStore):
    ctx = make_store(store)
    first = control.execute(ctx, instr("init", sid="d" * 32))
    assert first["ok"] and first["reused"] is False
    assert len(first["public_key"]) > 20
    second = control.execute(ctx, instr("init", sid="e" * 32))
    assert second["reused"] is True and second["public_key"] == first["public_key"]

    imported = control.execute(ctx, instr("import", {"curl": CURL_SAMPLE, "label": "主号"}, sid="f" * 32))
    assert imported["ok"] and imported["label"] == "主号"
    assert store.load_account(imported["account_id"]) is not None


def test_update_validates_status_and_target(store: StateStore):
    ctx = make_store(store)
    imported = control.execute(ctx, instr("import", {"curl": CURL_SAMPLE}, sid="g" * 32))
    account_id = imported["account_id"]

    updated = control.execute(ctx, instr("update", {"account_id": account_id, "target_count": 9999, "label": "改"}, sid="h" * 32))
    assert updated["ok"] and updated["label"] == "改"
    assert store.load_account(account_id)["target_count"] == 200

    bad = control.execute(ctx, instr("update", {"account_id": account_id, "status": "whatever"}, sid="i" * 32))
    assert bad["ok"] is False and "非法状态" in bad["error"]


def test_delete_removes_account(store: StateStore):
    ctx = make_store(store)
    imported = control.execute(ctx, instr("import", {"curl": CURL_SAMPLE}, sid="j" * 32))
    deleted = control.execute(ctx, instr("delete", {"account_id": imported["account_id"]}, sid="k" * 32))
    assert deleted["ok"] is True
    assert store.load_account(imported["account_id"]) is None


def test_schedule_and_notifications_are_sanitized(store: StateStore):
    ctx = make_store(store)
    scheduled = control.execute(ctx, instr("schedule", {"daily": "3:7", "warm_hours": 6}, sid="l" * 32))
    assert scheduled["ok"] and scheduled["schedule"]["daily"] == "03:07"

    bad = control.execute(ctx, instr("schedule", {"warm_hours": 5}, sid="m" * 32))
    assert bad["ok"] is False

    saved = control.execute(
        ctx,
        instr(
            "notifications",
            {"enabled": True, "channels": ["email"], "email": {"host": "smtp.example.com", "user": "u", "password": "p", "to": "u@example.com"}},
            sid="n" * 32,
        ),
    )
    assert saved["ok"] is True
    assert saved["notifications"]["email"] == {"configured": True}
    assert "password" not in str(saved)

    unknown = control.execute(ctx, instr("notifications", {"channels": ["sms"]}, sid="o" * 32))
    assert unknown["ok"] is False


def test_test_push_uses_injected_sender(store: StateStore):
    from wxread.notify import NotifyResult

    calls: list[str] = []

    def fake_send(channel, cfg, title, text):
        calls.append(channel)
        return NotifyResult(channel, True)

    ctx = control.ControlContext(store=store, now_iso=lambda: "2026-11-23T10:00:00Z", notify_send=fake_send)
    receipt = control.execute(ctx, instr("test-push", {"channel": "feishu"}, sid="p" * 32))
    assert receipt["ok"] is True and calls == ["feishu"]


def test_receipt_is_encrypted_when_reply_key_given(store: StateStore):
    ctx = make_store(store)
    public, secret = new_keypair()
    instruction = control.Instruction(
        action="init", session_id="q" * 32, issued_at=int(time.time()), nonce="0123456789abcdef", payload={}, reply_key=b64e(public)
    )
    control.execute(ctx, instruction)
    envelope = store.read_response("q" * 32)
    assert envelope["v"] == 1 and "plain" not in envelope
    from wxread.crypto import unseal

    assert unseal(envelope, secret)["ok"] is True

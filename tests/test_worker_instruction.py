"""`control.yml` 的 instruction 全链路：这一步就是网页按"生成二维码"走的路。

回归来源：`worker.cmd_control` 用过 `json.loads` 却没 `import json`，CI 里直接
`NameError`——init 分支不碰 json，`control.execute` 的单测也绕过了 worker，
所以两边都绿、真跑必挂。这里把密文指令从 env 一路喂到回执，堵住这个缝。
"""

from __future__ import annotations

import json
import os
import time

from wxread import crypto
from wxread.state import StateStore
from wxread.worker import cmd_control


def make_instruction(action: str, payload: dict, public_key: bytes, secret: bytes):
    """造一条和网页端同构的密文指令，返回 (env 片段, session_id, 回执私钥)。"""
    session_id = "sess" + action
    reply = crypto.new_keypair()
    instruction = {
        "action": action,
        "session_id": session_id,
        "issued_at": int(time.time()),
        "nonce": crypto.b64e(os.urandom(12)),
        "payload": payload,
        "reply_key": crypto.b64e(reply[0]),
    }
    envelope = crypto.seal(instruction, public_key)
    env = {
        "WXREAD_CONTROL": "instruction",
        "WXREAD_INSTRUCTION": json.dumps(envelope),
        "WXREAD_CONTROL_SK": crypto.b64e(secret),
        "WXREAD_SESSION": session_id,
    }
    return env, session_id, reply[1]


def seed_control_key(store: StateStore):
    public, secret = crypto.new_keypair()
    store.save_control(public_key=public, key_version=1)  # 传原始字节，save_control 内部 base64
    return public, secret


def test_instruction_path_reads_json_from_env(store: StateStore):
    public, secret = seed_control_key(store)
    env, session_id, reply_secret = make_instruction("schedule", {"daily": "02:30", "enabled": True}, public, secret)

    assert cmd_control(store, env) == 0

    file = store.backend.get(f"responses/{session_id}.json")
    assert file is not None
    receipt = crypto.unseal(json.loads(file[0].decode("utf-8")), reply_secret)
    assert receipt["ok"] is True
    assert receipt["action"] == "schedule"
    assert store.load_settings()["schedule"]["daily"] == "02:30"


def test_instruction_path_reports_failure_with_exit_code(store: StateStore):
    public, secret = seed_control_key(store)
    env, session_id, reply_secret = make_instruction("delete", {"account_id": "nope"}, public, secret)

    assert cmd_control(store, env) == 1

    file = store.backend.get(f"responses/{session_id}.json")
    receipt = crypto.unseal(json.loads(file[0].decode("utf-8")), reply_secret)
    assert receipt["ok"] is False
    assert "nope" in receipt["error"]


def test_instruction_needs_both_env_values(store: StateStore):
    import pytest

    with pytest.raises(Exception, match="instruction"):
        cmd_control(store, {"WXREAD_CONTROL": "instruction"})

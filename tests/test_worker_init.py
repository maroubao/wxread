"""`control.yml init` 的密钥交接：私钥不经过 CI，公钥由 workflow 输入带入。"""

from __future__ import annotations

import pytest

from wxread import control, crypto
from wxread.state import StateStore
from wxread.worker import ConfigError, cmd_control


def env(**overrides):
    base = {"WXREAD_CONTROL": "init"}
    base.update(overrides)
    return base


def test_init_without_public_key_is_refused(store: StateStore):
    """空仓库上想在 CI 里现生成密钥对 → 直接拒，要求本机 genkey。"""
    with pytest.raises(ConfigError, match="public_key"):
        cmd_control(store, env())


def test_init_stores_supplied_public_key(store: StateStore):
    public, secret = crypto.new_keypair()
    text = crypto.b64e(public)

    assert cmd_control(store, env(WXREAD_INIT_PUBLIC_KEY=text)) == 0

    saved = store.load_control()
    assert saved["public_key"] == text
    # 回执里不出现私钥
    assert "server_secret_key" not in saved
    # 用同一对密钥能封能解，说明存进去的公钥确实可用
    envelope = crypto.seal({"hello": 1}, crypto.b64d(saved["public_key"]))
    assert crypto.unseal(envelope, secret) == {"hello": 1}


def test_init_reuses_existing_key_without_input(store: StateStore):
    public, _ = crypto.new_keypair()
    cmd_control(store, env(WXREAD_INIT_PUBLIC_KEY=crypto.b64e(public)))

    assert cmd_control(store, env()) == 0
    assert store.load_control()["public_key"] == crypto.b64e(public)


def test_init_rejects_bad_public_key(store: StateStore):
    with pytest.raises(control.ControlError, match="base64|字节"):
        cmd_control(store, env(WXREAD_INIT_PUBLIC_KEY="not-base64!!"))


def test_rotating_to_a_new_public_key_replaces_the_old_one(store: StateStore):
    first, _ = crypto.new_keypair()
    second, _ = crypto.new_keypair()
    cmd_control(store, env(WXREAD_INIT_PUBLIC_KEY=crypto.b64e(first)))

    cmd_control(store, env(WXREAD_INIT_PUBLIC_KEY=crypto.b64e(second)))

    assert store.load_control()["public_key"] == crypto.b64e(second)

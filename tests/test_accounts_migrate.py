"""P1 账户模型与旧配置迁移测试。"""

from __future__ import annotations

import hashlib
import json

import pytest

from wxread.accounts import (
    DEFAULT_TARGET_COUNT,
    MAX_TARGET_COUNT,
    STATUS_NEEDS_LOGIN,
    STATUS_OK,
    Account,
    AccountError,
    MissingFieldsError,
    account_from_curl,
    account_from_dict,
    clamp_target_count,
    derive_account_id,
    from_legacy,
    parse_curl,
)

from support import CURL_SAMPLE, CURL_SECOND


def test_parse_curl_extracts_three_parts():
    headers, cookies, payload = parse_curl(CURL_SAMPLE)
    assert cookies["wr_vid"] == "1234567890"
    assert cookies["wr_skey"] == "abcdefghij"
    assert "cookie" not in {k.lower() for k in headers}
    assert headers["user-agent"].startswith("Mozilla/5.0")
    assert payload["appId"] == "wb182564874603h266381671"
    assert payload["ps"] and payload["pc"]


def test_account_id_is_hash_not_phone():
    cookies = {"wr_vid": "1234567890"}
    expected = hashlib.sha256(b"1234567890").hexdigest()[:12]
    assert derive_account_id(cookies) == expected
    assert "1234567890" not in derive_account_id(cookies)


def test_account_from_curl_applies_defaults():
    account = account_from_curl(CURL_SAMPLE, label="主账户")
    assert account.target_count == DEFAULT_TARGET_COUNT
    assert account.status == STATUS_OK
    assert account.enabled is True
    assert set(account.read_payload) <= {"appId", "ps", "pc", "ci", "co", "sm", "pr", "b", "c"}
    assert "b" not in account.read_payload or len(account.read_payload["b"]) > 0
    assert account.renewed_at and account.last_ok_at is None


def test_missing_device_fields_reported():
    broken = CURL_SAMPLE.replace('"ps":"4ee326507a65a465g015fae"', '"ps":""')
    with pytest.raises(MissingFieldsError) as excinfo:
        account_from_curl(broken)
    assert excinfo.value.fields == ["ps"]


def test_no_curl_rejected():
    with pytest.raises(AccountError):
        parse_curl("https://weread.qq.com/web/book/read")


def test_target_count_clamped():
    assert clamp_target_count(0) == 1
    assert clamp_target_count(10_000) == MAX_TARGET_COUNT
    assert clamp_target_count("60") == 60
    with pytest.raises(AccountError):
        clamp_target_count("many")


def test_legacy_single_curl():
    accounts = from_legacy(curl_bash=CURL_SAMPLE)
    assert len(accounts) == 1
    assert accounts[0].label == "主账户"


def test_legacy_accounts_json_merges_same_identity():
    payload = json.dumps([{"curl": CURL_SAMPLE, "label": "甲"}, {"curl": CURL_SAMPLE, "label": "乙"}])
    accounts = from_legacy(accounts_json=payload)
    assert len(accounts) == 1


def test_legacy_two_identities():
    payload = json.dumps([{"curl": CURL_SAMPLE}, {"curl": CURL_SECOND}])
    accounts = from_legacy(accounts_json=payload)
    assert len(accounts) == 2
    ids = {account.id for account in accounts}
    assert len(ids) == 2


def test_legacy_bad_json_rejected():
    with pytest.raises(AccountError):
        from_legacy(accounts_json="{not json")


def test_roundtrip_keeps_fields():
    account = account_from_curl(CURL_SAMPLE, label="主账户")
    restored = Account.from_dict(json.loads(json.dumps(account.to_dict())))
    assert restored.id == account.id
    assert restored.cookies == account.cookies
    assert restored.read_payload == account.read_payload
    assert restored.target_count == account.target_count


def test_unknown_status_falls_back():
    payload = {"id": "x", "cookies": {"wr_vid": "1"}, "read_payload": {"appId": "a", "ps": "b", "pc": "c"}, "status": "weird"}
    account = Account.from_dict(payload)
    assert account.status == STATUS_NEEDS_LOGIN


def test_mark_renewed_writes_key_and_time_together():
    account = account_from_curl(CURL_SAMPLE)
    account.mark_renewed("newkey1234", "2026-11-23T01:07:00Z")
    assert account.cookies["wr_skey"] == "newkey1234"
    assert account.renewed_at == "2026-11-23T01:07:00Z"


def test_mark_needs_login_and_back():
    account = account_from_curl(CURL_SAMPLE)
    account.mark_needs_login("cookie 失效")
    assert account.status == STATUS_NEEDS_LOGIN
    account.mark_ok("2026-11-23T02:00:00Z")
    assert account.status == STATUS_OK and account.last_ok_at == "2026-11-23T02:00:00Z"


def test_merge_keeps_label_and_updates_credentials():
    base = account_from_curl(CURL_SAMPLE, label="旧名")
    fresh = account_from_curl(CURL_SAMPLE, label="新名")
    fresh.cookies["wr_skey"] = "updated123"
    base.merge(fresh)
    assert base.label == "旧名"
    assert base.cookies["wr_skey"] == "updated123"


def test_label_truncated():
    account = account_from_curl(CURL_SAMPLE, label="长" * 50)
    assert len(account.label) == 24


def test_account_from_dict_requires_device_fields():
    with pytest.raises(MissingFieldsError):
        account_from_dict({"id": "abc", "cookies": {"wr_vid": "1"}, "read_payload": {"appId": "a"}})

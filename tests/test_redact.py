"""P1 脱敏测试：任何凭据进入日志前必须只剩前 2 位。"""

from __future__ import annotations

from wxread.logsafe import cookies_brief, mask, scrub, scrub_mapping

from support import CURL_SAMPLE


def test_mask():
    assert mask("abcdefghij") == "ab***"
    assert mask("ab") == "***"
    assert mask("") == ""
    assert mask(None) == "None"[:2] + "***"


def test_scrub_cookie_pairs():
    cleaned = scrub("cookies: wr_vid=1234567890; wr_skey=abcdefghij")
    assert "1234567890" not in cleaned and "abcdefghij" not in cleaned
    assert "wr_vid=12***" in cleaned and "wr_skey=ab***" in cleaned


def test_scrub_whole_curl():
    cleaned = scrub(CURL_SAMPLE)
    for secret in ("1234567890", "abcdefghij"):
        assert secret not in cleaned


def test_scrub_github_token_and_bearer():
    cleaned = scrub("Authorization: Bearer ghp_abcdefghijklmnop  github_pat_11ABCDEFG")
    assert "ghp_abcdefghijklmnop" not in cleaned
    assert "11ABCDEFG" not in cleaned
    assert "abcdefghijklmnop" not in cleaned


def test_scrub_mapping_does_not_mutate_input():
    original = {"wr_skey": "abcdefghij", "nested": {"token": "supersecret"}, "level": "L0"}
    cleaned = scrub_mapping(original)
    assert cleaned["wr_skey"] == "ab***"
    assert cleaned["nested"]["token"] == "su***"
    assert cleaned["level"] == "L0"
    assert original["wr_skey"] == "abcdefghij"


def test_cookies_brief():
    assert cookies_brief({"wr_skey": "abcdefghij", "wr_vid": "1234567890"}) == "wr_skey=ab***, wr_vid=12***"

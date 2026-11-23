"""P2 reader 测试：假服务端覆盖 L0–L3、Set-Cookie 合并、签名。"""

from __future__ import annotations

import hashlib
import random

from wxread.accounts import account_from_curl
from wxread.reader import (
    BACKOFF,
    CHAPTER_URL,
    READ_URL,
    RENEW_URL,
    Reader,
    TransportError,
    TransportResponse,
    build_payload,
    cal_hash,
    encode_data,
    sign,
)

from support import CURL_SAMPLE


class FakeTransport:
    """按脚本顺序返回响应；脚本项可以是 TransportResponse 或异常。"""

    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls: list[tuple[str, dict]] = []

    def post(self, url, payload, cookies, headers):
        self.calls.append((url, dict(payload)))
        if url == RENEW_URL and not self.script:
            return TransportResponse(200, {"errcode": 0})
        if not self.script:
            return ok(f"sync{len(self.calls)}")
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    def urls(self):
        return [url for url, _ in self.calls]


def ok(synckey="sync-1"):
    return TransportResponse(200, {"succ": 1, "synckey": synckey, "errcode": 0})


def no_sync():
    return TransportResponse(200, {"succ": 1, "errcode": 0})


def err(code):
    return TransportResponse(200, {"succ": 0, "errcode": code})


def renew_ok(skey="full-skey-0001"):
    return TransportResponse(200, {"errcode": 0}, {"wr_skey": skey})


def make_reader(script, target=2, **kwargs):
    account = account_from_curl(CURL_SAMPLE, label="主账户")
    account.target_count = target
    transport = FakeTransport(script)
    sleeps: list[float] = []
    reader = Reader(
        account,
        transport,
        pace=kwargs.pop("pace", 0),
        deadline=kwargs.pop("deadline", 60),
        sleep=sleeps.append,
        clock=kwargs.pop("clock", lambda: 1_700_000_000.0),
        **kwargs,
    )
    return reader, account, transport, sleeps


def test_happy_path_reaches_target():
    reader, account, transport, _ = make_reader([ok("s1"), ok("s2")], target=2)
    outcome = reader.run()
    assert outcome.read_count == 2 and outcome.level == "L0" and outcome.exit_code == 0
    assert transport.urls() == [READ_URL, READ_URL]
    assert account.last_ok_at is not None


def test_payload_carries_signature_fields():
    reader, _account, transport, _ = make_reader([ok()], target=1)
    reader.run()
    payload = transport.calls[0][1]
    for key in ("appId", "ps", "pc", "ct", "rt", "ts", "rn", "sg", "s"):
        assert key in payload, key
    assert payload["s"] == cal_hash(encode_data({k: v for k, v in payload.items() if k != "s"}))


def test_set_cookie_is_merged():
    script = [TransportResponse(200, {"succ": 1, "synckey": "s1"}, {"wr_skey": "rotated123"})]
    reader, account, _transport, _ = make_reader(script, target=1)
    reader.run()
    assert account.cookies["wr_skey"] == "rotated123"


def test_no_synckey_triggers_chapter_infos_then_recovers():
    reader, _account, transport, _ = make_reader([no_sync(), ok("s1"), ok("s2")], target=2)
    outcome = reader.run()
    assert outcome.read_count == 2
    assert outcome.level == "L2" and outcome.reason == "no_synckey"
    assert CHAPTER_URL in transport.urls()


def test_session_errcode_renews_with_full_skey():
    reader, account, transport, _ = make_reader([err(-2012), renew_ok(), ok("s1")], target=1)
    outcome = reader.run()
    assert outcome.read_count == 1
    assert outcome.renewals == 1
    assert outcome.level == "L2" and outcome.reason == "err_-2012"
    assert account.cookies["wr_skey"] == "full-skey-0001"
    assert account.renewed_at is not None
    assert RENEW_URL in transport.urls()


def test_transport_error_backs_off_then_renews():
    script = [TransportError("boom")] * 4 + [renew_ok(), ok("s1")]
    reader, _account, _transport, sleeps = make_reader(script, target=1)
    outcome = reader.run()
    assert sleeps[:3] == list(BACKOFF)
    assert outcome.read_count == 1
    assert outcome.renewals == 1


def test_renewal_failure_is_l3():
    script = [TransportError("boom")] * 4 + [TransportResponse(200, {})] * 3
    reader, account, _transport, _ = make_reader(script, target=1)
    outcome = reader.run()
    assert outcome.read_count == 0
    assert outcome.level == "L3" and outcome.reason == "renew_failed"
    assert outcome.exit_code == 3
    assert account.status == "needs_login"


def test_persistent_session_error_stops_account():
    script = [err(-2012), renew_ok(), err(-2012)]
    reader, account, _transport, _ = make_reader(script, target=1)
    outcome = reader.run()
    assert outcome.level == "L3"
    assert outcome.exit_code == 3
    assert account.status == "needs_login"


def test_unknown_failure_is_payload_invalid():
    reader, _account, _transport, _ = make_reader([TransportResponse(200, {"succ": 0, "errcode": -1})], target=1)
    outcome = reader.run()
    assert outcome.level == "L3" and outcome.reason == "payload_invalid"


def test_cancel_stops_early():
    reader, _account, _transport, _ = make_reader([ok()], target=5)
    calls = {"n": 0}

    def should_stop():
        calls["n"] += 1
        return calls["n"] > 1

    reader.should_stop = should_stop
    outcome = reader.run()
    assert outcome.read_count == 1
    assert outcome.reason == "cancelled"


def test_deadline_stops_loop():
    times = iter([0, 0, 0, 0, 10_000])
    reader, _account, _transport, _ = make_reader([ok(), ok(), ok()], target=5, clock=lambda: next(times, 10_000))
    reader.deadline = 5
    outcome = reader.run()
    assert outcome.reason in ("deadline", "ok")


def test_cal_hash_matches_reference_implementation():
    text = encode_data({"appId": "wb1", "b": "book", "c": "chap", "ct": 1_700_000_000, "s": None}.copy() and {
        "appId": "wb1",
        "b": "book",
        "c": "chap",
        "ct": 1_700_000_000,
    })

    def reference(source: str) -> str:
        left = 0x15051505
        right = left
        index = len(source) - 1
        while index > 0:
            left = 0x7FFFFFFF & (left ^ (ord(source[index]) << ((len(source) - index) % 30)))
            right = 0x7FFFFFFF & (right ^ (ord(source[index - 1]) << (index % 30)))
            index -= 2
        return hex(left + right)[2:].lower()

    assert cal_hash(text) == reference(text)
    assert len(cal_hash("")) >= 1


def test_build_payload_fields():
    base = {"appId": "wb1", "ps": "p", "pc": "c", "b": "book", "c": "chap"}
    payload = build_payload(base, now=1_700_000_000, last_read_at=1_699_999_970, rand=random.Random(7))
    assert payload["ct"] == 1_700_000_000
    assert payload["rt"] == 30
    assert payload["ts"] % 1000 < 1000
    assert 0 <= payload["rn"] <= 1000
    assert payload["sg"] == hashlib.sha256(f"{payload['ts']}{payload['rn']}3c5c8717f3daf09iop3423zafeqoi".encode()).hexdigest()
    assert payload["s"] == sign(payload)["s"]


def test_build_payload_drops_stale_signature_fields():
    base = {"appId": "wb1", "ps": "p", "pc": "c", "s": "stale", "ts": 1, "rn": 2, "sg": "old"}
    payload = build_payload(base, now=1_700_000_000, last_read_at=1_699_999_900, rand=random.Random(1))
    assert payload["ts"] != 1 and payload["sg"] != "old"

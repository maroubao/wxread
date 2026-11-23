"""单账户阅读客户端：签名、renewal、read、失败分级（ARCHITECTURE §4.4 / §5）。

本模块不读环境变量、不碰状态分支：所有依赖由调用方注入，便于用假服务端测试。
"""

from __future__ import annotations

import hashlib
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import quote
from uuid import uuid4

import requests

from .accounts import Account
from .logsafe import scrub

log = logging.getLogger("wxread.reader")

READ_URL = "https://weread.qq.com/web/book/read"
RENEW_URL = "https://weread.qq.com/web/login/renewal"
CHAPTER_URL = "https://weread.qq.com/web/book/chapterInfos"
SIGN_KEY = "3c5c8717f3daf09iop3423zafeqoi"

DEFAULT_TIMEOUT = 10.0
DEFAULT_PACE = 30.0
DEFAULT_DEADLINE = 20 * 60
MAX_BODY_BYTES = 64 * 1024
BACKOFF = (5, 15, 45)

LEVELS = ("L0", "L1", "L2", "L3")
SESSION_ERRCODES = (-2012, -2013)
COOKIE_VARIANTS = (
    {"rq": "%2Fweb%2Fbook%2Fread", "ql": False},
    {"rq": "%2Fweb%2Fbook%2Fread", "ql": True},
    {"rq": "%2Fweb%2Fbook%2Fread"},
)


class ReadError(RuntimeError):
    """阅读流程错误基类。"""


class TransportError(ReadError):
    """网络 / HTTP / 响应体异常，可重试（L1）。"""


class SessionInvalid(ReadError):
    """服务端明确表示会话失效（L2 → renewal）。"""


class PayloadInvalid(ReadError):
    """请求体参数失效（L3，需要重新抓取）。"""


class RenewalFailed(ReadError):
    """renewal 拿不到 wr_skey（L3，需要重新扫码）。"""


# ---------------------------------------------------------------- 签名


def encode_data(payload: Mapping[str, Any]) -> str:
    return "&".join(f"{key}={quote(str(payload[key]), safe='')}" for key in sorted(payload))


def cal_hash(text: str) -> str:
    left = 0x15051505
    right = left
    index = len(text) - 1
    while index > 0:
        left = 0x7FFFFFFF & (left ^ (ord(text[index]) << ((len(text) - index) % 30)))
        right = 0x7FFFFFFF & (right ^ (ord(text[index - 1]) << (index % 30)))
        index -= 2
    return hex(left + right)[2:].lower()


def sign(payload: Mapping[str, Any]) -> dict:
    body = {key: value for key, value in payload.items() if key != "s"}
    body["s"] = cal_hash(encode_data(body))
    return body


def build_payload(
    base: Mapping[str, Any],
    *,
    now: int | None = None,
    last_read_at: int | None = None,
    rand: random.Random | None = None,
) -> dict:
    """在账户基础参数上补 ct/rt/ts/rn/sg/s（与旧 `main.build_read_payload` 等价）。"""
    rand = rand or random.Random()
    moment = int(now if now is not None else time.time())
    previous = int(last_read_at if last_read_at is not None else moment - int(DEFAULT_PACE))
    body = {key: value for key, value in base.items() if key not in ("s", "ct", "rt", "ts", "rn", "sg")}
    body["ct"] = moment
    body["rt"] = max(0, moment - previous)
    body["ts"] = moment * 1000 + rand.randint(0, 1000)
    body["rn"] = rand.randint(0, 1000)
    body["sg"] = hashlib.sha256(f"{body['ts']}{body['rn']}{SIGN_KEY}".encode("utf-8")).hexdigest()
    return sign(body)


# ---------------------------------------------------------------- 传输层


@dataclass
class TransportResponse:
    status_code: int
    payload: dict[str, Any]
    set_cookies: dict[str, str] = field(default_factory=dict)
    error: str = ""


class Transport(Protocol):
    def post(
        self,
        url: str,
        payload: Mapping[str, Any],
        cookies: Mapping[str, str],
        headers: Mapping[str, str],
    ) -> TransportResponse: ...


class RequestsTransport:
    """真实网络实现：每次响应都回报 Set-Cookie（旧实现把它们丢掉了）。"""

    def __init__(self, session: requests.Session | None = None, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.session = session or requests.Session()
        self.timeout = timeout

    def post(
        self,
        url: str,
        payload: Mapping[str, Any],
        cookies: Mapping[str, str],
        headers: Mapping[str, str],
    ) -> TransportResponse:
        jar = getattr(self.session, "cookies", None)
        if jar is not None:
            jar.clear()  # 请求凭据以账户字典为准，避免Session的旧域Cookie重复发送
        try:
            resp = self.session.post(
                url, json=payload, headers=dict(headers), cookies=dict(cookies), timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise TransportError(f"请求失败：{type(exc).__name__}") from exc
        set_cookies = {k: v for k, v in resp.cookies.items()}
        if resp.status_code != 200:
            return TransportResponse(resp.status_code, {}, set_cookies, f"HTTP {resp.status_code}")
        if len(resp.content) > MAX_BODY_BYTES:
            return TransportResponse(200, {}, set_cookies, "响应体过大")
        try:
            data = resp.json()
        except ValueError:
            return TransportResponse(200, {}, set_cookies, "响应不是合法 JSON")
        if not isinstance(data, dict):
            return TransportResponse(200, {}, set_cookies, "响应不是 JSON 对象")
        return TransportResponse(200, data, set_cookies)


# ---------------------------------------------------------------- 结果


@dataclass
class Outcome:
    level: str = "L0"
    reason: str = "ok"
    read_count: int = 0
    target_count: int = 0
    attempts: int = 0
    renewals: int = 0
    exit_code: int = 0
    notes: list[str] = field(default_factory=list)

    def raise_level(self, level: str, reason: str) -> None:
        if LEVELS.index(level) > LEVELS.index(self.level):
            self.level = level
            self.reason = reason
        elif level == self.level and self.reason == "ok":
            self.reason = reason

    def as_run(self, account_id: str, started_at: str, finished_at: str) -> dict:
        return {
            "run_id": uuid4().hex,
            "account_id": account_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "read_count": self.read_count,
            "target_count": self.target_count,
            "level": self.level,
            "reason": self.reason,
            "exit_code": self.exit_code,
        }


# ---------------------------------------------------------------- 客户端


class Reader:
    def __init__(
        self,
        account: Account,
        transport: Transport | None = None,
        *,
        pace: float = DEFAULT_PACE,
        deadline: float = DEFAULT_DEADLINE,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        rand: random.Random | None = None,
        on_progress: Callable[[dict], None] | None = None,
        on_cookies: Callable[[Account], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        self.account = account
        self.transport = transport or RequestsTransport()
        self.pace = max(0.0, float(pace))
        self.deadline = float(deadline)
        self.sleep = sleep
        self.clock = clock
        self.rand = rand or random.Random()
        self.on_progress = on_progress
        self.on_cookies = on_cookies
        self.should_stop = should_stop or (lambda: False)
        self.last_read_at = int(clock())

    # -- 单步

    def _post(self, url: str, payload: Mapping[str, Any]) -> TransportResponse:
        response = self.transport.post(url, payload, self.account.cookies, self.account.headers)
        if response.set_cookies:
            self.account.cookies.update(response.set_cookies)  # 关键：Set-Cookie 必须合并
            if response.set_cookies.get("wr_skey"):
                self.account.mark_renewed(response.set_cookies["wr_skey"])
            if self.on_cookies:
                self.on_cookies(self.account)
        if response.error or response.status_code != 200:
            raise TransportError(response.error or f"HTTP {response.status_code}")
        return response

    def renew(self) -> str:
        """renewal 拿完整 wr_skey（旧实现只取前 8 位）。"""
        network_failures = 0
        invalid_session = False
        for variant in COOKIE_VARIANTS:
            try:
                response = self._post(RENEW_URL, dict(variant))
            except TransportError as exc:
                network_failures += 1
                log.warning("renewal 变体失败：%s", scrub(str(exc)))
                continue
            if response.payload.get("errcode", response.payload.get("errCode")) in SESSION_ERRCODES:
                invalid_session = True
                continue
            skey = (response.set_cookies or {}).get("wr_skey") or response.payload.get("wr_skey")
            if skey and str(skey).strip():
                self.account.cookies["wr_skey"] = str(skey)
                return str(skey)
            if response.payload.get("succ") == 1 and self.account.cookies.get("wr_skey"):
                return self.account.cookies["wr_skey"]
        if invalid_session:
            raise SessionInvalid("renewal 确认登录态失效")
        if network_failures == len(COOKIE_VARIANTS):
            raise TransportError("renewal 网络请求均失败")
        raise RenewalFailed("renewal 未返回 wr_skey")

    def fix_synckey(self, book_id: str) -> None:
        try:
            self._post(CHAPTER_URL, {"bookIds": [book_id]})
        except TransportError as exc:
            log.warning("chapterInfos 失败：%s", scrub(str(exc)))

    def read_once(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return dict(self._post(READ_URL, payload).payload)

    # -- 主循环

    def run(self) -> Outcome:
        account = self.account
        try:
            account.validate()
        except ValueError as exc:
            return Outcome("L3", "payload_invalid", 0, account.target_count, exit_code=3, notes=[str(exc)])

        outcome = Outcome(target_count=account.target_count)
        base = dict(account.read_payload)
        book_id = str(base.get("b") or "")
        started = self.clock()
        l1_streak = 0
        renewed_for: set[str] = set()
        fixed_book = False

        if self.pace and not self.should_stop():
            self.sleep(self.pace)

        while outcome.read_count < account.target_count:
            if self.should_stop():
                outcome.raise_level("L2" if outcome.read_count else "L1", "cancelled")
                break
            if self.clock() - started > self.deadline:
                outcome.raise_level("L2", "deadline")
                break

            outcome.attempts += 1
            payload = build_payload(base, now=int(self.clock()), last_read_at=self.last_read_at, rand=self.rand)
            try:
                data = self.read_once(payload)
            except TransportError as exc:
                l1_streak += 1
                log.info("第 %s 次读取失败：%s", outcome.attempts, scrub(str(exc)))
                outcome.raise_level("L1", "net")
                if l1_streak <= len(BACKOFF):
                    self.sleep(BACKOFF[l1_streak - 1])
                    continue
                if "transport" in renewed_for:
                    outcome.raise_level("L2", "net")
                    break
                try:
                    account.mark_renewed(self.renew())
                except SessionInvalid:
                    outcome.raise_level("L3", "renew_failed")
                    break
                except (RenewalFailed, TransportError):
                    outcome.raise_level("L2", "renew_unavailable")
                    break
                outcome.renewals += 1
                renewed_for.add("transport")
                l1_streak = 0
                outcome.raise_level("L2", "net")
                continue

            errcode = data.get("errcode", data.get("errCode"))
            if errcode in SESSION_ERRCODES:
                if "session" in renewed_for:
                    outcome.raise_level("L3", "renew_failed")
                    break
                try:
                    account.mark_renewed(self.renew())
                except ReadError:
                    outcome.raise_level("L3", "renew_failed")
                    break
                outcome.renewals += 1
                renewed_for.add("session")
                outcome.raise_level("L2", f"err_{errcode}")
                continue

            synckey = data.get("synckey")
            if data.get("succ") == 1 and synckey:
                outcome.read_count += 1
                l1_streak = 0
                renewed_for.clear()
                self.last_read_at = int(self.clock())
                account.mark_ok()
                if self.on_progress:
                    self.on_progress(
                        {
                            "account_id": account.id,
                            "read_count": outcome.read_count,
                            "target_count": account.target_count,
                        }
                    )
                if outcome.read_count < account.target_count:
                    self.sleep(self.pace)
                continue

            if data.get("succ") == 1 and not synckey:
                outcome.raise_level("L2", "no_synckey")
                if not fixed_book and book_id:
                    self.fix_synckey(book_id)
                    fixed_book = True
                    continue
                if "synckey" not in renewed_for:
                    try:
                        account.mark_renewed(self.renew())
                    except TransportError:
                        break
                    except ReadError:
                        outcome.raise_level("L3", "renew_failed")
                        break
                    outcome.renewals += 1
                    renewed_for.add("synckey")
                    continue
                outcome.raise_level("L3", "no_synckey")
                break

            outcome.raise_level("L2", "payload_invalid")
            if not fixed_book and book_id:
                self.fix_synckey(book_id)
                fixed_book = True
                continue
            if "payload" not in renewed_for:
                try:
                    account.mark_renewed(self.renew())
                except TransportError:
                    break
                except ReadError:
                    outcome.raise_level("L3", "payload_invalid")
                    break
                outcome.renewals += 1
                renewed_for.add("payload")
                continue
            outcome.raise_level("L3", "payload_invalid")
            break

        outcome.read_count = min(outcome.read_count, outcome.target_count)
        needs_login = outcome.level == "L3" and outcome.reason in ("renew_failed", "no_synckey", "payload_invalid")
        if outcome.read_count == 0:
            outcome.exit_code = 3
            outcome.level = "L3" if outcome.level == "L0" else outcome.level
        elif outcome.level == "L3" or outcome.read_count < outcome.target_count:
            outcome.exit_code = 2
        else:
            outcome.exit_code = 0
        if needs_login:
            account.mark_needs_login(outcome.reason)
        return outcome

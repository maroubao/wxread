"""账户模型：字段、默认值、校验，以及从旧配置（curl / accounts JSON）迁移。

字段与默认值以 ARCHITECTURE §3.2 为准；任何新增字段都必须同时改这里和文档。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

SCHEMA_VERSION = 1
DEFAULT_TARGET_COUNT = 40
MIN_TARGET_COUNT = 1
MAX_TARGET_COUNT = 200
STATUS_OK = "ok"
STATUS_NEEDS_LOGIN = "needs_login"
STATUS_STOPPED = "stopped"
STATUSES = (STATUS_OK, STATUS_NEEDS_LOGIN, STATUS_STOPPED)
LABEL_MAX_LEN = 24
REQUIRED_PAYLOAD_FIELDS = ("appId", "ps", "pc")
PROMOTED_PAYLOAD_FIELDS = ("appId", "ps", "pc", "ci", "co", "sm", "pr", "b", "c")
NUMERIC_PAYLOAD_FIELDS = ("ci", "co", "pr")

_COOKIE_HEADER = re.compile(r"-H\s+'([Cc]ookie):\s*([^']+)'")
_COOKIE_FLAG = re.compile(r"(?:-b|--cookie)\s+'([^']+)'")
_HEADER_PAIR = re.compile(r"-H\s+'([^:']+):\s*([^']*)'")
_DATA = (
    re.compile(r"--data-raw\s+'(.+?)'(?=\s|$)", re.S),
    re.compile(r"--data-binary\s+'(.+?)'(?=\s|$)", re.S),
    re.compile(r"--data\s+'(.+?)'(?=\s|$)", re.S),
)


class AccountError(ValueError):
    """账户数据不合法。"""


class MissingFieldsError(AccountError):
    def __init__(self, fields: Iterable[str]) -> None:
        self.fields = sorted(set(fields))
        super().__init__("缺少字段：" + ", ".join(self.fields))


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _clean_label(label: str | None) -> str:
    text = str(label or "").strip()
    return text[:LABEL_MAX_LEN]


def derive_account_id(cookies: Mapping[str, str]) -> str:
    """账户主键 = sha256(wr_vid 或 wr_skey)[:12]，与手机号/昵称无关。"""
    for key in ("wr_vid", "wr_skey"):
        value = str(cookies.get(key) or "").strip()
        if value:
            return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    raise MissingFieldsError(["wr_vid", "wr_skey"])


def clamp_target_count(value: Any) -> int:
    if value is None or value == "":
        return DEFAULT_TARGET_COUNT
    try:
        count = int(value)
    except (TypeError, ValueError) as exc:
        raise AccountError(f"target_count 不是整数：{value!r}") from exc
    return max(MIN_TARGET_COUNT, min(MAX_TARGET_COUNT, count))


def normalize_read_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """保留请求字段类型，兼容旧扫码记录中被转为字符串的整数。"""
    result = dict(payload)
    for key in NUMERIC_PAYLOAD_FIELDS:
        value = result.get(key)
        if isinstance(value, str) and value.lstrip("-").isdigit():
            result[key] = int(value)
    return result


@dataclass
class Account:
    id: str
    label: str
    cookies: dict[str, str]
    headers: dict[str, str]
    read_payload: dict[str, Any]
    target_count: int = DEFAULT_TARGET_COUNT
    enabled: bool = True
    status: str = STATUS_OK
    renewed_at: str | None = None
    last_ok_at: str | None = None
    note: str = ""
    schema_version: int = SCHEMA_VERSION
    extra: dict[str, Any] = field(default_factory=dict)

    # -- 生命周期

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any], account_id: str | None = None) -> "Account":
        if not isinstance(payload, Mapping):
            raise AccountError("账户数据必须是对象")
        cookies = dict(payload.get("cookies") or {})
        headers = dict(payload.get("headers") or {})
        read_payload = normalize_read_payload(payload.get("read_payload") or {})
        resolved_id = str(payload.get("id") or account_id or "")
        if not resolved_id:
            resolved_id = derive_account_id(cookies)
        status = str(payload.get("status") or STATUS_OK)
        if status not in STATUSES:
            status = STATUS_NEEDS_LOGIN if status else STATUS_OK
        known = {
            "id",
            "label",
            "cookies",
            "headers",
            "read_payload",
            "target_count",
            "enabled",
            "status",
            "renewed_at",
            "last_ok_at",
            "note",
            "schema_version",
        }
        return cls(
            id=resolved_id,
            label=_clean_label(payload.get("label")) or f"账户-{resolved_id[:4]}",
            cookies=cookies,
            headers=headers,
            read_payload=read_payload,
            target_count=clamp_target_count(payload.get("target_count", DEFAULT_TARGET_COUNT)),
            enabled=bool(payload.get("enabled", True)),
            status=status,
            renewed_at=payload.get("renewed_at"),
            last_ok_at=payload.get("last_ok_at"),
            note=str(payload.get("note") or "")[:200],
            schema_version=int(payload.get("schema_version") or SCHEMA_VERSION),
            extra={k: v for k, v in payload.items() if k not in known},
        )

    def to_dict(self) -> dict[str, Any]:
        data = dict(self.extra)
        data.update(
            {
                "schema_version": self.schema_version,
                "id": self.id,
                "label": self.label,
                "enabled": self.enabled,
                "target_count": self.target_count,
                "cookies": dict(self.cookies),
                "headers": dict(self.headers),
                "read_payload": dict(self.read_payload),
                "renewed_at": self.renewed_at,
                "last_ok_at": self.last_ok_at,
                "status": self.status,
                "note": self.note,
            }
        )
        return data

    def validate(self) -> None:
        if not self.cookies.get("wr_vid") and not self.cookies.get("wr_skey"):
            raise MissingFieldsError(["wr_vid", "wr_skey"])
        missing = [key for key in REQUIRED_PAYLOAD_FIELDS if not self.read_payload.get(key)]
        if missing:
            raise MissingFieldsError(missing)
        if self.status not in STATUSES:
            raise AccountError(f"未知状态：{self.status}")

    # -- 状态迁移（ARCHITECTURE §3.2 不变量）

    def mark_renewed(self, skey: str, when: str | None = None) -> None:
        self.cookies["wr_skey"] = skey
        self.renewed_at = when or utcnow_iso()

    def mark_ok(self, when: str | None = None) -> None:
        self.last_ok_at = when or utcnow_iso()
        self.status = STATUS_OK
        self.note = ""
        self.extra.update({"warm_failures": 0, "warm_notified": False})

    def mark_needs_login(self, note: str = "") -> None:
        self.status = STATUS_NEEDS_LOGIN
        if note:
            self.note = note[:200]

    def merge(self, other: "Account", *, keep_label: bool = True) -> None:
        """同 id 重复绑定：更新凭据与设备字段，保留人工设置。"""
        self.cookies.update(other.cookies)
        self.headers.update(other.headers)
        self.read_payload.update(other.read_payload)
        if not keep_label and other.label:
            self.label = other.label
        if other.status == STATUS_OK:
            self.status = STATUS_OK
            self.extra.update({"warm_failures": 0, "warm_notified": False})
        self.renewed_at = other.renewed_at or self.renewed_at


# ---------------------------------------------------------------- curl 解析


def parse_curl(curl_command: str) -> tuple[dict[str, str], dict[str, str], dict[str, Any]]:
    """从浏览器复制的 curl 里取 headers / cookies / data。规则与旧 `config.convert` 一致。"""
    if not curl_command or "curl" not in curl_command:
        raise AccountError("不是有效 curl 命令")

    headers: dict[str, str] = {}
    for name, value in _HEADER_PAIR.findall(curl_command):
        headers[name.strip()] = value.strip()

    cookie_text = ""
    header_cookie = next((v for k, v in headers.items() if k.lower() == "cookie"), "")
    flag = _COOKIE_FLAG.search(curl_command)
    if flag:
        cookie_text = flag.group(1)
    elif header_cookie:
        cookie_text = header_cookie

    cookies: dict[str, str] = {}
    for chunk in cookie_text.split(";"):
        if "=" in chunk:
            name, _, value = chunk.strip().partition("=")
            if name:
                cookies[name.strip()] = value.strip()

    headers = {k: v for k, v in headers.items() if k.lower() != "cookie"}

    payload: dict[str, Any] = {}
    for pattern in _DATA:
        match = pattern.search(curl_command)
        if not match:
            continue
        raw = match.group(1).strip()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            payload = parsed
            break

    return headers, cookies, payload


def account_from_curl(
    curl_command: str,
    label: str | None = None,
    target_count: int = DEFAULT_TARGET_COUNT,
    now: str | None = None,
) -> Account:
    headers, cookies, payload = parse_curl(curl_command)
    if not cookies:
        raise MissingFieldsError(["cookie"])
    read_payload = {key: payload.get(key) for key in PROMOTED_PAYLOAD_FIELDS if payload.get(key) is not None}
    account = Account(
        id=derive_account_id(cookies),
        label=_clean_label(label) or "",
        cookies=cookies,
        headers=headers,
        read_payload=read_payload,
        target_count=clamp_target_count(target_count),
        renewed_at=now or utcnow_iso(),
        last_ok_at=None,
    )
    account.validate()
    return account


def account_from_dict(payload: Mapping[str, Any], account_id: str | None = None) -> Account:
    account = Account.from_dict(payload, account_id)
    account.validate()
    return account


# ---------------------------------------------------------------- 迁移


def from_legacy(curl_bash: str | None = None, accounts_json: str | None = None) -> list[Account]:
    """读取旧配置：单个 `WXREAD_CURL_BASH` 或 `WXREAD_ACCOUNTS_JSON`（数组）。"""
    accounts: list[Account] = []
    if curl_bash:
        accounts.append(account_from_curl(curl_bash, label="主账户"))
    if accounts_json:
        try:
            raw = json.loads(accounts_json)
        except json.JSONDecodeError as exc:
            raise AccountError("WXREAD_ACCOUNTS_JSON 不是合法 JSON") from exc
        if not isinstance(raw, list):
            raise AccountError("WXREAD_ACCOUNTS_JSON 必须是数组")
        for index, item in enumerate(raw, start=1):
            if not isinstance(item, Mapping):
                raise AccountError(f"第 {index} 个账户不是对象")
            if item.get("curl"):
                account = account_from_curl(str(item["curl"]), label=item.get("label") or f"账户{index}")
            else:
                account = account_from_dict(item)
            accounts.append(account)
    merged: dict[str, Account] = {}
    for account in accounts:
        if account.id in merged:
            merged[account.id].merge(account)
        else:
            merged[account.id] = account
    return list(merged.values())

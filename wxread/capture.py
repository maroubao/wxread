"""从浏览器自己发出的请求里取阅读参数——省掉手工复制 cURL。

扫码登录用的那个 Chromium 已经是登录态，让站点自己打开一本书，正文渲染时它会
POST `web/book/read`。那次请求的 JSON body 就是账户需要的 `read_payload` 基础字段
（`appId / ps / pc / b / c / ci / co / sm / pr` 等），`reader.build_payload()` 会在
它上面重新生成 `ct / rt / ts / rn / sg / s`，所以抓到的值不会过期。

为什么不直接算：`ps`/`pc` 是站点按当前章节、设备、时间算出来的，本地没有可靠算法；
抓一次现成的比自己拼要稳。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping
from urllib.parse import urlparse

from .accounts import PROMOTED_PAYLOAD_FIELDS, REQUIRED_PAYLOAD_FIELDS, normalize_read_payload

log = logging.getLogger(__name__)

# 站点读正文时发的那几条：主心跳是 web/book/read，进度上报是 readingProgress
READ_PATH_HINTS = ("/web/book/read",)

# 这些头由 transport 自己决定，抄进账户反而会让请求变形
DROP_HEADERS = frozenset(
    {
        "cookie",
        "content-length",
        "host",
        "connection",
        "accept-encoding",
        "content-type",
        "sec-fetch-dest",
        "sec-fetch-mode",
        "sec-fetch-site",
    }
)

SHELF_URL = "https://weread.qq.com/web/shelf"
BOOK_LINK_SELECTORS = ("a[href*='/web/reader/']", "a[href*='/web/book/']", ".shelfBook", ".bookItem")


def parse_read_body(body: str | None) -> dict[str, Any] | None:
    """把 `web/book/read` 的 POST body 解析成 payload；不合格返回 None。"""
    if not body:
        return None
    text = body.strip()
    if not text.startswith("{"):
        # 老版本用表单编码，这里不猜，直接放弃
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    if any(not parsed.get(key) for key in REQUIRED_PAYLOAD_FIELDS):
        return None
    return parsed


def promoted(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """只留账户里用得上的基础字段（与 curl 导入保持一致）。"""
    if not payload:
        return {}
    return normalize_read_payload({
        key: payload[key]
        for key in PROMOTED_PAYLOAD_FIELDS
        if payload.get(key) is not None and str(payload[key]).strip()
    })


def accepted_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    if not headers:
        return {}
    return {name: value for name, value in headers.items() if name.lower() not in DROP_HEADERS}


class ReadSniffer:
    """喂进来的请求里挑出第一条合格的 `web/book/read`。"""

    def __init__(self) -> None:
        self._payload: dict[str, Any] = {}
        self._headers: dict[str, str] = {}
        self.confirmed = False
        self.seen = 0

    def feed(self, url: str, method: str, body: str | None, headers: Mapping[str, str] | None = None) -> bool:
        """吃一条请求；抓到合格 payload 返回 True（后面再喂也不会覆盖）。"""
        if self._payload:
            return True
        if (method or "").upper() != "POST":
            return False
        parsed_url = urlparse(url or "")
        if parsed_url.hostname != "weread.qq.com" or parsed_url.path not in READ_PATH_HINTS:
            return False
        parsed = parse_read_body(body)
        if not parsed:
            return False
        self.seen += 1
        self._payload = parsed
        self._headers = accepted_headers(headers)
        log.info("抓到阅读参数：%d 个字段", len(parsed))
        return True

    def confirm_response(self, url: str, method: str, body: str | None, headers: Mapping[str, str] | None, response: Mapping[str, Any]) -> bool:
        """仅保存获得succ/synckey确认的同一条阅读请求。"""
        if self.confirmed:
            return True
        parsed_url = urlparse(url or "")
        if method != "POST" or parsed_url.hostname != "weread.qq.com" or parsed_url.path not in READ_PATH_HINTS:
            return False
        payload = parse_read_body(body)
        if not payload or not payload.get("b") or not payload.get("c"):
            return False
        if response.get("succ") != 1 or not response.get("synckey"):
            return False
        self._payload = payload
        self._headers = accepted_headers(headers)
        self.confirmed = True
        return True

    @property
    def ready(self) -> bool:
        return bool(self._payload)

    @property
    def payload(self) -> dict[str, int | float | str]:
        return dict(self._payload)

    @property
    def read_payload(self) -> dict[str, Any]:
        return promoted(self._payload)

    @property
    def headers(self) -> dict[str, str]:
        return dict(self._headers)

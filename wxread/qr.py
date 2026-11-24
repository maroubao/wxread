"""扫码绑定：在 runner / 本机 Chrome 上打开微信读书登录页，抓二维码，等手机确认。

设计约束（`docs/ARCHITECTURE.md` §6.3、`docs/PLAN.md` §6 P3）：

- 二维码**只在用户本人触发的 run 里出现**（T-10），截图随会话删除（T-12）；
- 二维码会定时刷新，同一张图不重复落盘（指纹去重），避免页面闪；
- 登录成功的判据是**拿到 `wr_vid` + `wr_skey`**，不是"页面跳转了"；
- 本模块不读环境变量：所有参数由调用方（`control.py` / 本地脚本）传入。

Playwright 是可选的运行期依赖：测试注入假页面，不需要浏览器。
"""

from __future__ import annotations

import base64
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urljoin, urlparse

from .accounts import Account, derive_account_id, normalize_read_payload
from .capture import BOOK_LINK_SELECTORS, SHELF_URL, ReadSniffer
from .logsafe import mask

LOGIN_URL = "https://weread.qq.com/"
DEFAULT_TIMEOUT = 600
POLL_SECONDS = 3
DEFAULT_REFRESH_AFTER = 85.0  # 微信读书二维码约 2 分钟过期，到期前整页重开换新码
DEFAULT_CAPTURE_TIMEOUT = 180.0
# 常见二维码载体：微信读书登录卡片里的 img/canvas，兜底整页截图
LOGIN_TRIGGER = "a.wr_index_page_top_section_header_action_link"
QR_SELECTORS = ("img.wr_login_modal_qr_img", "img[src^='data:image']", "canvas")
SCREENSHOT_SCALE = 3
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
PAYLOAD_HINT = "登录已确认，阅读参数未取齐，重试自动获取"


class QrError(RuntimeError):
    """扫码流程失败（超时、页面异常、登录后仍无 cookie）。"""


@dataclass
class QrShot:
    fingerprint: str
    png: bytes
    at: float


class LoginPage(Protocol):
    """`ChromeLoginPage` 的最小契约，测试用假对象即可满足。"""

    def open(self) -> None: ...

    def qr_png(self) -> bytes | None: ...

    def cookies(self) -> dict[str, str]: ...

    def user_agent(self) -> str: ...

    def close(self) -> None: ...


@dataclass
class QrResult:
    ok: bool
    account: Account | None = None
    shots: list[QrShot] = field(default_factory=list)
    waited: float = 0.0
    reason: str = ""

    def as_receipt(self) -> dict:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "account_id": self.account.id if self.account else None,
            "label": self.account.label if self.account else None,
            "qr_count": len(self.shots),
            "waited": round(self.waited, 1),
        }


def fingerprint(png: bytes) -> str:
    return hashlib.sha256(png).hexdigest()[:16]


class ChromeLoginPage:
    """Playwright + 系统 Chrome（`channel="chrome"`）。

    Actions 的 ubuntu-latest 预装 Google Chrome，本机也有，所以不下载 Playwright 自带浏览器。
    """

    def __init__(self, headless: bool = True, viewport: tuple[int, int] = (900, 1100), initial_cookies: Mapping[str, str] | None = None):
        self._headless = headless
        self._viewport = viewport
        self._pw = None
        self._browser = None
        self._page = None
        self._ua = DEFAULT_UA
        self._initial_cookies = dict(initial_cookies or {})
        self.capture_error = ""

    def open(self) -> None:
        from playwright.sync_api import sync_playwright  # 延迟导入：无浏览器环境也能 import 本模块

        self._pw = sync_playwright().start()
        for channel in ("chrome", "msedge", None):
            try:
                options = {"headless": self._headless}
                if channel:
                    options["channel"] = channel
                self._browser = self._pw.chromium.launch(**options)
                break
            except Exception:
                if channel is None:
                    raise
        context = self._browser.new_context(
            viewport={"width": self._viewport[0], "height": self._viewport[1]},
            device_scale_factor=SCREENSHOT_SCALE,
            locale="zh-CN",
        )
        if self._initial_cookies:
            context.add_cookies([
                {"name": name, "value": str(value), "url": LOGIN_URL}
                for name, value in self._initial_cookies.items() if value
            ])
        self._page = context.new_page()
        self._page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45_000)
        self._page.wait_for_timeout(1200)
        self._ua = self._page.evaluate("() => navigator.userAgent") or DEFAULT_UA
        if not self._initial_cookies:
            self._open_login_modal()

    def _open_login_modal(self) -> None:
        """微信读书把登录做成首页浮层：点右上角"登录"，二维码才出现。"""
        assert self._page is not None
        for selector in QR_SELECTORS:
            if self._page.query_selector(selector):
                return
        try:
            self._page.click(LOGIN_TRIGGER, timeout=8000)
        except Exception:
            try:
                self._page.get_by_text("登录", exact=True).first.click(timeout=5000)
            except Exception:
                return
        try:
            self._page.wait_for_selector(", ".join(QR_SELECTORS), timeout=10_000)
        except Exception:
            pass

    EXPIRED_HINT = "点击刷新二维码"
    # 手机扫过之后微信读书换这些文案；此时整页重开等于把这次登录取消掉
    SCAN_HINTS = ("扫描成功", "请在手机上确认", "已扫码", "确认登录")

    def scanned(self) -> bool:
        """手机上已扫、等确认。这期间**不能**重开页面，否则微信侧会报"登录失败，请重新扫码"。"""
        assert self._page is not None
        try:
            content = self._page.content() or ""
        except Exception:
            return False
        return any(hint in content for hint in self.SCAN_HINTS)

    def _refresh_if_expired(self) -> bool:
        """二维码到期后微信读书会盖一层"点击刷新二维码"，点掉它再截。"""
        assert self._page is not None
        try:
            locator = self._page.get_by_text(self.EXPIRED_HINT, exact=False).first
            if locator.count() and locator.is_visible():
                locator.click(timeout=3000)
                self._page.wait_for_timeout(900)
                return True
        except Exception:
            return False
        return False

    def qr_png(self) -> bytes | None:
        """优先返回站点自己给的二维码位图，其次才是截图。

        CI 的 headless Chrome 没装中文字体：二维码过期后微信读书会盖一层
        "点击刷新二维码"，那行汉字在容器里渲染成一串空方块，正好压在码上，
        于是"第 4 张开始扫不动"。直接读 `<img src="data:image/png;base64,...">`
        拿到的是干净位图，没有文字、没有浮层、也不受 DPR 影响。
        """
        assert self._page is not None
        self._refresh_if_expired()
        data = self._qr_data_url()
        if data:
            return data
        for selector in QR_SELECTORS:
            element = self._page.query_selector(selector)
            if element is None:
                continue
            try:
                shot = element.screenshot(scale="device")
            except Exception:  # 元素被刷新掉/不可见，换下一个选择器
                continue
            if shot and len(shot) > 512:
                # 过期浮层是近似空白的覆盖层：大于 14KB 且页面里出现提示文案就不是有效码
                if len(shot) > 14_000 and self._expired_overlay():
                    return None
                return shot
        try:
            return self._page.screenshot()
        except Exception:
            return None

    def _qr_data_url(self) -> bytes | None:
        """从 `data:image/png;base64,...` 里取二维码原始字节。"""
        assert self._page is not None
        for selector in QR_SELECTORS:
            try:
                element = self._page.query_selector(selector)
            except Exception:
                continue
            if element is None:
                continue
            try:
                src = element.get_attribute("src") or ""
            except Exception:
                continue
            if not src.startswith("data:image"):
                continue
            _, _, payload = src.partition(",")
            try:
                raw = base64.b64decode(payload)
            except Exception:
                continue
            if len(raw) > 512:
                return raw
        return None

    def _expired_overlay(self) -> bool:
        assert self._page is not None
        try:
            return self.EXPIRED_HINT in (self._page.content() or "")
        except Exception:
            return False

    def reopen(self) -> None:
        """整页重开登录浮层，换一张新二维码（点"点击刷新"在浮层里取不到）。"""
        assert self._page is not None
        self._page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45_000)
        self._page.wait_for_timeout(1200)
        self._open_login_modal()

    def cookies(self) -> dict[str, str]:
        assert self._page is not None
        jar = self._page.context.cookies()
        return {item["name"]: item["value"] for item in jar if item.get("name")}

    def capture_read_payload(self, timeout: float = DEFAULT_CAPTURE_TIMEOUT) -> tuple[dict[str, Any], dict[str, str]]:
        """进入可读正文，监听整个浏览器上下文并等待成功阅读响应。"""
        assert self._page is not None
        sniffer = ReadSniffer()
        context = self._page.context
        started = time.monotonic()

        def on_request(request: Any) -> None:
            try:
                sniffer.feed(request.url, request.method, request.post_data, request.headers)
            except Exception:
                pass

        def on_response(response: Any) -> None:
            try:
                request = response.request
                if response.status == 200 and "/web/book/read" in request.url:
                    sniffer.confirm_response(request.url, request.method, request.post_data, request.headers, response.json())
            except Exception:
                pass

        context.on("request", on_request)
        context.on("response", on_response)
        try:
            self._page.goto(SHELF_URL, wait_until="domcontentloaded", timeout=30_000)
            self._page.wait_for_timeout(1500)
            candidates = self._reader_urls()
            if not candidates:
                self._page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
                self._page.wait_for_timeout(1500)
                candidates = self._reader_urls()
            if not candidates:
                self.capture_error = "没有找到可打开的正文"
                return {}, {}
            for url in candidates[:3]:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    break
                if not self._open_first_book(url):
                    continue
                deadline = min(started + timeout, time.monotonic() + 90)
                turns = 0
                while not sniffer.confirmed and time.monotonic() < deadline:
                    if turns < 3:
                        self._nudge_reader()
                        turns += 1
                    self._page.wait_for_timeout(2000)
                if sniffer.confirmed:
                    self.capture_error = ""
                    return sniffer.read_payload, sniffer.headers
            self.capture_error = "阅读请求未得到成功确认，可以重试自动获取"
            return {}, {}
        except Exception as exc:
            self.capture_error = f"正文加载失败（{type(exc).__name__}），可以重试自动获取"
            return {}, {}
        finally:
            for event, callback in (("request", on_request), ("response", on_response)):
                try:
                    context.remove_listener(event, callback)
                except Exception:
                    pass

    def _reader_urls(self) -> list[str]:
        assert self._page is not None
        urls = []
        for selector in BOOK_LINK_SELECTORS:
            for link in self._page.query_selector_all(selector):
                href = link.get_attribute("href") or ""
                url = urljoin(self._page.url, href)
                parsed = urlparse(url)
                if parsed.hostname == "weread.qq.com" and parsed.path.startswith("/web/reader/") and url not in urls:
                    urls.append(url)
        return urls

    def _open_first_book(self, url: str | None = None) -> bool:
        """直接导航到正文href，避免书卡内部的作者链接或新窗口。"""
        assert self._page is not None
        if not url:
            urls = self._reader_urls()
            url = urls[0] if urls else None
        if not url:
            return False
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            self._page.wait_for_timeout(1500)
            return True
        except Exception:
            return False

    def _nudge_reader(self) -> None:
        """通过实际的下一页按钮进入正文，之后等待站点自己的阅读心跳。"""
        assert self._page is not None
        try:
            next_page = self._page.get_by_role("button", name="下一页", exact=True)
            if next_page.is_visible():
                next_page.click(timeout=1500)
                return
        except Exception:
            pass
        try:
            self._page.keyboard.press("ArrowRight")
        except Exception:
            pass

    def user_agent(self) -> str:
        return self._ua

    def close(self) -> None:
        for closer in (
            getattr(self._browser, "close", None),
            getattr(self._pw, "stop", None),
        ):
            if closer:
                try:
                    closer()
                except Exception:
                    pass
        self._page = None
        self._browser = None
        self._pw = None


def wait_for_login(
    page: LoginPage,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    poll: float = POLL_SECONDS,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    on_qr: Callable[[QrShot], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    refresh_after: float = DEFAULT_REFRESH_AFTER,
) -> QrResult:
    """轮询二维码与 cookie，直到登录成功 / 超时 / 被取消。

    返回的 `shots` 里每张图都是**新指纹**（同一张刷新图不会重复），调用方可只渲染最后一张。
    每 `refresh_after` 秒整页重开一次拿新码：hover 浮层里的"点击刷新"取不到，只能重开。
    """
    started = now()
    shots: list[QrShot] = []
    last_fp = ""
    next_refresh = refresh_after
    scanned_check = getattr(page, "scanned", None)
    while True:
        cookies = page.cookies()
        if cookies.get("wr_vid") and cookies.get("wr_skey"):
            return QrResult(ok=True, shots=shots, waited=now() - started, reason="logged_in")
        if should_stop and should_stop():
            return QrResult(ok=False, shots=shots, waited=now() - started, reason="cancelled")
        png = page.qr_png()
        if png:
            fp = fingerprint(png)
            if fp != last_fp:
                last_fp = fp
                shot = QrShot(fingerprint=fp, png=png, at=now())
                shots.append(shot)
                if on_qr:
                    on_qr(shot)
        elapsed = now() - started
        if elapsed >= timeout:
            return QrResult(ok=False, shots=shots, waited=elapsed, reason="timeout")
        if refresh_after and elapsed >= next_refresh:
            if callable(scanned_check) and scanned_check():
                next_refresh = elapsed + refresh_after  # 手机扫过了：安静等确认，别打断
            else:
                reopen = getattr(page, "reopen", None)
                if reopen is not None:
                    try:
                        reopen()
                    except Exception:
                        pass
                    last_fp = ""  # 重开后即便同图也重新上报一次
                next_refresh = elapsed + refresh_after
        sleep(min(poll, max(0.1, timeout - elapsed)))


def account_from_cookies(
    cookies: Mapping[str, str],
    *,
    headers: Mapping[str, str] | None = None,
    label: str = "",
    target_count: int = 40,
    payload: Mapping[str, Any] | None = None,
    now_iso: str = "",
) -> Account:
    """把扫码得到的 cookie 组装成 Account；缺 ps/pc 时留空并打 note 提示补全。"""
    account = Account(
        id=derive_account_id(cookies),
        label=label,
        cookies={k: v for k, v in cookies.items() if k in {"wr_vid", "wr_skey", "pac_uid"} or k.startswith("wr_")},
        headers=dict(headers or {}),
        read_payload=normalize_read_payload({k: v for k, v in (payload or {}).items() if v is not None and str(v).strip()}),
        target_count=target_count,
        enabled=True,
        status="ok",
        renewed_at=now_iso,
    )
    if not account.read_payload.get("ps") or not account.read_payload.get("pc"):
        account.note = PAYLOAD_HINT
    else:
        account.note = ""
    return account


def describe(result: QrResult) -> str:
    if result.ok and result.account:
        return f"绑定成功：{result.account.label or result.account.id}（skey {mask(result.account.cookies.get('wr_skey', ''))}）"
    if result.reason == "timeout":
        return f"扫码超时（{int(result.waited)}s），二维码已刷新 {len(result.shots)} 次"
    return f"扫码结束：{result.reason}"

"""扫码绑定：在 runner / 本机 Chrome 上打开微信读书登录页，抓二维码，等手机确认。

设计约束（`docs/ARCHITECTURE.md` §6.3、`docs/PLAN.md` §6 P3）：

- 二维码**只在用户本人触发的 run 里出现**（T-10），截图随会话删除（T-12）；
- 二维码会定时刷新，同一张图不重复落盘（指纹去重），避免页面闪；
- 登录成功的判据是**拿到 `wr_vid` + `wr_skey`**，不是"页面跳转了"；
- 本模块不读环境变量：所有参数由调用方（`control.py` / 本地脚本）传入。

Playwright 是可选的运行期依赖：测试注入假页面，不需要浏览器。
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping, Protocol

from .accounts import Account, account_from_curl, derive_account_id  # noqa: F401  (re-export 给调用方)
from .logsafe import mask

LOGIN_URL = "https://weread.qq.com/"
DEFAULT_TIMEOUT = 600
POLL_SECONDS = 3
DEFAULT_REFRESH_AFTER = 85.0  # 微信读书二维码约 2 分钟过期，到期前整页重开换新码
# 常见二维码载体：微信读书登录卡片里的 img/canvas，兜底整页截图
LOGIN_TRIGGER = "a.wr_index_page_top_section_header_action_link"
QR_SELECTORS = ("img.wr_login_modal_qr_img", "img[src^='data:image']", "canvas")
SCREENSHOT_SCALE = 3
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
PAYLOAD_HINT = "扫码账户未带 ps/pc，请导入一次 curl 或手动补全 read_payload"


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

    def __init__(self, headless: bool = True, viewport: tuple[int, int] = (900, 1100)):
        self._headless = headless
        self._viewport = viewport
        self._pw = None
        self._browser = None
        self._page = None
        self._ua = DEFAULT_UA

    def open(self) -> None:
        from playwright.sync_api import sync_playwright  # 延迟导入：无浏览器环境也能 import 本模块

        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(channel="chrome", headless=self._headless)
        except Exception:  # CI 里只有 Playwright 自带 chromium 时退而用它
            self._browser = self._pw.chromium.launch(headless=self._headless)
        context = self._browser.new_context(
            viewport={"width": self._viewport[0], "height": self._viewport[1]},
            device_scale_factor=SCREENSHOT_SCALE,
            locale="zh-CN",
        )
        self._page = context.new_page()
        self._page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45_000)
        self._page.wait_for_timeout(1200)
        self._ua = self._page.evaluate("() => navigator.userAgent") or DEFAULT_UA
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
        assert self._page is not None
        self._refresh_if_expired()
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
    payload: Mapping[str, str] | None = None,
    now_iso: str = "",
) -> Account:
    """把扫码得到的 cookie 组装成 Account；缺 ps/pc 时留空并打 note 提示补全。"""
    account = Account(
        id=derive_account_id(cookies),
        label=label,
        cookies={k: v for k, v in cookies.items() if k in {"wr_vid", "wr_skey", "pac_uid"} or k.startswith("wr_")},
        headers=dict(headers or {}),
        read_payload={k: str(v) for k, v in (payload or {}).items() if v},
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

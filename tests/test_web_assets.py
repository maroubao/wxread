"""静态资源装配：index.html 引了什么、文件在不在、顺序对不对。

回归来源：`web/index.html` 曾漏掉 tweetnacl 的 <script>，页面上点"生成二维码"
直接报 `Cannot read properties of undefined (reading 'box')`——单测和 node 测试
都发现不了，因为它们直接 require 那份文件。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"


def test_index_loads_tweetnacl_before_the_module():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    vendor = html.find("assets/vendor/nacl-fast.min.js")
    module = html.find('type="module"')
    assert vendor != -1, "index.html 没有引入 tweetnacl"
    assert module != -1, "index.html 没有 module 入口"
    assert vendor < module, "tweetnacl 必须在 app.js 之前加载"


def test_every_referenced_asset_exists():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    refs = re.findall(r'(?:src|href)="([^"#:]+)"', html)
    assert refs, "index.html 没有任何本地引用"
    missing = [ref for ref in refs if not (WEB / ref).exists() and not (ROOT / ref).exists()]
    assert missing == []


def test_crypto_js_fails_loud_when_nacl_is_absent():
    source = (WEB / "crypto.js").read_text(encoding="utf-8")
    assert "globalThis.nacl" in source
    assert "tweetnacl 未加载" in source


def test_every_element_app_js_touches_exists_in_the_page():
    """app.js 里 $(...) 抓的每个 id 都必须在 index.html 里存在。

    回归来源：`$("btn-run-now").addEventListener(...)` 这类新按钮如果只改了 js
    没改 HTML，页面会在 bind() 阶段抛 `Cannot read properties of null`，整页按钮全失效。
    """
    html = (WEB / "index.html").read_text(encoding="utf-8")
    source = (WEB / "app.js").read_text(encoding="utf-8")
    ids = set(re.findall(r'\$\("([a-z0-9-]+)"\)', source))
    assert ids, "app.js 里没有 $() 选择器"
    missing = sorted(identifier for identifier in ids if f'id="{identifier}"' not in html)
    assert missing == [], f"index.html 缺少这些 id：{missing}"


def test_qr_panel_starts_hidden_and_closes_when_the_receipt_lands():
    """扫码成功后必须立刻收起二维码区，不能一直轮询到 15 分钟预算烧完。

    回归来源：用户扫码成功后页面还在一张张拉新码，最后报“轮询超时”——因为
    pollQr 只盯着 responses/qr-*.png，从来没看 responses/<session>.json 这个回执。
    """
    html = (WEB / "index.html").read_text(encoding="utf-8")
    source = (WEB / "app.js").read_text(encoding="utf-8")
    assert 'id="qr-live" hidden' in html, "扫码区默认必须是收起的"
    assert 'id="btn-qr-cancel"' in html, "扫码中要能取消"
    assert "async function qrReceipt(" in source
    assert "function finishQr(" in source
    # pollQr 每轮先看回执，再看有没有新帧
    assert source.index("await qrReceipt(sessionId)") < source.index("await findSessionQr(sessionId)")
    # 回执到了要收面板，而不是继续排下一次轮询
    finish = source[source.index("function finishQr(") : source.index("async function qrReceipt(")]
    assert "showQrPanel(false)" in finish

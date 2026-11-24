"""正文入口回归使用浏览器观察到的链接与按钮形态。"""

from types import SimpleNamespace

from wxread.qr import ChromeLoginPage


def test_book_navigation_uses_reader_href_instead_of_card_click():
    visited = []
    link = SimpleNamespace(get_attribute=lambda name: "https://weread.qq.com/web/reader/book-id" if name == "href" else None)
    page = ChromeLoginPage()
    page._page = SimpleNamespace(
        url="https://weread.qq.com/web/shelf",
        query_selector_all=lambda selector: [link] if "/web/reader/" in selector else [],
        goto=lambda url, **kwargs: visited.append(url),
        wait_for_timeout=lambda _ms: None,
    )
    assert page._open_first_book() is True
    assert visited == ["https://weread.qq.com/web/reader/book-id"]


def test_entering_text_clicks_the_visible_next_page_button():
    clicked = []
    next_page = SimpleNamespace(is_visible=lambda: True, click=lambda **kwargs: clicked.append(True))
    page = ChromeLoginPage()
    page._page = SimpleNamespace(get_by_role=lambda *args, **kwargs: next_page)
    page._nudge_reader()
    assert clicked == [True]

"""验证 Tailwind 生产构建链路：模板不再引用 CDN，改为引用构建产物 main.css。

构建产物本身是 CSS 文本，难以单元测试；这里聚焦"模板契约"：
- 三个独立 HTML 模板（base/login/setup）都不再引用 cdn.tailwindcss.com
- base.html 引用了 /static/css/main.css（构建产物路径）
"""

from __future__ import annotations

from pathlib import Path

BASE_HTML = Path("web/templates/base.html")
LOGIN_HTML = Path("web/templates/login.html")
SETUP_HTML = Path("web/templates/setup.html")


class TestNoCdnTailwind:
    def test_base_html_has_no_cdn_script(self) -> None:
        text = BASE_HTML.read_text(encoding="utf-8")
        assert "cdn.tailwindcss.com" not in text

    def test_login_html_has_no_cdn_script(self) -> None:
        text = LOGIN_HTML.read_text(encoding="utf-8")
        assert "cdn.tailwindcss.com" not in text

    def test_setup_html_has_no_cdn_script(self) -> None:
        text = SETUP_HTML.read_text(encoding="utf-8")
        assert "cdn.tailwindcss.com" not in text

    def test_base_html_links_compiled_css(self) -> None:
        text = BASE_HTML.read_text(encoding="utf-8")
        assert "/static/css/main.css" in text

    def test_login_html_links_compiled_css(self) -> None:
        text = LOGIN_HTML.read_text(encoding="utf-8")
        assert "/static/css/main.css" in text

    def test_setup_html_links_compiled_css(self) -> None:
        text = SETUP_HTML.read_text(encoding="utf-8")
        assert "/static/css/main.css" in text

    def test_tokens_css_link_removed(self) -> None:
        """tokens.css 内容已合并进 main.css，模板不应再引用 tokens.css。"""
        for path in (BASE_HTML, LOGIN_HTML, SETUP_HTML):
            assert "/static/tokens.css" not in path.read_text(encoding="utf-8")

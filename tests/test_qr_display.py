from __future__ import annotations

from shared.auth.qr_display import display_qr_in_terminal


class TestDisplayQrInTerminal:
    def test_prints_to_stdout(self, capsys: object) -> None:
        from typing import cast

        from _pytest.capture import CaptureFixture

        capsys = cast(CaptureFixture[str], capsys)
        display_qr_in_terminal("https://example.com/qr")
        captured = capsys.readouterr()
        assert "█" in captured.out
        assert "▀" in captured.out
        assert "▄" in captured.out

    def test_output_includes_url(self, capsys: object) -> None:
        from typing import cast

        from _pytest.capture import CaptureFixture

        capsys = cast(CaptureFixture[str], capsys)
        url = "https://example.com/qr"
        display_qr_in_terminal(url)
        captured = capsys.readouterr()
        assert url in captured.out

    def test_url_appears_below_qr(self, capsys: object) -> None:
        from typing import cast

        from _pytest.capture import CaptureFixture

        capsys = cast(CaptureFixture[str], capsys)
        url = "https://example.com/qr"
        display_qr_in_terminal(url)
        captured = capsys.readouterr()
        qr_pos = captured.out.index("█")
        url_pos = captured.out.index(url)
        assert url_pos > qr_pos, "URL should appear after the QR rendering"


class TestPackageReExport:
    def test_display_qr_in_terminal_importable_from_package(self) -> None:
        from shared.auth import display_qr_in_terminal as fn

        assert callable(fn)

    def test_display_qr_in_terminal_in_all(self) -> None:
        import shared.auth

        assert "display_qr_in_terminal" in shared.auth.__all__

from __future__ import annotations

import qrcode

# QR 码紧凑渲染：使用半块字符，高度减半
_UPPER = "▀"
_LOWER = "▄"
_FULL = "█"
_EMPTY = " "


def _render_qr_matrix(url: str) -> str:
    """Render URL as compact Unicode half-block QR code string."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=1,
        border=2,
    )
    qr.add_data(url)
    qr.make(fit=True)
    matrix = qr.get_matrix()

    # 每两行合并为一行，使用半块字符
    lines: list[str] = []
    rows = len(matrix)
    for y in range(0, rows, 2):
        line_chars: list[str] = []
        for x in range(len(matrix[0])):
            top = matrix[y][x]
            bottom = matrix[y + 1][x] if y + 1 < rows else False
            if top and bottom:
                line_chars.append(_FULL)
            elif top and not bottom:
                line_chars.append(_UPPER)
            elif not top and bottom:
                line_chars.append(_LOWER)
            else:
                line_chars.append(_EMPTY)
        lines.append("".join(line_chars))
    return "\n".join(lines)


def display_qr_in_terminal(url: str) -> None:
    """Print QR code and copyable URL to stdout."""
    print()
    print(_render_qr_matrix(url))
    print()
    print(f"扫码链接（备用）: {url}")
    print()

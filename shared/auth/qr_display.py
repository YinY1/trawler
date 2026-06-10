from __future__ import annotations

import qrcode

_FULL = "▓"
_EMPTY = "░"


def _render_qr_matrix(url: str) -> str:
    """Render URL as Unicode block-character QR code string."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=1,
        border=2,
    )
    qr.add_data(url)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    return "\n".join(
        "".join(_FULL if cell else _EMPTY for cell in row) for row in matrix
    )


def display_qr_in_terminal(url: str) -> None:
    """Print QR code and copyable URL to stdout."""
    print()
    print(_render_qr_matrix(url))
    print()
    print(f"扫码链接（备用）: {url}")
    print()
